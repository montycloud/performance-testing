"""Runs the real inventory locustfile headless against a local mock of the MontyCloud API."""

import csv
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
LOCUSTFILE = REPO / "scenarios/api/inventory/locustfile.py"


class MockApi(BaseHTTPRequestHandler):
    hits: list = []

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.hits.append(("POST", self.path, None))
        if self.path == "/auth/signin":
            return self._reply(200, {"Token": "test-token"})
        return self._reply(404, {})

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        self.hits.append(("GET", url.path, self.headers.get("authorization")))
        if self.headers.get("authorization") != "test-token":
            return self._reply(401, {})
        if url.path == "/auth/user":
            return self._reply(200, {"OrganizationId": "org-1"})
        if url.path == "/org/inventory-summary":
            return self._reply(200, {"SummaryType": parse_qs(url.query)["SummaryType"][0]})
        return self._reply(404, {})

    def log_message(self, *_args):
        pass


@pytest.fixture
def mock_api():
    MockApi.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockApi)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.mark.e2e
def test_inventory_scenario_against_mock_api(tmp_path, mock_api):
    users = tmp_path / "users.csv"
    users.write_text(
        "Email,Password,New Password\nu1@example.test,p,\nu2@example.test,p,\n", encoding="utf-8",
    )

    cfg = yaml.safe_load((LOCUSTFILE.parent / "config.yaml").read_text(encoding="utf-8"))
    cfg["api"]["base_url"] = mock_api
    cfg["load"].update(
        users_csv=str(users), user_count=2, iterations=2,
        think_time_min=0, think_time_max=0, batch_think_time_min=0, batch_think_time_max=0,
    )
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable, "-m", "locust", "-f", str(LOCUSTFILE), "--headless",
            "--users", "2", "--spawn-rate", "2", "--run-time", "60s",
            "--csv", str(tmp_path / "inv"), "--mc-config", str(config),
        ],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]

    with open(tmp_path / "inv_stats.csv", newline="", encoding="utf-8") as fh:
        rows = {r["Name"]: r for r in csv.DictReader(fh)}

    # 2 users × 2 iterations × 3 summary types, one sign-in per user, no failures.
    for summary_type in ("by-account", "by-region", "by-resourcetype"):
        assert rows[f"[Inventory] inventory-summary ({summary_type})"]["Request Count"] == "4"
    assert rows["[Auth] /auth/signin"]["Request Count"] == "2"
    assert rows["Aggregated"]["Failure Count"] == "0"

    reports = list(tmp_path.glob("custom_inventory_*.html"))
    assert len(reports) == 1, proc.stderr[-3000:]
    assert "inventory-summary (by-region)" in reports[0].read_text(encoding="utf-8")
