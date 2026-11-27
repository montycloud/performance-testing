"""Inventory endpoint-targeted load test.

Unlike mcperf.api.inventory (one realistic, sequential Inventory-page journey
per iteration), this decouples /org/resources, /org/resources_paginated,
/org/inventory-summary and /org/resource-governance-summary into independent,
continuously-looping User classes so each endpoint can be paced toward its own
throughput target instead of being capped by the slowest step in a shared
journey.

Four independent User classes, each paced with Locust's constant_throughput()
wait_time (target OPS per simulated user of that class — see each class below
and the `endpoint_load:` section of config.yaml):

  ResourcesLoadUser          -> GET /org/resources, round-robin across
                                 configured cloud providers.
  ResourcesPaginatedLoadUser -> GET /org/resources_paginated, round-robin
                                 across `drilldown_resource_types`.
  InventorySummaryLoadUser   -> GET /org/inventory-summary, round-robin across
                                 the cross-product of `summary_types` x
                                 configured cloud providers (unfiltered only
                                 — no filter apply/clear cycle).
  GovernanceLoadUser         -> GET /org/resource-governance-summary, looped
                                 at a low rate. GET /auth/user already fires
                                 once per user as part of sign-in (see
                                 mcperf.locust.http.signin) so it needs no
                                 separate class here.

A class's aggregate request rate ~= (its share of --users, controlled by its
`weight`, set from config) x its target_ops_per_user. The *target OPS* in
config.yaml's comments refer to backend (Lambda) invocation rates observed in
practice with a handful of concurrent users — not a literal Locust
requests/second figure to scale --users toward. Start small (e.g. 5-20 users)
and validate against real backend telemetry before scaling up; `weight` /
`target_ops_per_user` are starting points to recalibrate empirically, not
exact targets.

Request names are prefixed [Inventory] (and GET /auth/user, fired once at
sign-in, [Auth]) so the report groups them in their existing sections.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from locust import constant_throughput

from mcperf.api.inventory import DEFAULT_SUMMARY_TYPES, DrilldownResourceType
from mcperf.core.config import ScenarioConfig, require_section
from mcperf.locust.base_user import MontyCloudUser


@dataclass(frozen=True)
class EndpointLoadClassConfig:
    """`weight` and `wait_time` inputs for one of the 4 User classes below."""

    weight: int = 1
    target_ops_per_user: float = 0.1

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, default_weight: int, default_rate: float) -> EndpointLoadClassConfig:
        d = d or {}
        return cls(
            weight=max(1, int(d.get("weight", default_weight))),
            target_ops_per_user=max(0.01, float(d.get("target_ops_per_user", default_rate))),
        )


@dataclass(frozen=True)
class EndpointLoadConfig(ScenarioConfig):
    cloud_provider: str = "AWS"
    cloud_providers: tuple[str, ...] = ("AWS",)
    batch_a_org_resource_type: str = "CustomerAccount"
    summary_types: tuple[str, ...] = DEFAULT_SUMMARY_TYPES
    drilldown_resource_types: tuple[DrilldownResourceType, ...] = ()
    resources: EndpointLoadClassConfig = EndpointLoadClassConfig()
    resources_paginated: EndpointLoadClassConfig = EndpointLoadClassConfig()
    inventory_summary: EndpointLoadClassConfig = EndpointLoadClassConfig()
    governance: EndpointLoadClassConfig = EndpointLoadClassConfig()

    @classmethod
    def load_file(cls, path: Path) -> EndpointLoadConfig:
        kwargs = cls.base_kwargs(path)
        section = require_section(kwargs["raw"], "endpoint_load", kwargs["path"])
        cloud_provider = str(section.get("cloud_provider", "AWS"))
        cfg = cls(
            **kwargs,
            cloud_provider=cloud_provider,
            cloud_providers=tuple(section.get("cloud_providers") or (cloud_provider,)),
            batch_a_org_resource_type=str(section.get("batch_a_org_resource_type", "CustomerAccount")),
            summary_types=tuple(section.get("summary_types") or DEFAULT_SUMMARY_TYPES),
            drilldown_resource_types=tuple(
                DrilldownResourceType.from_dict(d) for d in section.get("drilldown_resource_types") or ()
            ),
            # Starting weight/target_ops_per_user: weight mirrors the relative
            # Prod backend-OPS target per endpoint (resources/paginated/summary
            # ~= 30/24/46); target_ops_per_user is each endpoint's measured
            # 1/avg_response_time ceiling, so constant_throughput never
            # throttles a user below what the backend can sustain alone.
            resources=EndpointLoadClassConfig.from_dict(section.get("resources"), 30, 1.0),
            resources_paginated=EndpointLoadClassConfig.from_dict(section.get("resources_paginated"), 24, 0.9),
            inventory_summary=EndpointLoadClassConfig.from_dict(section.get("inventory_summary"), 46, 0.11),
            governance=EndpointLoadClassConfig.from_dict(section.get("governance"), 3, 0.14),
        )
        if not cfg.drilldown_resource_types:
            raise ValueError(
                f"{path}: endpoint_load.drilldown_resource_types must have at least one entry."
            )
        return cfg


class _EndpointLoadUser(MontyCloudUser):
    """Shared base: reuse MontyCloudUser's claim/sign-in/api_get, but loop
    continuously at a configured rate instead of one journey per iteration."""

    abstract = True
    cfg: ClassVar[EndpointLoadConfig | None] = None
    think_before_flow = False
    # Name of the EndpointLoadConfig field holding this class's weight/rate.
    class_cfg_attr: ClassVar[str] = ""

    @classmethod
    def configure(cls, cfg: EndpointLoadConfig) -> None:
        super().configure(cfg)
        class_cfg: EndpointLoadClassConfig = getattr(cfg, cls.class_cfg_attr)
        cls.weight = class_cfg.weight
        cls.wait_time = constant_throughput(class_cfg.target_ops_per_user)

    def on_start(self) -> None:
        super().on_start()
        self._rr_index = 0

    def _next(self, options: tuple) -> Any:
        value = options[self._rr_index % len(options)]
        self._rr_index += 1
        return value


class ResourcesLoadUser(_EndpointLoadUser):
    """Loops GET /org/resources, round-robin across configured cloud providers
    (ResourceType stays fixed — per captured traffic, only CloudProvider
    varies between calls, not ResourceType)."""

    log = logging.getLogger("perf_test.inventory_endpoint_load.resources")
    class_cfg_attr = "resources"

    def run_flow(self) -> None:
        cfg = self.cfg
        cloud_provider = self._next(cfg.cloud_providers)
        filters = {
            "Orgs": [self.org_id],
            "Detail": True,
            "ResourceType": cfg.batch_a_org_resource_type,
        }
        self.api_get(
            "/org/resources",
            "[Inventory] resources",
            {"Filters": json.dumps(filters), "CloudProvider": cloud_provider},
        )


class ResourcesPaginatedLoadUser(_EndpointLoadUser):
    """Loops GET /org/resources_paginated, round-robin across
    `drilldown_resource_types`."""

    log = logging.getLogger("perf_test.inventory_endpoint_load.resources_paginated")
    class_cfg_attr = "resources_paginated"

    def run_flow(self) -> None:
        drilldown: DrilldownResourceType = self._next(self.cfg.drilldown_resource_types)
        filters = {"ResourceType": drilldown.resource_type, "Detail": drilldown.detail}
        params: dict[str, Any] = {"Filters": json.dumps(filters), "Limit": drilldown.limit}
        if drilldown.cloud_provider:
            params["CloudProvider"] = drilldown.cloud_provider
        if drilldown.offset is not None:
            params["Offset"] = drilldown.offset
        self.api_get(
            "/org/resources_paginated",
            f"[Inventory] resources_paginated ({drilldown.resource_type})",
            params,
        )


class InventorySummaryLoadUser(_EndpointLoadUser):
    """Loops GET /org/inventory-summary, round-robin across the cross-product
    of `summary_types` x configured cloud providers (unfiltered only)."""

    log = logging.getLogger("perf_test.inventory_endpoint_load.inventory_summary")
    class_cfg_attr = "inventory_summary"

    def on_start(self) -> None:
        super().on_start()
        cfg = self.cfg
        self._combos = tuple((st, cp) for st in cfg.summary_types for cp in cfg.cloud_providers)

    def run_flow(self) -> None:
        summary_type, cloud_provider = self._next(self._combos)
        self.api_get(
            "/org/inventory-summary",
            f"[Inventory] inventory-summary ({summary_type})",
            {"CloudProvider": cloud_provider, "SummaryType": summary_type},
        )


class GovernanceLoadUser(_EndpointLoadUser):
    """Low-rate loop on GET /org/resource-governance-summary. GET /auth/user
    already fires once per user during sign-in — no separate class for it."""

    log = logging.getLogger("perf_test.inventory_endpoint_load.governance")
    class_cfg_attr = "governance"

    def run_flow(self) -> None:
        self.api_get(
            "/org/resource-governance-summary",
            "[Inventory] resource-governance-summary",
            {"CloudProvider": self.cfg.cloud_provider},
        )


ENDPOINT_LOAD_USER_CLASSES = (
    ResourcesLoadUser,
    ResourcesPaginatedLoadUser,
    InventorySummaryLoadUser,
    GovernanceLoadUser,
)
