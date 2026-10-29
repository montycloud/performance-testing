"""Keep secrets out of log lines. Every module that logs URLs or headers goes through here."""

from __future__ import annotations

import re

SECRET_HEADERS = ("authorization", "x-api-key")


def redact_ws_url(url: str) -> str:
    """Strip the JWT out of a WebSocket URL's Authorization query parameter."""
    return re.sub(r"(Authorization=)[^&]+", r"\1<redacted>", url)


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Shorten secret-bearing header values to their first 4 characters."""
    return {
        key: f"{value[:4]}...<redacted>" if key.lower() in SECRET_HEADERS and value else value
        for key, value in headers.items()
    }
