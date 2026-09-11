#!/usr/bin/env python3
"""
Multi-user pipeline runner
============================
Runs a Locust file headless once per user-count in 1..N, each as its own
sequential run with its own --csv/--html output, so a CI/pipeline step can
verify behavior at increasing concurrency without hand-editing config.

Does NOT touch config.yaml/cost_dashboard_config.yaml — only Locust's own
--users flag changes between runs (user_count in config governs how many
credential rows are loaded from the CSV; make sure it's >= --max-users).

Usage:
    python3 run_users_sweep.py
    python3 run_users_sweep.py --locustfile cost_dashboard_locustfile.py --max-users 5
    python3 run_users_sweep.py --locustfile locustfile.py --max-users 10 --spawn-rate 5 --run-time 3m
    python3 run_users_sweep.py --users 1,2,5,10   # explicit list instead of 1..N

Exit code is non-zero if any individual run failed, so this can be used
directly as a single CI/pipeline step.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

_HERE = Path(__file__).parent
_REPORTS_DIR = _HERE / "reports"


def _parse_user_counts(args: argparse.Namespace) -> List[int]:
    if args.users:
        counts = []
        for part in args.users.split(","):
            part = part.strip()
            if not part:
                continue
            n = int(part)
            if n < 1:
                raise ValueError(f"--users values must be >= 1, got {n}")
            counts.append(n)
        if not counts:
            raise ValueError("--users produced an empty list.")
        return counts
    if args.max_users < 1:
        raise ValueError("--max-users must be >= 1.")
    return list(range(1, args.max_users + 1))


def _run_one(locustfile: str, users: int, spawn_rate: int, run_time: Optional[str], extra_args: List[str]) -> int:
    label = f"{users}u"
    csv_prefix = _REPORTS_DIR / f"sweep_{Path(locustfile).stem}_{label}"
    html_out = _REPORTS_DIR / f"sweep_{Path(locustfile).stem}_{label}.html"
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "locust",
        "-f", locustfile,
        "--headless",
        "--users", str(users),
        "--spawn-rate", str(spawn_rate),
        "--csv", str(csv_prefix),
        "--html", str(html_out),
    ]
    if run_time:
        cmd += ["--run-time", run_time]
    cmd += extra_args

    print(f"\n{'=' * 70}\nRun: users={users}  ->  {csv_prefix}\n{'=' * 70}")
    proc = subprocess.run(cmd, cwd=str(_HERE))
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a Locust file headless once per user-count (1..N or an explicit list).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--locustfile", default="cost_dashboard_locustfile.py",
        help="Locust file to run (default: cost_dashboard_locustfile.py)",
    )
    parser.add_argument(
        "--max-users", type=int, default=5,
        help="Run user counts 1..N (default: 5). Ignored if --users is given.",
    )
    parser.add_argument(
        "--users", default="",
        help="Comma-separated explicit list of user counts, e.g. 1,2,5,10 (overrides --max-users).",
    )
    parser.add_argument("--spawn-rate", type=int, default=5, help="Locust --spawn-rate (default: 5)")
    parser.add_argument(
        "--run-time", default=None,
        help="Locust --run-time (e.g. 2m). Omit to let single_journey mode exit on its own.",
    )
    parser.add_argument(
        "--stop-on-failure", action="store_true",
        help="Stop the sweep as soon as one run fails (default: run all, report failures at the end).",
    )
    args, extra_args = parser.parse_known_args()

    locustfile_path = _HERE / args.locustfile
    if not locustfile_path.exists():
        print(f"Locust file not found: {locustfile_path}", file=sys.stderr)
        return 2

    try:
        user_counts = _parse_user_counts(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    failures: List[int] = []
    for users in user_counts:
        rc = _run_one(args.locustfile, users, args.spawn_rate, args.run_time, extra_args)
        if rc != 0:
            failures.append(users)
            print(f"Run with users={users} FAILED (exit code {rc}).", file=sys.stderr)
            if args.stop_on_failure:
                break

    print(f"\n{'=' * 70}")
    if failures:
        print(f"Sweep finished with failures at user counts: {failures}")
        return 1
    print(f"Sweep finished successfully for user counts: {user_counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
