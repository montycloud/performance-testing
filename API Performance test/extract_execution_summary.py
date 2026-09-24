#!/usr/bin/env python3
"""Extract per-execution summary rows from chat sweep report artifacts.

Usage:
    python3 extract_execution_summary.py \
      --manifest executions_wafr.yaml \
      --reports-dir reports/reports_executed_on_ec2/.../WAFR/1_parallel_users

The script matches each execution name in the manifest to its generated files:
  - custom_<name>_*.html
  - stats_<name>_stats.csv
  - stats_<name>_failures.csv
  - stats_<name>_exceptions.csv
  - stats_<name>_session_timeouts.json

It emits CSV rows with these columns:
  Type, Number of Chats, Number of Tenants, Domain / Area, Query(Intials),
  Complexity, Completed in, time_to_first_token, Errors if Any

Notes:
  - Completed in is the overall test duration.
  - time_to_first_token is the p95 value from the [Chat] time_to_first_token row.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import yaml


_COL_NAME = "Name"
_COL_REQS = "Request Count"
_COL_RPS = "Requests/s"
_COL_P95 = "95%"

_AGGREGATED = "Aggregated"
_TTFT_ROW = "[Chat] time_to_first_token"

_DURATION_CARD_RE = re.compile(
    r'<div class="card-label">Test Duration</div><div class="card-value">([^<]+)</div>'
)


@dataclass
class ExecutionArtifacts:
    stats_csv: Optional[Path]
    failures_csv: Optional[Path]
    exceptions_csv: Optional[Path]
    session_timeouts_json: Optional[Path]
    custom_html: Optional[Path]


def _parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Extract a CSV summary from a chat sweep manifest and report folder."
    )
    parser.add_argument(
        "--manifest",
        required=True,
        help="Path to the executions_*.yaml manifest.",
    )
    parser.add_argument(
        "--reports-dir",
        required=True,
        help="Directory containing custom HTML and stats_* sidecar files.",
    )
    parser.add_argument(
        "--output",
        help=(
            "Output CSV path. Defaults to <reports-dir>/<manifest-stem>_summary.csv. "
            "Use '-' to write CSV to stdout."
        ),
    )
    parser.add_argument(
        "--type",
        default="Baseline",
        help="Value to use for the Type column. Default: Baseline.",
    )
    parser.add_argument(
        "--domain",
        help="Optional override for Domain / Area. Defaults to inferring from the manifest name.",
    )
    parser.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="Exit non-zero if any execution is missing its stats CSV.",
    )
    parser.set_defaults(base_dir=here)
    return parser.parse_args()


def _resolve_path(raw_path: str, base_dir: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _infer_domain(manifest_path: Path) -> str:
    stem = manifest_path.stem
    if stem.startswith("executions_"):
        domain = stem[len("executions_") :]
    else:
        domain = stem

    aliases = {
        "wafr": "WAFR",
        "api": "API",
    }
    if domain in aliases:
        return aliases[domain]
    return domain.replace("_", " ").title()


def _normalize_description_lines(description: Optional[str]) -> List[str]:
    if not description:
        return []
    return [line.strip() for line in description.splitlines() if line.strip()]


def _extract_tenant_count(lines: List[str]) -> str:
    if len(lines) < 3:
        return ""
    match = re.search(r"Tenants\s+(\d+)", lines[2], re.IGNORECASE)
    return match.group(1) if match else ""


def _extract_query_text(lines: List[str]) -> str:
    return lines[3] if len(lines) >= 4 else ""


def _extract_complexity(execution_name: str) -> str:
    match = re.search(r"_(high|medium|low)_", execution_name, re.IGNORECASE)
    if not match:
        return ""
    return match.group(1).capitalize()


def _discover_artifacts(reports_dir: Path, execution_name: str) -> ExecutionArtifacts:
    def _latest(pattern: str) -> Optional[Path]:
        matches = sorted(reports_dir.glob(pattern), key=lambda path: path.stat().st_mtime)
        return matches[-1] if matches else None

    return ExecutionArtifacts(
        stats_csv=_latest(f"stats_{execution_name}_stats.csv"),
        failures_csv=_latest(f"stats_{execution_name}_failures.csv"),
        exceptions_csv=_latest(f"stats_{execution_name}_exceptions.csv"),
        session_timeouts_json=_latest(f"stats_{execution_name}_session_timeouts.json"),
        custom_html=_latest(f"custom_{execution_name}_*.html"),
    )


def _read_stats_rows(csv_path: Optional[Path]) -> List[Dict[str, str]]:
    if not csv_path or not csv_path.exists():
        return []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _find_row(rows: Iterable[Dict[str, str]], row_name: str) -> Optional[Dict[str, str]]:
    for row in rows:
        if row.get(_COL_NAME, "") == row_name:
            return row
    return None


def _parse_html_duration(custom_html: Optional[Path]) -> Optional[str]:
    if not custom_html or not custom_html.exists():
        return None
    content = custom_html.read_text(encoding="utf-8", errors="ignore")
    match = _DURATION_CARD_RE.search(content)
    if not match:
        return None
    return unescape(match.group(1)).strip()


def _derive_duration_from_aggregated(row: Optional[Dict[str, str]]) -> str:
    if not row:
        return ""
    try:
        requests = float(row.get(_COL_REQS, 0) or 0)
        requests_per_second = float(row.get(_COL_RPS, 0) or 0)
    except (TypeError, ValueError):
        return ""
    if requests_per_second <= 0:
        return ""
    seconds = requests / requests_per_second
    return _format_seconds(seconds)


def _format_seconds(total_seconds: float) -> str:
    rounded = int(round(total_seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _format_ttft_p95(row: Optional[Dict[str, str]]) -> str:
    if not row:
        return ""
    raw_value = row.get(_COL_P95, "")
    if raw_value in (None, ""):
        return ""
    try:
        milliseconds = float(raw_value)
    except (TypeError, ValueError):
        return ""
    seconds = milliseconds / 1000.0
    return f"{seconds:.2f}s"


def _collect_failure_messages(csv_path: Optional[Path], message_field: str) -> List[str]:
    rows = _read_stats_rows(csv_path)
    messages: List[str] = []
    for row in rows:
        message = (row.get(message_field) or "").strip()
        occurrences = (row.get("Occurrences") or row.get("Count") or "").strip()
        if not message:
            continue
        if occurrences:
            messages.append(f"{message} x{occurrences}")
        else:
            messages.append(message)
    return messages


def _collect_session_timeout_messages(json_path: Optional[Path]) -> List[str]:
    if not json_path or not json_path.exists():
        return []
    with json_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not data:
        return []
    total_continuations = 0
    affected_users = 0
    for value in data.values():
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count > 0:
            total_continuations += count
            affected_users += 1
    if total_continuations == 0:
        return []
    return [
        f"Session Time limit Reached x{total_continuations}",
        f"Continuation retries across {affected_users} user(s)",
    ]


def _dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen = set()
    ordered: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def _build_errors_field(artifacts: ExecutionArtifacts) -> str:
    messages = []
    messages.extend(_collect_failure_messages(artifacts.failures_csv, "Error"))
    messages.extend(_collect_failure_messages(artifacts.exceptions_csv, "Message"))
    messages.extend(_collect_session_timeout_messages(artifacts.session_timeouts_json))
    return "; ".join(_dedupe_preserve_order(messages))


def _load_manifest(manifest_path: Path) -> Dict[str, object]:
    with manifest_path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Manifest must contain a top-level mapping: {manifest_path}")
    return data


def _build_row(
    execution: Dict[str, object],
    shared_users: object,
    domain: str,
    row_type: str,
    reports_dir: Path,
) -> Dict[str, str]:
    execution_name = str(execution.get("name") or "").strip()
    description_lines = _normalize_description_lines(execution.get("description"))
    artifacts = _discover_artifacts(reports_dir, execution_name)
    stats_rows = _read_stats_rows(artifacts.stats_csv)
    aggregated = _find_row(stats_rows, _AGGREGATED)
    ttft_row = _find_row(stats_rows, _TTFT_ROW)

    completed_in = _parse_html_duration(artifacts.custom_html)
    if not completed_in:
        completed_in = _derive_duration_from_aggregated(aggregated)

    return {
        "Type": row_type,
        "Number of Chats": str(shared_users or ""),
        "Number of Tenants": _extract_tenant_count(description_lines),
        "Domain / Area": domain,
        "Query(Intials)": _extract_query_text(description_lines),
        "Complexity": _extract_complexity(execution_name),
        "Completed in": completed_in,
        "time_to_first_token": _format_ttft_p95(ttft_row),
        "Errors if Any": _build_errors_field(artifacts),
    }


def _write_csv(rows: List[Dict[str, str]], output_path: Optional[Path]) -> None:
    fieldnames = [
        "Type",
        "Number of Chats",
        "Number of Tenants",
        "Domain / Area",
        "Query(Intials)",
        "Complexity",
        "Completed in",
        "time_to_first_token",
        "Errors if Any",
    ]

    if output_path is None:
        writer = csv.DictWriter(sys.stdout, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = _parse_args()
    base_dir = Path(__file__).resolve().parent
    manifest_path = _resolve_path(args.manifest, base_dir)
    reports_dir = _resolve_path(args.reports_dir, base_dir)

    manifest = _load_manifest(manifest_path)
    run_block = manifest.get("run") or {}
    executions = manifest.get("executions") or []

    if not isinstance(run_block, dict):
        raise ValueError("Manifest 'run' block must be a mapping.")
    if not isinstance(executions, list):
        raise ValueError("Manifest 'executions' block must be a list.")

    domain = args.domain or _infer_domain(manifest_path)
    shared_users = run_block.get("users", "")
    rows = [
        _build_row(execution, shared_users, domain, args.type, reports_dir)
        for execution in executions
        if isinstance(execution, dict)
    ]

    if args.fail_on_missing:
        missing = []
        for execution in executions:
            if not isinstance(execution, dict):
                continue
            artifacts = _discover_artifacts(reports_dir, str(execution.get("name") or ""))
            if not artifacts.stats_csv:
                missing.append(str(execution.get("name") or ""))
        if missing:
            print(
                "Missing stats CSV for executions: " + ", ".join(missing),
                file=sys.stderr,
            )
            return 1

    output_path: Optional[Path]
    if args.output == "-":
        output_path = None
    elif args.output:
        output_path = _resolve_path(args.output, base_dir)
    else:
        output_path = reports_dir / f"{manifest_path.stem}_summary.csv"

    _write_csv(rows, output_path)

    if output_path is None:
        return 0

    print(f"Wrote {len(rows)} row(s) to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())