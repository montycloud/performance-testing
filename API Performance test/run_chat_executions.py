#!/usr/bin/env python3
"""
Chat query sweep runner
========================
Runs the existing Locust chat scenario (locustfile.py) multiple times — same
users/concurrency, a different chat_queries file (and optionally description)
each time — driven by a YAML manifest (default: executions.yaml).

This script never modifies locustfile.py, report_generator.py, or the
*content* of config.yaml: it overwrites config.yaml with a per-run variant
right before invoking locust, then restores the original bytes once the whole
sweep finishes (or is interrupted). Each run's outputs — the custom HTML
report, locust's own --html/--csv, and the chat transcript log — are all
suffixed with that run's execution name so nothing gets overwritten.

Usage:
    python3 run_chat_executions.py                      # uses ./executions.yaml
    python3 run_chat_executions.py --manifest sweep.yaml
    python3 run_chat_executions.py --dry-run             # validate only, no run

Exit code is non-zero if any execution's locust run failed, so this can be
used directly as a single CI/pipeline step.
"""

import argparse
import copy
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

_HERE = Path(__file__).parent
_CONFIG_FILE = _HERE / "config.yaml"
_BACKUP_SENTINEL = _HERE / "config.yaml.sweep-backup"
_REPORTS_DIR = _HERE / "reports"

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _load_yaml(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_manifest(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    manifest = _load_yaml(path)
    executions = manifest.get("executions") or []
    if not executions:
        raise ValueError(f"Manifest {path} has no 'executions' entries.")

    seen_names = set()
    for i, entry in enumerate(executions):
        name = entry.get("name")
        if not name:
            raise ValueError(f"executions[{i}] is missing required field 'name'.")
        if not _NAME_RE.match(name):
            raise ValueError(
                f"executions[{i}].name={name!r} is invalid — use only letters, "
                "digits, '_' and '-' (it is used directly in output file names)."
            )
        if name in seen_names:
            raise ValueError(f"executions[{i}].name={name!r} is duplicated.")
        seen_names.add(name)

        if not entry.get("queries_file"):
            raise ValueError(f"executions[{i}] ({name}) is missing 'queries_file'.")
        qf = Path(entry["queries_file"])
        if not qf.is_absolute():
            qf = (_HERE / qf).resolve()
        if not qf.exists():
            raise FileNotFoundError(f"executions[{i}] ({name}): queries_file not found: {qf}")

    return manifest


def _restore_config(original_text: str) -> None:
    _CONFIG_FILE.write_text(original_text, encoding="utf-8")
    if _BACKUP_SENTINEL.exists():
        _BACKUP_SENTINEL.unlink()
    print(f"[sweep] Restored original {_CONFIG_FILE.name}")


def _latest_custom_report(name: str) -> Optional[str]:
    matches = sorted(_REPORTS_DIR.glob(f"custom_{name}_*.html"), key=lambda p: p.stat().st_mtime)
    return str(matches[-1]) if matches else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the chat Locust test across multiple chat_queries/description combinations."
    )
    parser.add_argument(
        "--manifest",
        default=str(_HERE / "executions.yaml"),
        help="Path to the execution manifest YAML (default: executions.yaml)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the manifest and print the planned runs without invoking locust or touching config.yaml",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = (Path.cwd() / manifest_path).resolve()

    manifest = _load_manifest(manifest_path)
    run_cfg = manifest.get("run", {})
    users = run_cfg.get("users", 1)
    spawn_rate = run_cfg.get("spawn_rate", 1)
    run_time = run_cfg.get("run_time", "")
    executions = manifest["executions"]

    if args.dry_run:
        print(f"[sweep] Manifest: {manifest_path}")
        print(
            f"[sweep] Shared run settings: users={users} spawn_rate={spawn_rate} "
            f"run_time={run_time or '(none — single_journey auto-stop)'}"
        )
        for entry in executions:
            desc_note = "  (description override)" if entry.get("description") else ""
            print(f"  - {entry['name']}: queries_file={entry['queries_file']}{desc_note}")
        print(f"[sweep] {len(executions)} execution(s) validated. Dry run — nothing was executed.")
        return 0

    if _BACKUP_SENTINEL.exists():
        print(
            f"[sweep] ERROR: {_BACKUP_SENTINEL.name} exists — a previous sweep may have "
            f"crashed before restoring {_CONFIG_FILE.name}. Inspect/restore it manually, "
            "then delete the sentinel before re-running.",
            file=sys.stderr,
        )
        return 2

    original_text = _CONFIG_FILE.read_text(encoding="utf-8")
    _BACKUP_SENTINEL.write_text(original_text, encoding="utf-8")
    base_config = yaml.safe_load(original_text)

    restored = False

    def _restore_and_maybe_exit(signum=None, frame=None):
        nonlocal restored
        if not restored:
            _restore_config(original_text)
            restored = True
        if signum is not None:
            sys.exit(130)

    signal.signal(signal.SIGINT, _restore_and_maybe_exit)
    signal.signal(signal.SIGTERM, _restore_and_maybe_exit)

    results: List[Dict[str, Any]] = []
    try:
        for entry in executions:
            name = entry["name"]
            print(f"\n[sweep] === {name} ===")

            cfg = copy.deepcopy(base_config)
            cfg["chat"]["queries_file"] = entry["queries_file"]
            if entry.get("description"):
                cfg["test"]["description"] = entry["description"]
            cfg["test"]["report_name"] = name
            cfg["chat"]["transcript_log"] = f"./reports/chat_transcript_{name}.log"

            _CONFIG_FILE.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

            html_path = _REPORTS_DIR / f"locust_report_{name}.html"
            csv_prefix = _REPORTS_DIR / f"stats_{name}"

            cmd = [
                sys.executable, "-m", "locust",
                "-f", "locustfile.py",
                "--headless",
                "--users", str(users),
                "--spawn-rate", str(spawn_rate),
                "--html", str(html_path),
                "--csv", str(csv_prefix),
            ]
            if run_time:
                cmd += ["--run-time", str(run_time)]

            start = time.monotonic()
            proc = subprocess.run(cmd, cwd=_HERE)
            elapsed = time.monotonic() - start

            results.append({
                "name": name,
                "exit_code": proc.returncode,
                "elapsed_s": round(elapsed, 1),
                "custom_report": _latest_custom_report(name),
                "html": str(html_path),
                "csv_prefix": str(csv_prefix),
                "transcript": f"reports/chat_transcript_{name}.log",
            })
    finally:
        _restore_and_maybe_exit()

    print("\n[sweep] Summary")
    print(f"{'Name':<24} {'Exit':>5} {'Time(s)':>8}  Report")
    overall_ok = True
    for r in results:
        overall_ok = overall_ok and r["exit_code"] == 0
        report = r["custom_report"] or r["html"]
        print(f"{r['name']:<24} {r['exit_code']:>5} {r['elapsed_s']:>8}  {report}")

    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
