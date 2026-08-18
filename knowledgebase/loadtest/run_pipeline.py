"""Stage 4 — run Stage 1 (collections) -> Stage 2 (uploads) -> Stage 3 (metrics)
as a single command, for pipeline/CI use.

Each stage's standalone script (setup_collections.py, main.py, fetch_metrics.py)
keeps working unmodified; this orchestrator just calls the same functions
in-process and passes each stage's output as the next stage's input.

    export KB_ROOT_PASSWORD=<msp root password>
    python run_pipeline.py --config collections_config.yaml \
        --concurrency 10 --batches 5 --file-type random \
        --poll-every 30 --timeout 600
"""
import argparse
import datetime
import os
import sys
import uuid

import boto3
import yaml

from auth import fetch_org_id, signin
from client import KBClient, base_url_for_env
from fetch_metrics import poll_until_complete, render_rows, save_raw_export
from load_runner import run_load
from setup_collections import create_collections, load_config


def parse_args():
    p = argparse.ArgumentParser(
        description="Run the full KB load-test pipeline: create collections, "
                    "upload documents, then poll CloudWatch until ingestion completes."
    )
    # Stage 1
    p.add_argument("--config", default="collections_config.yaml",
                   help="YAML with env, root creds and collections (default: "
                        "collections_config.yaml)")
    p.add_argument("--root-password", default=os.environ.get("KB_ROOT_PASSWORD"),
                   help="MSP root password (or set KB_ROOT_PASSWORD)")
    p.add_argument("--request-timeout", type=int, default=30,
                   help="Per-request timeout seconds (default: 30)")
    # Stage 2
    p.add_argument("--concurrency", type=int, default=5,
                   help="Concurrent uploads per batch (default: 5)")
    p.add_argument("--batches", type=int, default=1,
                   help="Number of upload batches / waves (default: 1)")
    p.add_argument("--batch-delay", type=float, default=0.0,
                   help="Seconds to sleep between batches (default: 0)")
    p.add_argument("--file-type", default="random",
                   help="'random' or a specific extension, e.g. pdf, docx (default: random)")
    p.add_argument("--corpus-dir",
                   default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "corpus"),
                   help="Corpus directory (default: ./corpus)")
    p.add_argument("--run-id", default=uuid.uuid4().hex[:12],
                   help="Run id; also the run label suffix (default: generated)")
    p.add_argument("--log-file", default=None,
                   help="JSONL upload log path (default: logs/kb_loadtest_<run_id>.jsonl)")
    # Stage 3
    p.add_argument("--aws-profile", default=None, help="AWS named profile (optional)")
    p.add_argument("--aws-region", default="us-east-2",
                   help="AWS region (default: us-east-2)")
    p.add_argument("--format", choices=["markdown", "tsv", "csv"], default="markdown",
                   help="Output format for the final timing table")
    p.add_argument("--output-dir",
                   default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
                   help="Directory to save the raw CloudWatch export (default: ./logs)")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="Seconds between CloudWatch query-status polls (default: 2)")
    p.add_argument("--query-timeout", type=int, default=120,
                   help="Max seconds to wait for one CloudWatch query to complete "
                        "(default: 120)")
    p.add_argument("--poll-every", type=float, default=30.0,
                   help="Seconds between Stage 3 poll cycles (default: 30)")
    p.add_argument("--timeout", type=int, default=600,
                   help="Stage 3: max seconds to wait for all uploaded documents to "
                        "reach kb_ingestion_completed (default: 600 = 10 min)")

    args = p.parse_args()
    if args.log_file is None:
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        args.log_file = os.path.join(logs_dir, f"kb_loadtest_{args.run_id}.jsonl")
    return args


def run_stage1(args):
    cfg = load_config(args.config)
    if not args.root_password:
        sys.exit("No root password: pass --root-password or set KB_ROOT_PASSWORD.")

    env = cfg["env"]
    mc_debug_mode = cfg.get("mc_debug_mode", True)
    print(f"[Stage 1] Signing in as {cfg['root']['email']} ({env}) ...")
    token = signin(env, cfg["root"]["email"], args.root_password, mc_debug_mode,
                   args.request_timeout)
    org_id = fetch_org_id(env, token, args.request_timeout)
    client = KBClient(base_url_for_env(env), token, org_id=org_id,
                       timeout=args.request_timeout)

    print(f"[Stage 1] Creating {len(cfg['collections'])} collection(s) ...")
    collections = create_collections(client, cfg["collections"])
    if not collections:
        sys.exit("[Stage 1] No collections created — aborting pipeline.")

    output = f"collections_output_{datetime.datetime.now():%Y%m%d_%H%M%S}.yaml"
    with open(output, "w", encoding="utf-8") as fh:
        yaml.safe_dump({"collections": collections}, fh, sort_keys=False)
    print(f"[Stage 1] Wrote {output}")

    return env, client, collections


def run_stage2(args, client, collections):
    collection_ids = [c["id"] for c in collections if c.get("id")]
    args.collection_id = ",".join(collection_ids)
    print(f"[Stage 2] Uploading to collection(s): {args.collection_id}")
    results = run_load(args, client)

    succeeded = [r["document_id"] for r in results
                 if r.get("document_id") and r.get("api_status") == 201
                 and r.get("s3_status") in (200, 204)]
    print(f"[Stage 2] {len(succeeded)}/{len(results)} uploads succeeded "
          f"(tracking these for Stage 3).")
    return succeeded


def run_stage3(args, env, document_ids):
    if not document_ids:
        sys.exit("[Stage 3] No successfully uploaded documents to track — skipping.")
    args.env = env
    session = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region)
    logs_client = session.client("logs")

    print(f"[Stage 3] Polling CloudWatch for {len(document_ids)} document(s) "
          f"every {args.poll_every}s (timeout {args.timeout}s) ...")
    payload, rows = poll_until_complete(logs_client, args, document_ids)

    raw_path = save_raw_export(payload, args.output_dir, env)
    print(f"[Stage 3] Saved raw export ({len(payload)} log lines) to {raw_path}")

    rows = [row for row in rows if row["document_id"] in document_ids]
    if not rows:
        print("[Stage 3] No metrics found for the tracked documents.")
        return
    render_rows(rows, args.format)


def main():
    args = parse_args()
    env, client, collections = run_stage1(args)
    document_ids = run_stage2(args, client, collections)
    run_stage3(args, env, document_ids)


if __name__ == "__main__":
    main()
