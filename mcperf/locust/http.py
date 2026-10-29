"""Sign-in and GET helpers for Locust's HttpSession (they rely on catch_response=True).

Request names are prefixed with a section tag such as "[Auth]" or "[Inventory]";
mcperf.reporting groups report rows by that prefix.
"""

from __future__ import annotations

import logging
from typing import Any

from mcperf.core.credentials import password_for
from mcperf.core.redact import redact_headers

_default_logger = logging.getLogger(__name__)


def signin(
    client,
    creds: dict[str, str],
    mc_debug_mode: bool,
    timeout: int,
    log: logging.Logger | None = None,
    auth_prefix: str = "[Auth]",
) -> tuple[str | None, str]:
    """POST /auth/signin then GET /auth/user. Returns (token, organization_id).

    token is None on failure; organization_id is "" if it could not be
    determined (either signin failed, or /auth/user didn't return one).
    """
    log = log or _default_logger
    email = creds.get("Email", "")

    with client.post(
        "/auth/signin",
        json={"Username": email, "Password": password_for(creds), "MC_DEBUG_MODE": mc_debug_mode},
        name=f"{auth_prefix} /auth/signin",
        catch_response=True,
        timeout=timeout,
    ) as resp:
        if resp.status_code != 200:
            resp.failure(f"HTTP {resp.status_code}")
            log.error("[%s] signin failed: HTTP %d — %s", email, resp.status_code, resp.text[:300])
            return None, ""
        try:
            data = resp.json() or {}
        except Exception:
            resp.failure("Signin response is not valid JSON")
            log.error("[%s] signin response is not JSON", email)
            return None, ""

        token = data.get("Token", "")
        if not token:
            resp.failure("Signin response missing Token")
            log.error("[%s] signin response missing 'Token' field", email)
            return None, ""

        resp.success()
        log.info("[%s] signed in successfully", email)

    body: dict[str, Any] = {}
    with client.get(
        "/auth/user",
        headers={"authorization": token},
        name=f"{auth_prefix} /auth/user",
        catch_response=True,
        timeout=timeout,
    ) as resp:
        if resp.status_code != 200:
            resp.failure(f"HTTP {resp.status_code}")
            log.error("[%s] GET /auth/user failed: HTTP %d", email, resp.status_code)
            return token, ""
        try:
            body = resp.json() or {}
            resp.success()
        except Exception:
            resp.failure("GET /auth/user response is not valid JSON")
            return token, ""

    org_id = body.get("OrganizationId", "")
    if not org_id:
        log.warning("[%s] GET /auth/user returned no OrganizationId", email)
    else:
        log.info("[%s] OrganizationId = %s", email, org_id)
    return token, org_id


def authenticated_get(
    client,
    token: str | None,
    path: str,
    name: str,
    timeout: int,
    log: logging.Logger | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET with the session token. Returns the parsed JSON body, or {} on error."""
    log = log or _default_logger
    headers: dict[str, str] = {"authorization": token} if token else {}

    body: dict[str, Any] = {}
    with client.get(
        path, headers=headers, params=params, name=name, catch_response=True, timeout=timeout,
    ) as resp:
        if resp.status_code >= 400:
            resp.failure(f"HTTP {resp.status_code}")
            log.warning("FAIL  %-60s  →  HTTP %d", name, resp.status_code)
        else:
            try:
                body = resp.json() or {}
                resp.success()
            except Exception:
                resp.failure("Response is not valid JSON")
                body = {}
    return body


def raw_header_get(
    client,
    headers: dict[str, str],
    path: str,
    name: str,
    timeout: int,
    log: logging.Logger | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET with caller-supplied headers (no session token), e.g. Day2SDK's x-api-key.

    On failure, logs the URL, params, redacted headers and a response snippet at
    WARNING so failures are debuggable from the log file, not just the stats CSV.
    """
    log = log or _default_logger
    body: dict[str, Any] = {}
    with client.get(
        path, headers=headers, params=params, name=name, catch_response=True, timeout=timeout,
    ) as resp:
        if resp.status_code >= 400:
            resp.failure(f"HTTP {resp.status_code}")
            log.warning(
                "FAIL  %-60s  →  HTTP %d\n"
                "    URL:      %s\n"
                "    Params:   %s\n"
                "    Headers:  %s\n"
                "    Response: %s",
                name, resp.status_code, resp.url, params or {},
                redact_headers(headers), resp.text[:1000],
            )
        else:
            try:
                body = resp.json() or {}
                resp.success()
            except Exception as exc:
                resp.failure(str(exc))
                if not resp.status_code:
                    # No HTTP response at all (connection reset/timeout). `path`
                    # has real IDs substituted, unlike `name`, which is a template.
                    log.warning(
                        "FAIL  %-60s  →  No HTTP response (connection/timeout error) — %s\n"
                        "    Path:   %s\n"
                        "    Params: %s",
                        name, exc, path, params or {},
                    )
                else:
                    log.warning(
                        "FAIL  %-60s  →  Response is not valid JSON (HTTP %d)\n"
                        "    URL:      %s\n"
                        "    Params:   %s\n"
                        "    Response: %s",
                        name, resp.status_code, resp.url, params or {}, resp.text[:1000],
                    )
                body = {}
    return body
