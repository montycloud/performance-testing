"""Per-run logging: one JSON file per user per step, plus a combined run log."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SENSITIVE_KEYS = {
    "Password",
    "CaptchaCode",
    "Session",
    "Token",
    "AccessToken",
    "RefreshToken",
    "IdToken",
    "authorization",
    "Authorization",
}


def fingerprint(value: Optional[str]) -> str:
    """Short non-reversible id so per-user tokens can be told apart in logs."""
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def mask(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (f"***<{fingerprint(v)}>" if k in SENSITIVE_KEYS and isinstance(v, str) else mask(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    return obj


class RunLogger:
    """Owns logs/<run_ts>/ and writes step artefacts into it."""

    def __init__(self, base_dir: Path, run_ts: Optional[str] = None, console_level: int = logging.INFO):
        self.run_ts = run_ts or datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = Path(base_dir) / self.run_ts
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._install_handlers(console_level)
        logger.info("Run directory: %s", self.run_dir)

    def _install_handlers(self, console_level: int) -> None:
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        fmt = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        file_handler = logging.FileHandler(self.run_dir / "run.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

        if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root.handlers):
            console = logging.StreamHandler()
            console.setFormatter(fmt)
            root.addHandler(console)
        for handler in root.handlers:
            if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
                handler.setLevel(console_level)

    def write_step(
        self,
        user_index: int,
        step_no: int,
        step_name: str,
        record: Dict[str, Any],
        org_id: Optional[str] = None,
    ) -> Path:
        payload = {
            "run_ts": self.run_ts,
            "user": user_index,
            "step": step_no,
            "step_name": step_name,
            "org_id": org_id,
            **mask(record),
        }
        path = self.run_dir / f"user{user_index:02d}_step{step_no}_{step_name}.json"
        with self._lock:
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def write_json(self, filename: str, data: Any) -> Path:
        path = self.run_dir / filename
        with self._lock:
            path.write_text(json.dumps(mask(data), indent=2, default=str), encoding="utf-8")
        return path

    def write_lines(self, filename: str, lines: List[str]) -> Path:
        path = self.run_dir / filename
        with self._lock:
            path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return path
