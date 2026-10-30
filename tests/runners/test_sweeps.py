from pathlib import Path

import pytest

from mcperf.runners.chat_sweep import load_manifest, run_config
from mcperf.runners.users_sweep import _scenario_name

REPO = Path(__file__).resolve().parents[2]
JOURNEY = REPO / "scenarios/api/journey"


def test_scenario_name_uses_folder_for_locustfile():
    assert _scenario_name(Path("scenarios/api/inventory/locustfile.py")) == "inventory"
    assert _scenario_name(Path("custom_test.py")) == "custom_test"


@pytest.mark.parametrize("manifest", sorted((JOURNEY / "executions").glob("*.yaml")), ids=lambda p: p.name)
def test_shipped_manifests_validate(manifest):
    assert load_manifest(manifest, JOURNEY)["executions"]


def test_run_config_makes_paths_absolute_and_sets_run_fields(tmp_path):
    base = {
        "load": {"users_csv": "../users.csv"},
        "report": {"report_name": "base", "description": "base"},
        "chat": {"queries_file": "./chat_queries.txt", "tenants_file": "./Tenant.json", "transcript_log": ""},
    }
    entry = {"name": "run_1", "queries_file": "./chat_queries/q.txt", "description": "override"}
    cfg = run_config(base, JOURNEY, entry, tmp_path)

    assert cfg["load"]["users_csv"] == str((JOURNEY.parent / "users.csv").resolve())
    assert cfg["chat"]["tenants_file"] == str(JOURNEY / "Tenant.json")
    assert cfg["chat"]["queries_file"] == str(JOURNEY / "chat_queries/q.txt")
    assert cfg["chat"]["transcript_log"] == str(tmp_path / "chat_transcript_run_1.log")
    assert cfg["report"] == {"report_name": "run_1", "description": "override"}
    assert base["report"]["report_name"] == "base"  # base config untouched


def test_bad_manifest_names_are_rejected(tmp_path):
    manifest = tmp_path / "m.yaml"
    manifest.write_text("executions:\n  - name: 'has space'\n    queries_file: x.txt\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid"):
        load_manifest(manifest, tmp_path)
