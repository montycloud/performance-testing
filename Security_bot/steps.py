"""Security Bot flow steps. Each step takes a UserContext, mutates it, and logs its response."""

from __future__ import annotations

import csv
import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from http_client import SecurityBotClient, StepError, StepResponse
from run_logger import RunLogger, fingerprint
from settings import Settings, env

logger = logging.getLogger(__name__)


@dataclass
class UserContext:
    """Everything one simulated user owns. Never shared between users."""

    index: int
    name: str
    email: str
    password: str
    client: SecurityBotClient
    org_id: Optional[str] = None
    org_name: Optional[str] = None
    signin_token: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    tenant_token: Optional[str] = None
    findings: List[Dict[str, Any]] = field(default_factory=list)
    rescan_ids: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"user{self.index:02d}"

    def summary(self) -> Dict[str, Any]:
        return {
            "user": self.index,
            "name": self.name,
            "email": self.email,
            "org_id": self.org_id,
            "org_name": self.org_name,
            "signin_token_fp": fingerprint(self.signin_token),
            "tenant_token_fp": fingerprint(self.tenant_token),
            "finding_count": len(self.findings),
            "finding_ids": [f.get("Id") for f in self.findings],
            "rescan_ids": self.rescan_ids,
            "errors": self.errors,
        }


class OrgPool:
    """Hands out strictly unique org ids; pool size is the hard cap on concurrent users."""

    def __init__(self, orgs: List[Dict[str, Any]]):
        self._all = orgs
        self._available: Deque[Dict[str, Any]] = deque(orgs)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._all)

    @property
    def orgs(self) -> List[Dict[str, Any]]:
        return list(self._all)

    @property
    def root_org_id(self) -> Optional[str]:
        for org in self._all:
            if not org.get("ParentOrganizationId"):
                return org.get("ID")
        return None

    def acquire(self) -> Dict[str, Any]:
        with self._lock:
            if not self._available:
                raise RuntimeError(
                    "No organisations left to assign. Reduce run.users or add more orgs."
                )
            return self._available.popleft()

    def release(self, org: Dict[str, Any]) -> None:
        with self._lock:
            self._available.append(org)

    def find(self, reference: str) -> Dict[str, Any]:
        """Resolve a CSV org id, full name, or short Tenant label."""
        value = reference.strip()
        direct = [org for org in self._all if value in {org.get("ID"), org.get("Name")}]
        if not direct and value.lower().startswith("tenant "):
            direct = [org for org in self._all if org.get("Name", "").endswith(value)]
        if len(direct) != 1:
            raise ValueError(
                f"organization {reference!r} matched {len(direct)} entries in organizations.json"
            )
        return direct[0]


def load_org_pool(settings: Settings) -> OrgPool:
    path = settings.organizations_file
    if not path.exists():
        raise FileNotFoundError(f"organizations file not found: {path}")
    orgs = json.loads(path.read_text(encoding="utf-8"))
    if settings.run.get("exclude_root_org"):
        orgs = [o for o in orgs if o.get("ParentOrganizationId")]
    if not orgs:
        raise ValueError(f"no organisations available in {path}")
    return OrgPool(orgs)


def load_users(settings: Settings, count: int) -> List[Dict[str, str]]:
    """CSV rows if users.csv exists, otherwise the single .env fallback account."""
    path = settings.users_csv
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if (r.get("Email") or "").strip()]
        selected = rows[:count]
        missing = [r["Email"] for r in selected if not (r.get("Password") or "").strip()]
        if missing:
            raise ValueError(f"{path} has rows with a blank Password: {', '.join(missing)}")
        if len(rows) < count:
            raise ValueError(f"{path} has {len(rows)} usable rows but {count} users were requested")
        no_org = [r["Email"] for r in selected if not _org_reference(r)]
        if no_org:
            raise ValueError(
                f"{path} needs OrganizationId, Organization, or Tenant for: {', '.join(no_org)}"
            )
        return [
            {
                "Name": (r.get("Name") or r["Email"]).strip(),
                "Email": r["Email"].strip(),
                "Password": r["Password"].strip(),
                "Organization": _org_reference(r),
            }
            for r in selected
        ]

    email, password = env("ROOT_EMAIL"), env("ROOT_PASSWORD")
    if not email or not password:
        raise FileNotFoundError(
            f"{path} not found and ROOT_EMAIL/ROOT_PASSWORD are not set in .env.\n"
            f"  cp {path.parent / 'users.csv.example'} {path}   # then fill in passwords\n"
            f"  or set ROOT_EMAIL / ROOT_PASSWORD in .env for a single-user run"
        )
    if count > 1:
        raise ValueError(
            f"{count} users requested but only the .env fallback account is available. "
            f"Create {path} with one row per user."
        )
    return [{"Name": email, "Email": email, "Password": password, "Organization": "Monty Cloud"}]


def _org_reference(row: Dict[str, str]) -> str:
    for column in ("OrganizationId", "Organization", "Tenant"):
        value = (row.get(column) or "").strip()
        if value:
            return value
    return ""


def assign_organizations(users: List[Dict[str, str]], pool: OrgPool) -> List[Dict[str, str]]:
    """Attach and validate the organization chosen on each CSV row."""
    assigned_ids = set()
    for user in users:
        org = pool.find(user["Organization"])
        org_id = str(org["ID"])
        if org_id in assigned_ids:
            raise ValueError(f"organization {org.get('Name')!r} is assigned to more than one user")
        assigned_ids.add(org_id)
        user["OrganizationId"] = org_id
        user["OrganizationName"] = str(org.get("Name", ""))
    return users


def _check(resp: StepResponse, ctx: UserContext, what: str) -> None:
    if not resp.ok and not resp.body == {"dry_run": True}:
        ctx.errors.append(f"{what}: {resp.error}")
        raise StepError(f"{ctx.label} {what} failed - {resp.error}", resp)


# --------------------------------------------------------------------------- #
# Step 1 - sign in
# --------------------------------------------------------------------------- #
def step_signin(ctx: UserContext, settings: Settings, run_log: RunLogger) -> StepResponse:
    payload = {
        "Username": ctx.email,
        "Password": ctx.password,
        "MC_DEBUG_MODE": bool(settings.api["mc_debug_mode"]),
    }
    resp = ctx.client.post("/auth/signin", payload, name="/auth/signin")
    run_log.write_step(ctx.index, 1, "signin", resp.to_record(), ctx.org_id)
    _check(resp, ctx, "signin")

    body = resp.body or {}
    ctx.signin_token = body.get("Token")
    ctx.access_token = body.get("AccessToken")
    ctx.refresh_token = body.get("RefreshToken")
    if not ctx.client.dry_run and not ctx.signin_token:
        ctx.errors.append("signin: response contained no Token")
        raise StepError(f"{ctx.label} signin returned no Token", resp)

    logger.info(
        "%s signed in as %s (token=%s)", ctx.label, ctx.email, fingerprint(ctx.signin_token)
    )
    return resp


# --------------------------------------------------------------------------- #
# Step 2 - pick a unique organisation
# --------------------------------------------------------------------------- #
def step_select_org(ctx: UserContext, pool: OrgPool, run_log: RunLogger) -> Dict[str, Any]:
    if not ctx.org_id or not ctx.org_name:
        raise StepError(f"{ctx.label} has no assigned organization", StepResponse("", ""))
    org = {"ID": ctx.org_id, "Name": ctx.org_name}
    run_log.write_step(
        ctx.index,
        2,
        "select_org",
        {
            "source": "users.csv -> organizations.json",
            "pool_size": len(pool),
            "selected": org,
        },
        ctx.org_id,
    )
    logger.info("%s assigned org %s (%s)", ctx.label, ctx.org_name, ctx.org_id)
    return org


# --------------------------------------------------------------------------- #
# Step 3 - switch organisation context
# --------------------------------------------------------------------------- #
def step_switch_org(
    ctx: UserContext,
    settings: Settings,
    run_log: RunLogger,
    root_org_id: Optional[str] = None,
) -> StepResponse:
    if not ctx.org_id:
        raise StepError(f"{ctx.label} has no org assigned; run step 2 first", StepResponse("", ""))

    cookie_org = ctx.org_id
    if settings.auth.get("switch_cookie_org") == "root" and root_org_id:
        cookie_org = root_org_id

    resp = ctx.client.post(
        f"/auth/switch-context/organization/{ctx.org_id}",
        {},
        token=ctx.signin_token,
        cookies={"d2oid": cookie_org},
        name="/auth/switch-context/organization/[org_id]",
    )
    run_log.write_step(ctx.index, 3, "switch_org", resp.to_record(), ctx.org_id)
    _check(resp, ctx, "switch-context")

    status = (resp.body or {}).get("Status")
    if not ctx.client.dry_run and status != "Success":
        ctx.errors.append(f"switch-context: unexpected Status={status!r}")
        raise StepError(f"{ctx.label} switch-context returned Status={status!r}", resp)

    logger.info("%s switched context to %s (%s)", ctx.label, ctx.org_name, ctx.org_id)
    return resp


# --------------------------------------------------------------------------- #
# Step 4 - exchange for a token scoped to the switched tenant
# --------------------------------------------------------------------------- #
def step_refresh_token(ctx: UserContext, settings: Settings, run_log: RunLogger) -> StepResponse:
    source = settings.auth.get("refresh_token_source", "refresh_token")
    token_value = ctx.access_token if source == "access_token" else ctx.refresh_token
    if not token_value and not ctx.client.dry_run:
        ctx.errors.append(f"refresh-token: signin produced no {source}")
        raise StepError(f"{ctx.label} has no {source} to refresh with", StepResponse("", ""))

    resp = ctx.client.post(
        "/auth/refresh-token",
        {"RefreshToken": token_value},
        token=ctx.signin_token,
        cookies={"d2oid": ctx.org_id} if ctx.org_id else None,
        name="/auth/refresh-token",
    )
    run_log.write_step(
        ctx.index,
        4,
        "refresh_token",
        {**resp.to_record(), "refresh_token_source": source},
        ctx.org_id,
    )
    _check(resp, ctx, "refresh-token")

    body = resp.body or {}
    ctx.tenant_token = body.get("Token")
    if body.get("AccessToken"):
        ctx.access_token = body["AccessToken"]
    if not ctx.client.dry_run and not ctx.tenant_token:
        ctx.errors.append("refresh-token: response contained no Token")
        raise StepError(f"{ctx.label} refresh-token returned no Token", resp)

    logger.info(
        "%s got tenant token %s for %s (signin token was %s)",
        ctx.label,
        fingerprint(ctx.tenant_token),
        ctx.org_name,
        fingerprint(ctx.signin_token),
    )
    return resp


# --------------------------------------------------------------------------- #
# Step 5 - fetch findings for the tenant
# --------------------------------------------------------------------------- #
_FINDING_FIELDS = (
    "Id",
    "FindingId",
    "InsightId",
    "InsightStatus",
    "CheckName",
    "Title",
    "Severity",
    "ResourceType",
    "ResourceId",
    "AccountNumber",
    "RegionCode",
    "RescanInProgress",
)


def step_fetch_findings(
    ctx: UserContext,
    settings: Settings,
    run_log: RunLogger,
    limit: Optional[int] = None,
) -> StepResponse:
    limit = limit if limit is not None else int(settings.run["findings_limit"])
    # Compact separators keep the encoded Filters value identical to the portal's.
    filters = json.dumps({"Orgs": [ctx.org_id]}, separators=(",", ":"))
    configured_statuses = settings.run.get("findings_statuses", [])
    wanted = {str(status).upper() for status in configured_statuses if str(status).strip()}
    keyword_search = [
        {"Field": "InsightStatus", "Value": status}
        for status in sorted(wanted)
    ]

    resp = ctx.client.get(
        settings.insights_path(),
        token=ctx.tenant_token,
        cookies={"d2oid": ctx.org_id} if ctx.org_id else None,
        params={
            "Filters": filters,
            "Limit": limit,
            "KeywordSearch": json.dumps(keyword_search, separators=(",", ":")),
        },
        name="/bots/[bot_id]/insights",
    )
    run_log.write_step(ctx.index, 5, "fetch_findings", resp.to_record(), ctx.org_id)
    _check(resp, ctx, "fetch-findings")

    body = resp.body or {}
    items = body.get("Items") or []
    if wanted:
        items = [i for i in items if str(i.get("InsightStatus", "")).upper() in wanted]
    cap = int(settings.run.get("rescan_count") or 0)
    if cap > 0:
        items = items[:cap]

    ctx.findings = [
        {k: item.get(k) for k in _FINDING_FIELDS if k in item}
        for item in items
        if item.get("Id")
    ]
    if not ctx.client.dry_run and not ctx.findings:
        ctx.errors.append(
            f"fetch-findings: no {', '.join(sorted(wanted)) or 'any'} findings for org {ctx.org_id} "
            f"(Count={body.get('Count')}, returned={len(body.get('Items') or [])})"
        )
        raise StepError(f"{ctx.label} got 0 usable findings for {ctx.org_name}", resp)

    logger.info(
        "%s fetched %d eligible findings (%s) for %s (page=%d, total available=%s)",
        ctx.label,
        len(ctx.findings),
        ", ".join(sorted(wanted)) or "any status",
        ctx.org_name,
        len(body.get("Items") or []),
        body.get("Count"),
    )
    return resp


# --------------------------------------------------------------------------- #
# Step 6 - trigger a rescan per finding
# --------------------------------------------------------------------------- #
def _thread_spawn(calls: List[Any], max_workers: int) -> List[Any]:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(lambda fn: fn(), calls))


def step_trigger_rescan(
    ctx: UserContext,
    settings: Settings,
    run_log: RunLogger,
    spawn: Optional[Any] = None,
) -> List[StepResponse]:
    """One POST per finding - the API rejects more than one FindingId per request."""
    if not ctx.findings:
        raise StepError(f"{ctx.label} has no findings to rescan", StepResponse("", ""))

    scope = settings.rescan.get("scope", "FINDING")
    path = settings.rescan_path()
    cookies = {"d2oid": ctx.org_id} if ctx.org_id else None

    def make_call(finding_id: str):
        return lambda: ctx.client.post(
            path,
            {"RescanScope": scope, "FindingIds": [finding_id]},
            token=ctx.tenant_token,
            cookies=cookies,
            name="/bots/api/[bot_id]/rescan",
        )

    finding_ids = [f["Id"] for f in ctx.findings]
    calls = [make_call(fid) for fid in finding_ids]

    if settings.rescan.get("parallel", True) and len(calls) > 1:
        runner = spawn or _thread_spawn
        responses = runner(calls, int(settings.rescan.get("max_workers", 10)))
    else:
        responses = [fn() for fn in calls]

    results = []
    for finding_id, resp in zip(finding_ids, responses):
        rescan_id = (resp.body or {}).get("RescanId")
        if rescan_id:
            ctx.rescan_ids.append(rescan_id)
        elif not ctx.client.dry_run:
            ctx.errors.append(f"rescan[{finding_id}]: {resp.error or 'no RescanId returned'}")
        results.append(
            {"finding_id": finding_id, "rescan_id": rescan_id, **resp.to_record()}
        )

    run_log.write_step(
        ctx.index,
        6,
        "trigger_rescan",
        {
            "scope": scope,
            "requested": len(finding_ids),
            "succeeded": len(ctx.rescan_ids),
            "parallel": bool(settings.rescan.get("parallel", True)),
            "calls": results,
        },
        ctx.org_id,
    )

    logger.info(
        "%s triggered %d/%d rescans for %s",
        ctx.label,
        len(ctx.rescan_ids),
        len(finding_ids),
        ctx.org_name,
    )
    return responses
