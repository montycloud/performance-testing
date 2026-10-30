"""Day2SDK public API — GET endpoints grouped by the API's own tags.

Journey per iteration:
  1. Auth tag (always, sequential): /auth/user, /roles
  2. /tenants once, shared by every tenant-scoped tag
  3. Every enabled tag runs as its own greenlet, in parallel with the others;
     calls inside one tag stay sequential.

Authentication is a static AccessKey/SecretKey pair per simulated user, sent as
`x-api-key` / `authorization` headers on every call (no sign-in). Pairs come
from DAY2SDK_ACCESS_KEY_<N> / DAY2SDK_SECRET_KEY_<N> in .env, never a CSV.

Endpoint inventory: scenarios/api/day2sdk/endpoints.csv. Request names are
prefixed [Day2SDK].
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcperf.core.config import ScenarioConfig, require_section
from mcperf.core.credentials import load_env_credentials
from mcperf.locust.base_user import MontyCloudUser
from mcperf.locust.http import raw_header_get

API = "/day2/api/v1"
CREDENTIAL_PREFIX = "DAY2SDK"

# Tags whose calls run once per tenant from /tenants. `organization` is independent.
TENANT_TAGS = ("tenant", "assessment", "user", "account", "resource", "health", "project", "security")
ALL_TAGS = (*TENANT_TAGS, "organization")


@dataclass(frozen=True)
class Day2SDKConfig(ScenarioConfig):
    tags: frozenset[str] = frozenset({"tenant"})
    tenants_page_size: int = 10
    assessment_status: str = "PENDING"
    organizations_page_number: int = 1
    organizations_page_size: int = 10
    failures_log: Path | None = None

    @classmethod
    def load_file(cls, path: Path) -> Day2SDKConfig:
        kwargs = cls.base_kwargs(path)
        section = require_section(kwargs["raw"], "day2sdk", kwargs["path"])
        toggles = section.get("tags") or {}
        unknown = set(toggles) - set(ALL_TAGS)
        if unknown:
            raise ValueError(f"day2sdk.tags has unknown tag(s) {sorted(unknown)}; known: {list(ALL_TAGS)}.")
        failures_log = str(section.get("failures_log", "") or "").strip()
        return cls(
            **kwargs,
            tags=frozenset(tag for tag in ALL_TAGS if toggles.get(tag, tag == "tenant")),
            tenants_page_size=int(section.get("tenants_page_size", 10)),
            assessment_status=str(section.get("assessment_status", "PENDING")),
            organizations_page_number=int(section.get("organizations_page_number", 1)),
            organizations_page_size=int(section.get("organizations_page_size", 10)),
            failures_log=(kwargs["path"].parent / failures_log).resolve() if failures_log else None,
        )


class Day2SDKUser(MontyCloudUser):
    cfg: Day2SDKConfig | None = None
    log = logging.getLogger("perf_test.day2sdk")

    @classmethod
    def configure(cls, cfg: Day2SDKConfig) -> None:
        super().configure(cfg)
        if cfg.failures_log:
            # Every WARNING+ (incl. raw_header_get's failure dumps) also lands here.
            cfg.failures_log.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(cfg.failures_log, mode="a", encoding="utf-8")
            handler.setLevel(logging.WARNING)
            handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            cls.log.addHandler(handler)

    @classmethod
    def load_credentials(cls, cfg: ScenarioConfig) -> list[dict[str, str]]:
        return load_env_credentials(CREDENTIAL_PREFIX)

    def authenticate(self) -> bool:
        self.headers = {"x-api-key": self.creds["AccessKey"], "authorization": self.creds["SecretKey"]}
        return True

    def api_get(self, path: str, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return raw_header_get(self.client, self.headers, path, name, self.cfg.api.timeout, self.log, params=params)

    @property
    def label(self) -> str:
        return f"{self.creds.get('AccessKey', '')[:4]}…"

    # ------------------------------------------------------------------
    # Journey
    # ------------------------------------------------------------------

    def run_flow(self) -> None:
        self.api_get(f"{API}/auth/user", "[Day2SDK] /auth/user")
        self.api_get(f"{API}/roles", "[Day2SDK] /roles")

        tags = self.cfg.tags
        tenant_ids = self.fetch_tenant_ids() if tags & set(TENANT_TAGS) else []
        flows: dict[str, Callable[[], None]] = {
            "tenant": lambda: self.tenant_flow(tenant_ids),
            "assessment": lambda: self.assessment_flow(tenant_ids),
            "user": lambda: self.per_tenant(tenant_ids, [("/users", None)]),
            "account": lambda: self.account_flow(tenant_ids),
            "resource": lambda: self.per_tenant(tenant_ids, [
                ("/resources/regions", None),
                ("/resources/resource-types", None),
                ("/resources", None),
                ("/resources/inventory-summary", {"CloudProvider": "AWS", "SummaryType": "By-Account"}),
            ]),
            "health": lambda: self.per_tenant(tenant_ids, [
                ("/health/events/summary", None),
                ("/health/policies", None),
                ("/health/events/breakdown-summary",
                 {"Dimension": "EventTypeCode", "PageSize": 10, "PageNumber": 1}),
            ]),
            "project": lambda: self.per_tenant(tenant_ids, [("/projects", None), ("/projects/requests", None)]),
            "security": lambda: self.security_flow(tenant_ids),
            "organization": self.organization_flow,
        }
        branches = [flows[tag] for tag in ALL_TAGS if tag in tags]
        if branches:
            self.run_batch(branches)

    def fetch_tenant_ids(self) -> list[str]:
        body = self.api_get(f"{API}/tenants", "[Day2SDK] /tenants", {"PageSize": self.cfg.tenants_page_size})
        tenant_ids = [t["ID"] for t in body.get("Tenants", []) or [] if t.get("ID")]
        if not tenant_ids:
            self.log.warning("[%s] /tenants returned no tenants — tenant-scoped calls skipped.", self.label)
        return tenant_ids

    def tenant_get(self, tenant_id: str, suffix: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET /tenants/{TenantId}<suffix>, named with the template so stats aggregate across tenants."""
        return self.api_get(
            f"{API}/tenants/{tenant_id}{suffix}", f"[Day2SDK] /tenants/{{TenantId}}{suffix}", params,
        )

    def per_tenant(self, tenant_ids: list[str], calls: list[tuple[str, dict[str, Any] | None]]) -> None:
        for tenant_id in tenant_ids:
            for suffix, params in calls:
                self.tenant_get(tenant_id, suffix, params)

    # ------------------------------------------------------------------
    # Tags that capture IDs from one call for the next
    # ------------------------------------------------------------------

    def tenant_flow(self, tenant_ids: list[str]) -> None:
        if not tenant_ids:
            return
        self.api_get(f"{API}/tenants/categories", "[Day2SDK] /tenants/categories")
        for tenant_id in tenant_ids:
            self.tenant_get(tenant_id, "")

    def assessment_flow(self, tenant_ids: list[str]) -> None:
        if not tenant_ids:
            self.log.warning("[%s] No tenant IDs available — skipping Assessment tag.", self.label)
            return
        status = self.cfg.assessment_status
        for tenant_id in tenant_ids:
            body = self.tenant_get(tenant_id, "/assessments", {"Status": status})
            assessments = body.get("Assessments") or body.get("Items") or []
            if not assessments:
                self.log.info("[%s] tenant %s has no assessments (Status=%s).", self.label, tenant_id, status)
                continue
            assessment_id = assessments[0].get("ID") or assessments[0].get("AssessmentId")
            if not assessment_id:
                self.log.warning(
                    "[%s] tenant %s: first assessment has no ID/AssessmentId (keys: %s).",
                    self.label, tenant_id, sorted(assessments[0].keys()),
                )
                continue

            base = f"{API}/tenants/{tenant_id}/assessments/{assessment_id}"
            self.api_get(base, "[Day2SDK] /tenants/{TenantId}/assessments/{AssessmentId}")
            for suffix, params in (
                ("/findings", None),
                ("/resource-types", None),
                ("/top-checks", None),
                ("/top-resources", None),
                ("/findings/summary", None),
                ("/findings/summary-severity", {"Dimension": "ResourceType"}),
                ("/schedules", None),
            ):
                self.api_get(f"{base}{suffix}", f"[Day2SDK] .../assessments/{{AssessmentId}}{suffix}", params)

    def account_flow(self, tenant_ids: list[str]) -> None:
        for tenant_id in tenant_ids:
            body = self.tenant_get(
                tenant_id, "/accounts", {"CloudProvider": "AWS", "PageSize": 10, "PageNumber": 1},
            )
            self.tenant_get(tenant_id, "/accounts/count")
            account_ids = [a["AccountId"] for a in body.get("Accounts", []) or [] if a.get("AccountId")]
            if not account_ids:
                self.log.info("[%s] tenant %s has no accounts — skipping region-status.", self.label, tenant_id)
                continue
            self.api_get(
                f"{API}/tenants/{tenant_id}/accounts/{random.choice(account_ids)}/region-status",
                "[Day2SDK] /tenants/{TenantId}/accounts/{AccountId}/region-status",
            )

    def security_flow(self, tenant_ids: list[str]) -> None:
        """ReportType=SHIP is fixed — it's the only value this API accepts today."""
        for tenant_id in tenant_ids:
            self.tenant_get(tenant_id, "/security/posture/summary",
                            {"ReportType": "SHIP", "Dimension": "TENANT_SUMMARY"})
            body = self.tenant_get(tenant_id, "/security/services",
                                   {"ReportType": "SHIP", "PageNumber": 1, "PageSize": 10})
            self.tenant_get(tenant_id, "/security/checks/summary",
                            {"ReportType": "SHIP", "PageNumber": 1, "PageSize": 10})
            self.tenant_get(tenant_id, "/security/top-entities", {"ReportType": "SHIP", "Dimension": "SERVICES"})

            services = body.get("Services") or body.get("Items") or []
            if not services:
                self.log.info("[%s] tenant %s has no security services.", self.label, tenant_id)
                continue
            service_id = services[0].get("ServiceId")
            if not service_id:
                self.log.warning(
                    "[%s] tenant %s: first security service has no ServiceId (keys: %s).",
                    self.label, tenant_id, sorted(services[0].keys()),
                )
                continue
            self.api_get(
                f"{API}/tenants/{tenant_id}/security/services/{service_id}/summary",
                "[Day2SDK] /tenants/{TenantId}/security/services/{ServiceId}/summary",
                {"ReportType": "SHIP"},
            )

    def organization_flow(self) -> None:
        """Captures OrganizationId(s), reserved for a future Workload tag."""
        cfg = self.cfg
        body = self.api_get(
            f"{API}/organizations", "[Day2SDK] /organizations",
            {"PageNumber": cfg.organizations_page_number, "PageSize": cfg.organizations_page_size},
        )
        orgs = body.get("Organizations") or body.get("Items") or []
        org_ids = [o["ID"] for o in orgs if o.get("ID")]
        if org_ids:
            self.log.info("[%s] Organization tag: captured %d organization ID(s).", self.label, len(org_ids))
        else:
            self.log.warning(
                "[%s] /organizations returned no organizations (keys: %s).",
                self.label, sorted(body.keys()) if body else [],
            )
