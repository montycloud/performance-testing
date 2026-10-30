"""Typed scenario configuration.

Every scenario config file shares three sections, parsed here once:

  api:     base_url, mc_debug_mode, timeout_seconds
  load:    users_csv, user_count, run_mode, iterations, think times
  report:  description, report_name

Scenario-specific sections (e.g. `inventory:`) are parsed by the scenario's own
config class, which composes these. Relative paths resolve against the config
file's directory, not the current working directory.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from mcperf.core import environments

RUN_MODES = ("single_journey", "timed")


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, raising a clear error if it's missing or not a mapping."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {path}")
    return data


def resolve_path(raw: str | Path, base_dir: Path) -> Path:
    """Resolve *raw* relative to *base_dir* unless it is already absolute."""
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def require_section(raw: dict[str, Any], name: str, path: Path) -> dict[str, Any]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise ValueError(f"{path} has no '{name}:' section.")
    return section


@dataclass(frozen=True)
class ApiSettings:
    base_url: str
    mc_debug_mode: bool = True
    timeout: int = 40

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ApiSettings:
        """Accepts either `env: stg1` (see mcperf.core.environments) or an explicit `base_url`."""
        if d.get("base_url"):
            url = str(d["base_url"])
        elif d.get("env"):
            url = environments.base_url(str(d["env"]))
        else:
            raise ValueError("api.env or api.base_url is required.")
        return cls(
            base_url=url.rstrip("/"),
            mc_debug_mode=bool(d.get("mc_debug_mode", True)),
            timeout=int(d.get("timeout_seconds", 40)),
        )

    def for_env(self, env: str) -> ApiSettings:
        return replace(self, base_url=environments.base_url(env))


@dataclass(frozen=True)
class LoadProfile:
    users_csv: Path
    user_count: int = 1
    run_mode: str = "single_journey"
    iterations: int = 1
    think_time_min: float = 2.0
    think_time_max: float = 5.0
    batch_think_time_min: float = 1.0
    batch_think_time_max: float = 3.0

    @property
    def is_single_journey(self) -> bool:
        return self.run_mode == "single_journey"

    @classmethod
    def from_dict(cls, d: dict[str, Any], base_dir: Path) -> LoadProfile:
        run_mode = str(d.get("run_mode", "single_journey")).strip().lower()
        if run_mode not in RUN_MODES:
            raise ValueError(f"load.run_mode must be one of {RUN_MODES}, got {run_mode!r}.")
        profile = cls(
            users_csv=resolve_path(d.get("users_csv", "./users.csv"), base_dir),
            user_count=int(d.get("user_count", 1)),
            run_mode=run_mode,
            iterations=max(1, int(d.get("iterations", 1))),
            think_time_min=float(d.get("think_time_min", 2)),
            think_time_max=float(d.get("think_time_max", 5)),
            batch_think_time_min=float(d.get("batch_think_time_min", 1)),
            batch_think_time_max=float(d.get("batch_think_time_max", 3)),
        )
        if profile.think_time_min > profile.think_time_max:
            raise ValueError("load.think_time_min must be <= load.think_time_max.")
        if profile.batch_think_time_min > profile.batch_think_time_max:
            raise ValueError("load.batch_think_time_min must be <= load.batch_think_time_max.")
        return profile


@dataclass(frozen=True)
class ReportSettings:
    description: str = ""
    report_name: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReportSettings:
        return cls(
            description=str(d.get("description", "") or ""),
            report_name=str(d.get("report_name", "") or ""),
        )


@dataclass(frozen=True)
class ScenarioConfig:
    """Sections every scenario has. Scenario configs subclass this."""

    path: Path
    raw: dict[str, Any]
    api: ApiSettings
    load: LoadProfile
    report: ReportSettings

    @classmethod
    def base_kwargs(cls, path: Path) -> dict[str, Any]:
        """Parse the shared sections; subclasses add their own fields and call cls(**kwargs)."""
        path = Path(path).resolve()
        raw = load_yaml(path)
        return {
            "path": path,
            "raw": raw,
            "api": ApiSettings.from_dict(require_section(raw, "api", path)),
            "load": LoadProfile.from_dict(raw.get("load") or {}, path.parent),
            "report": ReportSettings.from_dict(raw.get("report") or {}),
        }

    @classmethod
    def load_file(cls, path: Path) -> ScenarioConfig:
        return cls(**cls.base_kwargs(path))

    def with_overrides(self, env: str | None = None, report_name: str | None = None) -> ScenarioConfig:
        """Apply command-line overrides (--env, --report-name) on top of the file."""
        cfg = self
        if env:
            cfg = replace(cfg, api=cfg.api.for_env(env))
        if report_name:
            cfg = replace(cfg, report=replace(cfg.report, report_name=report_name))
        return cfg

    def resolve(self, raw: str | Path) -> Path:
        """Resolve a path from this config relative to the config file's directory."""
        return resolve_path(raw, self.path.parent)
