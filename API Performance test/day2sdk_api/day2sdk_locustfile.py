#!/usr/bin/env python3
"""
Day2SDK Tenant API — Locust Performance Test
=========================================================
Standalone Locust scenario covering Day2SDK GET endpoints, grouped by the
API's own tags (see day2_api_endpoints.csv):

  Auth tag (always runs, sequential, first — compulsory for every user):
    GET /auth/user
    GET /roles

  GET /tenants?PageSize=<tenants_page_size> runs ONCE next (sequential),
  shared by every tenant-scoped tag below — no duplicate /tenants calls.

  Then every enabled tag below runs as a PARALLEL gevent greenlet (via
  common.run_batch) — each tag's own calls are sequential internally, but
  different tags overlap in time with each other:

    Tenant (day2sdk_api.tags.tenant):
      GET /tenants/categories
      GET /tenants/{TenantId}                   (once per captured tenant)

    Assessment (tags.assessment): per tenant —
      GET /tenants/{TenantId}/assessments?Status=<assessment_status>
                                                 -> capture first Assessment ID
      GET .../assessments/{AssessmentId}, findings, resource-types,
          top-checks, top-resources, findings/summary,
          findings/summary-severity, schedules

    User (tags.user): per tenant — GET /tenants/{TenantId}/users

    Account (tags.account): per tenant —
      GET /tenants/{TenantId}/accounts, /accounts/count

    Resource (tags.resource): per tenant —
      GET /tenants/{TenantId}resource-types, resources

    Health (tags.health): per tenant —
      GET /tenants/{TenantId}/health/events/summary, health/policies

    Project (tags.project): per tenant —
      GET /tenants/{TenantId}/projects, projects/requests

    Organization (tags.organization): independent of Tenant (separate
    OrganizationId space) — GET /organizations?PageNumber=&PageSize=
    captures OrganizationId(s), reserved for a future Workload tag.

  Deferred (not implemented yet): Workload, Security, Report, Cost,
  Optimization, Bot, MAP Project. Azure-tagged endpoints are excluded by
  design, not just deferred.

Authentication is NOT the Email/Password users.csv flow used by the other
locustfiles in this project — Day2SDK uses static per-user AccessKey/
SecretKey pairs sent as raw `x-api-key` / `authorization` headers on every
call (no signin/token exchange). Credentials are read from .env
(DAY2SDK_ACCESS_KEY_<N> / DAY2SDK_SECRET_KEY_<N>) rather than a CSV, to
avoid ever committing secrets to the repo.

This file intentionally does NOT touch locustfile.py's Home/WAFR/Health/Chat
journey or inventory_locustfile.py — it shares the same config.yaml but reads
its own fully self-contained `day2sdk_api:` section (credentials, environments,
load profile, tags) and runs as a separate Locust file with its own HttpUser.

Usage (headless — recommended):
    locust -f day2sdk_locustfile.py --headless \\
      --users 5 --spawn-rate 2 --env stg \\
      --html ../reports/day2sdk_report.html \\
      --csv ../reports/day2sdk_stats

--env selects which Day2SDK environment to hit (dev or stg); defaults to
`day2sdk_api.default_env` in config.yaml when omitted. To run a true
"single journey per user" test, set day2sdk_api.iterations: 1 in config.yaml
(each of the --users virtual users then runs the journey exactly once and
stops — the test exits automatically, --run-time is not needed).
"""

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

logger = logging.getLogger("perf_test.day2sdk")

# ---------------------------------------------------------------------------
# Bootstrap: config + credentials (shared helpers live in common.py)
# ---------------------------------------------------------------------------

_HERE = Path(__file__).parent
_ROOT = _HERE.parent  # "API Performance test" — config.yaml/.env/common.py live here
_CONFIG_FILE = _ROOT / "config.yaml"
_ENV_FILE = _ROOT / ".env"

# Every FAIL/error logged via `logger` (incl. the detailed failure dumps from
# common.raw_header_get) also lands here, for debugging after a headless run.
_FAILURES_LOG = _ROOT / "reports" / "day2sdk_failures.log"
_FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
_failures_handler = logging.FileHandler(_FAILURES_LOG, mode="a", encoding="utf-8")
_failures_handler.setLevel(logging.WARNING)
_failures_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
logger.addHandler(_failures_handler)

load_dotenv(dotenv_path=_ENV_FILE, override=False)

if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import common  # noqa: E402

CFG: Dict[str, Any] = common.load_config(_CONFIG_FILE)

# Day2SDK flow config (config section: `day2sdk_api`).
_day2sdk_cfg: Dict[str, Any] = CFG.get("day2sdk_api", {}) or {}
if not _day2sdk_cfg:
    raise ValueError(
        "config.yaml has no 'day2sdk_api:' section — see config.yaml for the "
        "expected keys (enabled, environments, default_env, ...)."
    )

_DAY2SDK_ENABLED: bool = bool(_day2sdk_cfg.get("enabled", True))
if not _DAY2SDK_ENABLED:
    raise SystemExit(
        "day2sdk_api.enabled is false in config.yaml — nothing to run. "
        "Set it to true to execute this test."
    )

_ENVIRONMENTS: Dict[str, str] = {
    str(k).strip().lower(): str(v).rstrip("/")
    for k, v in (_day2sdk_cfg.get("environments", {}) or {}).items()
}
_DEFAULT_ENV: str = str(_day2sdk_cfg.get("default_env", "stg")).strip().lower()
if _DEFAULT_ENV not in _ENVIRONMENTS:
    raise ValueError(
        f"day2sdk_api.default_env={_DEFAULT_ENV!r} is not a key in "
        f"day2sdk_api.environments ({sorted(_ENVIRONMENTS)})."
    )

TIMEOUT: int = int(_day2sdk_cfg.get("timeout_seconds", 40))

# Load profile — fully self-contained, independent of the main `test:`
# section's Home/WAFR journey settings.
_RUN_MODE: str = str(_day2sdk_cfg.get("run_mode", "single_journey")).strip().lower()
_ITERATIONS: int = max(1, int(_day2sdk_cfg.get("iterations", 1)))
_THINK_MIN: float = float(_day2sdk_cfg.get("think_time_min", 1))
_THINK_MAX: float = float(_day2sdk_cfg.get("think_time_max", 2))

# Which API tags to exercise. Auth is compulsory and always runs — it has no
# toggle. Tenant defaults on; everything else defaults off (enable in config).
_tags_cfg: Dict[str, Any] = _day2sdk_cfg.get("tags", {}) or {}
_TAG_TENANT_ENABLED: bool = bool(_tags_cfg.get("tenant", True))
_TAG_ASSESSMENT_ENABLED: bool = bool(_tags_cfg.get("assessment", False))
_TAG_USER_ENABLED: bool = bool(_tags_cfg.get("user", False))
_TAG_ACCOUNT_ENABLED: bool = bool(_tags_cfg.get("account", False))
_TAG_RESOURCE_ENABLED: bool = bool(_tags_cfg.get("resource", False))
_TAG_HEALTH_ENABLED: bool = bool(_tags_cfg.get("health", False))
_TAG_PROJECT_ENABLED: bool = bool(_tags_cfg.get("project", False))
_TAG_ORGANIZATION_ENABLED: bool = bool(_tags_cfg.get("organization", False))
_TAG_SECURITY_ENABLED: bool = bool(_tags_cfg.get("security", False))

# Tenant tag: page size for the /tenants list call.
_TENANTS_PAGE_SIZE: int = int(_day2sdk_cfg.get("tenants_page_size", 10))

# Assessment tag: Status query param for listAssessments (required by the API).
_ASSESSMENT_STATUS: str = str(_day2sdk_cfg.get("assessment_status", "PENDING"))

# Organization tag: PageNumber/PageSize query params (both required by the API).
_ORG_PAGE_NUMBER: int = int(_day2sdk_cfg.get("organizations_page_number", 1))
_ORG_PAGE_SIZE: int = int(_day2sdk_cfg.get("organizations_page_size", 10))

# Static AccessKey/SecretKey pairs from .env — ignores users.csv entirely.
_CREDENTIALS: List[Dict[str, str]] = common.load_env_credentials("DAY2SDK", logger)
_cred_claimer = common.UserClaimer(_CREDENTIALS)

_API_PREFIX = "/day2/api/v1"


# ---------------------------------------------------------------------------
# Custom CLI flag: --env dev|stg
# ---------------------------------------------------------------------------


@events.init_command_line_parser.add_listener
def _add_custom_args(parser) -> None:
    parser.add_argument(
        "--env",
        type=str,
        default=None,
        choices=sorted(_ENVIRONMENTS),
        help=(
            f"Day2SDK environment to target ({'/'.join(sorted(_ENVIRONMENTS))}). "
            f"Defaults to day2sdk_api.default_env in config.yaml ({_DEFAULT_ENV})."
        ),
    )
    parser.add_argument(
        "--report-name",
        type=str,
        default=None,
        help=(
            "Custom HTML report name segment (custom_<name>_<timestamp>.html). "
            "Defaults to day2sdk_api.report_name in config.yaml when omitted."
        ),
    )


# ---------------------------------------------------------------------------
# Locust User class
# ---------------------------------------------------------------------------


class Day2SDKUser(HttpUser):
    """
    Simulates a single Day2SDK API consumer:
      1. on_start — claim an AccessKey/SecretKey pair, resolve env, build headers.
      2. @task: run the tag-based Day2SDK journey (Auth, then Tenant/Assessment
         per the day2sdk_api.tags config toggles).

    Request names are prefixed [Day2SDK] so report_generator.py buckets them
    into their own report section, same convention as [Auth]/[Inventory].
    """

    wait_time = constant(0)
    host = _ENVIRONMENTS[_DEFAULT_ENV]

    def on_start(self) -> None:
        self._creds: Dict[str, str] = _cred_claimer.claim()
        self._headers: Dict[str, str] = {
            "x-api-key": self._creds["AccessKey"],
            "authorization": self._creds["SecretKey"],
        }
        self._iterations_done: int = 0

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

        _adapter = HTTPAdapter(pool_connections=1, pool_maxsize=20)
        self.client.mount("https://", _adapter)
        self.client.mount("http://", _adapter)

    def _get(self, path: str, name: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return common.raw_header_get(
            self.client, self._headers, path, name, TIMEOUT, logger, params=params,
        )

    # ------------------------------------------------------------------
    # Main task
    # ------------------------------------------------------------------

    @task
    def day2sdk_journey(self) -> None:
        gevent.sleep(random.uniform(_THINK_MIN, _THINK_MAX))
        self._day2sdk_flow()

        if _RUN_MODE == "single_journey":
            self._iterations_done += 1
            if self._iterations_done >= _ITERATIONS:
                raise StopUser()
            logger.info(
                "[%s] journey %d/%d complete",
                self._creds["AccessKey"], self._iterations_done, _ITERATIONS,
            )

    # ------------------------------------------------------------------
    # Day2SDK flow — Auth, then TenantId fetch (both sequential, once),
    # then enabled tags run in PARALLEL sharing that same tenant_ids list.
    # ------------------------------------------------------------------

    def _day2sdk_flow(self) -> None:
        self._auth_flow()

        tenant_tags_enabled = (
            _TAG_TENANT_ENABLED or _TAG_ASSESSMENT_ENABLED or _TAG_USER_ENABLED
            or _TAG_ACCOUNT_ENABLED or _TAG_RESOURCE_ENABLED or _TAG_HEALTH_ENABLED
            or _TAG_PROJECT_ENABLED or _TAG_SECURITY_ENABLED
        )
        tenant_ids: List[str] = self._fetch_tenant_ids() if tenant_tags_enabled else []

        branches = []
        if _TAG_TENANT_ENABLED:
            branches.append(lambda: self._tenant_flow(tenant_ids))
        if _TAG_ASSESSMENT_ENABLED:
            branches.append(lambda: self._assessment_flow(tenant_ids))
        if _TAG_USER_ENABLED:
            branches.append(lambda: self._user_flow(tenant_ids))
        if _TAG_ACCOUNT_ENABLED:
            branches.append(lambda: self._account_flow(tenant_ids))
        if _TAG_RESOURCE_ENABLED:
            branches.append(lambda: self._resource_flow(tenant_ids))
        if _TAG_HEALTH_ENABLED:
            branches.append(lambda: self._health_flow(tenant_ids))
        if _TAG_PROJECT_ENABLED:
            branches.append(lambda: self._project_flow(tenant_ids))
        if _TAG_SECURITY_ENABLED:
            branches.append(lambda: self._security_flow(tenant_ids))
        if _TAG_ORGANIZATION_ENABLED:
            branches.append(self._organization_flow)

        if branches:
            common.run_batch(branches)

    def _auth_flow(self) -> None:
        """Auth tag — compulsory for every user, always runs regardless of config."""
        self._get(f"{_API_PREFIX}/auth/user", "[Day2SDK] /auth/user")
        self._get(f"{_API_PREFIX}/roles", "[Day2SDK] /roles")

    def _fetch_tenant_ids(self) -> List[str]:
        """Fetch the tenant list ONCE, shared by every tenant-scoped tag that
        runs afterward (read-only list, safe to share across greenlets).
        """
        body = self._get(
            f"{_API_PREFIX}/tenants", "[Day2SDK] /tenants",
            params={"PageSize": _TENANTS_PAGE_SIZE},
        )
        tenant_ids = [t["ID"] for t in body.get("Tenants", []) or [] if t.get("ID")]
        if not tenant_ids:
            logger.warning(
                "[%s] /tenants returned no tenants — downstream tenant-scoped "
                "calls will be skipped.", self._creds["AccessKey"],
            )
        return tenant_ids

    def _tenant_flow(self, tenant_ids: List[str]) -> None:
        """Tenant tag — categories, then getTenantById per tenant (list already
        fetched once in _day2sdk_flow)."""
        if not tenant_ids:
            return
        self._get(f"{_API_PREFIX}/tenants/categories", "[Day2SDK] /tenants/categories")
        for tenant_id in tenant_ids:
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}", "[Day2SDK] /tenants/{TenantId}")

    def _assessment_flow(self, tenant_ids: List[str]) -> None:
        """Assessment tag — for each tenant, list assessments then drill into
        the first assessment's GET detail endpoints. Sequential per tenant.
        """
        if not tenant_ids:
            logger.warning(
                "[%s] No tenant IDs available — skipping Assessment tag.",
                self._creds["AccessKey"],
            )
            return

        for tenant_id in tenant_ids:
            body = self._get(
                f"{_API_PREFIX}/tenants/{tenant_id}/assessments",
                "[Day2SDK] /tenants/{TenantId}/assessments",
                params={"Status": _ASSESSMENT_STATUS},
            )
            assessments = body.get("Assessments") or body.get("Items") or []
            if not assessments:
                logger.info(
                    "[%s] tenant %s has no assessments (Status=%s) — skipping details.",
                    self._creds["AccessKey"], tenant_id, _ASSESSMENT_STATUS,
                )
                continue

            assessment_id = assessments[0].get("ID") or assessments[0].get("AssessmentId")
            if not assessment_id:
                logger.warning(
                    "[%s] tenant %s: first assessment has no ID/AssessmentId field "
                    "(keys: %s) — skipping details.",
                    self._creds["AccessKey"], tenant_id, sorted(assessments[0].keys()),
                )
                continue

            base = f"{_API_PREFIX}/tenants/{tenant_id}/assessments/{assessment_id}"
            self._get(base, "[Day2SDK] /tenants/{TenantId}/assessments/{AssessmentId}")
            self._get(f"{base}/findings", "[Day2SDK] .../assessments/{AssessmentId}/findings")
            self._get(f"{base}/resource-types", "[Day2SDK] .../assessments/{AssessmentId}/resource-types")
            self._get(f"{base}/top-checks", "[Day2SDK] .../assessments/{AssessmentId}/top-checks")
            self._get(f"{base}/top-resources", "[Day2SDK] .../assessments/{AssessmentId}/top-resources")
            self._get(f"{base}/findings/summary", "[Day2SDK] .../assessments/{AssessmentId}/findings/summary")
            self._get(
                f"{base}/findings/summary-severity",
                "[Day2SDK] .../assessments/{AssessmentId}/findings/summary-severity",
                params={"Dimension": "ResourceType"},
            )
            self._get(f"{base}/schedules", "[Day2SDK] .../assessments/{AssessmentId}/schedules")

    def _user_flow(self, tenant_ids: List[str]) -> None:
        """User tag — per tenant, list users."""
        for tenant_id in tenant_ids:
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}/users", "[Day2SDK] /tenants/{TenantId}/users")

    def _account_flow(self, tenant_ids: List[str]) -> None:
        """Account tag — per tenant: accounts list (capture AccountId list),
        accounts count, then region-status for one randomly-picked account.
        """
        for tenant_id in tenant_ids:
            body = self._get(
                f"{_API_PREFIX}/tenants/{tenant_id}/accounts", "[Day2SDK] /tenants/{TenantId}/accounts",
                params={"CloudProvider": "AWS", "PageSize": 10, "PageNumber": 1},
            )
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}/accounts/count", "[Day2SDK] /tenants/{TenantId}/accounts/count")

            account_ids = [a["AccountId"] for a in body.get("Accounts", []) or [] if a.get("AccountId")]
            if not account_ids:
                logger.info(
                    "[%s] tenant %s has no accounts — skipping region-status.",
                    self._creds["AccessKey"], tenant_id,
                )
                continue
            account_id = random.choice(account_ids)
            self._get(
                f"{_API_PREFIX}/tenants/{tenant_id}/accounts/{account_id}/region-status",
                "[Day2SDK] /tenants/{TenantId}/accounts/{AccountId}/region-status",
            )

    def _resource_flow(self, tenant_ids: List[str]) -> None:
        """Resource tag — per tenant, regions/resource-types/resources/inventory-summary."""
        for tenant_id in tenant_ids:
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}/resources/regions", "[Day2SDK] /tenants/{TenantId}/resources/regions")
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}/resources/resource-types", "[Day2SDK] /tenants/{TenantId}/resources/resource-types")
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}/resources", "[Day2SDK] /tenants/{TenantId}/resources")
            self._get(
                f"{_API_PREFIX}/tenants/{tenant_id}/resources/inventory-summary",
                "[Day2SDK] /tenants/{TenantId}/resources/inventory-summary",
                params={"CloudProvider": "AWS", "SummaryType": "By-Account"},
            )

    def _health_flow(self, tenant_ids: List[str]) -> None:
        """Health tag — per tenant, events summary + policies + breakdown-summary."""
        for tenant_id in tenant_ids:
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}/health/events/summary", "[Day2SDK] /tenants/{TenantId}/health/events/summary")
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}/health/policies", "[Day2SDK] /tenants/{TenantId}/health/policies")
            self._get(
                f"{_API_PREFIX}/tenants/{tenant_id}/health/events/breakdown-summary",
                "[Day2SDK] /tenants/{TenantId}/health/events/breakdown-summary",
                params={"Dimension": "EventTypeCode", "PageSize": 10, "PageNumber": 1},
            )

    def _project_flow(self, tenant_ids: List[str]) -> None:
        """Project tag — per tenant, projects list + project requests."""
        for tenant_id in tenant_ids:
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}/projects", "[Day2SDK] /tenants/{TenantId}/projects")
            self._get(f"{_API_PREFIX}/tenants/{tenant_id}/projects/requests", "[Day2SDK] /tenants/{TenantId}/projects/requests")

    def _security_flow(self, tenant_ids: List[str]) -> None:
        """Security tag — per tenant: posture summary (hardcoded Dimension),
        services list (capture first ServiceId), that service's summary,
        checks summary, and top-entities (hardcoded Dimension=SERVICES).
        ReportType=SHIP is fixed — it's the only value this API accepts today.
        """
        for tenant_id in tenant_ids:
            base = f"{_API_PREFIX}/tenants/{tenant_id}/security"
            self._get(
                f"{base}/posture/summary", "[Day2SDK] /tenants/{TenantId}/security/posture/summary",
                params={"ReportType": "SHIP", "Dimension": "TENANT_SUMMARY"},
            )
            body = self._get(
                f"{base}/services", "[Day2SDK] /tenants/{TenantId}/security/services",
                params={"ReportType": "SHIP", "PageNumber": 1, "PageSize": 10},
            )
            self._get(
                f"{base}/checks/summary", "[Day2SDK] /tenants/{TenantId}/security/checks/summary",
                params={"ReportType": "SHIP", "PageNumber": 1, "PageSize": 10},
            )
            self._get(
                f"{base}/top-entities", "[Day2SDK] /tenants/{TenantId}/security/top-entities",
                params={"ReportType": "SHIP", "Dimension": "SERVICES"},
            )

            services = body.get("Services") or body.get("Items") or []
            if not services:
                logger.info(
                    "[%s] tenant %s has no security services — skipping service summary.",
                    self._creds["AccessKey"], tenant_id,
                )
                continue
            service_id = services[0].get("ServiceId")
            if not service_id:
                logger.warning(
                    "[%s] tenant %s: first security service has no ServiceId field "
                    "(keys: %s) — skipping service summary.",
                    self._creds["AccessKey"], tenant_id, sorted(services[0].keys()),
                )
                continue
            self._get(
                f"{base}/services/{service_id}/summary",
                "[Day2SDK] /tenants/{TenantId}/security/services/{ServiceId}/summary",
                params={"ReportType": "SHIP"},
            )

    def _organization_flow(self) -> None:
        """Organization tag — independent of Tenant (separate OrganizationId
        space). Captures OrganizationId(s), reserved for a future Workload tag.
        """
        body = self._get(
            f"{_API_PREFIX}/organizations", "[Day2SDK] /organizations",
            params={"PageNumber": _ORG_PAGE_NUMBER, "PageSize": _ORG_PAGE_SIZE},
        )
        orgs = body.get("Organizations") or body.get("Items") or []
        org_ids = [o["ID"] for o in orgs if o.get("ID")]
        if not org_ids:
            logger.warning(
                "[%s] /organizations returned no organizations (or response "
                "shape is unexpected, keys: %s).",
                self._creds["AccessKey"], sorted(body.keys()) if body else [],
            )
        else:
            logger.info(
                "[%s] Organization tag: captured %d organization ID(s) "
                "(reserved for a future Workload tag).",
                self._creds["AccessKey"], len(org_ids),
            )


# ---------------------------------------------------------------------------
# Event hook: single_journey auto-exit (same watchdog as the other locustfiles)
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
            "Pass --csv reports/day2sdk_stats to enable it."
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

    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

    opts = getattr(environment, "parsed_options", None)
    report_name = (getattr(opts, "report_name", None) or _day2sdk_cfg.get("report_name", ""))

    try:
        import report_generator as rg  # noqa: PLC0415
        out = rg.generate(
            stats_csv,
            description=_day2sdk_cfg.get("description", ""),
            config_data=CFG,
            report_name=report_name,
        )
        logger.info("Custom HTML report  →  %s", out)
    except Exception as exc:
        logger.error("Failed to generate custom HTML report: %s", exc, exc_info=True)
