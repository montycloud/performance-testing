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
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import gevent
import gevent.pool
import yaml
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
from locust import HttpUser, constant, events, task
from locust.exception import StopUser
from locust.runners import STATE_CLEANUP, STATE_STOPPED, STATE_STOPPING, WorkerRunner

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
        """Complete user journey: Home Page → WAFR Page → (optional) Health Events.

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

        if _HEALTH_ENABLED and _HEALTH_MODE == "standalone":
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
