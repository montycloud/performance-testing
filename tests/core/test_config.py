from pathlib import Path

import pytest

from mcperf.api.inventory import InventoryConfig
from mcperf.core.config import LoadProfile, ScenarioConfig, resolve_path

REPO = Path(__file__).resolve().parents[2]


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_resolve_path_is_relative_to_base_dir(tmp_path):
    assert resolve_path("../users.csv", tmp_path / "a") == (tmp_path / "users.csv").resolve()
    assert resolve_path("/abs/users.csv", tmp_path) == Path("/abs/users.csv")


def test_scenario_config_defaults_and_paths(tmp_path):
    path = write(tmp_path, "api:\n  base_url: https://example.test/\nload:\n  users_csv: ./u.csv\n")
    cfg = ScenarioConfig.load_file(path)
    assert cfg.api.base_url == "https://example.test"
    assert cfg.api.timeout == 40
    assert cfg.load.users_csv == (tmp_path / "u.csv").resolve()
    assert cfg.load.is_single_journey
    assert cfg.report.report_name == ""


def test_missing_api_section_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="api"):
        ScenarioConfig.load_file(write(tmp_path, "load: {}\n"))


@pytest.mark.parametrize(
    "load, message",
    [
        ({"run_mode": "forever"}, "run_mode"),
        ({"think_time_min": 5, "think_time_max": 1}, "think_time_min"),
    ],
)
def test_load_profile_validation(tmp_path, load, message):
    with pytest.raises(ValueError, match=message):
        LoadProfile.from_dict(load, tmp_path)


def test_iterations_never_below_one(tmp_path):
    assert LoadProfile.from_dict({"iterations": 0}, tmp_path).iterations == 1


def test_shipped_inventory_config_parses():
    cfg = InventoryConfig.load_file(REPO / "scenarios/api/inventory/config.yaml")
    assert cfg.summary_types == ("by-account", "by-region", "by-resourcetype")
    assert cfg.cloud_provider == "AWS"
    assert cfg.load.users_csv == (REPO / "scenarios/api/users.csv").resolve()
    assert cfg.report.report_name == "inventory"
