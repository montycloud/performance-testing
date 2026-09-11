#!/usr/bin/env python3
"""
Shared helpers for MontyCloud Locust performance tests
=========================================================
Extracted from locustfile.py so both it and cost_dashboard_locustfile.py can
reuse the same config/CSV bootstrap, sign-in, authenticated-GET, and
parallel-batch-runner logic without duplicating it.

Nothing in this module reads a specific config.yaml/cost_dashboard_config.yaml
directly — every function takes its inputs as parameters so each locustfile
stays in charge of its own config file and module-level constants.
"""

import csv
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import gevent
import gevent.pool
import yaml


# ---------------------------------------------------------------------------
# Config / CSV bootstrap
# ---------------------------------------------------------------------------


def load_config(config_file: Path) -> Dict[str, Any]:
    """Load a YAML config file, raising a clear error if it's missing."""
    if not config_file.exists():
        raise FileNotFoundError(
            f"Config file not found at {config_file}. "
            "See config.yaml in this directory for a template."
        )
    with open(config_file, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_users(csv_path: Path, limit: int, logger) -> List[Dict[str, str]]:
    """Load up to *limit* rows from a users CSV (columns: Name, Email, Password, ...)."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Users CSV not found: {csv_path}")
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"Users CSV is empty: {csv_path}")
    users = rows[:limit]
    logger.info(
        "Loaded %d user(s) from %s  [user_count limit = %d]",
        len(users), csv_path, limit,
    )
    return users


class UserClaimer:
    """Thread-safe round-robin credential dispenser over a fixed user list."""

    def __init__(self, users: List[Dict[str, str]]):
        if not users:
            raise ValueError("UserClaimer requires a non-empty users list.")
        self._users = users
        self._lock = threading.Lock()
        self._index = 0

    def claim(self) -> Dict[str, str]:
        """Return the next user credential row, cycling through the list."""
        with self._lock:
            idx = self._index % len(self._users)
            self._index += 1
        return self._users[idx]


# ---------------------------------------------------------------------------
# Misc small helpers
# ---------------------------------------------------------------------------


def redact_ws_url(url: str) -> str:
    """Strip the JWT out of a WS URL before it ever reaches a log line."""
    return re.sub(r"(Authorization=)[^&]+", r"\1<redacted>", url)


def run_batch(callables: List[Callable]) -> None:
    """Spawn all *callables* as gevent greenlets and join (wait for all to finish).

    This is what makes calls within one "batch" fire in parallel while batches
    themselves stay sequential (caller awaits run_batch() before starting the
    next one).
    """
    group = gevent.pool.Group()
    for fn in callables:
        group.spawn(fn)
    group.join()


# ---------------------------------------------------------------------------
# Auth / HTTP helpers
# ---------------------------------------------------------------------------


def signin(
    client,
    creds: Dict[str, str],
    mc_debug_mode: bool,
    timeout: int,
    logger,
    auth_prefix: str = "[Auth]",
) -> Tuple[Optional[str], str]:
    """POST /auth/signin then GET /auth/user. Returns (token, organization_id).

    token is None on failure; organization_id is "" if it could not be
    determined (either signin failed, or /auth/user didn't return one).
    """
    email = creds.get("Email", "")
    # Use "New Password" (permanent, post-reset); fall back to "Password".
    password = creds.get("New Password") or creds.get("Password", "")

    with client.post(
        "/auth/signin",
        json={
            "Username": email,
            "Password": password,
            "MC_DEBUG_MODE": mc_debug_mode,
        },
        name=f"{auth_prefix} /auth/signin",
        catch_response=True,
        timeout=timeout,
    ) as resp:
        if resp.status_code != 200:
            resp.failure(f"HTTP {resp.status_code}")
            logger.error(
                "[%s] signin failed: HTTP %d — %s",
                email, resp.status_code, resp.text[:300],
            )
            return None, ""
        try:
            data = resp.json() or {}
        except Exception:
            resp.failure("Signin response is not valid JSON")
            logger.error("[%s] signin response is not JSON", email)
            return None, ""

        token = data.get("Token", "")
        if not token:
            resp.failure("Signin response missing Token")
            logger.error("[%s] signin response missing 'Token' field", email)
            return None, ""

        resp.success()
        logger.info("[%s] signed in successfully", email)

    body: Dict[str, Any] = {}
    with client.get(
        "/auth/user",
        headers={"authorization": token},
        name=f"{auth_prefix} /auth/user",
        catch_response=True,
        timeout=timeout,
    ) as resp:
        if resp.status_code != 200:
            resp.failure(f"HTTP {resp.status_code}")
            logger.error("[%s] GET /auth/user failed: HTTP %d", email, resp.status_code)
            return token, ""
        try:
            body = resp.json() or {}
            resp.success()
        except Exception:
            resp.failure("GET /auth/user response is not valid JSON")
            return token, ""

    org_id = body.get("OrganizationId", "")
    if not org_id:
        logger.warning("[%s] GET /auth/user returned no OrganizationId", email)
    else:
        logger.info("[%s] OrganizationId = %s", email, org_id)
    return token, org_id


def authenticated_get(
    client,
    token: Optional[str],
    path: str,
    name: str,
    timeout: int,
    logger,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Issue an authenticated GET request.

    Returns the parsed JSON body (or {} on error). Records the request in
    Locust stats using *name* for grouping.
    """
    headers: Dict[str, str] = {}
    if token:
        headers["authorization"] = token

    body: Dict[str, Any] = {}
    with client.get(
        path,
        headers=headers,
        params=params,
        name=name,
        catch_response=True,
        timeout=timeout,
    ) as resp:
        if resp.status_code >= 400:
            resp.failure(f"HTTP {resp.status_code}")
            logger.warning("FAIL  %-60s  →  HTTP %d", name, resp.status_code)
        else:
            try:
                body = resp.json() or {}
                resp.success()
            except Exception:
                resp.failure("Response is not valid JSON")
                body = {}
    return body
