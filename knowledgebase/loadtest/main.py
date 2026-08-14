"""KB document upload load test — CLI entry point.

    export KB_LOADTEST_TOKEN=<jwt>
    python main.py --env dev1 --collection-id <cid> --concurrency 10 --batches 5
    python main.py --env dev1 --collection-id <cid> --cleanup --run-id <run_id>
"""
import argparse
import os
import uuid

from cleanup import run_cleanup
from client import ENVIRONMENTS, KBClient, base_url_for_env
from load_runner import run_load

DEFAULT_ENV = "dev1"


def parse_args():
    p = argparse.ArgumentParser(description="KB upload load-test runner.")
    p.add_argument("--env", default=DEFAULT_ENV,
                   help=f"Target environment; base URL is derived automatically. "
                        f"Known: {', '.join(ENVIRONMENTS)} (default: {DEFAULT_ENV})")
    p.add_argument("--token", default=os.environ.get("KB_LOADTEST_TOKEN"),
                   help="JWT auth token (or set KB_LOADTEST_TOKEN)")
    p.add_argument("--org-id", default=None,
                   help="Target tenant / OrganizationId, sent as the d2oid cookie. "
                        "Optional: omit to use the token user's default org")
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
    args = p.parse_args()
    if not args.token:
        p.error("no auth token: pass --token or set KB_LOADTEST_TOKEN")
    args.base_url = base_url_for_env(args.env)
    if args.log_file is None:
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        args.log_file = os.path.join(logs_dir, f"kb_loadtest_{args.run_id}.jsonl")
    return args


def main():
    """Build the client, then dispatch to cleanup mode or a load run."""
    args = parse_args()
    client = KBClient(args.base_url, args.token, args.org_id, args.request_timeout)
    if args.cleanup:
        run_cleanup(args, client)
    else:
        run_load(args, client)


if __name__ == "__main__":
    main()