#!/usr/bin/env python3
"""
Security Bot Rescan Performance Test - CLI driver
=================================================
Runs the Security Bot flow (see Security_bot.prd) and writes every step's
request/response into logs/<run_ts>/.

Usage:
  python security_bot.py [--users N] [--dry-run] [--config config.yaml] [-v]

Setup:
  cp .env.example .env                 # ROOT_EMAIL / ROOT_PASSWORD for single-user runs
  cp users.csv.example users.csv       # one row per simulated user, then fill Password
  pip install -r requirements.txt
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from http_client import SecurityBotClient, StepError  # noqa: E402
from run_logger import RunLogger  # noqa: E402
from settings import load_settings  # noqa: E402
from steps import (  # noqa: E402
    UserContext,
    assign_organizations,
    load_org_pool,
    load_users,
    step_fetch_findings,
    step_refresh_token,
    step_select_org,
    step_signin,
    step_switch_org,
    step_trigger_rescan,
)

logger = logging.getLogger("security_bot")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Security Bot rescan performance test")
    p.add_argument("--config", default=None, help="config file (default: config.yaml)")
    p.add_argument("--users", type=int, default=None, help="number of simulated users")
    p.add_argument(
        "--findings-limit",
        type=int,
        default=None,
        help="findings fetched (and rescanned) per user",
    )
    p.add_argument("--dry-run", action="store_true", help="log the calls without sending them")
    p.add_argument("-v", "--verbose", action="store_true", help="debug console output")
    return p.parse_args()


def run_user(ctx: UserContext, settings, pool, run_log: RunLogger, findings_limit=None, rescan_barrier=None) -> None:
    try:
        step_signin(ctx, settings, run_log)
        step_select_org(ctx, pool, run_log)
        step_switch_org(ctx, settings, run_log, root_org_id=pool.root_org_id)
        step_refresh_token(ctx, settings, run_log)
        step_fetch_findings(ctx, settings, run_log, limit=findings_limit)
        if rescan_barrier:
            logger.info("%s is ready for the synchronized rescan", ctx.label)
            rescan_barrier.wait()
        step_trigger_rescan(ctx, settings, run_log)
    except StepError as exc:
        logger.error("%s aborted: %s", ctx.label, exc)
        if rescan_barrier:
            rescan_barrier.abort()
    except threading.BrokenBarrierError:
        ctx.errors.append("rescan synchronization cancelled because another user failed before step 6")
        logger.error("%s skipped rescan because the synchronization barrier was cancelled", ctx.label)
    finally:
        ctx.client.close()


def main() -> int:
    args = _parse_args()
    settings = load_settings(args.config)
    n_users = args.users if args.users is not None else int(settings.run["users"])

    run_log = RunLogger(
        settings.logs_dir, console_level=logging.DEBUG if args.verbose else logging.INFO
    )

    try:
        pool = load_org_pool(settings)
        if n_users > len(pool):
            logger.error(
                "%d users requested but only %d organisations are available "
                "(orgs are assigned strictly unique). Lower --users or add orgs.",
                n_users,
                len(pool),
            )
            return 2
        users = assign_organizations(load_users(settings, n_users), pool)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 2

    logger.info(
        "Starting run: users=%d org_pool=%d base_url=%s dry_run=%s",
        n_users,
        len(pool),
        settings.base_url,
        args.dry_run,
    )

    contexts = [
        UserContext(
            index=i + 1,
            name=row["Name"],
            email=row["Email"],
            password=row["Password"],
            org_id=row["OrganizationId"],
            org_name=row["OrganizationName"],
            client=SecurityBotClient(
                base_url=settings.base_url,
                timeout=settings.timeout,
                dry_run=args.dry_run,
                label=f"user{i + 1:02d}",
            ),
        )
        for i, row in enumerate(users)
    ]

    rescan_barrier = threading.Barrier(n_users) if n_users > 1 else None
    with ThreadPoolExecutor(max_workers=n_users, thread_name_prefix="security-bot-user") as executor:
        futures = [
            executor.submit(
                run_user,
                ctx,
                settings,
                pool,
                run_log,
                args.findings_limit,
                rescan_barrier,
            )
            for ctx in contexts
        ]
        for future in as_completed(futures):
            future.result()

    run_log.write_json("summary.json", {"run_ts": run_log.run_ts, "users": [c.summary() for c in contexts]})
    run_log.write_lines("rescan_ids.txt", [rid for c in contexts for rid in c.rescan_ids])
    failed = [c.index for c in contexts if c.errors]
    logger.info("Run complete. Artefacts in %s", run_log.run_dir)
    if failed:
        logger.warning("Users with errors: %s", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
