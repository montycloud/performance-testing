"""Inventory page — GET /org/inventory-summary, /org/resources,
/org/resource-governance-summary, /org/resources_paginated.

Journey per iteration (see "Document for Implementation/Inventory_new_resources.md"):

  Signin -> think
    -> Batch A (parallel): resources + inventory-summary x3 + governance-summary
    -> batch think
    -> for each configured filter_resource_types entry:
         Batch B (parallel): inventory-summary x3, filtered by ResourceType
         -> batch think
         Batch C (parallel): inventory-summary x3 (unfiltered) + governance-summary
         -> batch think
    -> Batch D (sequential): one /org/resources_paginated call per configured
       drilldown_resource_types entry, with randomized think time between each.

An empty/omitted filter_resource_types skips the Batch B/C cycle entirely —
the journey goes straight from Batch A to Batch D.

Request names are prefixed [Inventory] so the report groups them in their own section.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcperf.core.config import ScenarioConfig, require_section
from mcperf.locust.base_user import MontyCloudUser

DEFAULT_SUMMARY_TYPES = ("by-account", "by-region", "by-resourcetype")


@dataclass(frozen=True)
class DrilldownResourceType:
    """One Batch D sequential drilldown call: GET /org/resources_paginated."""

    resource_type: str
    limit: int = 10
    offset: int | None = None
    cloud_provider: str | None = None
    detail: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DrilldownResourceType:
        offset = d.get("offset")
        return cls(
            resource_type=str(d["type"]),
            limit=int(d.get("limit", 10)),
            offset=int(offset) if offset is not None else None,
            cloud_provider=str(d["cloud_provider"]) if d.get("cloud_provider") else None,
            detail=bool(d.get("detail", True)),
        )


@dataclass(frozen=True)
class InventoryConfig(ScenarioConfig):
    cloud_provider: str = "AWS"
    summary_types: tuple[str, ...] = DEFAULT_SUMMARY_TYPES
    batch_a_enabled: bool = True
    batch_a_org_resource_type: str = "CustomerAccount"
    filter_resource_types: tuple[str, ...] = ()
    drilldown_resource_types: tuple[DrilldownResourceType, ...] = ()
    drilldown_think_time_min: float = 2.0
    drilldown_think_time_max: float = 10.0

    @classmethod
    def load_file(cls, path: Path) -> InventoryConfig:
        kwargs = cls.base_kwargs(path)
        section = require_section(kwargs["raw"], "inventory", kwargs["path"])
        summary_types = tuple(section.get("summary_types") or DEFAULT_SUMMARY_TYPES)
        filter_resource_types = tuple(section.get("filter_resource_types") or ())
        drilldown_resource_types = tuple(
            DrilldownResourceType.from_dict(d) for d in section.get("drilldown_resource_types") or ()
        )
        cfg = cls(
            **kwargs,
            cloud_provider=str(section.get("cloud_provider", "AWS")),
            summary_types=summary_types,
            batch_a_enabled=bool(section.get("batch_a_enabled", True)),
            batch_a_org_resource_type=str(section.get("batch_a_org_resource_type", "CustomerAccount")),
            filter_resource_types=filter_resource_types,
            drilldown_resource_types=drilldown_resource_types,
            drilldown_think_time_min=float(section.get("drilldown_think_time_min", 2)),
            drilldown_think_time_max=float(section.get("drilldown_think_time_max", 10)),
        )
        if cfg.drilldown_think_time_min > cfg.drilldown_think_time_max:
            raise ValueError("inventory.drilldown_think_time_min must be <= drilldown_think_time_max.")
        return cfg


class InventoryUser(MontyCloudUser):
    cfg: InventoryConfig | None = None
    log = logging.getLogger("perf_test.inventory")

    def run_flow(self) -> None:
        cfg = self.cfg

        # Batch A — initial page load (resources + summary trio + governance)
        if cfg.batch_a_enabled:
            self.run_batch([self.resources_call(), *self.summary_calls(), self.governance_call()])
        else:
            self.run_batch(self.summary_calls())
        self.batch_think()

        # Batch B/C — filter apply/clear cycle, once per configured ResourceType
        for resource_type in cfg.filter_resource_types:
            self.run_batch(self.summary_calls(resource_type=resource_type))
            self.batch_think()
            self.run_batch([*self.summary_calls(), self.governance_call()])
            self.batch_think()

        # Batch D — sequential resource-type drilldowns, not run_batch (one call
        # at a time, with a randomized pause between each, per the observed flow)
        for drilldown in cfg.drilldown_resource_types:
            self.resources_paginated_call(drilldown)()
            self.think(cfg.drilldown_think_time_min, cfg.drilldown_think_time_max)

    def summary_calls(self, resource_type: str | None = None) -> list:
        """The by-account/by-region/by-resourcetype trio, optionally ResourceType-filtered."""
        cfg = self.cfg
        params_base: dict[str, Any] = {"CloudProvider": cfg.cloud_provider}
        if resource_type:
            params_base["Filters"] = json.dumps({"ResourceType": [resource_type]})
        return [
            self.call(
                "/org/inventory-summary",
                f"[Inventory] inventory-summary ({summary_type})",
                {"SummaryType": summary_type, **params_base},
            )
            for summary_type in cfg.summary_types
        ]

    def resources_call(self):
        """Batch A's GET /org/resources call, scoped to the signed-in user's org."""
        cfg = self.cfg
        filters = {
            "Orgs": [self.org_id],
            "Detail": True,
            "ResourceType": cfg.batch_a_org_resource_type,
        }
        return self.call(
            "/org/resources",
            "[Inventory] resources",
            {"Filters": json.dumps(filters), "CloudProvider": cfg.cloud_provider},
        )

    def governance_call(self):
        return self.call(
            "/org/resource-governance-summary",
            "[Inventory] resource-governance-summary",
            {"CloudProvider": self.cfg.cloud_provider},
        )

    def resources_paginated_call(self, drilldown: DrilldownResourceType):
        """One Batch D call: GET /org/resources_paginated for a single resource type."""
        filters = {"ResourceType": drilldown.resource_type, "Detail": drilldown.detail}
        params: dict[str, Any] = {"Filters": json.dumps(filters), "Limit": drilldown.limit}
        if drilldown.cloud_provider:
            params["CloudProvider"] = drilldown.cloud_provider
        if drilldown.offset is not None:
            params["Offset"] = drilldown.offset
        return self.call(
            "/org/resources_paginated",
            f"[Inventory] resources_paginated ({drilldown.resource_type})",
            params,
        )
