"""Fetch KB document-lifecycle metrics from CloudWatch Logs Insights (Stage 3).

Replaces the manual "export from CloudWatch console, then run
analyze_kb_timings.py" workflow with one command: runs the query, saves the
raw export to logs/, and prints the same timing table.

    python fetch_metrics.py --env stg1 --minutes-back 60
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import boto3

from analyze_kb_timings import build_rows, render_csv, render_markdown, render_tsv

# {env} substituted per --env, e.g. dev1, stg1, prd01 (see client.ENVIRONMENTS).
# The bedrock-agentcore runtime log group has a random suffix per deployment —
# update this template if it doesn't match for a given env.
LOG_GROUP_TEMPLATES = [
    "/aws/api-gateway/marvinknowledgebase-{env}",
    "/aws/bedrock-agentcore/runtimes/marvinknowledgebase_knowledgeBaseAssistant_{env}-lv6imPFd5F-DEFAULT",
    "/aws/lambda/marvinknowledgebase-{env}-delete_tenant_collections",
    "/aws/lambda/marvinknowledgebase-{env}-knowledge_base_api_handler",
    "/aws/lambda/marvinknowledgebase-{env}-process_pending_checks",
    "/aws/lambda/marvinknowledgebase-{env}-route_events",
    "/aws/lambda/marvinknowledgebase-{env}-summarize_document",
    "/aws/lambda/marvinknowledgebase-{env}-sync_knowledge_base",
    "/aws/lambda/marvinknowledgebase-{env}-upload_metadata",
]

QUERY_STRING = (
    "fields @message, @log, @timestamp, @logStream "
    "| filter @message like /PLATFORM_METRIC_KB_DOCUMENT_LIFECYCLE/"
)

HEADERS = [
    "document_id",
    "organization_id",
    "collection_id",
    "document_type",
    "document_size",
    "s3_upload_time_s",
    "metadata_creation_time_s",
    "summarization_time_s",
    "kb_ingestion_time_s",
]


def parse_args():
    p = argparse.ArgumentParser(
        description="Fetch KB document-lifecycle metrics from CloudWatch Logs Insights "
                    "and render the same timing table as analyze_kb_timings.py."
    )
    p.add_argument("--env", required=True,
                   help="Environment; substituted into the log group names, "
                        "e.g. dev1, stg1, prd01")
    p.add_argument("--minutes-back", type=int, default=60,
                   help="Query window: from now-N minutes to now (default: 60)")
    p.add_argument("--aws-profile", default=None, help="AWS named profile (optional)")
    p.add_argument("--aws-region", default="us-east-2",
                   help="AWS region (default: us-east-2)")
    p.add_argument("--format", choices=["markdown", "tsv", "csv"], default="markdown",
                   help="Output format for the results table")
    p.add_argument("--output-dir",
                   default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs"),
                   help="Directory to save the raw Logs Insights export (default: ./logs)")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="Seconds between query-status polls (default: 2)")
    p.add_argument("--query-timeout", type=int, default=120,
                   help="Max seconds to wait for the query to complete (default: 120)")
    p.add_argument("--document-ids", default=None,
                   help="Comma-separated document ids; switches into poll mode: re-runs "
                        "the query every --poll-every seconds until ALL given ids reach "
                        "kb_ingestion_completed, or --timeout elapses")
    p.add_argument("--poll-every", type=float, default=30.0,
                   help="Seconds between poll-mode query re-runs (default: 30)")
    p.add_argument("--timeout", type=int, default=600,
                   help="Poll mode: max seconds to wait for all document ids to complete "
                        "(default: 600 = 10 min)")
    return p.parse_args()


def log_group_names(env):
    return [tmpl.format(env=env) for tmpl in LOG_GROUP_TEMPLATES]


def run_query(client, log_groups, start_time, end_time, query_string,
              poll_interval, query_timeout):
    query_id = client.start_query(
        logGroupNames=log_groups,
        startTime=start_time,
        endTime=end_time,
        queryString=query_string,
    )["queryId"]

    deadline = time.monotonic() + query_timeout
    while True:
        response = client.get_query_results(queryId=query_id)
        status = response["status"]
        if status == "Complete":
            return response["results"]
        if status in ("Failed", "Cancelled", "Timeout"):
            sys.exit(f"CloudWatch Logs Insights query {status.lower()}: {query_id}")
        if time.monotonic() > deadline:
            sys.exit(f"CloudWatch Logs Insights query did not complete within "
                      f"{query_timeout}s: {query_id}")
        time.sleep(poll_interval)


def rows_to_payload(results):
    """Convert raw Logs Insights field/value rows into analyze_kb_timings' expected shape."""
    payload = []
    for result in results:
        fields = {item["field"]: item["value"] for item in result}
        raw_message = fields.get("@message")
        if not raw_message:
            continue
        try:
            message = json.loads(raw_message)
        except json.JSONDecodeError:
            continue
        payload.append({"@message": message})
    return payload


def save_raw_export(payload, output_dir, env):
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, f"cloudwatch_{env}_{timestamp}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    return path


def fetch_payload(client, env, start_dt, end_dt, args):
    results = run_query(
        client,
        log_group_names(env),
        int(start_dt.timestamp()),
        int(end_dt.timestamp()),
        QUERY_STRING,
        args.poll_interval,
        args.query_timeout,
    )
    return rows_to_payload(results)


def poll_until_complete(client, args, target_ids):
    """Re-run the query every --poll-every seconds until every target id's row has
    kb_ingestion_completed, or --timeout elapses. Returns (payload, rows)"""
    poll_start_dt = datetime.now(timezone.utc)
    deadline = time.monotonic() + args.timeout
    poll_count = 0
    payload, rows = [], []

    while True:
        poll_count += 1
        now_dt = datetime.now(timezone.utc)
        payload = fetch_payload(client, args.env, poll_start_dt, now_dt, args)
        rows = build_rows(payload)
        by_id = {row["document_id"]: row for row in rows}
        pending = [tid for tid in target_ids if not by_id.get(tid, {}).get("kb_ingestion_completed")]
        completed = len(target_ids) - len(pending)
        status = f"[poll {poll_count}] {completed}/{len(target_ids)} completed"
        if pending:
            status += f" — waiting on: {', '.join(pending)}"
        print(status, file=sys.stderr)

        if not pending:
            return payload, rows
        if time.monotonic() > deadline:
            print(f"Timed out after {args.timeout}s with {len(pending)} document(s) "
                  f"still pending: {', '.join(pending)}", file=sys.stderr)
            return payload, rows
        time.sleep(args.poll_every)


def render_rows(rows, fmt):
    if fmt == "markdown":
        render_markdown(rows, HEADERS)
    elif fmt == "csv":
        render_csv(rows, HEADERS)
    else:
        render_tsv(rows, HEADERS)


def main():
    args = parse_args()
    session = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region)
    client = session.client("logs")

    if args.document_ids:
        target_ids = [i.strip() for i in args.document_ids.split(",") if i.strip()]
        payload, rows = poll_until_complete(client, args, target_ids)
        rows = [row for row in rows if row["document_id"] in target_ids]
    else:
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(minutes=args.minutes_back)
        payload = fetch_payload(client, args.env, start_dt, end_dt, args)
        rows = build_rows(payload)

    raw_path = save_raw_export(payload, args.output_dir, args.env)
    print(f"Saved raw export ({len(payload)} log lines) to {raw_path}", file=sys.stderr)

    if not rows:
        print("No PLATFORM_METRIC_KB_DOCUMENT_LIFECYCLE events found in the query window.",
              file=sys.stderr)
        return

    render_rows(rows, args.format)


if __name__ == "__main__":
    main()
