#!/usr/bin/env python3
"""
MontyCloud Inventory — Endpoint-Targeted Load Test
=========================================================
Standalone Locust scenario that, unlike inventory_locustfile.py (which
replicates one realistic, sequential Inventory-page journey), decouples
/org/resources, /org/resources_paginated and /org/inventory-summary into
independent, continuously-looping User classes so each endpoint's throughput
can be paced toward its own target (e.g. matching a prod OPS figure) instead
of being capped by the slowest step in a shared journey.

Four independent User classes, each paced with Locust's constant_throughput()
wait_time (iterations/second PER SIMULATED USER of that class — see each
class below and the `endpoint_load:` section of config.yaml):

  ResourcesLoadUser          -> GET /org/resources, looped directly.
  ResourcesPaginatedLoadUser -> GET /org/resources_paginated, round-robin
                                 across the same 7 resource types
                                 inventory_locustfile.py's Batch D uses.
  InventorySummaryLoadUser   -> GET /org/inventory-summary, round-robin
                                 across `endpoint_load.summary_types`
                                 (unfiltered only — no filter apply/clear
                                 cycle).
  BackgroundLightLoadUser    -> GET /auth/user + GET
                                 /org/resource-governance-summary,
                                 alternating, at a low rate — keeps them
                                 minimally exercised without dominating load.

A class's aggregate OPS ~= (its share of --users, controlled by its `weight`
class attribute / config.yaml's `endpoint_load.<class>.weight`) x its
target_ops_per_user. Actual achieved OPS is still capped by real backend
response time (Little's Law: concurrency needed ~= target_OPS x avg response
time) regardless of these settings — calibrate weight/target_ops_per_user
empirically with short runs and check the measured OPS in the stats CSV.

This file intentionally does NOT import inventory_locustfile.py (that would
also pull in InventoryUser and make it spawn as part of this test). The
small list of drilldown resource types is duplicated here on purpose — see
config.yaml's `endpoint_load:` section comment.

Sign-in happens once per simulated user (on_start), exactly as in every
other locustfile here — it is not a separate pace-able "load", since it's
bounded by how many virtual users you spawn, not by a throughput target.

Usage (headless — recommended):
    locust -f inventory_endpoint_load_locustfile.py --headless \\
      --users 500 --spawn-rate 50 --run-time 5m --env qa \\
      --html reports/endpoint_load_report.html \\
      --csv reports/endpoint_load_stats
"""

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from locust import HttpUser, constant_throughput, events, task
from locust.exception import StopUser

logger = logging.getLogger("perf_test.inventory_endpoint_load")

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

_ENVIRONMENTS, _DEFAULT_ENV = common.resolve_environments(
    CFG["api"], "inventory_endpoint_load_locustfile.py"
)
BASE_URL: str = _ENVIRONMENTS[_DEFAULT_ENV]
MC_DEBUG_MODE: bool = bool(CFG["api"].get("mc_debug_mode", True))
TIMEOUT: int = int(CFG["api"].get("timeout_seconds", 40))

_cfg: Dict[str, Any] = CFG.get("endpoint_load", {}) or {}
if not _cfg:
    raise ValueError(
        "config.yaml has no 'endpoint_load:' section — see config.yaml for "
        "the expected keys (enabled, resources, resources_paginated, "
        "inventory_summary, background, ...)."
    )

_users_csv_raw: str = _cfg.get("users_csv", "./users.csv")
_users_csv_path = Path(_users_csv_raw)
if not _users_csv_path.is_absolute():
    _users_csv_path = (_HERE / _users_csv_path).resolve()
USER_COUNT: int = int(_cfg.get("user_count", 1))

USERS: List[Dict[str, str]] = common.load_users(_users_csv_path, USER_COUNT, logger)
_user_claimer = common.UserClaimer(USERS)

_CLOUD_PROVIDER: str = str(_cfg.get("cloud_provider", "AWS"))
_BATCH_A_ORG_RESOURCE_TYPE: str = str(_cfg.get("batch_a_org_resource_type", "CustomerAccount"))
_SUMMARY_TYPES: List[str] = list(
    _cfg.get("summary_types", ["by-account", "by-region", "by-resourcetype"]) or []
)

_RESOURCES_CFG: Dict[str, Any] = _cfg.get("resources", {}) or {}
_RESOURCES_PAGINATED_CFG: Dict[str, Any] = _cfg.get("resources_paginated", {}) or {}
_INVENTORY_SUMMARY_CFG: Dict[str, Any] = _cfg.get("inventory_summary", {}) or {}
_BACKGROUND_CFG: Dict[str, Any] = _cfg.get("background", {}) or {}

# Cloud providers to round-robin across for /org/resources and
# /org/inventory-summary — per captured real traffic, the page fires one call
# per configured cloud provider (e.g. AWS, Azure) rather than a single fixed
# value. /org/resources_paginated's captured calls had no CloudProvider param
# at all for non-offset drilldown types, so it's left untouched.
_CLOUD_PROVIDERS: List[str] = list(_cfg.get("cloud_providers", [_CLOUD_PROVIDER]) or [_CLOUD_PROVIDER])

# Cross-product of summary_types x cloud_providers, round-robinned by
# InventorySummaryLoadUser so every summary type gets exercised against every
# configured provider over time.
_SUMMARY_TYPE_PROVIDER_COMBOS: List[tuple] = [
    (st, cp) for st in _SUMMARY_TYPES for cp in _CLOUD_PROVIDERS
]


def _weight(class_cfg: Dict[str, Any], default: int) -> int:
    return max(1, int(class_cfg.get("weight", default)))


def _rate(class_cfg: Dict[str, Any], default: float) -> float:
    return max(0.01, float(class_cfg.get("target_ops_per_user", default)))


# Same 7 resource types as inventory_locustfile.py's Batch D
# (_DRILLDOWN_RESOURCE_TYPES) — kept as an independent constant here (not
# imported) so this file never pulls in InventoryUser. Keep in sync by hand
# if inventory_locustfile.py's list ever changes.
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
# Shared base — sign-in (once per user, not paced) + authenticated GET helper
# ---------------------------------------------------------------------------


class _BaseLoadUser(HttpUser):
    abstract = True
    host = BASE_URL

    def on_start(self) -> None:
        self._creds: Dict[str, str] = _user_claimer.claim()
        self._token: Optional[str] = None
        self._org_id: str = ""
        self._rr_index: int = 0

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

        # Sign-in is a one-time setup cost, not a pace-able load — it scales
        # 1:1 with how many virtual users are spawned, same as every other
        # locustfile here.
        self._token, self._org_id = common.signin(
            self.client, self._creds, MC_DEBUG_MODE, TIMEOUT, logger,
        )
        if not self._token:
            logger.error(
                "[%s] No token available; removing user from pool.",
                self._creds.get("Email", "unknown"),
            )
            raise StopUser()

    def _get(self, path: str, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return common.authenticated_get(
            self.client, self._token, path, name, TIMEOUT, logger, params=params,
        )


# ---------------------------------------------------------------------------
# Endpoint-targeted User classes
# ---------------------------------------------------------------------------


class ResourcesLoadUser(_BaseLoadUser):
    """Loops GET /org/resources, round-robin across configured cloud providers
    (ResourceType stays fixed at CustomerAccount — per captured traffic, only
    CloudProvider varies between calls, not ResourceType)."""

    weight = _weight(_RESOURCES_CFG, 3)
    wait_time = constant_throughput(_rate(_RESOURCES_CFG, 0.67))

    @task
    def hit_resources(self) -> None:
        cloud_provider = _CLOUD_PROVIDERS[self._rr_index % len(_CLOUD_PROVIDERS)]
        self._rr_index += 1
        self._get(
            "/org/resources",
            f"[Inventory] resources ({cloud_provider})",
            params={
                "CloudProvider": cloud_provider,
                "Filters": json.dumps({
                    "Orgs": [self._org_id],
                    "Detail": True,
                    "ResourceType": _BATCH_A_ORG_RESOURCE_TYPE,
                }),
            },
        )


class ResourcesPaginatedLoadUser(_BaseLoadUser):
    """Loops GET /org/resources_paginated, round-robin across all 7 resource types."""

    weight = _weight(_RESOURCES_PAGINATED_CFG, 3)
    wait_time = constant_throughput(_rate(_RESOURCES_PAGINATED_CFG, 0.53))

    @task
    def hit_resources_paginated(self) -> None:
        drilldown = _DRILLDOWN_RESOURCE_TYPES[self._rr_index % len(_DRILLDOWN_RESOURCE_TYPES)]
        self._rr_index += 1
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


class InventorySummaryLoadUser(_BaseLoadUser):
    """Loops GET /org/inventory-summary, round-robin across the cross-product
    of `summary_types` x configured cloud providers (unfiltered only — per
    captured traffic, CloudProvider varies the same way /org/resources does)."""

    weight = _weight(_INVENTORY_SUMMARY_CFG, 10)
    wait_time = constant_throughput(_rate(_INVENTORY_SUMMARY_CFG, 0.11))

    @task
    def hit_inventory_summary(self) -> None:
        summary_type, cloud_provider = _SUMMARY_TYPE_PROVIDER_COMBOS[
            self._rr_index % len(_SUMMARY_TYPE_PROVIDER_COMBOS)
        ]
        self._rr_index += 1
        self._get(
            "/org/inventory-summary",
            f"[Inventory] inventory-summary ({summary_type}, {cloud_provider})",
            params={"CloudProvider": cloud_provider, "SummaryType": summary_type},
        )


class BackgroundLightLoadUser(_BaseLoadUser):
    """Low-rate pings on /auth/user + /org/resource-governance-summary (alternating)."""

    weight = _weight(_BACKGROUND_CFG, 1)
    wait_time = constant_throughput(_rate(_BACKGROUND_CFG, 0.05))

    @task
    def hit_background(self) -> None:
        if self._rr_index % 2 == 0:
            self._get("/auth/user", "[Auth] /auth/user")
        else:
            self._get(
                "/org/resource-governance-summary",
                "[Inventory] resource-governance-summary",
                params={"CloudProvider": _CLOUD_PROVIDER},
            )
        self._rr_index += 1


if not bool(_cfg.get("enabled", True)):
    raise SystemExit(
        "endpoint_load.enabled is false in config.yaml — "
        "nothing to run. Set it to true to execute this test."
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
# Event hook: auto-generate custom HTML report when --csv is used
# ---------------------------------------------------------------------------


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs) -> None:
    """After the test run, generate the shared custom HTML report."""
    from locust.stats import CSV_STATS_FLUSH_INTERVAL_SEC, CSV_STATS_INTERVAL_SEC
    import gevent

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
            "Pass --csv reports/endpoint_load_stats to enable it."
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
            description=_cfg.get("description", ""),
            config_data=CFG,
            report_name=_cfg.get("report_name", ""),
        )
        logger.info("Custom HTML report  →  %s", out)
    except Exception as exc:
        logger.error("Failed to generate custom HTML report: %s", exc, exc_info=True)
