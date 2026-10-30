#!/usr/bin/env python3
"""
Multi-user pipeline runner
============================
Runs a Locust file headless once per user-count in 1..N, each as its own
sequential run with its own --csv/--html output, so a CI/pipeline step can
verify behavior at increasing concurrency without hand-editing config.

Never touches a config file — only Locust's own --users flag changes between
runs (load.user_count in the scenario config governs how many credential rows
are loaded; make sure it's >= --max-users). Extra arguments are passed through
to Locust (e.g. --env stg1, --mc-config other.yaml).

Usage (from the repo root):
    python -m mcperf.runners.users_sweep --locustfile scenarios/api/cost_dashboard/locustfile.py --max-users 5
    python -m mcperf.runners.users_sweep --locustfile scenarios/api/journey/locustfile.py \
        --max-users 10 --spawn-rate 5 --run-time 3m
    python -m mcperf.runners.users_sweep --locustfile scenarios/api/inventory/locustfile.py --users 1,2,5,10

Output: <reports-dir>/sweep_<scenario>_<N>u_stats.csv / .html, where <scenario>
is the locustfile's folder name.

Exit code is non-zero if any individual run failed, so this can be used
directly as a single CI/pipeline step.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List, Optional



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


def _scenario_name(locustfile: Path) -> str:
    return locustfile.parent.name if locustfile.stem == "locustfile" else locustfile.stem


def _run_one(
    locustfile: Path, reports_dir: Path, users: int, spawn_rate: int,
    run_time: Optional[str], extra_args: List[str],
) -> int:
    label = f"{users}u"
    csv_prefix = reports_dir / f"sweep_{_scenario_name(locustfile)}_{label}"
    html_out = reports_dir / f"sweep_{_scenario_name(locustfile)}_{label}.html"
    reports_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "-m", "locust",
        "-f", str(locustfile),
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
    proc = subprocess.run(cmd)
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a Locust file headless once per user-count (1..N or an explicit list).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--locustfile", required=True,
        help="Locust file to run, e.g. scenarios/api/cost_dashboard/locustfile.py",
    )
    parser.add_argument(
        "--reports-dir", default="reports",
        help="Where CSV/HTML output goes (default: ./reports)",
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

    locustfile_path = Path(args.locustfile).resolve()
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
        rc = _run_one(locustfile_path, Path(args.reports_dir).resolve(), users, args.spawn_rate,
                      args.run_time, extra_args)
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
