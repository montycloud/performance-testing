"""Shared root sign-in helpers, used by setup_collections.py and main.py."""
import sys

import requests

from client import auth_base_url_for_env


def signin(env, email, password, mc_debug_mode, timeout):
    """Sign in as a root user; returns the raw JWT (no cookies, no Bearer prefix)."""
    url = f"{auth_base_url_for_env(env)}/auth/signin"
    resp = requests.post(url, json={
        "Username": email,
        "Password": password,
        "MC_DEBUG_MODE": mc_debug_mode, # to bypass the captcha code
    }, timeout=timeout)
    if resp.status_code != 200:
        sys.exit(f"Sign-in failed (HTTP {resp.status_code}): {resp.text[:300]}")
    token = (resp.json() or {}).get("Token")
    if not token:
        sys.exit("Sign-in response had no 'Token' field.")
    return token


def fetch_org_id(env, token, timeout):
    """GET /auth/user to resolve the signed-in user's OrganizationId."""
    url = f"{auth_base_url_for_env(env)}/auth/user"
    resp = requests.get(url, headers={"authorization": token}, timeout=timeout)
    if resp.status_code != 200:
        sys.exit(f"GET /auth/user failed (HTTP {resp.status_code}): {resp.text[:300]}")
    org_id = (resp.json() or {}).get("OrganizationId")
    if not org_id:
        print("  warning: GET /auth/user returned no OrganizationId")
    return org_id
