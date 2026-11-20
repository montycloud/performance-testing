"""Home Page — 7 sequential batches, replaying the browser waterfall.

Batches 1-3 split out org/auth/preference bootstrap calls (previously
duplicated across batches); Batch 4's landing burst drops
/customeraccount/logo (frequently non-JSON, pollutes failure stats) and the
duplicate /auth/organizations, /org/organizations/, /subscription/product-catalog
calls, which are now isolated in Batch 5.
"""

from __future__ import annotations

import json

from mcperf.locust.base_user import MontyCloudUser

P = "[HomePage]"


def homepage_flow(user: MontyCloudUser) -> None:
    get = user.call
    org = user.org_id

    # Batch 1
    user.run_batch([get("/org/organizations/", f"{P} /org/organizations/")])

    # Batch 2
    user.run_batch([get("/auth/user", f"{P} /auth/user")])

    # Batch 3
    user.run_batch([get("/org/customer-preference", f"{P} /org/customer-preference")])

    # Batch 4 — main landing burst after org load (8 calls)
    user.run_batch([
        get("/auth/organizations", f"{P} /auth/organizations"),
        get(
            "/customeraccount/api/v1/faa-roles/configured-accounts",
            f"{P} /customeraccount/api/v1/faa-roles/configured-accounts",
            {"PageNumber": 1, "PageSize": 500},
        ),
        get("/notifications", f"{P} /notifications", {"pageNumber": 1, "pageSize": 25}),
        get("/notifications", f"{P} /notifications", {"pageSize": 25, "pageNumber": 1}),
        get("/resource/regions", f"{P} /resource/regions", {"CloudProvider": "Azure"}),
        get("/auth/user-data", f"{P} /auth/user-data"),
        get("/auth/portal/users", f"{P} /auth/portal/users"),
        get(f"/org/organization/{org}", f"{P} /org/organization/{{id}}"),
    ])

    # Batch 5 — endpoints not covered by batches above, isolated to keep
    # duplicate /subscription/product-catalog calls out of Batch 4.
    user.run_batch([
        get("/v1/mc/list-regions", f"{P} /v1/mc/list-regions"),
        get("/subscription/product-catalog", f"{P} /subscription/product-catalog"),
    ])

    # Batch 6 — customer accounts per cloud
    account_filter = json.dumps({"Orgs": [org], "Detail": True, "ResourceType": "CustomerAccount"})
    user.run_batch([
        get("/org/resources", f"{P} /org/resources ({cloud} CustomerAccount)",
            {"Filters": account_filter, "CloudProvider": cloud})
        for cloud in ("AWS", "Azure")
    ])

    # Batch 7 — 7 widget calls
    user.run_batch([
        get("/cost/cost-dashboard", f"{P} /cost/cost-dashboard", {"CloudProvider": "AWS"}),
        get("/cost/cost-dashboard/lookup", f"{P} /cost/cost-dashboard/lookup", {"CloudProvider": "AWS"}),
        get(
            "/optimizations/api/v1/aws/cost/opportunities/Saving/service-type/top-n",
            f"{P} /optimizations/api/v1/aws/cost/opportunities/top-n",
            {"Tenure": "Monthly", "Count": 5, "PaymentTerm": "ri_sp_1y_all_upf"},
        ),
        get(
            "/optimizations/api/v1/aws/cost/overview-summary",
            f"{P} /optimizations/api/v1/aws/cost/overview-summary",
            {"Tenure": "Monthly", "PaymentTerm": "ri_sp_1y_all_upf"},
        ),
        get("/org/resource-governance-summary", f"{P} /org/resource-governance-summary",
            {"CloudProvider": "AWS"}),
        get("/org/inventory-summary", f"{P} /org/inventory-summary (by-region)",
            {"CloudProvider": "AWS", "SummaryType": "by-region"}),
        get("/org/inventory-summary", f"{P} /org/inventory-summary (by-resourcetype)",
            {"CloudProvider": "AWS", "SummaryType": "by-resourcetype"}),
    ])
