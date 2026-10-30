"""Cost Dashboard page — four sequential batches, calls inside a batch run in parallel.

  Batch 1  bootstrap: cost-dashboard + lookup
  Batch 2  "Total Spend" widgets (6 calls) — once per date-range preset, plus an
           optional unfiltered pass first
  Batch 3  tag-keys lookup
  Batch 4  "Spend Trend" tab widgets (3 calls) — once per date-range preset

Request names are prefixed [Cost]; the report groups several of them into one row.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from mcperf.core.config import ScenarioConfig, require_section
from mcperf.locust.base_user import MontyCloudUser

CHARGE_TYPES_EXCLUDE = {"exclude": ["Credit", "Refund"]}


@dataclass(frozen=True)
class DatePreset:
    label: str
    days_back: int


@dataclass(frozen=True)
class CostDashboardConfig(ScenarioConfig):
    presets: tuple[DatePreset, ...] = ()
    charge_types_exclude: bool = True
    include_no_filter_pass: bool = True
    default_days_back: int = 40
    tag_keys_limit: int = 20
    breakdown_limit: int = 25
    preset_think_time_min: float = 1.0
    preset_think_time_max: float = 3.0

    @classmethod
    def load_file(cls, path: Path) -> CostDashboardConfig:
        kwargs = cls.base_kwargs(path)
        section = require_section(kwargs["raw"], "cost_dashboard", kwargs["path"])
        presets = tuple(
            DatePreset(label=str(p.get("label", p["days_back"])), days_back=int(p["days_back"]))
            for p in section.get("date_range_presets") or []
        )
        if not presets:
            raise ValueError(
                f"{kwargs['path']}: cost_dashboard.date_range_presets is empty — "
                "at least one preset ({label, days_back}) is required."
            )
        return cls(
            **kwargs,
            presets=presets,
            charge_types_exclude=bool(section.get("charge_types_exclude", True)),
            include_no_filter_pass=bool(section.get("include_no_filter_pass", True)),
            default_days_back=int(section.get("default_days_back", 40)),
            tag_keys_limit=int(section.get("tag_keys_limit", 20)),
            breakdown_limit=int(section.get("breakdown_limit", 25)),
            preset_think_time_min=float(section.get("preset_think_time_min", 1)),
            preset_think_time_max=float(section.get("preset_think_time_max", 3)),
        )


def date_filters(days_back: int, exclude_charge_types: bool, today: date | None = None) -> dict[str, Any]:
    """{StartDate, EndDate[, ChargeTypes]} with EndDate = today."""
    end = today or date.today()
    filters: dict[str, Any] = {
        "StartDate": (end - timedelta(days=days_back)).isoformat(),
        "EndDate": end.isoformat(),
    }
    if exclude_charge_types:
        filters["ChargeTypes"] = CHARGE_TYPES_EXCLUDE
    return filters


class CostDashboardUser(MontyCloudUser):
    cfg: CostDashboardConfig | None = None
    log = logging.getLogger("perf_test.cost_dashboard")

    def run_flow(self) -> None:
        cfg = self.cfg

        # Batch 1 — dashboard bootstrap
        self.run_batch([
            self.cost_get("/cost/cost-dashboard", "[Cost] cost-dashboard"),
            self.cost_get("/cost/cost-dashboard/lookup", "[Cost] lookup"),
        ])
        self.batch_think()

        # Batch 2 — "Total Spend" widgets, once per pass
        passes: list[tuple[int, bool]] = []
        if cfg.include_no_filter_pass:
            passes.append((cfg.default_days_back, False))
        passes += [(p.days_back, cfg.charge_types_exclude) for p in cfg.presets]
        for days_back, exclude in passes:
            self.run_batch(self.total_spend_batch(date_filters(days_back, exclude)))
            self.preset_think()

        # Batch 3 — tag-keys lookup
        self.run_batch([
            self.cost_get(
                "/cost/cost-dashboard/lookup/tag-keys", "[Cost] lookup-tag-keys",
                Limit=cfg.tag_keys_limit, Offset=0,
            ),
        ])
        self.batch_think()

        # Batch 4 — "Spend Trend" tab, once per preset
        for preset in cfg.presets:
            self.run_batch(self.spend_trend_batch(date_filters(preset.days_back, cfg.charge_types_exclude)))
            self.preset_think()

    def total_spend_batch(self, base: dict[str, Any]) -> list:
        by_account = {"SummaryType": "by-account", **base}
        return [
            self.cost_get("/cost/cost-dashboard/cost-summary", "[Cost] cost-summary (by-account)",
                          filters=by_account),
            self.cost_get("/cost/cost-dashboard/cost-summary", "[Cost] cost-summary (by-service)",
                          filters={"SummaryType": "by-service", **base}),
            self.cost_get("/cost/cost-dashboard/region-summary", "[Cost] region-summary", filters=base),
            self.cost_get("/cost/cost-dashboard/resource-summary", "[Cost] resource-summary", filters=base),
            self.cost_get("/cost/cost-dashboard/spend-trend", "[Cost] spend-trend", filters=by_account),
            self.cost_get("/cost/cost-by-charge-type", "[Cost] cost-by-charge-type", filters=base),
        ]

    def spend_trend_batch(self, base: dict[str, Any]) -> list:
        limit = self.cfg.breakdown_limit
        trend = {"SummaryType": "by-account", **base}
        breakdown = {**base, "SummaryType": "by-account"}
        return [
            self.cost_get("/cost/cost-dashboard/spend-trend", "[Cost] spend-trend-tab", filters=trend),
            self.cost_get("/cost/cost-dashboard/spend-breakdown-summary", "[Cost] spend-breakdown-summary",
                          filters=breakdown, Limit=limit, Offset=0),
            self.cost_get("/cost/cost-dashboard/resource-count", "[Cost] resource-count",
                          filters=breakdown, Limit=limit, Offset=0),
        ]

    def cost_get(self, path: str, name: str, filters: dict[str, Any] | None = None, **params: Any):
        """A pending GET with CloudProvider=AWS and optional JSON-encoded Filters."""
        if filters is not None:
            params = {"Filters": json.dumps(filters), **params}
        return self.call(path, name, {**params, "CloudProvider": "AWS"})

    def preset_think(self) -> None:
        self.think(self.cfg.preset_think_time_min, self.cfg.preset_think_time_max)
