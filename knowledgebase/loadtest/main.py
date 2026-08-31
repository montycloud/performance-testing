"""KB document upload load test — CLI entry point.

    python main.py --env dev1 --collection-id <cid> --concurrency 10 --batches 5
    python main.py --env dev1 --collection-id <cid> --cleanup --run-id <run_id>

Auth: signs in as the root user from --config (collections_config.yaml) by
default; pass --token to skip sign-in and use a pre-obtained JWT instead.
"""
import argparse
import os
import sys
import uuid

import yaml

from auth import fetch_org_id, signin
from cleanup import run_cleanup
from client import ENVIRONMENTS, KBClient, base_url_for_env
from load_runner import run_load

DEFAULT_ENV = "dev1"


def parse_args():
    p = argparse.ArgumentParser(description="KB upload load-test runner.")
    p.add_argument("--env", default=None,
                   help=f"Target environment; base URL is derived automatically. "
                        f"Known: {', '.join(ENVIRONMENTS)}. Defaults to --config's 'env' "
                        f"(or {DEFAULT_ENV} if neither is set)")
    p.add_argument("--token", default=os.environ.get("KB_LOADTEST_TOKEN"),
                   help="Pre-obtained JWT auth token (or set KB_LOADTEST_TOKEN); skips "
                        "sign-in when provided")
    p.add_argument("--config", default="collections_config.yaml",
                   help="YAML with env + root creds, used to sign in when --token is not "
                        "given (default: collections_config.yaml)")
    p.add_argument("--root-password", default=os.environ.get("KB_ROOT_PASSWORD"),
                   help="Root password for sign-in (or set KB_ROOT_PASSWORD); ignored if "
                        "--token is given")
    p.add_argument("--org-id", default=None,
                   help="Target tenant / OrganizationId, sent as the d2oid cookie. "
                        "Optional: omit to use the signed-in user's default org")
    p.add_argument("--collection-id", required=True,
                   help="Collection id(s), comma-separated; uploads spread across them")
    p.add_argument("--concurrency", type=int, default=5,
                   help="Concurrent uploads per batch (default: 5)")
    p.add_argument("--batches", type=int, default=1,
                   help="Number of batches / waves (default: 1)")
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
                   help="JSONL log path (default: logs/kb_loadtest_<run_id>.jsonl)")
    p.add_argument("--request-timeout", type=int, default=60,
                   help="Per-request timeout seconds (default: 60)")
    p.add_argument("--cleanup", action="store_true",
                   help="Purge a previous run's documents by label (needs --run-id)")
    p.add_argument("--cookies-file", default=None,
                   help="YAML file with a top-level 'cookies:' name->value map "
                        "required by the backend on collection/upload requests")
    args = p.parse_args()
    if args.log_file is None:
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        args.log_file = os.path.join(logs_dir, f"kb_loadtest_{args.run_id}.jsonl")
    return args


def load_root_config(path):
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_cookies(cookies_file):
    if not cookies_file:
        return None
    with open(cookies_file, encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("cookies") or {}


def resolve_auth(args, cfg):
    """Return (env, token, org_id): use --token as-is, else sign in via cfg's root creds."""
    env = args.env or cfg.get("env") or DEFAULT_ENV
    if args.token:
        return env, args.token, args.org_id
    email = cfg.get("root", {}).get("email")
    if not email:
        sys.exit("No --token and no root.email in --config: pass --token, or set "
                 "root.email in the config to sign in.")
    if not args.root_password:
        sys.exit("No root password: pass --root-password or set KB_ROOT_PASSWORD.")
    mc_debug_mode = cfg.get("mc_debug_mode", True)
    token = signin(env, email, args.root_password, mc_debug_mode, args.request_timeout)
    org_id = args.org_id or fetch_org_id(env, token, args.request_timeout)
    return env, token, org_id


def main():
    """Sign in (or use --token), build the client, then dispatch to cleanup or a load run."""
    args = parse_args()
    cfg = load_root_config(args.config)
    env, token, org_id = resolve_auth(args, cfg)
    args.base_url = base_url_for_env(env)
    cookies = load_cookies(args.cookies_file)
    client = KBClient(args.base_url, token, org_id, args.request_timeout, cookies)
    if args.cleanup:
        run_cleanup(args, client)
    else:
        run_load(args, client)


if __name__ == "__main__":
    main()