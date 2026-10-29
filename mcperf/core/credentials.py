"""Credential sources for simulated users.

Two sources exist today:
  load_users_csv        — Email / Password / New Password rows (sign-in users)
  load_env_credentials  — numbered {PREFIX}_ACCESS_KEY_<N> / _SECRET_KEY_<N> pairs

UserClaimer hands rows out round-robin, so --users larger than the row count
recycles credentials instead of failing.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


def load_users_csv(csv_path: Path, limit: int) -> list[dict[str, str]]:
    """Load up to *limit* rows from a users CSV (columns: Name, Email, Password, ...)."""
    if not csv_path.exists():
        raise FileNotFoundError(f"Users CSV not found: {csv_path}")
    with open(csv_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"Users CSV is empty: {csv_path}")
    users = rows[:limit]
    logger.info("Loaded %d user(s) from %s  [user_count limit = %d]", len(users), csv_path, limit)
    return users


def password_for(creds: dict[str, str]) -> str:
    """Prefer the permanent post-reset "New Password"; fall back to "Password"."""
    return creds.get("New Password") or creds.get("Password", "")


class UserClaimer:
    """Thread-safe round-robin credential dispenser over a fixed user list."""

    def __init__(self, users: list[dict[str, str]]):
        if not users:
            raise ValueError("UserClaimer requires a non-empty users list.")
        self._users = users
        self._lock = threading.Lock()
        self._index = 0

    def claim(self) -> dict[str, str]:
        """Return the next user credential row, cycling through the list."""
        with self._lock:
            idx = self._index % len(self._users)
            self._index += 1
        return self._users[idx]


def load_env_credentials(prefix: str) -> list[dict[str, str]]:
    """Scan os.environ for numbered {prefix}_ACCESS_KEY_<N> / {prefix}_SECRET_KEY_<N> pairs.

    Keeping key pairs in .env (not a CSV) avoids committing secrets to the repo.
    """
    pattern = re.compile(rf"^{re.escape(prefix)}_ACCESS_KEY_(\d+)$")
    indices = sorted({int(m.group(1)) for key in os.environ if (m := pattern.match(key))})

    creds: list[dict[str, str]] = []
    for idx in indices:
        access_key = os.environ.get(f"{prefix}_ACCESS_KEY_{idx}", "").strip()
        secret_key = os.environ.get(f"{prefix}_SECRET_KEY_{idx}", "").strip()
        if not access_key or not secret_key:
            logger.warning("Skipping %s credential #%d: missing AccessKey or SecretKey.", prefix, idx)
            continue
        creds.append({"AccessKey": access_key, "SecretKey": secret_key})

    if not creds:
        raise ValueError(
            f"No {prefix} credentials found in environment. Set "
            f"{prefix}_ACCESS_KEY_1 / {prefix}_SECRET_KEY_1 (and _2, _3, ...) in .env."
        )
    logger.info("Loaded %d %s credential pair(s) from environment.", len(creds), prefix)
    return creds
