#!/usr/bin/env python3
"""
MontyCloud Inventory — Locust Performance Test
=========================================================
Standalone Locust scenario simulating the Inventory page, derived from HAR
analysis of real UI traffic (see inventory_Plan_additional_resources.md):

  Sign-in -> think time -> Batch A (initial load) ->
  [Batch B (filter applied) -> Batch C (filter cleared)] per configured
  ResourceType -> Batch D (sequential resource-type drilldowns).

  Batch A, B and C calls fire in parallel via gevent greenlets (see
  common.py's run_batch()); Batch D calls are strictly sequential, one call
  per simulated "click", with 2-10s think time between each.

Batch A — initial page load (parallel):
  GET /org/resources (Filters.ResourceType=CustomerAccount), GET
  /org/inventory-summary for SummaryType=by-account/by-region/by-resourcetype,
  GET /org/resource-governance-summary.

Batch B — filter applied (parallel, repeated once per `filter_resource_types`
  entry): the 3 inventory-summary calls above, filtered by
  Filters.ResourceType=<type>.

Batch C — filter cleared (parallel): same shape as Batch A minus
  /org/resources.

Batch D — sequential resource-type drilldowns (NOT parallel): one
  GET /org/resources_paginated call per resource type in the hardcoded
  `_DRILLDOWN_RESOURCE_TYPES` constant below, 2-10s apart.

A 1-3s pause (batch_think_time_min/max) is inserted after Batch A/B/C; a
2-10s pause (drilldown_think_time_min/max) after each Batch D call.

The target environment is selectable via `--env <key>` (e.g. `--env qa`),
resolved from the shared `api.environments`/`api.default_env` block in
config.yaml (falls back to `api.default_env` when --env is omitted).

This file intentionally does NOT touch locustfile.py's Home/WAFR/Health/Chat
journey — it shares the same config.yaml (its own `inventory:` section) and
users_csv, but runs as a fully separate Locust file with its own HttpUser and
its own on_test_start/on_test_stop hooks.

Usage (headless — recommended):
    locust -f inventory_locustfile.py --headless \\
      --users 10 --spawn-rate 2 --run-time 5m --env qa \\
      --html reports/inventory_report.html \\
      --csv reports/inventory_stats
"""

import json
import logging
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import gevent
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv
from locust import HttpUser, constant, events, task
from locust.exception import StopUser
from locust.runners import STATE_CLEANUP, STATE_STOPPED, STATE_STOPPING, WorkerRunner
from locust.stats import CSV_STATS_FLUSH_INTERVAL_SEC, CSV_STATS_INTERVAL_SEC

logger = logging.getLogger("perf_test.inventory")

# ---------------------------------------------------------------------------
# Bootstrap: config + users (shared helpers live in common.py)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_CONFIG_FILE = _HERE / "config.yaml"
_ENV_FILE = _HERE / ".env"

load_dotenv(dotenv_path=_ENV_FILE, override=False)

if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import common  # noqa: E402

CFG: Dict[str, Any] = common.load_config(_CONFIG_FILE)

# Named environments (api.environments/api.default_env) selectable via
# --env <key> at the command line; falls back to a single "default" entry
# built from api.base_url if environments: isn't configured.
_ENVIRONMENTS, _DEFAULT_ENV = common.resolve_environments(CFG["api"], "inventory_locustfile.py")
BASE_URL: str = _ENVIRONMENTS[_DEFAULT_ENV]
MC_DEBUG_MODE: bool = bool(CFG["api"].get("mc_debug_mode", True))
TIMEOUT: int = int(CFG["api"].get("timeout_seconds", 40))

# Inventory flow config (config section: `inventory`) — fully self-contained,
# does not read the main `test:` block used by locustfile.py.
_inventory_cfg: Dict[str, Any] = CFG.get("inventory", {}) or {}
if not _inventory_cfg:
    raise ValueError(
        "config.yaml has no 'inventory:' section — see config.yaml for the "
        "expected keys (enabled, mode, cloud_provider, users_csv, ...)."
    )

_users_csv_raw: str = _inventory_cfg.get("users_csv", "./users.csv")
_users_csv_path = Path(_users_csv_raw)
if not _users_csv_path.is_absolute():
    _users_csv_path = (_HERE / _users_csv_path).resolve()
USER_COUNT: int = int(_inventory_cfg.get("user_count", 1))

_THINK_MIN: float = float(_inventory_cfg.get("think_time_min", 2))
_THINK_MAX: float = float(_inventory_cfg.get("think_time_max", 5))

# Think time after each Inventory batch.
_BATCH_THINK_MIN: float = float(_inventory_cfg.get("batch_think_time_min", 1))
_BATCH_THINK_MAX: float = float(_inventory_cfg.get("batch_think_time_max", 3))

_RUN_MODE: str = str(_inventory_cfg.get("run_mode", "single_journey")).strip().lower()
_ITERATIONS: int = max(1, int(_inventory_cfg.get("iterations", 1)))

USERS: List[Dict[str, str]] = common.load_users(_users_csv_path, USER_COUNT, logger)
_user_claimer = common.UserClaimer(USERS)

_INVENTORY_ENABLED: bool = bool(_inventory_cfg.get("enabled", True))
_INVENTORY_MODE: str = str(_inventory_cfg.get("mode", "standalone")).strip().lower()
_CLOUD_PROVIDER: str = str(_inventory_cfg.get("cloud_provider", "AWS"))
_BATCH_2_SUMMARY_TYPES: List[str] = list(
    _inventory_cfg.get("batch_2_summary_types", ["by-account", "by-region", "by-resourcetype"]) or []
)

# Batch A — initial page load additions (GET /org/resources +
# GET /org/resource-governance-summary alongside the 3 summary calls above).
_BATCH_A_ENABLED: bool = bool(_inventory_cfg.get("batch_a_enabled", True))
_BATCH_A_ORG_RESOURCE_TYPE: str = str(_inventory_cfg.get("batch_a_org_resource_type", "CustomerAccount"))

# Batch B/C — filter apply/clear cycle. All entries run every iteration, in
# order (not a random pick) — see plan §3.
_FILTER_RESOURCE_TYPES: List[str] = list(_inventory_cfg.get("filter_resource_types", []) or [])

# Batch D — think time between sequential drilldown calls (2-10s in the HAR).
_DRILLDOWN_THINK_MIN: float = float(_inventory_cfg.get("drilldown_think_time_min", 2))
_DRILLDOWN_THINK_MAX: float = float(_inventory_cfg.get("drilldown_think_time_max", 10))

# Batch D — sequential resource-type drilldowns, fired one per click with
# 2-10s think time between each (not parallel). Hardcoded here (not in
# config.yaml) per decision: this is a fixed replication of the observed HAR
# sequence, not a per-run tunable. Deduped from the HAR's raw sequence (the
# repeated Image call via /org/resources and the trailing repeated Instance
# call were dropped — each resource type appears exactly once).
_DRILLDOWN_RESOURCE_TYPES: List[Dict[str, Any]] = [
    {"type": "Instance", "limit": 10},
    {"type": "Image", "limit": 10},
    {"type": "Workspace", "offset": 0, "limit": 100},
    {"type": "NotebookInstance", "offset": 0, "limit": 100},
    {"type": "Volume", "limit": 10},
    {"type": "Snapshot", "limit": 10},
    {"type": "S3", "limit": 10},
]


# ---------------------------------------------------------------------------
# Custom CLI flag: --env <key>
# ---------------------------------------------------------------------------


@events.init_command_line_parser.add_listener
def _add_custom_args(parser) -> None:
    parser.add_argument(
        "--env",
        type=str,
        default=None,
        choices=sorted(_ENVIRONMENTS),
        help=(
            f"Environment to target ({'/'.join(sorted(_ENVIRONMENTS))}). "
            f"Defaults to api.default_env in config.yaml ({_DEFAULT_ENV})."
        ),
    )


# ---------------------------------------------------------------------------
# Locust User class
# ---------------------------------------------------------------------------


class InventoryUser(HttpUser):
    """
    Simulates a single MontyCloud user viewing the Inventory page:
      1. Sign in (on_start — once per spawn).
      2. @task: run the Inventory flow — Batch A (initial load), then
         Batch B/C (filter apply/clear) per configured ResourceType, then
         Batch D (sequential resource-type drilldowns).

    Request names are prefixed [Auth] / [Inventory] so report_generator.py
    can bucket them into sections, same convention as locustfile.py.
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

        opts = getattr(self.environment, "parsed_options", None)
        env_choice = (getattr(opts, "env", None) or _DEFAULT_ENV).strip().lower()
        base_url = _ENVIRONMENTS.get(env_choice)
        if not base_url:
            logger.error(
                "Unknown --env %r (expected one of %s); falling back to %r.",
                env_choice, sorted(_ENVIRONMENTS), _DEFAULT_ENV,
            )
            base_url = _ENVIRONMENTS[_DEFAULT_ENV]
        self.client.base_url = base_url

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
    def inventory_journey(self) -> None:
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
        self._inventory_flow()

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
    # Inventory flow  (Batch A -> [Batch B -> Batch C]* -> Batch D)
    # ------------------------------------------------------------------

    def _inventory_summary(self, summary_type: str, resource_type: Optional[str] = None):
        params: Dict[str, Any] = {"CloudProvider": _CLOUD_PROVIDER, "SummaryType": summary_type}
        name = f"[Inventory] inventory-summary ({summary_type})"
        if resource_type:
            params["Filters"] = json.dumps({"ResourceType": [resource_type]})
            name = f"[Inventory] inventory-summary ({summary_type}, filtered)"
        return self._get("/org/inventory-summary", name, params=params)

    def _resources(self) -> None:
        self._get(
            "/org/resources",
            "[Inventory] resources",
            params={
                "CloudProvider": _CLOUD_PROVIDER,
                "Filters": json.dumps({
                    "Orgs": [self._org_id],
                    "Detail": True,
                    "ResourceType": _BATCH_A_ORG_RESOURCE_TYPE,
                }),
            },
        )

    def _resource_governance_summary(self) -> None:
        self._get(
            "/org/resource-governance-summary",
            "[Inventory] resource-governance-summary",
            params={"CloudProvider": _CLOUD_PROVIDER},
        )

    def _resources_paginated(self, drilldown: Dict[str, Any]) -> None:
        resource_type = drilldown["type"]
        params: Dict[str, Any] = {
            "Filters": json.dumps({"ResourceType": resource_type, "Detail": True}),
            "Limit": drilldown["limit"],
        }
        if "offset" in drilldown:
            params["CloudProvider"] = _CLOUD_PROVIDER
            params["Offset"] = drilldown["offset"]
        self._get(
            "/org/resources_paginated",
            f"[Inventory] resources_paginated ({resource_type})",
            params=params,
        )

    def _batch_a(self) -> None:
        """Initial page load: resources + 3x inventory-summary + governance-summary (parallel)."""
        calls = [
            lambda _st=st: self._inventory_summary(_st) for st in _BATCH_2_SUMMARY_TYPES
        ]
        if _BATCH_A_ENABLED:
            calls = [self._resources, *calls, self._resource_governance_summary]
        self._run_batch(calls)
        gevent.sleep(random.uniform(_BATCH_THINK_MIN, _BATCH_THINK_MAX))

    def _batch_b(self, resource_type: str) -> None:
        """Filter applied: 3x inventory-summary, filtered by ResourceType (parallel)."""
        self._run_batch([
            lambda _st=st: self._inventory_summary(_st, resource_type) for st in _BATCH_2_SUMMARY_TYPES
        ])
        gevent.sleep(random.uniform(_BATCH_THINK_MIN, _BATCH_THINK_MAX))

    def _batch_c(self) -> None:
        """Filter cleared: back to the unfiltered 3x inventory-summary + governance-summary (parallel)."""
        self._run_batch([
            *[lambda _st=st: self._inventory_summary(_st) for st in _BATCH_2_SUMMARY_TYPES],
            self._resource_governance_summary,
        ])
        gevent.sleep(random.uniform(_BATCH_THINK_MIN, _BATCH_THINK_MAX))

    def _batch_d(self) -> None:
        """Sequential resource-type drilldowns, one call per click (not parallel)."""
        for drilldown in _DRILLDOWN_RESOURCE_TYPES:
            self._resources_paginated(drilldown)
            gevent.sleep(random.uniform(_DRILLDOWN_THINK_MIN, _DRILLDOWN_THINK_MAX))

    def _inventory_flow(self) -> None:
        self._batch_a()
        for resource_type in _FILTER_RESOURCE_TYPES:
            self._batch_b(resource_type)
            self._batch_c()
        self._batch_d()


if not _INVENTORY_ENABLED:
    raise SystemExit(
        "inventory.enabled is false in config.yaml — "
        "nothing to run. Set it to true to execute this test."
    )
if _INVENTORY_MODE != "standalone":
    logger.warning(
        "inventory.mode=%r is not yet supported by this file (only "
        "'standalone' is implemented) — running standalone anyway.",
        _INVENTORY_MODE,
    )

# ---------------------------------------------------------------------------
# Event hook: detailed per-request failures CSV (full URL, not just name)
# ---------------------------------------------------------------------------


@events.test_start.add_listener
def on_test_start_failure_logger(environment, **kwargs) -> None:
    opts = getattr(environment, "parsed_options", None)
    csv_prefix = getattr(opts, "csv_prefix", None) if opts else None
    common.attach_failure_detail_logger(environment, csv_prefix, logger)


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
            "Pass --csv reports/inventory_stats to enable it."
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
            description=_inventory_cfg.get("description", ""),
            config_data=CFG,
            report_name=_inventory_cfg.get("report_name", ""),
        )
        logger.info("Custom HTML report  →  %s", out)
    except Exception as exc:
        logger.error("Failed to generate custom HTML report: %s", exc, exc_info=True)
