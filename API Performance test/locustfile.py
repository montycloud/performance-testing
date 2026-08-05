#!/usr/bin/env python3
"""
MontyCloud API Performance Test
=================================
Locust scenario that simulates the full user journey:
  Sign-in  →  Home Page (5 sequential batches)  →  WAFR Page (4 sequential batches)

Within each batch, all HTTP calls are fired in parallel using gevent greenlets.
Batches are strictly sequential: each batch waits for 100 % of its calls to
complete before the next batch starts.

Usage (headless — recommended):
    locust -f locustfile.py --headless \\
      --users 10 --spawn-rate 2 --run-time 5m \\
      --html reports/locust_report.html \\
      --csv reports/stats

Usage (interactive Web UI):
    locust -f locustfile.py
    # then open http://localhost:8089

See README.md for full setup and configuration details.
"""

import csv
import json
import logging
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import gevent
import gevent.pool
import websocket
import yaml
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
from locust import HttpUser, constant, events, task
from locust.exception import StopUser
from locust.runners import STATE_CLEANUP, STATE_STOPPED, STATE_STOPPING, WorkerRunner
from locust.stats import CSV_STATS_FLUSH_INTERVAL_SEC, CSV_STATS_INTERVAL_SEC

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("perf_test")

# ---------------------------------------------------------------------------
# Bootstrap: config + users
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_CONFIG_FILE = _HERE / "config.yaml"
_ENV_FILE = _HERE / ".env"

load_dotenv(dotenv_path=_ENV_FILE, override=False)


def _load_config() -> Dict[str, Any]:
    if not _CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {_CONFIG_FILE}. "
            "See config.yaml in this directory for a template."
        )
    with open(_CONFIG_FILE, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


CFG: Dict[str, Any] = _load_config()

BASE_URL: str = CFG["api"]["base_url"].rstrip("/")
MC_DEBUG_MODE: bool = bool(CFG["api"].get("mc_debug_mode", True))
TIMEOUT: int = int(CFG["api"].get("timeout_seconds", 40))

_test_cfg = CFG["test"]
_users_csv_raw: str = _test_cfg["users_csv"]
_users_csv_path = Path(_users_csv_raw)
if not _users_csv_path.is_absolute():
    _users_csv_path = (_HERE / _users_csv_path).resolve()
USER_COUNT: int = int(_test_cfg.get("user_count", 10))

# Think time between Home Page and WAFR Page (seconds)
_THINK_MIN: float = float(_test_cfg.get("think_time_min", 2))
_THINK_MAX: float = float(_test_cfg.get("think_time_max", 5))

# Think time between batches within the WAFR page (seconds)
# Falls back to the page-transition values if not explicitly configured.
_WAFR_THINK_MIN: float = float(_test_cfg.get("wafr_think_time_min", _THINK_MIN))
_WAFR_THINK_MAX: float = float(_test_cfg.get("wafr_think_time_max", _THINK_MAX))

# Run mode: "single_journey" (each user runs `iterations` journeys then stops)
#            "timed"          (users loop until --run-time expires)
_RUN_MODE: str = _test_cfg.get("run_mode", "single_journey").strip().lower()

# Journeys per user in single_journey mode (ignored in timed mode).
_ITERATIONS: int = max(1, int(_test_cfg.get("iterations", 1)))

# Health Events flow (config section: `health`).
#   enabled  - whether the Health Events calls run at all. False preserves
#              today's behaviour exactly (Home Page + WAFR only).
#   mode     - "appended"  : Signin -> Home Page -> WAFR -> think time -> Health
#              "standalone": Signin -> think time -> Health (skips Home/WAFR)
_health_cfg: Dict[str, Any] = CFG.get("health", {}) or {}
_HEALTH_ENABLED: bool = bool(_health_cfg.get("enabled", False))
_HEALTH_MODE: str = str(_health_cfg.get("mode", "appended")).strip().lower()

# Chat flow (config section: `chat`). WebSocket-based AI chat bot journey.
#   enabled  - whether the Chat call runs at all. False preserves today's
#              behaviour exactly (no WebSocket activity).
#   mode     - "appended"  : Signin -> Home Page -> WAFR -> (Health) -> think time -> Chat
#              "standalone": Signin -> think time -> Chat (skips Home/WAFR/Health)
#
# NOTE: each chat call opens ONE WebSocket connection and sends ONE query,
# then closes. Multi-turn conversations on a single connection are deferred
# — see WEBSOCKET_CHAT_APPROACH.md.
_chat_cfg: Dict[str, Any] = CFG.get("chat", {}) or {}
_CHAT_ENABLED: bool = bool(_chat_cfg.get("enabled", False))
_CHAT_MODE: str = str(_chat_cfg.get("mode", "appended")).strip().lower()
WS_BASE_URL: str = str(_chat_cfg.get("ws_base_url", "")).rstrip("/")
_CHAT_TIMEOUT: float = float(_chat_cfg.get("timeout_seconds", 120))
_CHAT_MODEL_ID: str = str(_chat_cfg.get("model_id", ""))
_CHAT_TEMPERATURE: float = float(_chat_cfg.get("temperature", 0.1))
_CHAT_TOP_P: float = float(_chat_cfg.get("top_p", 1))
_CHAT_TOP_K: int = int(_chat_cfg.get("top_k", 250))

if _CHAT_ENABLED and _HEALTH_ENABLED and _CHAT_MODE == "standalone" and _HEALTH_MODE == "standalone":
    logger.warning(
        "Both 'health.mode' and 'chat.mode' are 'standalone' — chat takes "
        "precedence; the Health Events flow will not run this test."
    )

# Optional per-message chat transcript log (config: chat.transcript_log).
# Blank/unset = disabled (no separate file; only the summary lines already
# logged via `logger` show up, subject to Locust's own --logfile/--loglevel).
_CHAT_TRANSCRIPT_RAW: str = str(_chat_cfg.get("transcript_log", "")).strip()
_CHAT_TRANSCRIPT_PATH: Optional[Path] = None
if _CHAT_ENABLED and _CHAT_TRANSCRIPT_RAW:
    _CHAT_TRANSCRIPT_PATH = Path(_CHAT_TRANSCRIPT_RAW)
    if not _CHAT_TRANSCRIPT_PATH.is_absolute():
        _CHAT_TRANSCRIPT_PATH = (_HERE / _CHAT_TRANSCRIPT_PATH).resolve()
    _CHAT_TRANSCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)

_chat_transcript_logger: Optional[logging.Logger] = None
if _CHAT_TRANSCRIPT_PATH is not None:
    _chat_transcript_logger = logging.getLogger("perf_test.chat_transcript")
    _chat_transcript_logger.setLevel(logging.INFO)
    _chat_transcript_logger.propagate = False
    if not _chat_transcript_logger.handlers:
        _transcript_handler = logging.FileHandler(_CHAT_TRANSCRIPT_PATH, mode="a", encoding="utf-8")
        _transcript_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
        _chat_transcript_logger.addHandler(_transcript_handler)
    logger.info("Chat transcript logging enabled -> %s", _CHAT_TRANSCRIPT_PATH)


def _redact_ws_url(url: str) -> str:
    """Strip the JWT out of a WS URL before it ever reaches a log line."""
    return re.sub(r"(Authorization=)[^&]+", r"\1<redacted>", url)


def _load_users(csv_path: Path, limit: int) -> List[Dict[str, str]]:
    """Load up to *limit* rows from the users CSV."""
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Users CSV not found: {csv_path}\n"
            f"  Update 'test.users_csv' in config.yaml."
        )
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"Users CSV is empty: {csv_path}")
    users = rows[:limit]
    logger.info(
        "Loaded %d user(s) from %s  [user_count limit = %d]",
        len(users), csv_path, limit,
    )
    return users


USERS: List[Dict[str, str]] = _load_users(_users_csv_path, USER_COUNT)


def _load_queries(path: Path) -> List[str]:
    """Load non-empty, non-comment lines from the chat queries file."""
    if not path.exists():
        raise FileNotFoundError(
            f"Chat queries file not found: {path}\n"
            f"  Update 'chat.queries_file' in config.yaml."
        )
    with open(path, "r", encoding="utf-8") as fh:
        lines = [line.strip() for line in fh]
    queries = [line for line in lines if line and not line.startswith("#")]
    if not queries:
        raise ValueError(f"Chat queries file is empty: {path}")
    logger.info("Loaded %d chat quer(y/ies) from %s", len(queries), path)
    return queries


def _load_tenants(path: Path) -> Dict[str, Dict[str, Any]]:
    """Load Tenant.json into a dict keyed by exact tenant `Name`."""
    if not path.exists():
        raise FileNotFoundError(
            f"Tenants file not found: {path}\n"
            f"  Update 'chat.tenants_file' in config.yaml."
        )
    with open(path, "r", encoding="utf-8") as fh:
        entries = json.load(fh)
    if not isinstance(entries, list):
        raise ValueError(f"Tenants file must be a JSON list: {path}")
    tenants = {e["Name"]: e for e in entries if isinstance(e, dict) and e.get("Name")}
    logger.info("Loaded %d tenant(s) from %s", len(tenants), path)
    return tenants


# Only load the queries/tenants files when the Chat flow is actually enabled,
# so existing setups without chat_queries.txt/Tenant.json configured keep
# working unchanged.
QUERIES: List[str] = []
TENANTS: Dict[str, Dict[str, Any]] = {}
# Every chat call sends all Tenant.json tenants, so this is built once here
# rather than per-user/per-call.
ALL_TENANT_SCOPE: List[Dict[str, str]] = []
if _CHAT_ENABLED:
    _queries_raw: str = str(_chat_cfg.get("queries_file", "./chat_queries.txt"))
    _queries_path = Path(_queries_raw)
    if not _queries_path.is_absolute():
        _queries_path = (_HERE / _queries_path).resolve()
    QUERIES = _load_queries(_queries_path)

    _tenants_raw: str = str(_chat_cfg.get("tenants_file", "./Tenant.json"))
    _tenants_path = Path(_tenants_raw)
    if not _tenants_path.is_absolute():
        _tenants_path = (_HERE / _tenants_path).resolve()
    TENANTS = _load_tenants(_tenants_path)
    ALL_TENANT_SCOPE = [
        {entry["ID"]: entry["Name"]} for entry in TENANTS.values() if entry.get("ID")
    ]

# ---------------------------------------------------------------------------
# Thread-safe round-robin user assignment
# ---------------------------------------------------------------------------

_index_lock = threading.Lock()
_index_counter = 0


def _claim_user() -> Dict[str, str]:
    """Return the next user credential row, cycling through USERS list."""
    global _index_counter
    with _index_lock:
        idx = _index_counter % len(USERS)
        _index_counter += 1
    return USERS[idx]


# Track total completed journeys across all users (thread-safe).
# Used to ensure the test exits after the configured number of journeys
# when running in `single_journey` mode, preventing Locust from
# continuously replacing stopped users and causing more journeys than
# requested to execute.
_completed_journeys = 0
_completed_journeys_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Locust User class
# ---------------------------------------------------------------------------


class MontyCloudUser(HttpUser):
    """
    Simulates a single MontyCloud user:
      1. Sign in (on_start — once per spawn).
      2. @task: run the full journey — Home Page then WAFR page.

    Request names are prefixed with [Auth], [HomePage], or [WAFR] so that
    Locust stats and the custom HTML report can separate the three sections.
    """

    wait_time = constant(0)
    host = BASE_URL

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        """Assign credentials, sign in, and fetch OrganizationId."""
        self._creds: Dict[str, str] = _claim_user()
        self._token: Optional[str] = None
        self._org_id: str = ""
        self._iterations_done: int = 0
        # Enlarge the connection pool so the largest parallel batch (28 calls
        # in WAFR Batch 2) never discards connections.  pool_connections=1
        # because we talk to a single host; pool_maxsize=50 gives headroom.
        _adapter = HTTPAdapter(pool_connections=1, pool_maxsize=50)
        self.client.mount("https://", _adapter)
        self.client.mount("http://", _adapter)
        self._signin()

    # ------------------------------------------------------------------
    # Sign-in helpers
    # ------------------------------------------------------------------

    def _signin(self) -> None:
        """POST /auth/signin → store JWT token, then GET /auth/user → store org_id."""
        email = self._creds.get("Email", "")
        # Use "New Password" (permanent, post-reset); fall back to "Password"
        password = self._creds.get("New Password") or self._creds.get("Password", "")
        with self.client.post(
            "/auth/signin",
            json={
                "Username": email,
                "Password": password,
                "MC_DEBUG_MODE": MC_DEBUG_MODE,
            },
            name="[Auth] /auth/signin",
            catch_response=True,
            timeout=TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                logger.error(
                    "[%s] signin failed: HTTP %d — %s",
                    email, resp.status_code, resp.text[:300],
                )
                return
            try:
                data = resp.json() or {}
            except Exception:
                resp.failure("Signin response is not valid JSON")
                logger.error("[%s] signin response is not JSON", email)
                return

            token = data.get("Token", "")
            if not token:
                resp.failure("Signin response missing Token")
                logger.error("[%s] signin response missing 'Token' field", email)
                return

            self._token = token
            resp.success()
            logger.info("[%s] signed in successfully", email)

        if not self._token:
            return

        # Fetch OrganizationId
        body: Dict[str, Any] = {}
        with self.client.get(
            "/auth/user",
            headers={"authorization": self._token},
            name="[Auth] /auth/user",
            catch_response=True,
            timeout=TIMEOUT,
        ) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                logger.error("[%s] GET /auth/user failed: HTTP %d", email, resp.status_code)
                return
            try:
                body = resp.json() or {}
                resp.success()
            except Exception:
                resp.failure("GET /auth/user response is not valid JSON")
                return

        self._org_id = body.get("OrganizationId", "")
        if not self._org_id:
            logger.warning("[%s] GET /auth/user returned no OrganizationId", email)
        else:
            logger.info("[%s] OrganizationId = %s", email, self._org_id)

    # ------------------------------------------------------------------
    # Generic GET helper
    # ------------------------------------------------------------------

    def _get(
        self,
        path: str,
        name: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Issue an authenticated GET request.

        Returns the parsed JSON body (or {} on error).
        Records the request in Locust stats using *name* for grouping.
        """
        headers: Dict[str, str] = {}
        if self._token:
            headers["authorization"] = self._token

        body: Dict[str, Any] = {}
        with self.client.get(
            path,
            headers=headers,
            params=params,
            name=name,
            catch_response=True,
            timeout=TIMEOUT,
        ) as resp:
            if resp.status_code >= 400:
                resp.failure(f"HTTP {resp.status_code}")
                logger.warning("FAIL  %-60s  →  HTTP %d", name, resp.status_code)
            else:
                resp.success()
                try:
                    body = resp.json() or {}
                except Exception:
                    body = {}
        return body

    # ------------------------------------------------------------------
    # Parallel batch runner
    # ------------------------------------------------------------------

    def _run_batch(self, callables: List) -> None:
        """Spawn all callables as gevent greenlets and join (wait for all)."""
        group = gevent.pool.Group()
        for fn in callables:
            group.spawn(fn)
        group.join()

    # ------------------------------------------------------------------
    # Main task
    # ------------------------------------------------------------------

    @task
    def full_journey(self) -> None:
        """Complete user journey: Home Page → WAFR Page → (optional) Health Events → (optional) Chat.

        Behaviour is controlled by ``run_mode`` in config.yaml:

          single_journey  Each virtual user runs this task ``iterations`` times
                          (default 1), then raises StopUser so Locust removes it
                          from the pool. The test exits automatically when the
                          last user finishes.
                          Use with: --users N  (no --run-time needed)

          timed           Task loops continuously until --run-time expires;
                          ``iterations`` is ignored.
                          Use with: --users N --run-time <duration>

        The Health Events flow is controlled by ``health`` in config.yaml:

          enabled=False              Home Page + WAFR only (default; unchanged
                                      behaviour).
          enabled=True, mode=appended    Home Page -> WAFR -> think time -> Health.
          enabled=True, mode=standalone  Skips Home Page/WAFR: think time -> Health.

        The Chat flow (WebSocket) is controlled by ``chat`` in config.yaml:

          enabled=False              No chat activity (default; unchanged behaviour).
          enabled=True, mode=appended    ...WAFR/(Health) -> think time -> Chat.
          enabled=True, mode=standalone  Skips Home Page/WAFR/Health: think time -> Chat.

        If both ``health.mode`` and ``chat.mode`` are "standalone", chat takes
        precedence (a startup warning is logged for this combination).
        """
        if not self._token:
            # Re-attempt signin if on_start failed
            self._signin()
            if not self._token:
                logger.error(
                    "[%s] No token available; removing user from pool.",
                    self._creds.get("Email", "unknown"),
                )
                raise StopUser()  # broken user — stop regardless of run_mode

        if _CHAT_ENABLED and _CHAT_MODE == "standalone":
            # Signin -> think time -> Chat only (Home Page/WAFR/Health skipped).
            gevent.sleep(random.uniform(_THINK_MIN, _THINK_MAX))
            self._chat_flow()
        elif _HEALTH_ENABLED and _HEALTH_MODE == "standalone":
            # Signin -> think time -> Health only (Home Page/WAFR skipped).
            gevent.sleep(random.uniform(_THINK_MIN, _THINK_MAX))
            self._health_flow()
        else:
            self._homepage_flow()
            # Variable think time simulating the user pausing before navigating to WAFR
            gevent.sleep(random.uniform(_THINK_MIN, _THINK_MAX))
            self._wafr_flow()
            if _HEALTH_ENABLED:
                # Appended mode: another think time before drilling into Health.
                gevent.sleep(random.uniform(_THINK_MIN, _THINK_MAX))
                self._health_flow()
            if _CHAT_ENABLED:
                # Appended mode: another think time before starting the Chat call.
                gevent.sleep(random.uniform(_THINK_MIN, _THINK_MAX))
                self._chat_flow()

        if _RUN_MODE == "single_journey":
            self._iterations_done += 1
            if self._iterations_done >= _ITERATIONS:
                raise StopUser()  # journeys done; test exits when all users finish
            logger.info(
                "[%s] journey %d/%d complete",
                self._creds.get("Email", "unknown"),
                self._iterations_done, _ITERATIONS,
            )

    # ------------------------------------------------------------------
    # Home Page flow  (Batches 1 → 5)
    # ------------------------------------------------------------------

    def _homepage_flow(self) -> None:
        org = self._org_id

        # ── Batch 1 ── (1 call) ──────────────────────────────────────
        self._run_batch([
            lambda: self._get("/auth/user", "[HomePage] /auth/user"),
        ])

        # ── Batch 2 ── (2 calls, fires after Batch 1) ────────────────
        self._run_batch([
            lambda: self._get("/org/customer-preference",  "[HomePage] /org/customer-preference"),
            lambda: self._get("/auth/user",                "[HomePage] /auth/user"),
        ])

        # ── Batch 3 ── (15 calls, fires after Batch 2) ───────────────
        # Capture `org` in default args to avoid late-binding surprises
        self._run_batch([
            lambda: self._get("/auth/organizations",
                              "[HomePage] /auth/organizations"),
            lambda: self._get("/customeraccount/logo",
                              "[HomePage] /customeraccount/logo"),
            lambda: self._get(
                "/customeraccount/api/v1/faa-roles/configured-accounts",
                "[HomePage] /customeraccount/api/v1/faa-roles/configured-accounts",
                params={"PageNumber": 1, "PageSize": 500},
            ),
            lambda: self._get(
                "/notifications",
                "[HomePage] /notifications",
                params={"pageNumber": 1, "pageSize": 25},
            ),
            lambda: self._get("/auth/organizations",
                              "[HomePage] /auth/organizations"),
            lambda: self._get("/org/organizations/",
                              "[HomePage] /org/organizations/"),
            lambda: self._get("/v1/mc/list-regions",
                              "[HomePage] /v1/mc/list-regions"),
            lambda: self._get(
                "/resource/regions",
                "[HomePage] /resource/regions",
                params={"CloudProvider": "Azure"},
            ),
            lambda: self._get("/auth/user-data",
                              "[HomePage] /auth/user-data"),
            lambda: self._get(
                "/notifications",
                "[HomePage] /notifications",
                params={"pageSize": 25, "pageNumber": 1},
            ),
            lambda: self._get("/auth/portal/users",
                              "[HomePage] /auth/portal/users"),
            lambda: self._get("/subscription/product-catalog",
                              "[HomePage] /subscription/product-catalog"),
            lambda: self._get("/subscription/product-catalog",
                              "[HomePage] /subscription/product-catalog"),
            # Uses OrganizationId from on_start; `org` captured at batch definition time
            lambda _org=org: self._get(
                f"/org/organization/{_org}",
                "[HomePage] /org/organization/{id}",
            ),
            lambda: self._get("/subscription/product-catalog",
                              "[HomePage] /subscription/product-catalog"),
        ])

        # ── Batch 4 ── (2 calls, fires after Batch 3) ────────────────
        self._run_batch([
            lambda _org=org: self._get(
                "/org/resources",
                "[HomePage] /org/resources (AWS CustomerAccount)",
                params={
                    "Filters": json.dumps({
                        "Orgs": [_org], "Detail": True, "ResourceType": "CustomerAccount",
                    }),
                    "CloudProvider": "AWS",
                },
            ),
            lambda _org=org: self._get(
                "/org/resources",
                "[HomePage] /org/resources (Azure CustomerAccount)",
                params={
                    "Filters": json.dumps({
                        "Orgs": [_org], "Detail": True, "ResourceType": "CustomerAccount",
                    }),
                    "CloudProvider": "Azure",
                },
            ),
        ])

        # ── Batch 5 ── (7 calls, fires after Batch 4) ────────────────
        self._run_batch([
            lambda: self._get(
                "/cost/cost-dashboard",
                "[HomePage] /cost/cost-dashboard",
                params={"CloudProvider": "AWS"},
            ),
            lambda: self._get(
                "/cost/cost-dashboard/lookup",
                "[HomePage] /cost/cost-dashboard/lookup",
                params={"CloudProvider": "AWS"},
            ),
            lambda: self._get(
                "/optimizations/api/v1/aws/cost/opportunities/Saving/service-type/top-n",
                "[HomePage] /optimizations/api/v1/aws/cost/opportunities/top-n",
                params={"Tenure": "Monthly", "Count": 5, "PaymentTerm": "ri_sp_1y_all_upf"},
            ),
            lambda: self._get(
                "/optimizations/api/v1/aws/cost/overview-summary",
                "[HomePage] /optimizations/api/v1/aws/cost/overview-summary",
                params={"Tenure": "Monthly", "PaymentTerm": "ri_sp_1y_all_upf"},
            ),
            lambda: self._get(
                "/org/resource-governance-summary",
                "[HomePage] /org/resource-governance-summary",
                params={"CloudProvider": "AWS"},
            ),
            lambda: self._get(
                "/org/inventory-summary",
                "[HomePage] /org/inventory-summary (by-region)",
                params={"CloudProvider": "AWS", "SummaryType": "by-region"},
            ),
            lambda: self._get(
                "/org/inventory-summary",
                "[HomePage] /org/inventory-summary (by-resourcetype)",
                params={"CloudProvider": "AWS", "SummaryType": "by-resourcetype"},
            ),
        ])

    # ------------------------------------------------------------------
    # WAFR Page flow  (Batches 1 → 4)
    # ------------------------------------------------------------------

    def _wafr_flow(self) -> None:
        # ── Batch 1 ── Fetch workloads; extract first WorkloadId ──────
        wl_data = self._get(
            "/war-assessment/workloads",
            "[WAFR] /war-assessment/workloads",
            params={
                "Filters": json.dumps({"Keyword": "", "FTRRequest": False, "Status": "PENDING"}),
                "Limit": 10,
                "PageNumber": 1,
            },
        )
        summaries = wl_data.get("WorkloadSummaries", []) if isinstance(wl_data, dict) else []
        if not summaries:
            logger.warning(
                "[%s] No WAFR workloads returned; skipping WAFR batches 2–4.",
                self._creds.get("Email", "unknown"),
            )
            return
        wid: str = summaries[0].get("WorkloadId", "")
        if not wid:
            logger.warning(
                "[%s] First workload has no WorkloadId; skipping WAFR batches 2–4.",
                self._creds.get("Email", "unknown"),
            )
            return

        logger.debug("[%s] WAFR WorkloadId = %s", self._creds.get("Email"), wid)

        # ── Helpers ──────────────────────────────────────────────────
        PILLARS = [
            "costOptimization",
            "security",
            "performance",
            "reliability",
            "operationalExcellence",
            "sustainability",
        ]

        def _pillar_fn(pillar: str):
            """Return a callable that GETs the findings-summary-by-severity for a pillar."""
            def _call(_p=pillar, _wid=wid):
                return self._get(
                    f"/war-assessment/workload/{_wid}/findings-summary-by-severity",
                    f"[WAFR] /war-assessment/workload/{{id}}/findings-summary-by-severity (pillar={_p})",
                    params={"Filters": json.dumps({
                        "Status": "failed", "Dimension": "Pillar", "PillarId": _p,
                    })},
                )
            return _call
        gevent.sleep(random.uniform(_WAFR_THINK_MIN, _WAFR_THINK_MAX))
        # ── Batch 2 ── (28 calls, all parallel) ──────────────────────
        # Faithfully replicates the browser's request pattern including
        # duplicate calls that occur when multiple components mount.
        self._run_batch([
            # --- First occurrence (items 1–18) ---
            lambda _wid=wid: self._get(
                "/war-assessment/workload_count_summary",
                "[WAFR] /war-assessment/workload_count_summary",
                params={"Filters": json.dumps({"Keyword": "", "FTRRequest": False})},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/finding-summary",
                "[WAFR] /war-assessment/workload/{id}/finding-summary (Status=Failed, Card=Workload)",
                params={"Filters": json.dumps({"Status": "Failed", "Card": "Workload"})},
            ),
            *[_pillar_fn(p) for p in PILLARS],
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/findings-summary-by-severity",
                "[WAFR] /war-assessment/workload/{id}/findings-summary-by-severity (ResourceType)",
                params={"Filters": json.dumps({"Status": "Failed", "Dimension": "ResourceType"})},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/top-resources",
                "[WAFR] /war-assessment/workload/{id}/top-resources",
                params={"Filters": json.dumps({"Status": "Failed"})},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/top-checks",
                "[WAFR] /war-assessment/workload/{id}/top-checks",
                params={"Filters": json.dumps({"Status": "Failed"})},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/resource-types",
                "[WAFR] /war-assessment/workload/{id}/resource-types",
                params={"Limit": 20, "Offset": 0},
            ),
            lambda: self._get(
                "/war-assessment/workload/lenses",
                "[WAFR] /war-assessment/workload/lenses",
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/finding-summary",
                "[WAFR] /war-assessment/workload/{id}/finding-summary (Card=Workload)",
                params={"Filters": json.dumps({"Card": "Workload"})},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/api/workload/{_wid}/schedules",
                "[WAFR] /war-assessment/api/workload/{id}/schedules",
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}",
                "[WAFR] /war-assessment/workload/{id}",
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/lenses",
                "[WAFR] /war-assessment/workload/{id}/lenses",
                params={"Lens": "wellarchitected"},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/finding-summary",
                "[WAFR] /war-assessment/workload/{id}/finding-summary (Card=Lens, wellarchitected)",
                params={"Filters": json.dumps({"Card": "Lens", "Lens": "wellarchitected"})},
            ),
            # --- Second occurrence (items 19–28, browser duplicate loads) ---
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/finding-summary",
                "[WAFR] /war-assessment/workload/{id}/finding-summary (Status=Failed, Card=Workload)",
                params={"Filters": json.dumps({"Status": "Failed", "Card": "Workload"})},
            ),
            *[_pillar_fn(p) for p in PILLARS],
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/findings-summary-by-severity",
                "[WAFR] /war-assessment/workload/{id}/findings-summary-by-severity (ResourceType)",
                params={"Filters": json.dumps({"Status": "Failed", "Dimension": "ResourceType"})},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/top-resources",
                "[WAFR] /war-assessment/workload/{id}/top-resources",
                params={"Filters": json.dumps({"Status": "Failed"})},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/top-checks",
                "[WAFR] /war-assessment/workload/{id}/top-checks",
                params={"Filters": json.dumps({"Status": "Failed"})},
            ),
        ])

        gevent.sleep(random.uniform(_WAFR_THINK_MIN, _WAFR_THINK_MAX))
        # ── Batch 3 ── (3 calls, fires after Batch 2) ────────────────
        _task_filters = json.dumps({
            "TaskType": "All",
            "Category": [
                "Compliance", "Security", "Remediation", "CustomActions", "EC2",
                "Storage", "PATCH-SCAN", "PATCH-INSTALL", "DESIRED-SERVER-STATE",
                "Volume", "BackupRestore",
            ],
        })
        self._run_batch([
            lambda _f=_task_filters: self._get(
                "/task/",
                "[WAFR] /task/ (all categories)",
                params={"Filters": _f},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/findings",
                "[WAFR] /war-assessment/workload/{id}/findings",
                params={"Limit": 25, "Offset": 0},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/finding-count",
                "[WAFR] /war-assessment/workload/{id}/finding-count (GroupBy=Title)",
                params={
                    "Limit": 25, "Offset": 0,
                    "Filters": json.dumps({"GroupBy": "Title"}),
                },
            ),
        ])

        # ── Batch 4 ── (5 calls, fires after Batch 3) ────────────────
        self._run_batch([
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/findings",
                "[WAFR] /war-assessment/workload/{id}/findings",
                params={"Limit": 25, "Offset": 0},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/finding-count",
                "[WAFR] /war-assessment/workload/{id}/finding-count (GroupBy=Severity)",
                params={
                    "Limit": 25, "Offset": 0,
                    "Filters": json.dumps({"GroupBy": "Severity"}),
                },
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/findings",
                "[WAFR] /war-assessment/workload/{id}/findings",
                params={"Limit": 25, "Offset": 0},
            ),
            lambda _wid=wid: self._get(
                f"/war-assessment/workload/{_wid}/milestones",
                "[WAFR] /war-assessment/workload/{id}/milestones",
                params={"PageNumber": 1, "Limit": 10},
            ),
            lambda: self._get(
                "/notifications",
                "[WAFR] /notifications",
                params={"pageSize": 25, "pageNumber": 1},
            ),
        ])

    # ------------------------------------------------------------------
    # Health Events flow
    # ------------------------------------------------------------------
    #
    # Sequencing below mirrors the real browser waterfall captured in a HAR
    # trace of the Health Events page: mostly sequential calls with exactly
    # two parallel pairs (summary + first breakdown-summary; and the two
    # Id-dependent detail calls at the end).

    def _health_flow(self) -> None:
        # ── Call 1 ── policies (standalone) ──────────────────────────
        self._get("/health/api/v1/policies", "[Health] /health/api/v1/policies")

        # ── Call 2 ── (2 calls, parallel) ────────────────────────────
        self._run_batch([
            lambda: self._get(
                "/health/api/v1/events/summary",
                "[Health] /health/api/v1/events/summary",
            ),
            lambda: self._get(
                "/health/api/v1/events/breakdown-summary",
                "[Health] /health/api/v1/events/breakdown-summary (open)",
                params={"Filters": json.dumps({
                    "Dimension": "EventTypeCode", "StatusCode": "open",
                }), "Limit": 25},
            ),
        ])

        # ── Call 3 ── breakdown-summary (closed) — sequential ────────
        self._get(
            "/health/api/v1/events/breakdown-summary",
            "[Health] /health/api/v1/events/breakdown-summary (closed)",
            params={"Filters": json.dumps({
                "Dimension": "EventTypeCode", "StatusCode": "closed",
            }), "Limit": 25},
        )

        # ── Call 4 ── breakdown-summary (upcoming) — sequential ──────
        self._get(
            "/health/api/v1/events/breakdown-summary",
            "[Health] /health/api/v1/events/breakdown-summary (upcoming)",
            params={"Filters": json.dumps({
                "Dimension": "EventTypeCode", "StatusCode": "upcoming",
            }), "Limit": 25},
        )

        # ── Call 5 ── events/events (EventTypeCode + StatusCode=open) ─
        self._get(
            "/health/api/v1/events/events",
            "[Health] /health/api/v1/events/events (EventTypeCode=AWS_ABUSE_PHISHING_CONTENT_REPORTED, open)",
            params={"Filters": json.dumps({
                "EventTypeCode": "AWS_ABUSE_PHISHING_CONTENT_REPORTED",
                "StatusCode": "open",
            }), "Limit": 25},
        )

        # ── Call 6 ── events/events (StatusCode=open only) ───────────
        # This response is the source of the Id used for the detail calls below.
        events_data = self._get(
            "/health/api/v1/events/events",
            "[Health] /health/api/v1/events/events (StatusCode=open)",
            params={"Filters": json.dumps({"StatusCode": "open"}), "Limit": 25},
        )
        health_events = events_data.get("HealthEvents", []) if isinstance(events_data, dict) else []
        if not health_events:
            logger.warning(
                "[%s] No HealthEvents returned; skipping event-timelines/affected-resources.",
                self._creds.get("Email", "unknown"),
            )
            return
        event_id: str = random.choice(health_events).get("Id", "")
        if not event_id:
            logger.warning(
                "[%s] Selected HealthEvent has no Id; skipping event-timelines/affected-resources.",
                self._creds.get("Email", "unknown"),
            )
            return

        logger.debug("[%s] Health event Id = %s", self._creds.get("Email"), event_id)

        # ── Call 7 ── (2 calls, parallel) ────────────────────────────
        self._run_batch([
            lambda _eid=event_id: self._get(
                f"/health/api/v1/events/{_eid}/event-timelines",
                "[Health] /health/api/v1/events/{id}/event-timelines",
                params={"Offset": 0, "Limit": 10},
            ),
            lambda _eid=event_id: self._get(
                f"/health/api/v1/events/{_eid}/affected-resources",
                "[Health] /health/api/v1/events/{id}/affected-resources",
                params={"Offset": 0, "Limit": 10},
            ),
        ])

    # ------------------------------------------------------------------
    # Chat flow (WebSocket)
    # ------------------------------------------------------------------
    #
    # Opens a single WebSocket connection, sends ONE query, and streams
    # frames until PROMPT_STATUS/ENDED (or the overall timeout elapses).
    # Multi-turn conversations on one connection are deferred — see
    # WEBSOCKET_CHAT_APPROACH.md.
    #
    # OrganizationId (WS URL query param) stays as self._org_id, the signed-in
    # user's own org fetched via /auth/user during sign-in — same as Home
    # Page/WAFR/Health. The `tenant_scope` in the message body is different:
    # it always lists every tenant from Tenant.json (config: chat.tenants_file),
    # regardless of which user is running — the chat bot's tenant scoping uses
    # tenant-specific org ids, not the user's own root org.

    def _fire_chat_metric(
        self,
        name: str,
        start_time: float,
        exception: Optional[BaseException] = None,
    ) -> None:
        """Record a pseudo-request into Locust's stats (custom-client pattern)."""
        elapsed_ms = (time.monotonic() - start_time) * 1000
        self.environment.events.request.fire(
            request_type="WS",
            name=name,
            response_time=elapsed_ms,
            response_length=0,
            exception=exception,
            context={},
        )

    def _log_chat_message(
        self,
        direction: str,
        category: str,
        elapsed_s: float,
        detail: str,
        max_len: int = 500,
    ) -> None:
        """Append one line to chat.transcript_log (no-op if not configured)."""
        if _chat_transcript_logger is None:
            return
        email = self._creds.get("Email", "unknown")
        if len(detail) > max_len:
            detail = f"{detail[:max_len]}...(+{len(detail) - max_len} more chars)"
        _chat_transcript_logger.info(
            "%-28s | %-4s | %-11s | +%8.3fs | %s",
            email, direction, category, elapsed_s, detail,
        )

    def _chat_flow(self) -> None:
        email = self._creds.get("Email", "unknown")
        if not WS_BASE_URL:
            logger.error("[%s] chat.ws_base_url is not configured; skipping Chat flow.", email)
            return
        if not QUERIES:
            logger.error("[%s] No chat queries loaded; skipping Chat flow.", email)
            return
        if not ALL_TENANT_SCOPE:
            logger.error("[%s] No tenants loaded from Tenant.json; skipping Chat flow.", email)
            return

        url = (
            f"{WS_BASE_URL}?Authorization={self._token}"
            f"&OrganizationId={self._org_id}&agentic=true"
        )

        connect_start = time.monotonic()
        try:
            ws = websocket.create_connection(url, timeout=_CHAT_TIMEOUT)
        except Exception as exc:
            logger.error("[%s] Chat WebSocket connect failed: %s", email, exc)
            self._log_chat_message(
                "SYS", "CONNECT_FAIL", time.monotonic() - connect_start,
                f"url={_redact_ws_url(url)} error={exc}",
            )
            self._fire_chat_metric("[Chat] full_response", connect_start, exc)
            return

        self._log_chat_message(
            "SYS", "CONNECTED", time.monotonic() - connect_start,
            f"url={_redact_ws_url(url)}",
        )

        query = random.choice(QUERIES)
        payload = {
            "query": query,
            "thread_id": "",
            "metadata": {
                "model_id": _CHAT_MODEL_ID,
                "temperature": _CHAT_TEMPERATURE,
                "top_p": _CHAT_TOP_P,
                "top_k": _CHAT_TOP_K,
            },
            "tenant_scope": ALL_TENANT_SCOPE,
        }

        t_send = time.monotonic()
        ttft_recorded = False
        ended = False
        chat_exc: Optional[BaseException] = None

        try:
            ws.send(json.dumps(payload))
            self._log_chat_message("SEND", "QUERY", 0.0, query)
            deadline = t_send + _CHAT_TIMEOUT
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    chat_exc = TimeoutError(
                        f"No PROMPT_STATUS/ENDED frame within {_CHAT_TIMEOUT}s"
                    )
                    logger.error("[%s] Chat timed out waiting for ENDED frame.", email)
                    self._log_chat_message(
                        "RECV", "TIMEOUT", time.monotonic() - t_send,
                        f"No ENDED frame within {_CHAT_TIMEOUT}s",
                    )
                    break

                ws.settimeout(remaining)
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    chat_exc = TimeoutError(
                        f"No PROMPT_STATUS/ENDED frame within {_CHAT_TIMEOUT}s"
                    )
                    logger.error("[%s] Chat timed out waiting for ENDED frame.", email)
                    self._log_chat_message(
                        "RECV", "TIMEOUT", time.monotonic() - t_send,
                        f"No ENDED frame within {_CHAT_TIMEOUT}s",
                    )
                    break

                if not raw:
                    continue

                frame_elapsed = time.monotonic() - t_send

                try:
                    frame = json.loads(raw)
                except (ValueError, TypeError):
                    logger.warning("[%s] Chat: non-JSON frame received; ignoring.", email)
                    self._log_chat_message("RECV", "NON_JSON", frame_elapsed, str(raw))
                    continue

                # Informational AWS API Gateway notice — keep listening, don't fail.
                if frame.get("message") == "Endpoint request timed out":
                    logger.warning(
                        "[%s] Chat received 'Endpoint request timed out' notice; "
                        "continuing to listen for further frames.",
                        email,
                    )
                    self._log_chat_message("RECV", "GW_TIMEOUT", frame_elapsed, raw)
                    continue

                if frame.get("type") == "THREAD_TITLE":
                    logger.info("[%s] Chat thread started: %s", email, frame.get("thread_title"))
                    self._log_chat_message("RECV", "THREAD_TITLE", frame_elapsed, raw)
                    continue

                # Top-level PROMPT_STATUS/ERROR (e.g. inaccessible tenant_scope
                # org id) — no "body" wrapper, "message" is an object rather
                # than a string. This is terminal: fail fast instead of
                # waiting out the full timeout for a frame that will never
                # arrive.
                top_message = frame.get("message")
                if frame.get("type") == "PROMPT_STATUS" and isinstance(top_message, dict) \
                        and top_message.get("message") == "ERROR":
                    chat_exc = RuntimeError(
                        f"Chat PROMPT_STATUS ERROR: {top_message.get('error', top_message)}"
                    )
                    logger.error("[%s] Chat error frame: %s", email, top_message)
                    self._log_chat_message("RECV", "PROMPT_ERROR", frame_elapsed, raw)
                    break

                # Top-level PROMPT_STATUS/REJECTED (e.g. SESSION_TIME_LIMIT_REACHED)
                # — "message" is the string "REJECTED" rather than an ERROR dict.
                # Also terminal: fail fast with the server's own reason instead of
                # waiting out the full timeout.
                if frame.get("type") == "PROMPT_STATUS" and top_message == "REJECTED":
                    chat_exc = RuntimeError(
                        f"Chat PROMPT_STATUS REJECTED "
                        f"(code={frame.get('code')}): {frame.get('display_message') or frame.get('code')}"
                    )
                    logger.error("[%s] Chat rejected: %s", email, chat_exc)
                    self._log_chat_message("RECV", "PROMPT_REJECTED", frame_elapsed, raw)
                    break

                body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
                body_type = body.get("type")
                body_message = body.get("message")

                if body_type == "PROMPT_STATUS" and body_message == "STARTED":
                    logger.debug("[%s] Chat prompt STARTED.", email)
                    self._log_chat_message("RECV", "STARTED", frame_elapsed, raw)
                    continue

                if body_type == "REASONING" and not ttft_recorded:
                    ttft_recorded = True
                    self._log_chat_message("RECV", "REASONING", frame_elapsed, raw)
                    self._fire_chat_metric("[Chat] time_to_first_token", t_send)
                    continue

                if body_type == "REASONING":
                    self._log_chat_message("RECV", "REASONING", frame_elapsed, raw)
                    continue

                if body_type == "TOOL_RESULT":
                    logger.debug("[%s] Chat TOOL_RESULT frame received.", email)
                    self._log_chat_message("RECV", "TOOL_RESULT", frame_elapsed, raw)
                    continue

                if body_type == "PROMPT_STATUS" and body_message == "ENDED":
                    self._log_chat_message("RECV", "ENDED", frame_elapsed, raw)
                    ended = True
                    break

                # Anything else: still log it so the transcript is complete.
                self._log_chat_message("RECV", body_type or "UNKNOWN", frame_elapsed, raw)
        except Exception as exc:
            chat_exc = exc
            logger.error("[%s] Chat WebSocket error: %s", email, exc)
            self._log_chat_message(
                "SYS", "ERROR", time.monotonic() - t_send, str(exc),
            )
        finally:
            try:
                ws.close()
            except Exception:
                pass

        if ended:
            self._fire_chat_metric("[Chat] full_response", t_send)
            self._log_chat_message(
                "SYS", "DONE", time.monotonic() - t_send, "status=SUCCESS",
            )
        else:
            self._fire_chat_metric(
                "[Chat] full_response", t_send,
                chat_exc or Exception("Chat call did not complete"),
            )
            self._log_chat_message(
                "SYS", "DONE", time.monotonic() - t_send,
                f"status=FAILED error={chat_exc}",
            )


# ---------------------------------------------------------------------------
# Event hook: single_journey auto-exit
# ---------------------------------------------------------------------------
#
# Locust does NOT stop on its own when every user has raised StopUser — a
# headless run without --run-time would sit at 0 users until Ctrl+C.  This
# watchdog polls the runner and shuts the test down once all users are done,
# making the "test exits automatically" behaviour of single_journey real.

_FINISHED_STATES = (STATE_STOPPING, STATE_STOPPED, STATE_CLEANUP)


@events.test_start.add_listener
def on_test_start(environment, **kwargs) -> None:
    if _RUN_MODE != "single_journey":
        return
    runner = environment.runner
    if runner is None or isinstance(runner, WorkerRunner):
        return  # workers follow the master; nothing to watch locally

    

    def _quit_when_all_users_done() -> None:
        # Phase 1: wait for the first user to spawn (ramp-up has begun).
        while runner.user_count == 0:
            if runner.state in _FINISHED_STATES:
                return
            gevent.sleep(1)
        # Phase 2: wait for the last user to finish its journey.
        while runner.user_count > 0:
            if runner.state in _FINISHED_STATES:
                return
            gevent.sleep(1)
        logger.info("single_journey: all users have finished — stopping the test.")
        opts = getattr(environment, "parsed_options", None)
        if opts and getattr(opts, "headless", False):
            runner.quit()   # headless: exit the process
        else:
            runner.stop()   # web UI: stop the run, keep the UI alive

    gevent.spawn(_quit_when_all_users_done)


# ---------------------------------------------------------------------------
# Event hook: auto-generate custom HTML report when --csv is used
# ---------------------------------------------------------------------------


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs) -> None:
    """After the test run, generate a custom two-section HTML report."""
    csv_prefix: Optional[str] = None
    try:
        opts = environment.parsed_options
        if opts:
            csv_prefix = getattr(opts, "csv_prefix", None)
    except Exception:
        pass

    if not csv_prefix:
        logger.info(
            "No --csv prefix configured; skipping custom HTML report. "
            "Pass --csv reports/stats to enable it."
        )
        return

    # Locust's CSV writer is a background greenlet that only rewrites the file
    # every CSV_STATS_INTERVAL_SEC and flushes it to disk every
    # CSV_STATS_FLUSH_INTERVAL_SEC — wait a full cycle so the very last stat
    # (e.g. a [Chat] full_response fired right before the test stopped) is
    # guaranteed to be on disk before we read it below.
    gevent.sleep(CSV_STATS_INTERVAL_SEC + CSV_STATS_FLUSH_INTERVAL_SEC + 1)

    stats_csv = Path(csv_prefix + "_stats.csv")
    if not stats_csv.exists():
        logger.warning(
            "Expected Locust stats CSV at %s — file not found; "
            "skipping custom HTML report.",
            stats_csv,
        )
        return

    # Ensure report_generator module in sys.path (same directory as this file)
    if str(_HERE) not in sys.path:
        sys.path.insert(0, str(_HERE))

    try:
        import report_generator as rg  # noqa: PLC0415
        out = rg.generate(
            stats_csv,
            description=_test_cfg.get("description", ""),
            config_data=CFG,
            report_name=_test_cfg.get("report_name", ""),
        )
        logger.info("Custom HTML report  →  %s", out)
    except Exception as exc:
        logger.error("Failed to generate custom HTML report: %s", exc, exc_info=True)
