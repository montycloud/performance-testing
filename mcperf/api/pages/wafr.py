"""WAFR page — workload list, then 3 batches against the first PENDING workload."""

from __future__ import annotations

import json
from collections.abc import Callable

from mcperf.locust.base_user import MontyCloudUser

P = "[WAFR]"
PILLARS = (
    "costOptimization", "security", "performance", "reliability", "operationalExcellence", "sustainability",
)
TASK_CATEGORIES = (
    "Compliance", "Security", "Remediation", "CustomActions", "EC2", "Storage", "PATCH-SCAN",
    "PATCH-INSTALL", "DESIRED-SERVER-STATE", "Volume", "BackupRestore",
)


def _filters(**f) -> str:
    return json.dumps(f)


def wafr_flow(user: MontyCloudUser, think: Callable[[], None]) -> None:
    """*think* is the pause before batches 2 and 3 (wafr think time)."""
    workloads = user.api_get(
        "/war-assessment/workloads", f"{P} /war-assessment/workloads",
        {"Filters": _filters(Keyword="", FTRRequest=False, Status="PENDING"), "Limit": 10, "PageNumber": 1},
    )
    summaries = workloads.get("WorkloadSummaries", []) if isinstance(workloads, dict) else []
    if not summaries:
        user.log.warning("[%s] No WAFR workloads returned; skipping WAFR batches 2–4.", user.label)
        return
    wid: str = summaries[0].get("WorkloadId", "")
    if not wid:
        user.log.warning("[%s] First workload has no WorkloadId; skipping WAFR batches 2–4.", user.label)
        return
    user.log.debug("[%s] WAFR WorkloadId = %s", user.label, wid)

    get = user.call
    w = f"/war-assessment/workload/{wid}"
    n = f"{P} /war-assessment/workload/{{id}}"

    def pillars():
        return [
            get(f"{w}/findings-summary-by-severity", f"{n}/findings-summary-by-severity (pillar={p})",
                {"Filters": _filters(Status="failed", Dimension="Pillar", PillarId=p)})
            for p in PILLARS
        ]

    def failed_summaries():
        return [
            get(f"{w}/findings-summary-by-severity", f"{n}/findings-summary-by-severity (ResourceType)",
                {"Filters": _filters(Status="Failed", Dimension="ResourceType")}),
            get(f"{w}/top-resources", f"{n}/top-resources", {"Filters": _filters(Status="Failed")}),
            get(f"{w}/top-checks", f"{n}/top-checks", {"Filters": _filters(Status="Failed")}),
        ]

    failed_workload_card = get(
        f"{w}/finding-summary", f"{n}/finding-summary (Status=Failed, Card=Workload)",
        {"Filters": _filters(Status="Failed", Card="Workload")},
    )

    think()
    # Batch 2 — 28 calls. The browser mounts several components twice, so the
    # second half repeats calls on purpose.
    user.run_batch([
        get("/war-assessment/workload_count_summary", f"{P} /war-assessment/workload_count_summary",
            {"Filters": _filters(Keyword="", FTRRequest=False)}),
        failed_workload_card,
        *pillars(),
        *failed_summaries(),
        get(f"{w}/resource-types", f"{n}/resource-types", {"Limit": 20, "Offset": 0}),
        get("/war-assessment/workload/lenses", f"{P} /war-assessment/workload/lenses"),
        get(f"{w}/finding-summary", f"{n}/finding-summary (Card=Workload)", {"Filters": _filters(Card="Workload")}),
        get(f"/war-assessment/api/workload/{wid}/schedules", f"{P} /war-assessment/api/workload/{{id}}/schedules"),
        get(w, n),
        get(f"{w}/lenses", f"{n}/lenses", {"Lens": "wellarchitected"}),
        get(f"{w}/finding-summary", f"{n}/finding-summary (Card=Lens, wellarchitected)",
            {"Filters": _filters(Card="Lens", Lens="wellarchitected")}),
        # second mount
        failed_workload_card,
        *pillars(),
        *failed_summaries(),
    ])

    think()
    # Batch 3
    findings = get(f"{w}/findings", f"{n}/findings", {"Limit": 25, "Offset": 0})
    user.run_batch([
        get("/task/", f"{P} /task/ (all categories)",
            {"Filters": _filters(TaskType="All", Category=list(TASK_CATEGORIES))}),
        findings,
        get(f"{w}/finding-count", f"{n}/finding-count (GroupBy=Title)",
            {"Limit": 25, "Offset": 0, "Filters": _filters(GroupBy="Title")}),
    ])

    # Batch 4
    user.run_batch([
        findings,
        get(f"{w}/finding-count", f"{n}/finding-count (GroupBy=Severity)",
            {"Limit": 25, "Offset": 0, "Filters": _filters(GroupBy="Severity")}),
        findings,
        get(f"{w}/milestones", f"{n}/milestones", {"PageNumber": 1, "Limit": 10}),
        get("/notifications", f"{P} /notifications", {"pageSize": 25, "pageNumber": 1}),
    ])
