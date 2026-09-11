#!/usr/bin/env python3
"""
MontyCloud Cost Dashboard — Locust Performance Test
=====================================================
Standalone Locust scenario simulating the Cost Dashboard page:

  Sign-in -> think time -> Cost Dashboard flow (4 sequential batches; calls
  within a batch fire in parallel via gevent greenlets, same pattern as
  locustfile.py's Home Page/WAFR flows — see common.py's run_batch()).

Batches (see Cost_dashboard_API/Cost_dashboard_api_plan.md for the full
HAR-derived design notes):

  Batch 1  — Dashboard bootstrap: cost-dashboard + lookup (parallel, 2 calls).
  Batch 2  — "Total Spend" widgets: cost-summary (by-account/by-service),
             region-summary, resource-summary, spend-trend, cost-by-charge-type
             (parallel, 6 calls) — fired once per configured date-range preset,
             plus an optional initial no-filter pass.
  Batch 3  — tag-keys lookup (single call).
  Batch 4  — "Spend Trend" tab widgets: spend-trend, spend-breakdown-summary,
             resource-count (parallel, 3 calls) — fired once per date-range preset.

This file intentionally does NOT touch locustfile.py/config.yaml — it is a
fully separate test with its own config (cost_dashboard_config.yaml) and its
own users_csv, sharing only common.py's helpers.

Usage (headless — recommended):
    locust -f cost_dashboard_locustfile.py --headless \\
      --users 10 --spawn-rate 2 --run-time 5m \\
      --html reports/cost_dashboard_report.html \\
      --csv reports/cost_dashboard_stats
"""

import json
import logging
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import gevent
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
from locust import HttpUser, constant, events, task
from locust.exception import StopUser
from locust.runners import STATE_CLEANUP, STATE_STOPPED, STATE_STOPPING, WorkerRunner
from locust.stats import CSV_STATS_FLUSH_INTERVAL_SEC, CSV_STATS_INTERVAL_SEC

logger = logging.getLogger("perf_test.cost_dashboard")

# ---------------------------------------------------------------------------
# Bootstrap: config + users (shared helpers live in common.py)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_CONFIG_FILE = _HERE / "cost_dashboard_config.yaml"
_ENV_FILE = _HERE / ".env"

load_dotenv(dotenv_path=_ENV_FILE, override=False)

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import common  # noqa: E402

CFG: Dict[str, Any] = common.load_config(_CONFIG_FILE)

BASE_URL: str = CFG["api"]["base_url"].rstrip("/")
MC_DEBUG_MODE: bool = bool(CFG["api"].get("mc_debug_mode", True))
TIMEOUT: int = int(CFG["api"].get("timeout_seconds", 40))

_test_cfg = CFG["test"]
_users_csv_raw: str = _test_cfg["users_csv"]
_users_csv_path = Path(_users_csv_raw)
if not _users_csv_path.is_absolute():
    _users_csv_path = (_HERE / _users_csv_path).resolve()
USER_COUNT: int = int(_test_cfg.get("user_count", 10))

_THINK_MIN: float = float(_test_cfg.get("think_time_min", 2))
_THINK_MAX: float = float(_test_cfg.get("think_time_max", 5))

# Think time between date-range presets within the Cost Dashboard flow.
_CD_THINK_MIN: float = float(_test_cfg.get("cost_dashboard_think_time_min", _THINK_MIN))
_CD_THINK_MAX: float = float(_test_cfg.get("cost_dashboard_think_time_max", _THINK_MAX))

_RUN_MODE: str = _test_cfg.get("run_mode", "single_journey").strip().lower()
_ITERATIONS: int = max(1, int(_test_cfg.get("iterations", 1)))

USERS: List[Dict[str, str]] = common.load_users(_users_csv_path, USER_COUNT, logger)
_user_claimer = common.UserClaimer(USERS)

# Cost Dashboard flow config (config section: `cost_dashboard`).
_cd_cfg: Dict[str, Any] = CFG.get("cost_dashboard", {}) or {}
_COST_DASHBOARD_ENABLED: bool = bool(_cd_cfg.get("enabled", True))
_COST_DASHBOARD_MODE: str = str(_cd_cfg.get("mode", "standalone")).strip().lower()
_CHARGE_TYPES_EXCLUDE: bool = bool(_cd_cfg.get("charge_types_exclude", True))
_INCLUDE_NO_FILTER_PASS: bool = bool(_cd_cfg.get("include_no_filter_pass", True))
_DEFAULT_DAYS_BACK: int = int(_cd_cfg.get("default_days_back", 40))
_TAG_KEYS_LIMIT: int = int(_cd_cfg.get("tag_keys_limit", 20))
_BREAKDOWN_LIMIT: int = int(_cd_cfg.get("breakdown_limit", 25))
_DATE_RANGE_PRESETS: List[Dict[str, Any]] = list(_cd_cfg.get("date_range_presets", []) or [])
if not _DATE_RANGE_PRESETS:
    raise ValueError(
        "cost_dashboard.date_range_presets is empty in cost_dashboard_config.yaml "
        "— at least one preset ({label, days_back}) is required."
    )


def _date_range(days_back: int) -> Dict[str, str]:
    """Return {StartDate, EndDate} as YYYY-MM-DD strings, EndDate = today."""
    end = datetime.now().date()
    start = end - timedelta(days=days_back)
    return {"StartDate": start.isoformat(), "EndDate": end.isoformat()}


def _charge_types_filter() -> Dict[str, Any]:
    return {"exclude": ["Credit", "Refund"]}


# ---------------------------------------------------------------------------
# Locust User class
# ---------------------------------------------------------------------------


class CostDashboardUser(HttpUser):
    """
    Simulates a single MontyCloud user viewing the Cost Dashboard:
      1. Sign in (on_start — once per spawn).
      2. @task: run the Cost Dashboard flow (4 sequential batches).

    Request names are prefixed [Auth] / [Cost] so report_generator.py can
    bucket them into sections, same convention as locustfile.py.
    """

    wait_time = constant(0)
    host = BASE_URL

    def on_start(self) -> None:
        self._creds: Dict[str, str] = _user_claimer.claim()
        self._token: Optional[str] = None
        self._org_id: str = ""
        self._iterations_done: int = 0
        _adapter = HTTPAdapter(pool_connections=1, pool_maxsize=20)
        self.client.mount("https://", _adapter)
        self.client.mount("http://", _adapter)
        self._token, self._org_id = common.signin(
            self.client, self._creds, MC_DEBUG_MODE, TIMEOUT, logger,
        )

    def _get(self, path: str, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return common.authenticated_get(
            self.client, self._token, path, name, TIMEOUT, logger, params=params,
        )

    def _run_batch(self, callables: List) -> None:
        common.run_batch(callables)

    # ------------------------------------------------------------------
    # Main task
    # ------------------------------------------------------------------

    @task
    def cost_dashboard_journey(self) -> None:
        if not self._token:
            self._token, self._org_id = common.signin(
                self.client, self._creds, MC_DEBUG_MODE, TIMEOUT, logger,
            )
            if not self._token:
                logger.error(
                    "[%s] No token available; removing user from pool.",
                    self._creds.get("Email", "unknown"),
                )
                raise StopUser()

        gevent.sleep(random.uniform(_THINK_MIN, _THINK_MAX))
        self._cost_dashboard_flow()

        if _RUN_MODE == "single_journey":
            self._iterations_done += 1
            if self._iterations_done >= _ITERATIONS:
                raise StopUser()
            logger.info(
                "[%s] journey %d/%d complete",
                self._creds.get("Email", "unknown"),
                self._iterations_done, _ITERATIONS,
            )

    # ------------------------------------------------------------------
    # Cost Dashboard flow  (Batches 1 -> 4)
    # ------------------------------------------------------------------

    def _cost_dashboard_flow(self) -> None:
        # ── Batch 1 ── Dashboard bootstrap (2 calls, parallel) ───────────
        self._run_batch([
            lambda: self._get("/cost/cost-dashboard",
                              "[Cost] cost-dashboard",
                              params={"CloudProvider": "AWS"}),
            lambda: self._get("/cost/cost-dashboard/lookup",
                              "[Cost] lookup",
                              params={"CloudProvider": "AWS"}),
        ])

        # ── Batch 2 ── "Total Spend" widgets, once per date-range preset ──
        passes: List[Dict[str, Any]] = []
        if _INCLUDE_NO_FILTER_PASS:
            passes.append({"label": "default", "days_back": _DEFAULT_DAYS_BACK, "exclude": False})
        for preset in _DATE_RANGE_PRESETS:
            passes.append({
                "label": preset.get("label", str(preset.get("days_back"))),
                "days_back": int(preset["days_back"]),
                "exclude": _CHARGE_TYPES_EXCLUDE,
            })

        for p in passes:
            date_range = _date_range(p["days_back"])
            base_filters: Dict[str, Any] = dict(date_range)
            if p["exclude"]:
                base_filters["ChargeTypes"] = _charge_types_filter()

            def _cost_summary(summary_type: str, _base=base_filters):
                filters = {"SummaryType": summary_type, **_base}
                return self._get(
                    "/cost/cost-dashboard/cost-summary",
                    f"[Cost] cost-summary ({summary_type})",
                    params={"Filters": json.dumps(filters), "CloudProvider": "AWS"},
                )

            def _spend_trend(_base=base_filters):
                filters = {"SummaryType": "by-account", **_base}
                return self._get(
                    "/cost/cost-dashboard/spend-trend",
                    "[Cost] spend-trend",
                    params={"Filters": json.dumps(filters), "CloudProvider": "AWS"},
                )

            self._run_batch([
                lambda _st="by-account": _cost_summary(_st),
                lambda _st="by-service": _cost_summary(_st),
                lambda _base=base_filters: self._get(
                    "/cost/cost-dashboard/region-summary",
                    "[Cost] region-summary",
                    params={"Filters": json.dumps(_base), "CloudProvider": "AWS"},
                ),
                lambda _base=base_filters: self._get(
                    "/cost/cost-dashboard/resource-summary",
                    "[Cost] resource-summary",
                    params={"Filters": json.dumps(_base), "CloudProvider": "AWS"},
                ),
                lambda: _spend_trend(),
                lambda _base=base_filters: self._get(
                    "/cost/cost-by-charge-type",
                    "[Cost] cost-by-charge-type",
                    params={"Filters": json.dumps(_base), "CloudProvider": "AWS"},
                ),
            ])
            gevent.sleep(random.uniform(_CD_THINK_MIN, _CD_THINK_MAX))

        # ── Batch 3 ── tag-keys lookup (single call) ──────────────────────
        self._run_batch([
            lambda: self._get(
                "/cost/cost-dashboard/lookup/tag-keys",
                "[Cost] lookup-tag-keys",
                params={"Limit": _TAG_KEYS_LIMIT, "Offset": 0, "CloudProvider": "AWS"},
            ),
        ])

        # ── Batch 4 ── "Spend Trend" tab widgets, once per date-range preset ──
        for preset in _DATE_RANGE_PRESETS:
            date_range = _date_range(int(preset["days_back"]))
            base_filters = dict(date_range)
            if _CHARGE_TYPES_EXCLUDE:
                base_filters["ChargeTypes"] = _charge_types_filter()
            trend_filters = {"SummaryType": "by-account", **base_filters}
            breakdown_filters = {**base_filters, "SummaryType": "by-account"}

            self._run_batch([
                lambda _f=trend_filters: self._get(
                    "/cost/cost-dashboard/spend-trend",
                    "[Cost] spend-trend-tab",
                    params={"Filters": json.dumps(_f), "CloudProvider": "AWS"},
                ),
                lambda _f=breakdown_filters: self._get(
                    "/cost/cost-dashboard/spend-breakdown-summary",
                    "[Cost] spend-breakdown-summary",
                    params={
                        "Filters": json.dumps(_f),
                        "Limit": _BREAKDOWN_LIMIT, "Offset": 0, "CloudProvider": "AWS",
                    },
                ),
                lambda _f=breakdown_filters: self._get(
                    "/cost/cost-dashboard/resource-count",
                    "[Cost] resource-count",
                    params={
                        "Filters": json.dumps(_f),
                        "Limit": _BREAKDOWN_LIMIT, "Offset": 0, "CloudProvider": "AWS",
                    },
                ),
            ])
            gevent.sleep(random.uniform(_CD_THINK_MIN, _CD_THINK_MAX))


if not _COST_DASHBOARD_ENABLED:
    raise SystemExit(
        "cost_dashboard.enabled is false in cost_dashboard_config.yaml — "
        "nothing to run. Set it to true to execute this test."
    )
if _COST_DASHBOARD_MODE != "standalone":
    logger.warning(
        "cost_dashboard.mode=%r is not yet supported by this file (only "
        "'standalone' is implemented) — running standalone anyway.",
        _COST_DASHBOARD_MODE,
    )

# ---------------------------------------------------------------------------
# Event hook: single_journey auto-exit (same watchdog as locustfile.py)
# ---------------------------------------------------------------------------

_FINISHED_STATES = (STATE_STOPPING, STATE_STOPPED, STATE_CLEANUP)


@events.test_start.add_listener
def on_test_start(environment, **kwargs) -> None:
    if _RUN_MODE != "single_journey":
        return
    runner = environment.runner
    if runner is None or isinstance(runner, WorkerRunner):
        return

    def _quit_when_all_users_done() -> None:
        while runner.user_count == 0:
            if runner.state in _FINISHED_STATES:
                return
            gevent.sleep(1)
        while runner.user_count > 0:
            if runner.state in _FINISHED_STATES:
                return
            gevent.sleep(1)
        logger.info("single_journey: all users have finished — stopping the test.")
        opts = getattr(environment, "parsed_options", None)
        if opts and getattr(opts, "headless", False):
            runner.quit()
        else:
            runner.stop()

    gevent.spawn(_quit_when_all_users_done)


# ---------------------------------------------------------------------------
# Event hook: auto-generate custom HTML report when --csv is used
# ---------------------------------------------------------------------------


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs) -> None:
    """After the test run, generate the shared custom HTML report."""
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
            "Pass --csv reports/cost_dashboard_stats to enable it."
        )
        return

    gevent.sleep(CSV_STATS_INTERVAL_SEC + CSV_STATS_FLUSH_INTERVAL_SEC + 1)

    stats_csv = Path(csv_prefix + "_stats.csv")
    if not stats_csv.exists():
        logger.warning(
            "Expected Locust stats CSV at %s — file not found; "
            "skipping custom HTML report.",
            stats_csv,
        )
        return

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
