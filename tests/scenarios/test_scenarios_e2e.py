"""Each shipped scenario, run headless by real Locust against the local mock API.

Request counts are pinned to the browser waterfalls the scenarios replay, so a
dropped or duplicated call in a refactor fails here.
"""

import pytest
from mock_api import DAY2_SECRET, mock_api, run_scenario

pytestmark = pytest.mark.e2e


def count(rows, name):
    return int(rows[name]["Request Count"])


def assert_clean(proc, rows):
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert rows, proc.stderr[-3000:]
    assert rows["Aggregated"]["Failure Count"] == "0", proc.stderr[-3000:]


def test_inventory(tmp_path):
    with mock_api() as (url, hits):
        proc, rows = run_scenario("inventory", tmp_path, url, {"load": {"iterations": 2}}, users=2)
    assert_clean(proc, rows)
    # 2 users × 2 iterations × 3 summary types; one sign-in per user.
    for summary_type in ("by-account", "by-region", "by-resourcetype"):
        assert count(rows, f"[Inventory] inventory-summary ({summary_type})") == 4
    assert count(rows, "[Auth] /auth/signin") == 2
    reports = list(tmp_path.glob("custom_inventory_*.html"))
    assert len(reports) == 1
    assert "inventory-summary (by-region)" in reports[0].read_text(encoding="utf-8")


def test_cost_dashboard(tmp_path):
    overrides = {"cost_dashboard": {
        "preset_think_time_min": 0, "preset_think_time_max": 0,
        "date_range_presets": [{"label": "30d", "days_back": 30}, {"label": "7d", "days_back": 7}],
    }}
    with mock_api() as (url, _hits):
        proc, rows = run_scenario("cost_dashboard", tmp_path, url, overrides)
    assert_clean(proc, rows)
    # Batch 2 runs for the unfiltered pass + 2 presets; batch 4 for the 2 presets.
    assert count(rows, "[Cost] cost-dashboard") == 1
    assert count(rows, "[Cost] cost-summary (by-account)") == 3
    assert count(rows, "[Cost] cost-by-charge-type") == 3
    assert count(rows, "[Cost] lookup-tag-keys") == 1
    assert count(rows, "[Cost] spend-trend-tab") == 2
    assert count(rows, "[Cost] resource-count") == 2
    assert count(rows, "Aggregated") == 2 + 2 + 6 * 3 + 1 + 3 * 2


def test_day2sdk_all_tags(tmp_path):
    tags = dict.fromkeys(
        ("tenant", "assessment", "user", "account", "resource", "health", "project", "security", "organization"),
        True,
    )
    overrides = {"day2sdk": {"tags": tags, "failures_log": ""}}
    env = {"DAY2SDK_ACCESS_KEY_1": "AKIDTEST", "DAY2SDK_SECRET_KEY_1": DAY2_SECRET}
    with mock_api() as (url, hits):
        proc, rows = run_scenario("day2sdk", tmp_path, url, overrides, env=env)
    assert_clean(proc, rows)
    assert ("POST", "/auth/signin") not in hits  # key-pair auth, no sign-in
    assert count(rows, "[Day2SDK] /tenants") == 1
    # Mock returns 2 tenants; every tenant-scoped call runs once per tenant.
    assert count(rows, "[Day2SDK] /tenants/{TenantId}") == 2
    assert count(rows, "[Day2SDK] .../assessments/{AssessmentId}/schedules") == 2
    assert count(rows, "[Day2SDK] /tenants/{TenantId}/accounts/{AccountId}/region-status") == 2
    assert count(rows, "[Day2SDK] /tenants/{TenantId}/security/services/{ServiceId}/summary") == 2
    assert count(rows, "[Day2SDK] /organizations") == 1
    # auth 2 + tenants 1 + categories 1 + per tenant (1 + 9 + 1 + 3 + 4 + 3 + 2 + 5) + orgs 1
    assert count(rows, "Aggregated") == 2 + 1 + 1 + 2 * 28 + 1


def test_journey_home_wafr_health(tmp_path):
    overrides = {
        "wafr": {"think_time_min": 0, "think_time_max": 0},
        "health": {"enabled": True, "mode": "appended"},
        "chat": {"enabled": False},
    }
    with mock_api() as (url, _hits):
        proc, rows = run_scenario("journey", tmp_path, url, overrides)
    assert_clean(proc, rows)
    assert count(rows, "[Auth] /auth/signin") == 1
    assert count(rows, "[HomePage] /subscription/product-catalog") == 3
    assert count(rows, "[HomePage] /org/organization/{id}") == 1
    assert count(rows, "[WAFR] /war-assessment/workload/{id}/findings-summary-by-severity (pillar=security)") == 2
    assert count(rows, "[WAFR] /war-assessment/workload/{id}/findings") == 3
    assert count(rows, "[Health] /health/api/v1/events/{id}/affected-resources") == 1
    # auth 2 + home (1+2+15+2+7) + wafr (1+28+3+5) + health (1+2+1+1+1+1+2)
    assert count(rows, "Aggregated") == 2 + 27 + 37 + 9


def test_journey_health_standalone_skips_home_and_wafr(tmp_path):
    overrides = {"health": {"enabled": True, "mode": "standalone"}, "chat": {"enabled": False}}
    with mock_api() as (url, _hits):
        proc, rows = run_scenario("journey", tmp_path, url, overrides)
    assert_clean(proc, rows)
    assert not any(name.startswith(("[HomePage]", "[WAFR]")) for name in rows)
    assert count(rows, "Aggregated") == 2 + 9
