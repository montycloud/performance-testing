from pathlib import Path

import pytest

from mcperf.api.cost_dashboard import CostDashboardConfig, date_filters
from mcperf.api.day2sdk import Day2SDKConfig
from mcperf.api.journey import JourneyConfig
from mcperf.core import environments
from mcperf.core.config import ApiSettings

REPO = Path(__file__).resolve().parents[2]


def test_aliases_and_unknown_env():
    assert environments.base_url("stg") == "https://stg1-api.montycloud.com"
    assert environments.base_url("DEV1") == "https://dev-api.montycloud.com"
    assert environments.is_production("prod")
    with pytest.raises(ValueError, match="Unknown environment"):
        environments.base_url("qa9")


def test_api_settings_env_or_base_url():
    assert ApiSettings.from_dict({"env": "stg"}).base_url == "https://stg1-api.montycloud.com"
    assert ApiSettings.from_dict({"base_url": "http://x/", "env": "stg"}).base_url == "http://x"
    with pytest.raises(ValueError):
        ApiSettings.from_dict({})


def test_command_line_overrides_keep_subclass_fields():
    cfg = CostDashboardConfig.load_file(REPO / "scenarios/api/cost_dashboard/config.yaml")
    out = cfg.with_overrides(env="dev", report_name="nightly")
    assert isinstance(out, CostDashboardConfig)
    assert out.api.base_url == "https://dev-api.montycloud.com"
    assert out.report.report_name == "nightly"
    assert out.presets == cfg.presets


def test_shipped_scenario_configs_parse():
    cost_cfg = CostDashboardConfig.load_file(REPO / "scenarios/api/cost_dashboard/config.yaml")
    assert len(cost_cfg.presets) == 6
    assert cost_cfg.v2_enabled is False
    day2 = Day2SDKConfig.load_file(REPO / "scenarios/api/day2sdk/config.yaml")
    assert day2.api.base_url == "https://stg1-api.montycloud.com"
    assert "organization" in day2.tags and "security" not in day2.tags
    journey = JourneyConfig.load_file(REPO / "scenarios/api/journey/config.yaml")
    assert (journey.wafr_think_time_min, journey.wafr_think_time_max) == (1, 3)
    assert journey.homepage_enabled is True
    assert journey.wafr_enabled is True
    assert not journey.chat.enabled


def test_journey_rejects_all_flows_disabled(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "api: {env: stg}\nload: {users_csv: users.csv}\n"
        "homepage: {enabled: false}\nwafr: {enabled: false}\n"
        "health: {enabled: false}\nchat: {enabled: false}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="all false"):
        JourneyConfig.load_file(cfg)


def test_day2sdk_rejects_unknown_tag(tmp_path):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("api: {env: stg}\nday2sdk:\n  tags: {tenant: true, workloads: true}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="workloads"):
        Day2SDKConfig.load_file(cfg)


def test_date_filters():
    from datetime import date

    assert date_filters(7, False, today=date(2026, 10, 30)) == {"StartDate": "2026-10-23", "EndDate": "2026-10-30"}
    assert date_filters(7, True, today=date(2026, 10, 30))["ChargeTypes"] == {"exclude": ["Credit", "Refund"]}
