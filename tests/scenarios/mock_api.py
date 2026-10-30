"""A local stand-in for the MontyCloud API, just rich enough for every scenario's ID-capturing calls."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml

REPO = Path(__file__).resolve().parents[2]
TOKEN = "test-token"
DAY2_SECRET = "test-secret"

# Response bodies for calls whose output feeds a later call. Everything else gets {}.
BODIES: dict[str, dict[str, Any]] = {
    "/war-assessment/workloads": {"WorkloadSummaries": [{"WorkloadId": "wl-1"}]},
    "/health/api/v1/events/events": {"HealthEvents": [{"Id": "ev-1"}]},
    "/day2/api/v1/tenants": {"Tenants": [{"ID": "t1"}, {"ID": "t2"}]},
    "/day2/api/v1/organizations": {"Organizations": [{"ID": "o1"}]},
}
SUFFIX_BODIES: dict[str, dict[str, Any]] = {
    "/assessments": {"Assessments": [{"ID": "a1"}]},
    "/accounts": {"Accounts": [{"AccountId": "acc-1"}]},
    "/security/services": {"Services": [{"ServiceId": "s1"}]},
}


class _Handler(BaseHTTPRequestHandler):
    hits: Counter = Counter()

    def _reply(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.hits[("POST", self.path)] += 1
        if self.path == "/auth/signin":
            return self._reply(200, {"Token": TOKEN})
        return self._reply(404, {})

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        self.hits[("GET", path)] += 1
        if path.startswith("/day2/"):
            authorized = self.headers.get("authorization") == DAY2_SECRET and self.headers.get("x-api-key")
        else:
            authorized = self.headers.get("authorization") == TOKEN
        if not authorized:
            return self._reply(401, {})
        if path == "/auth/user":
            return self._reply(200, {"OrganizationId": "org-1"})
        if path == "/org/inventory-summary":
            return self._reply(200, {"SummaryType": parse_qs(url.query)["SummaryType"][0]})
        if path in BODIES:
            return self._reply(200, BODIES[path])
        for suffix, body in SUFFIX_BODIES.items():
            if path.endswith(suffix):
                return self._reply(200, body)
        return self._reply(200, {})

    def log_message(self, *_args):
        pass


@contextmanager
def mock_api() -> Iterator[tuple[str, Counter]]:
    """Yields (base_url, hit counter keyed by (method, path))."""
    _Handler.hits = Counter()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _Handler.hits
    finally:
        server.shutdown()


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


NO_THINK = {
    "think_time_min": 0, "think_time_max": 0, "batch_think_time_min": 0, "batch_think_time_max": 0,
}


def run_scenario(
    scenario: str, tmp_path: Path, base_url: str, overrides: dict[str, Any] | None = None,
    users: int = 1, env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, dict[str, dict[str, str]]]:
    """Run scenarios/api/<scenario>/locustfile.py headless with the shipped config plus *overrides*.

    Returns the finished process and the stats CSV rows keyed by request name.
    """
    locustfile = REPO / "scenarios" / "api" / scenario / "locustfile.py"
    users_csv = tmp_path / "users.csv"
    users_csv.write_text(
        "Email,Password,New Password\n" + "".join(f"u{i}@example.test,p,\n" for i in range(users)),
        encoding="utf-8",
    )
    cfg = yaml.safe_load((locustfile.parent / "config.yaml").read_text(encoding="utf-8"))
    cfg["api"] = {"base_url": base_url, "timeout_seconds": 10}
    deep_merge(cfg, {"load": {"users_csv": str(users_csv), "user_count": users, "iterations": 1, **NO_THINK}})
    deep_merge(cfg, overrides or {})
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable, "-m", "locust", "-f", str(locustfile), "--headless",
            "--users", str(users), "--spawn-rate", str(users), "--run-time", "60s",
            "--csv", str(tmp_path / scenario), "--mc-config", str(config),
        ],
        capture_output=True, text=True, timeout=120, env={**_base_env(), **(env or {})},
    )
    stats = tmp_path / f"{scenario}_stats.csv"
    rows: dict[str, dict[str, str]] = {}
    if stats.exists():
        with open(stats, newline="", encoding="utf-8") as fh:
            rows = {r["Name"]: r for r in csv.DictReader(fh)}
    return proc, rows


def _base_env() -> dict[str, str]:
    import os

    # Keep the developer's own DAY2SDK_* keys out of test runs.
    return {k: v for k, v in os.environ.items() if not k.startswith("DAY2SDK_")}
