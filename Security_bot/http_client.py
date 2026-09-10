"""HTTP client for the Security Bot flow.

One instance per simulated user so cookie jars and connection pools are never shared.
The auth token is passed per call and never stored on the session.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests

from run_logger import mask

logger = logging.getLogger(__name__)


class StepError(RuntimeError):
    def __init__(self, message: str, response: "StepResponse"):
        super().__init__(message)
        self.response = response


@dataclass
class StepResponse:
    method: str
    url: str
    request_payload: Optional[Dict[str, Any]] = None
    request_params: Optional[Dict[str, Any]] = None
    request_cookies: Optional[Dict[str, str]] = None
    status_code: int = 0
    elapsed_ms: float = 0.0
    body: Any = None
    text: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300 and self.error is None

    def to_record(self) -> Dict[str, Any]:
        return {
            "request": {
                "method": self.method,
                "url": self.url,
                "params": self.request_params,
                "payload": self.request_payload,
                "cookies": self.request_cookies,
            },
            "status_code": self.status_code,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
            "response": self.body if self.body is not None else self.text[:4000],
        }


@dataclass
class SecurityBotClient:
    base_url: str
    timeout: int = 60
    dry_run: bool = False
    label: str = ""
    session: requests.Session = field(default_factory=requests.Session)

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        token: Optional[str] = None,
        cookies: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        name: Optional[str] = None,
    ) -> StepResponse:
        url = f"{self.base_url}{path}"
        resp = StepResponse(
            method=method.upper(),
            url=url,
            request_payload=payload,
            request_params=params,
            request_cookies=cookies,
        )

        if self.dry_run:
            logger.info(
                "[DRY-RUN] %s %s %s params=%s payload=%s",
                self.label,
                resp.method,
                url,
                params,
                json.dumps(mask(payload or {})),
            )
            resp.status_code = 0
            resp.body = {"dry_run": True}
            return resp

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["authorization"] = token

        kwargs: Dict[str, Any] = {"headers": headers, "timeout": self.timeout}
        if payload is not None:
            kwargs["json"] = payload
        if params:
            kwargs["params"] = params
        if cookies:
            kwargs["cookies"] = cookies
        # Locust's client keys stats by `name`; plain requests.Session ignores it.
        if name and hasattr(self.session, "request") and "locust" in type(self.session).__module__:
            kwargs["name"] = name
            kwargs["catch_response"] = False

        started = time.perf_counter()
        try:
            raw = self.session.request(resp.method, url, **kwargs)
        except requests.RequestException as exc:
            resp.elapsed_ms = (time.perf_counter() - started) * 1000
            resp.error = f"{type(exc).__name__}: {exc}"
            logger.error("%s %s %s failed: %s", self.label, resp.method, url, resp.error)
            return resp

        resp.elapsed_ms = (time.perf_counter() - started) * 1000
        resp.status_code = raw.status_code
        resp.text = raw.text
        if raw.text.strip():
            try:
                resp.body = raw.json()
            except ValueError:
                resp.body = None
        if not resp.ok:
            resp.error = f"HTTP {raw.status_code}: {raw.text[:500]}"

        logger.debug(
            "%s %s %s -> %d in %.0f ms",
            self.label,
            resp.method,
            url,
            resp.status_code,
            resp.elapsed_ms,
        )
        return resp

    def post(self, path: str, payload: Optional[Dict[str, Any]] = None, **kw) -> StepResponse:
        return self.request("POST", path, payload=payload if payload is not None else {}, **kw)

    def get(self, path: str, **kw) -> StepResponse:
        return self.request("GET", path, **kw)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
