"""Inventory summary page — GET /org/inventory-summary.

Journey per iteration: sign in → think → one batch of summary calls fired in
parallel (by-account, by-region, by-resourcetype by default) → batch think.

Request names are prefixed [Inventory] so the report groups them in their own section.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from mcperf.core.config import ScenarioConfig, require_section
from mcperf.locust.base_user import MontyCloudUser

DEFAULT_SUMMARY_TYPES = ("by-account", "by-region", "by-resourcetype")


@dataclass(frozen=True)
class InventoryConfig(ScenarioConfig):
    cloud_provider: str = "AWS"
    summary_types: tuple[str, ...] = DEFAULT_SUMMARY_TYPES

    @classmethod
    def load_file(cls, path: Path) -> InventoryConfig:
        kwargs = cls.base_kwargs(path)
        section = require_section(kwargs["raw"], "inventory", kwargs["path"])
        summary_types = tuple(section.get("summary_types") or DEFAULT_SUMMARY_TYPES)
        return cls(
            **kwargs,
            cloud_provider=str(section.get("cloud_provider", "AWS")),
            summary_types=summary_types,
        )


class InventoryUser(MontyCloudUser):
    cfg: InventoryConfig | None = None
    log = logging.getLogger("perf_test.inventory")

    def run_flow(self) -> None:
        self.run_batch(partial(self.inventory_summary, t) for t in self.cfg.summary_types)
        self.batch_think()

    def inventory_summary(self, summary_type: str) -> dict[str, Any]:
        return self.api_get(
            "/org/inventory-summary",
            f"[Inventory] inventory-summary ({summary_type})",
            params={"CloudProvider": self.cfg.cloud_provider, "SummaryType": summary_type},
        )
