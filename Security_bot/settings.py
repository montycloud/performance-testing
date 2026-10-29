"""config.yaml + .env loading, shared by the CLI driver and the Locust file."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
DEFAULT_CONFIG = BASE_DIR / "config.yaml"

load_dotenv(dotenv_path=BASE_DIR / ".env")


@dataclass
class Settings:
    raw: Dict[str, Any]
    path: Path

    @property
    def api(self) -> Dict[str, Any]:
        return self.raw["api"]

    @property
    def auth(self) -> Dict[str, Any]:
        return self.raw["auth"]

    @property
    def bot(self) -> Dict[str, Any]:
        return self.raw["bot"]

    @property
    def run(self) -> Dict[str, Any]:
        return self.raw["run"]

    @property
    def rescan(self) -> Dict[str, Any]:
        return self.raw["rescan"]

    @property
    def db(self) -> Dict[str, Any]:
        return self.raw.get("db", {})

    @property
    def base_url(self) -> str:
        return str(self.api["base_url"]).rstrip("/")

    @property
    def timeout(self) -> int:
        return int(self.api["timeout_seconds"])

    @property
    def logs_dir(self) -> Path:
        return _resolve(self.run["logs_dir"])

    @property
    def users_csv(self) -> Path:
        return _resolve(self.run["users_csv"])

    @property
    def organizations_file(self) -> Path:
        return _resolve(self.run["organizations_file"])

    def insights_path(self) -> str:
        return str(self.bot["insights_path"]).format(bot_id=self.bot["id"])

    def rescan_path(self) -> str:
        return str(self.bot["rescan_path"]).format(bot_id=self.bot["id"])


def _resolve(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else BASE_DIR / p


def load_settings(config_path: Optional[str] = None) -> Settings:
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    if not path.is_absolute():
        path = BASE_DIR / path
    if not path.exists():
        print(f"ERROR: config file not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return Settings(raw=yaml.safe_load(f), path=path)


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()
