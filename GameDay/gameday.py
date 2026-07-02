#!/usr/bin/env python3
"""
GameDay User Creation Tool
==========================
Reads a CSV of root/child user pairs, provisions each root user through the
MontyCloud API, then creates tenants under each root user.

Sub-commands:
  create-users    Provision root + child users from CSV (parallel, default 10 workers)
  create-tenants  Create org tenants for already-provisioned users

Usage:
  python gameday.py create-users  --csv sample_users.csv [--workers 10] [--dry-run]
  python gameday.py create-tenants --csv users_output_<ts>.csv [--workers 10] [--dry-run]

Setup:
  cp .env.example .env        # set CAPTCHA_CODE secret
  # Edit config.yaml          # set BASE_URL, workers, CSV path, etc.
  pip install -r requirements.txt
"""

import argparse
import csv
import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment / config
# ---------------------------------------------------------------------------

_ENV_FILE = Path(__file__).parent / ".env"
_CONFIG_FILE = Path(__file__).parent / "config.yaml"
load_dotenv(dotenv_path=_ENV_FILE)


def _load_config() -> Dict[str, Any]:
    """Load and validate config.yaml; abort with a clear message if missing."""
    if not _CONFIG_FILE.exists():
        print(
            f"ERROR: config.yaml not found at {_CONFIG_FILE}\n"
            f"  The file should be in the same directory as gameday.py.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _require_env(var: str) -> str:
    """Return the value of a .env variable; abort if it is unset or empty."""
    value = os.getenv(var, "").strip()
    if not value:
        print(
            f"ERROR: Required secret '{var}' is not set in .env.\n"
            f"  Copy .env.example → .env and fill in the value.\n"
            f"  Expected file: {_ENV_FILE}",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# Load config.yaml + .env
# ---------------------------------------------------------------------------

_cfg = _load_config()

# All non-secret config comes from config.yaml
BASE_URL: str = _cfg["api"]["base_url"].rstrip("/")
MC_DEBUG_MODE: bool = bool(_cfg["api"]["mc_debug_mode"])
PARALLEL_WORKERS: int = int(_cfg["users"]["parallel_workers"])
ROOT_DESIGNATION: str = _cfg["users"]["root_designation"]
_DEFAULT_CSV: str = _cfg["users"]["csv_file"]

# The captcha token is the only secret — name of its env var is in config.yaml
CAPTCHA_CODE: str = _require_env(_cfg["api"]["captcha_code_env_var"])

# Hardcoded role / org IDs (from PRD — same across all child users)
CHILD_ROLE_ID = "d4c81030-68e2-11ee-8c99-0242ac120002"

# Hardcoded feature settings for preferences and tenant creation
CUSTOMER_PREFERENCE_PAYLOAD: Dict[str, Any] = {
    "PreferenceType": "Features",
    "Preference": "AutomatedCloudOps",
    "OptionalFeatures": {"CUR": True, "COH": True},
}
TENANT_OPTIONAL_FEATURES: Dict[str, Any] = {"CUR": True, "COH": True}
TENANT_FEATURE = "AutomatedCloudOps"

# ---------------------------------------------------------------------------
# CSV column names
# ---------------------------------------------------------------------------

# Input columns
COL_ROOT_EMAIL = "Root Email"
COL_ROOT_NAME = "Root Name"
COL_MSP_ORG = "MSP Org Name"
COL_ROOT_PASSWORD = "Root Password"
COL_CHILD_EMAIL = "Child Email"
COL_CHILD_NAME = "Child Name"
COL_CHILD_PASSWORD = "Child Password"       # initial temp password used at creation
COL_CHILD_NEW_PASSWORD = "Child New Password"  # permanent password set via reset_temp_password
COL_TENANT1_NAME = "Child Tenant 1 Name"
COL_TENANT2_NAME = "Child Tenant 2 Name"

REQUIRED_COLUMNS = [
    COL_ROOT_EMAIL, COL_ROOT_NAME, COL_MSP_ORG, COL_ROOT_PASSWORD,
    COL_CHILD_EMAIL, COL_CHILD_NAME, COL_CHILD_PASSWORD, COL_CHILD_NEW_PASSWORD,
    COL_TENANT1_NAME, COL_TENANT2_NAME,
]

# Output columns added by create-users
# NOTE: Root JWT Token is intentionally NOT in output columns — tokens are
# never written to disk to prevent accidental checkins of secrets.
COL_ROOT_JWT = "Root JWT Token"   # used internally only; not persisted to CSV
COL_ROOT_ORG_ID = "Root Org ID"
COL_CHILD_USER_ID = "Child User ID"
COL_STATUS = "Status"
COL_ERROR = "Error"

# Output columns added by create-tenants
COL_TENANT1_ID = "Tenant 1 ID"
COL_TENANT2_ID = "Tenant 2 ID"
COL_TENANT_STATUS = "Tenant Status"
COL_TENANT_ERROR = "Tenant Error"

OUTPUT_COLUMNS = REQUIRED_COLUMNS + [
    COL_ROOT_ORG_ID, COL_CHILD_USER_ID,
    COL_STATUS, COL_ERROR,
]

TENANT_OUTPUT_COLUMNS = OUTPUT_COLUMNS + [
    COL_TENANT1_ID, COL_TENANT2_ID, COL_TENANT_STATUS, COL_TENANT_ERROR,
]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


def _post(
    path: str,
    payload: Dict[str, Any],
    token: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """POST to BASE_URL/path with optional Bearer token.

    Returns parsed JSON response. Raises requests.HTTPError on non-2xx.
    """
    url = f"{BASE_URL}{path}"
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["authorization"] = token

    if dry_run:
        logger.info("[DRY-RUN] POST %s  payload=%s", url, json.dumps(payload)[:120])
        return {}

    logger.debug("POST %s", url)
    resp = _session.post(url, json=payload, headers=headers, timeout=30)
    try:
        resp.raise_for_status()
    except requests.HTTPError:
        body = resp.text[:500]
        raise requests.HTTPError(
            f"POST {url} returned {resp.status_code}: {body}",
            response=resp,
        )
    return resp.json() if resp.text.strip() else {}


# ---------------------------------------------------------------------------
# API step functions
# ---------------------------------------------------------------------------


def step_signup(row: Dict[str, str], dry_run: bool = False) -> None:
    """Step 1 — Sign up root user."""
    payload = {
        "Name": row[COL_ROOT_NAME],
        "CompanyName": row[COL_MSP_ORG],
        "Email": row[COL_ROOT_EMAIL],
        "Password": row[COL_ROOT_PASSWORD],
        "Designation": ROOT_DESIGNATION,
        "CaptchaCode": CAPTCHA_CODE,
        "MC_DEBUG_MODE": MC_DEBUG_MODE,
    }
    _post("/auth/signup", payload, dry_run=dry_run)
    logger.info("[%s] Signup complete", row[COL_ROOT_EMAIL])


def step_signin_root(row: Dict[str, str], dry_run: bool = False) -> Tuple[str, str, str]:
    """Step 3 — Sign in as root user.

    Returns (jwt_token, org_id, refresh_token).
    org_id is parsed from the first Roles entry (Org field).
    refresh_token is used by step_refresh_token to obtain a fresh AccessToken.
    """
    payload = {
        "Username": row[COL_ROOT_EMAIL],
        "Password": row[COL_ROOT_PASSWORD],
        "MC_DEBUG_MODE": MC_DEBUG_MODE,
    }
    if dry_run:
        _post("/auth/signin", payload, dry_run=True)
        return ("DRY_RUN_TOKEN", "DRY_RUN_ORG_ID", "DRY_RUN_REFRESH_TOKEN")

    data = _post("/auth/signin", payload)
    token = data.get("Token", "")
    if not token:
        raise ValueError(f"signin returned no Token for {row[COL_ROOT_EMAIL]}")

    # Extract Org ID from first Roles entry
    roles = data.get("Roles", [])
    org_id = roles[0].get("Org", "*") if roles else "*"

    refresh_token = data.get("RefreshToken", "")
    logger.info("[%s] Signed in (org_id=%s)", row[COL_ROOT_EMAIL], org_id)
    return token, org_id, refresh_token


def step_refresh_token(
    token: str, refresh_token: str, dry_run: bool = False
) -> str:
    """Step 3b — Refresh the session to obtain a fresh AccessToken.

    Returns the AccessToken string, which is required by change-password.
    """
    payload = {"RefreshToken": refresh_token}
    if dry_run:
        _post("/auth/refresh-token", payload, token=token, dry_run=True)
        return "DRY_RUN_ACCESS_TOKEN"

    data = _post("/auth/refresh-token", payload, token=token)
    access_token = data.get("AccessToken", "")
    if not access_token:
        raise ValueError("refresh-token returned no AccessToken")
    logger.debug("Token refreshed successfully")
    return access_token


def step_change_password(
    row: Dict[str, str], token: str, access_token: str, dry_run: bool = False
) -> None:
    """Step 4 — Change root user password."""
    payload = {
        "PreviousPassword": row[COL_ROOT_PASSWORD],
        "ProposedPassword": row[COL_ROOT_PASSWORD],  # same password kept; update CSV if rotation needed
        "AccessToken": access_token,
        "UserEmail": row[COL_ROOT_EMAIL],
    }
    _post("/auth/change-password", payload, token=token, dry_run=dry_run)
    logger.info("[%s] Password change complete", row[COL_ROOT_EMAIL])


def step_customer_preference(
    row: Dict[str, str], token: str, dry_run: bool = False
) -> None:
    """Step 4b — Set customer preference."""
    _post("/org/customer-preference", CUSTOMER_PREFERENCE_PAYLOAD, token=token, dry_run=dry_run)
    logger.info("[%s] Customer preference set", row[COL_ROOT_EMAIL])


def step_subscription(
    row: Dict[str, str], token: str, dry_run: bool = False
) -> None:
    """Step 5 — Activate Trial subscription."""
    _post("/subscription", {"Mode": "Trial"}, token=token, dry_run=dry_run)
    logger.info("[%s] Subscription activated", row[COL_ROOT_EMAIL])


def step_submit_support_request(
    row: Dict[str, str], token: str, dry_run: bool = False
) -> None:
    """Step 6 — Submit customer support request."""
    _post("/customersupport/submitrequest/", {}, token=token, dry_run=dry_run)
    logger.info("[%s] Support request submitted", row[COL_ROOT_EMAIL])


def step_create_child_user(
    row: Dict[str, str], token: str, org_id: str, dry_run: bool = False
) -> str:
    """Step 7 — Create child user under root's org.

    Returns UserId of the created child user.
    """
    payload = {
        "Permissions": [
            {"Role": CHILD_ROLE_ID, "Dept": "*", "Org": org_id}
        ],
        "Name": row[COL_CHILD_NAME],
        "Email": row[COL_CHILD_EMAIL],
        "Password": row[COL_CHILD_PASSWORD],
        "Description": "",
    }
    if dry_run:
        _post("/auth/user", payload, token=token, dry_run=True)
        return "DRY_RUN_USER_ID"

    data = _post("/auth/user", payload, token=token)
    user_id = data.get("UserId", "")
    if not user_id:
        raise ValueError(f"create child user returned no UserId for {row[COL_CHILD_EMAIL]}")
    logger.info("[%s] Child user created (UserId=%s)", row[COL_CHILD_EMAIL], user_id)
    return user_id


def step_signin_child(row: Dict[str, str], dry_run: bool = False) -> str:
    """Step 8 — Sign in as child user to obtain the session token.

    Returns the Session string needed for password reset.
    """
    payload = {
        "Username": row[COL_CHILD_EMAIL],
        "Password": row[COL_CHILD_PASSWORD],
        "MC_DEBUG_MODE": MC_DEBUG_MODE,
    }
    if dry_run:
        _post("/auth/signin", payload, dry_run=True)
        return "DRY_RUN_SESSION"

    data = _post("/auth/signin", payload)
    session = data.get("Session", "")
    if not session:
        raise ValueError(
            f"child signin returned no Session for {row[COL_CHILD_EMAIL]}. "
            f"ChallengeName={data.get('ChallengeName')}"
        )
    logger.info("[%s] Child signin complete", row[COL_CHILD_EMAIL])
    return session


def step_reset_child_password(
    row: Dict[str, str], session: str, dry_run: bool = False
) -> None:
    """Step 9 — Reset child user's temporary password to the permanent new password."""
    payload = {
        "Session": session,
        "Username": row[COL_CHILD_EMAIL],
        "Password": row[COL_CHILD_NEW_PASSWORD],
        "CaptchaCode": CAPTCHA_CODE,
    }
    _post("/auth/reset_temp_password", payload, dry_run=dry_run)
    logger.info("[%s] Child password reset complete", row[COL_CHILD_EMAIL])


# ---------------------------------------------------------------------------
# Per-row provisioning logic
# ---------------------------------------------------------------------------


def provision_user(
    row: Dict[str, str],
    org_id_hint: Optional[str],
    dry_run: bool = False,
) -> Dict[str, str]:
    """Run steps 3–9 for a single root user row (called after email verification).

    'row' already has Root Email / Child Email etc. populated.
    'org_id_hint' is unused — org_id is obtained fresh from signin.

    Returns a dict of output column values to merge into the result row.
    """
    result: Dict[str, str] = {
        COL_ROOT_JWT: "",
        COL_ROOT_ORG_ID: "",
        COL_CHILD_USER_ID: "",
        COL_STATUS: "FAILED",
        COL_ERROR: "",
    }
    try:
        # Step 3: Signin as root
        token, org_id, refresh_token = step_signin_root(row, dry_run=dry_run)
        result[COL_ROOT_JWT] = token
        result[COL_ROOT_ORG_ID] = org_id

        # Step 3b: Refresh token to get AccessToken for change-password
        access_token = step_refresh_token(token, refresh_token, dry_run=dry_run)

        # Step 4: Change password
        step_change_password(row, token, access_token, dry_run=dry_run)

        # Step 4b: Customer preference
        step_customer_preference(row, token, dry_run=dry_run)

        # Step 5: Subscription
        step_subscription(row, token, dry_run=dry_run)

        # Step 6: Support request
        step_submit_support_request(row, token, dry_run=dry_run)

        # Step 7: Create child user
        child_user_id = step_create_child_user(row, token, org_id, dry_run=dry_run)
        result[COL_CHILD_USER_ID] = child_user_id

        # Step 8: Sign in child to get session
        session = step_signin_child(row, dry_run=dry_run)

        # Step 9: Reset child password
        step_reset_child_password(row, session, dry_run=dry_run)

        result[COL_STATUS] = "SUCCESS"
        logger.info("[%s] Provisioning complete", row[COL_ROOT_EMAIL])

    except Exception as exc:
        result[COL_ERROR] = str(exc)
        logger.error("[%s] FAILED: %s", row[COL_ROOT_EMAIL], exc)

    return result


# ---------------------------------------------------------------------------
# CSV I/O helpers
# ---------------------------------------------------------------------------


def load_csv(csv_path: Path) -> List[Dict[str, str]]:
    """Load CSV into list of row dicts. Validates required columns present."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        raise ValueError(f"CSV file is empty: {csv_path}")

    missing = [c for c in REQUIRED_COLUMNS if c not in rows[0]]
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")

    return rows


def write_output(rows: List[Dict[str, str]], output_path: Path, columns: List[str]) -> None:
    """Write result rows to a CSV file, filling missing columns with empty string."""
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})
    logger.info("CSV written → %s", output_path)


def write_json(rows: List[Dict[str, str]], json_path: Path) -> None:
    """Write result rows to a JSON file."""
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    logger.info("JSON written → %s", json_path)


def output_paths(suffix: str) -> Tuple[Path, Path]:
    """Generate timestamped output paths in the same directory as this script."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = Path(__file__).parent
    return base / f"users_{suffix}_{ts}.csv", base / f"results_{suffix}_{ts}.json"


# ---------------------------------------------------------------------------
# Thread-safe result accumulator
# ---------------------------------------------------------------------------

_lock = threading.Lock()


def _merge_result(
    row: Dict[str, str], update: Dict[str, str]
) -> Dict[str, str]:
    """Return a new row dict with update merged in (thread-safe copy)."""
    merged = dict(row)
    merged.update(update)
    return merged


# ---------------------------------------------------------------------------
# create-users command
# ---------------------------------------------------------------------------


def cmd_create_users(args: argparse.Namespace) -> None:
    """Provision root + child users from the input CSV.

    Workflow:
      For each batch of PARALLEL_WORKERS rows:
        1. Sign up all rows in the batch (parallel)
        2. Pause and prompt operator to verify emails
        3. Run steps 3-9 for all rows in the batch (parallel)
      Write full output CSV + JSON when done.

    Re-running is idempotent: rows with Status=SUCCESS are skipped.
    """
    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        # Prefer path relative to CWD; fall back to script directory
        if not csv_path.exists():
            csv_path = Path(__file__).parent / csv_path

    workers: int = args.workers or PARALLEL_WORKERS
    dry_run: bool = args.dry_run

    logger.info("Loading CSV: %s", csv_path)
    rows = load_csv(csv_path)

    # Detect if this is a re-run of an output CSV (has Status column already)
    has_status = COL_STATUS in rows[0]

    # Filter out already-succeeded rows
    pending = []
    skipped = []
    for row in rows:
        if has_status and row.get(COL_STATUS) == "SUCCESS":
            skipped.append(row)
            logger.info("[%s] Skipping (already SUCCESS)", row[COL_ROOT_EMAIL])
        else:
            pending.append(row)

    if not pending:
        logger.info("All rows already completed. Nothing to do.")
        return

    logger.info(
        "%d rows to process (%d skipped), %d parallel workers%s",
        len(pending),
        len(skipped),
        workers,
        " [DRY-RUN]" if dry_run else "",
    )

    # Results will accumulate here (order preserved via enumerate)
    results: List[Optional[Dict[str, str]]] = [None] * len(pending)

    # Process in batches of 'workers'
    batch_num = 0
    for batch_start in range(0, len(pending), workers):
        batch = pending[batch_start : batch_start + workers]
        batch_num += 1
        logger.info(
            "=== Batch %d: rows %d-%d ===",
            batch_num,
            batch_start + 1,
            batch_start + len(batch),
        )

        # --- Step 1: Sign up all users in batch (parallel) ---
        logger.info("Step 1: Signing up %d users in batch %d...", len(batch), batch_num)
        signup_futures = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for i, row in enumerate(batch):
                idx = batch_start + i
                fut = pool.submit(step_signup, row, dry_run)
                signup_futures[fut] = (idx, row)

            for fut in as_completed(signup_futures):
                idx, row = signup_futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    logger.error("[%s] Signup failed: %s", row[COL_ROOT_EMAIL], exc)
                    results[idx] = _merge_result(
                        row,
                        {COL_STATUS: "FAILED", COL_ERROR: f"signup: {exc}",
                         COL_ROOT_ORG_ID: "", COL_CHILD_USER_ID: ""},
                    )

        # Determine which rows had successful signup
        signup_ok = [
            (batch_start + i, row)
            for i, row in enumerate(batch)
            if results[batch_start + i] is None  # not yet failed
        ]

        if not signup_ok:
            logger.warning("No successful signups in batch %d; skipping.", batch_num)
            continue

        # --- Manual gate: email verification ---
        if not dry_run:
            print(
                f"\n{'='*60}\n"
                f"  MANUAL STEP REQUIRED — Batch {batch_num}\n"
                f"{'='*60}\n"
                f"  {len(signup_ok)} user(s) just signed up:\n"
            )
            for _, row in signup_ok:
                print(f"    • {row[COL_ROOT_EMAIL]}")
            print(
                "\n  Please verify their email addresses in the MontyCloud\n"
                "  console or via the verification emails, then press Enter\n"
                "  to continue with the rest of the provisioning steps.\n"
            )
            input("  Press Enter when email verification is complete > ")
            print()
        else:
            logger.info("[DRY-RUN] Skipping email verification gate for batch %d", batch_num)

        # --- Steps 3-9: Parallel provisioning after verification ---
        logger.info(
            "Steps 3-9: Provisioning %d users in batch %d...", len(signup_ok), batch_num
        )
        provision_futures = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for idx, row in signup_ok:
                fut = pool.submit(provision_user, row, None, dry_run)
                provision_futures[fut] = (idx, row)

            for fut in as_completed(provision_futures):
                idx, row = provision_futures[fut]
                try:
                    update = fut.result()
                except Exception as exc:
                    update = {
                        COL_STATUS: "FAILED",
                        COL_ERROR: str(exc),
                        COL_ROOT_ORG_ID: "",
                        COL_CHILD_USER_ID: "",
                    }
                with _lock:
                    results[idx] = _merge_result(row, update)

    # Combine skipped + processed results, preserving original CSV order
    # skipped rows go first (they were removed from pending), then pending results
    final_rows: List[Dict[str, str]] = list(skipped) + [
        r if r is not None else pending[i]  # fallback for any uncaptured failure
        for i, r in enumerate(results)
    ]

    # Write outputs
    out_csv, out_json = output_paths("output")
    write_output(final_rows, out_csv, OUTPUT_COLUMNS)
    write_json(final_rows, out_json)

    # Summary
    success = sum(1 for r in final_rows if r.get(COL_STATUS) == "SUCCESS")
    failed = sum(1 for r in final_rows if r.get(COL_STATUS) == "FAILED")
    logger.info(
        "Done. %d succeeded, %d failed, %d skipped (already done).",
        success,
        failed,
        len(skipped),
    )


# ---------------------------------------------------------------------------
# create-tenants command
# ---------------------------------------------------------------------------


def step_create_tenant(
    token: str,
    tenant_name: str,
    description: str,
    owner_user_id: str,
    dry_run: bool = False,
) -> str:
    """Create a single org/tenant. Returns TenantId."""
    payload: Dict[str, Any] = {
        "Name": tenant_name,
        "Description": description,
        "Owner": owner_user_id,
        "CategoryId": None,
        "Feature": TENANT_FEATURE,
        "OptionalFeatures": TENANT_OPTIONAL_FEATURES,
        "TenantType": None,
        "AdditionalDetails": {
            "IndustryVertical": None,
            "AddressLine1": None,
            "AddressLine2": None,
            "City": None,
            "State": None,
            "Country": None,
            "ZipCode": None,
            "Website": None,
            "CustomerDataUniversalNumberSystem": None,
            "ContactFirstName": None,
            "ContactLastName": None,
            "ContactTitle": None,
            "ContactEmail": None,
            "ContactPhone": None,
        },
    }
    if dry_run:
        _post("/org/organization/", payload, token=token, dry_run=True)
        return "DRY_RUN_TENANT_ID"

    data = _post("/org/organization/", payload, token=token)
    tenant_id = data.get("OrgId", data.get("Id", data.get("TenantId", "")))
    logger.info("Tenant '%s' created (id=%s)", tenant_name, tenant_id)
    return tenant_id


def provision_tenants_for_row(
    row: Dict[str, str], dry_run: bool = False
) -> Dict[str, str]:
    """Create both tenants for a single row. Returns update dict.

    Re-authenticates using credentials from the CSV row — no token is read
    from disk, eliminating any risk of persisted secrets.
    """
    result: Dict[str, str] = {
        COL_TENANT1_ID: "",
        COL_TENANT2_ID: "",
        COL_TENANT_STATUS: "FAILED",
        COL_TENANT_ERROR: "",
    }
    owner = row.get(COL_CHILD_USER_ID, "")
    msp_name = row.get(COL_MSP_ORG, "")

    if not owner:
        result[COL_TENANT_ERROR] = "Missing Child User ID — run create-users first"
        return result

    try:
        # Re-authenticate to get a fresh token — never read from CSV/disk
        token, _, _ = step_signin_root(row, dry_run=dry_run)

        tenant1_name = row.get(COL_TENANT1_NAME, "").strip()
        tenant2_name = row.get(COL_TENANT2_NAME, "").strip()

        if tenant1_name:
            result[COL_TENANT1_ID] = step_create_tenant(
                token=token,
                tenant_name=tenant1_name,
                description=f"Tenant under {msp_name}",
                owner_user_id=owner,
                dry_run=dry_run,
            )

        if tenant2_name:
            result[COL_TENANT2_ID] = step_create_tenant(
                token=token,
                tenant_name=tenant2_name,
                description=f"Tenant under {msp_name}",
                owner_user_id=owner,
                dry_run=dry_run,
            )

        result[COL_TENANT_STATUS] = "SUCCESS"
        logger.info("[%s] Tenants created", row[COL_ROOT_EMAIL])

    except Exception as exc:
        result[COL_TENANT_ERROR] = str(exc)
        logger.error("[%s] Tenant creation FAILED: %s", row[COL_ROOT_EMAIL], exc)

    return result


def cmd_create_tenants(args: argparse.Namespace) -> None:
    """Create org tenants for already-provisioned users.

    Reads the output CSV from create-users, creates tenants in parallel,
    and writes updated CSV + JSON.
    """
    csv_path = Path(args.csv)
    if not csv_path.is_absolute():
        # Prefer path relative to CWD; fall back to script directory
        if not csv_path.exists():
            csv_path = Path(__file__).parent / csv_path

    workers: int = args.workers or PARALLEL_WORKERS
    dry_run: bool = args.dry_run

    logger.info("Loading CSV: %s", csv_path)
    rows = load_csv(csv_path)

    # Filter to SUCCESS rows only (tenants can only be created after user provisioning)
    eligible = [r for r in rows if r.get(COL_STATUS) == "SUCCESS"]
    ineligible = [r for r in rows if r.get(COL_STATUS) != "SUCCESS"]

    if not eligible:
        logger.error(
            "No rows with Status=SUCCESS found. Run create-users first."
        )
        sys.exit(1)

    # Skip rows where tenants were already created
    pending = [r for r in eligible if r.get(COL_TENANT_STATUS) != "SUCCESS"]
    already_done = [r for r in eligible if r.get(COL_TENANT_STATUS) == "SUCCESS"]

    logger.info(
        "%d rows for tenant creation (%d skipped, %d ineligible)%s",
        len(pending),
        len(already_done),
        len(ineligible),
        " [DRY-RUN]" if dry_run else "",
    )

    results: List[Dict[str, str]] = []
    futures_map = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row in pending:
            fut = pool.submit(provision_tenants_for_row, row, dry_run)
            futures_map[fut] = row

        for fut in as_completed(futures_map):
            row = futures_map[fut]
            try:
                update = fut.result()
            except Exception as exc:
                update = {
                    COL_TENANT_STATUS: "FAILED",
                    COL_TENANT_ERROR: str(exc),
                    COL_TENANT1_ID: "",
                    COL_TENANT2_ID: "",
                }
            with _lock:
                results.append(_merge_result(row, update))

    final_rows = ineligible + already_done + results

    out_csv, out_json = output_paths("tenants")
    write_output(final_rows, out_csv, TENANT_OUTPUT_COLUMNS)
    write_json(final_rows, out_json)

    success = sum(1 for r in results if r.get(COL_TENANT_STATUS) == "SUCCESS")
    failed = sum(1 for r in results if r.get(COL_TENANT_STATUS) == "FAILED")
    logger.info("Done. %d succeeded, %d failed.", success, failed)


# ---------------------------------------------------------------------------
# Entry point / argparse
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="GameDay User Creation Tool — provision MontyCloud root/child users in parallel",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Provision all users in sample_users.csv with 10 parallel workers
  python gameday.py create-users --csv sample_users.csv --workers 10

  # Dry-run: log API calls without executing them
  python gameday.py create-users --csv sample_users.csv --dry-run

  # Create tenants after users are provisioned
  python gameday.py create-tenants --csv users_output_20260701_120000.csv

  # Create tenants with 5 workers
  python gameday.py create-tenants --csv users_output_20260701_120000.csv --workers 5
        """,
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    subparsers.required = True

    # --- create-users sub-command ---
    p_users = subparsers.add_parser(
        "create-users",
        help="Provision root + child users from CSV",
        description=(
            "Sign up root users, prompt for email verification, then run all "
            "remaining provisioning steps in parallel. Re-running is safe: rows "
            "already marked Status=SUCCESS are skipped."
        ),
    )
    p_users.add_argument(
        "--csv",
        default=_DEFAULT_CSV,
        metavar="FILE",
        help=f"Path to input CSV (default: csv_file in config.yaml = '{_DEFAULT_CSV}')",
    )
    p_users.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=f"Parallel workers (default: $PARALLEL_WORKERS or {PARALLEL_WORKERS})",
    )
    p_users.add_argument(
        "--dry-run",
        action="store_true",
        help="Log API calls without executing them; skip email verification gate",
    )
    p_users.set_defaults(func=cmd_create_users)

    # --- create-tenants sub-command ---
    p_tenants = subparsers.add_parser(
        "create-tenants",
        help="Create org tenants for already-provisioned users",
        description=(
            "Reads the output CSV from create-users (must have Status=SUCCESS rows "
            "with Root JWT Token and Child User ID populated) and creates the org "
            "tenants listed in the Tenant columns."
        ),
    )
    p_tenants.add_argument(
        "--csv",
        required=True,
        metavar="FILE",
        help="Path to the output CSV produced by create-users",
    )
    p_tenants.add_argument(
        "--workers",
        type=int,
        default=None,
        metavar="N",
        help=f"Parallel workers (default: $PARALLEL_WORKERS or {PARALLEL_WORKERS})",
    )
    p_tenants.add_argument(
        "--dry-run",
        action="store_true",
        help="Log API calls without executing them",
    )
    p_tenants.set_defaults(func=cmd_create_tenants)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
