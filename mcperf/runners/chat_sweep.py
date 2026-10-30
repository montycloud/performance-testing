#!/usr/bin/env python3
"""
Chat query sweep runner
========================
Runs the journey scenario several times — same users/concurrency, a different
chat queries file (and optionally description) each time — driven by a YAML
manifest (see scenarios/api/journey/executions/).

Each run gets its own generated config (written to <reports-dir>/sweep_configs/
and passed with --mc-config), so the scenario's config.yaml is never modified.
Each run's outputs — the custom HTML report, Locust's --html/--csv, and the chat
transcript — are suffixed with the execution name.

Paths in the manifest's `queries_file` resolve against the base config's folder
(scenarios/api/journey/ by default), same as chat.queries_file in that config.

Usage (from the repo root):
    python -m mcperf.runners.chat_sweep --manifest scenarios/api/journey/executions/wafr.yaml
    python -m mcperf.runners.chat_sweep --manifest ... --dry-run     # validate only
    python -m mcperf.runners.chat_sweep --manifest ... --env stg1    # extra args go to Locust

Exit code is non-zero if any execution's Locust run failed, so this can be used
directly as a single CI/pipeline step.
"""

from __future__ import annotations

import argparse
import copy
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from mcperf.core.config import load_yaml, resolve_path

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_LOCUSTFILE = _REPO / "scenarios" / "api" / "journey" / "locustfile.py"
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Config keys holding paths; made absolute so the generated config works from any folder.
_PATH_KEYS = (("load", "users_csv"), ("chat", "queries_file"), ("chat", "tenants_file"),
              ("chat", "transcript_log"))


def load_manifest(path: Path, base_dir: Path) -> dict[str, Any]:
    manifest = load_yaml(path)
    executions = manifest.get("executions") or []
    if not executions:
        raise ValueError(f"Manifest {path} has no 'executions' entries.")

    seen: set[str] = set()
    for i, entry in enumerate(executions):
        name = entry.get("name")
        if not name:
            raise ValueError(f"executions[{i}] is missing required field 'name'.")
        if not _NAME_RE.match(name):
            raise ValueError(
                f"executions[{i}].name={name!r} is invalid — use only letters, digits, '_' and '-' "
                "(it is used directly in output file names)."
            )
        if name in seen:
            raise ValueError(f"executions[{i}].name={name!r} is duplicated.")
        seen.add(name)
        if not entry.get("queries_file"):
            raise ValueError(f"executions[{i}] ({name}) is missing 'queries_file'.")
        queries = resolve_path(entry["queries_file"], base_dir)
        if not queries.exists():
            raise FileNotFoundError(f"executions[{i}] ({name}): queries_file not found: {queries}")
    return manifest


def run_config(base: dict[str, Any], base_dir: Path, entry: dict[str, Any], reports_dir: Path) -> dict[str, Any]:
    """The base config with this execution's queries, name, description and transcript."""
    cfg = copy.deepcopy(base)
    for section, key in _PATH_KEYS:
        value = (cfg.get(section) or {}).get(key)
        if value:
            cfg[section][key] = str(resolve_path(value, base_dir))
    name = entry["name"]
    cfg.setdefault("chat", {})["queries_file"] = str(resolve_path(entry["queries_file"], base_dir))
    cfg["chat"]["transcript_log"] = str(reports_dir / f"chat_transcript_{name}.log")
    report = cfg.setdefault("report", {})
    report["report_name"] = name
    if entry.get("description"):
        report["description"] = entry["description"]
    return cfg


def _latest_custom_report(reports_dir: Path, name: str) -> str | None:
    matches = sorted(reports_dir.glob(f"custom_{name}_*.html"), key=lambda p: p.stat().st_mtime)
    return str(matches[-1]) if matches else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the journey Locust test across multiple chat query files.",
    )
    parser.add_argument("--manifest", required=True, help="Sweep manifest YAML")
    parser.add_argument("--locustfile", default=str(_DEFAULT_LOCUSTFILE),
                        help="Scenario to run (default: scenarios/api/journey/locustfile.py)")
    parser.add_argument("--config", default=None,
                        help="Base config (default: config.yaml next to the locustfile)")
    parser.add_argument("--reports-dir", default="reports", help="Output folder (default: ./reports)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate the manifest and print the planned runs without running Locust")
    args, locust_args = parser.parse_known_args()

    locustfile = Path(args.locustfile).resolve()
    base_path = Path(args.config).resolve() if args.config else locustfile.parent / "config.yaml"
    base_dir = base_path.parent
    reports_dir = Path(args.reports_dir).resolve()

    manifest = load_manifest(Path(args.manifest).resolve(), base_dir)
    run_cfg = manifest.get("run", {}) or {}
    users = run_cfg.get("users", 1)
    spawn_rate = run_cfg.get("spawn_rate", 1)
    run_time = run_cfg.get("run_time", "")
    executions = manifest["executions"]

    if args.dry_run:
        print(f"[sweep] Manifest: {args.manifest}  base config: {base_path}")
        print(f"[sweep] Shared run settings: users={users} spawn_rate={spawn_rate} "
              f"run_time={run_time or '(none — single_journey auto-stop)'}")
        for entry in executions:
            desc_note = "  (description override)" if entry.get("description") else ""
            print(f"  - {entry['name']}: queries_file={entry['queries_file']}{desc_note}")
        print(f"[sweep] {len(executions)} execution(s) validated. Dry run — nothing was executed.")
        return 0

    base = load_yaml(base_path)
    configs_dir = reports_dir / "sweep_configs"
    configs_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for entry in executions:
        name = entry["name"]
        print(f"\n[sweep] === {name} ===")
        config_path = configs_dir / f"{name}.yaml"
        config_path.write_text(
            yaml.safe_dump(run_config(base, base_dir, entry, reports_dir), sort_keys=False), encoding="utf-8",
        )
        html_path = reports_dir / f"locust_report_{name}.html"
        csv_prefix = reports_dir / f"stats_{name}"
        cmd = [
            sys.executable, "-m", "locust", "-f", str(locustfile), "--headless",
            "--users", str(users), "--spawn-rate", str(spawn_rate),
            "--html", str(html_path), "--csv", str(csv_prefix),
            "--mc-config", str(config_path),
            *locust_args,
        ]
        if run_time:
            cmd += ["--run-time", str(run_time)]

        start = time.monotonic()
        proc = subprocess.run(cmd)
        results.append({
            "name": name,
            "exit_code": proc.returncode,
            "elapsed_s": round(time.monotonic() - start, 1),
            "report": _latest_custom_report(reports_dir, name) or str(html_path),
        })

    print("\n[sweep] Summary")
    print(f"{'Name':<24} {'Exit':>5} {'Time(s)':>8}  Report")
    for r in results:
        print(f"{r['name']:<24} {r['exit_code']:>5} {r['elapsed_s']:>8}  {r['report']}")
    return 0 if all(r["exit_code"] == 0 for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
