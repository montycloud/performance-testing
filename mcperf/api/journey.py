"""Main user journey: Home Page → WAFR → optional Health Events → optional AI Chat.

Modes (config `health.mode` / `chat.mode`, each only when enabled):
  default             sign in → Home → think → WAFR [→ think → Health] [→ think → Chat]
  chat standalone     sign in → think → Chat
  health standalone   sign in → think → Health
If both are standalone, chat wins (a warning is logged at start-up).

Request names are prefixed [Auth] / [HomePage] / [WAFR] / [Health] / [Chat].
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcperf.api.chat import ChatSession, ChatSettings, SessionTimeoutStats, Transcript
from mcperf.api.pages.health import health_flow
from mcperf.api.pages.homepage import homepage_flow
from mcperf.api.pages.wafr import wafr_flow
from mcperf.core.config import ScenarioConfig
from mcperf.locust.base_user import MontyCloudUser

MODES = ("appended", "standalone")


@dataclass(frozen=True)
class Toggle:
    enabled: bool = False
    mode: str = "appended"

    @classmethod
    def from_dict(cls, d: dict[str, Any], name: str) -> Toggle:
        mode = str(d.get("mode", "appended")).strip().lower()
        if mode not in MODES:
            raise ValueError(f"{name}.mode must be one of {MODES}, got {mode!r}.")
        return cls(bool(d.get("enabled", False)), mode)

    @property
    def standalone(self) -> bool:
        return self.enabled and self.mode == "standalone"


@dataclass(frozen=True)
class JourneyConfig(ScenarioConfig):
    wafr_think_time_min: float = 2.0
    wafr_think_time_max: float = 5.0
    health: Toggle = field(default_factory=Toggle)
    chat: Toggle = field(default_factory=Toggle)
    chat_settings: ChatSettings = field(default_factory=ChatSettings)

    @classmethod
    def load_file(cls, path: Path) -> JourneyConfig:
        kwargs = cls.base_kwargs(path)
        raw, load = kwargs["raw"], kwargs["load"]
        wafr = raw.get("wafr") or {}
        chat_raw = raw.get("chat") or {}
        chat = Toggle.from_dict(chat_raw, "chat")
        health = Toggle.from_dict(raw.get("health") or {}, "health")
        cfg = cls(
            **kwargs,
            wafr_think_time_min=float(wafr.get("think_time_min", load.think_time_min)),
            wafr_think_time_max=float(wafr.get("think_time_max", load.think_time_max)),
            health=health,
            chat=chat,
            chat_settings=(
                ChatSettings.from_dict(chat_raw, lambda p: (kwargs["path"].parent / p).resolve())
                if chat.enabled else ChatSettings()
            ),
        )
        if chat.standalone and health.standalone:
            logging.getLogger("perf_test").warning(
                "Both health.mode and chat.mode are 'standalone' — chat takes precedence; "
                "the Health Events flow will not run this test."
            )
        return cfg


class JourneyUser(MontyCloudUser):
    cfg: JourneyConfig | None = None
    log = logging.getLogger("perf_test")
    # WAFR batch 2 fires 28 calls at once.
    pool_maxsize = 50
    # The default journey goes straight from sign-in to the Home Page.
    think_before_flow = False

    session_timeouts = SessionTimeoutStats()
    transcript = Transcript(None)

    @classmethod
    def configure(cls, cfg: JourneyConfig) -> None:
        super().configure(cfg)
        cls.session_timeouts = SessionTimeoutStats()
        cls.transcript = Transcript(cfg.chat_settings.transcript_log if cfg.chat.enabled else None)

    @classmethod
    def report_extras(cls, csv_prefix: str) -> dict[str, Any]:
        """Write per-user session continuations next to the stats CSV and pass them to the report."""
        snapshot = cls.session_timeouts.snapshot()
        if not snapshot:
            return {}
        sidecar = Path(csv_prefix + "_session_timeouts.json")
        try:
            sidecar.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
            cls.log.info("Session timeout stats  →  %s", sidecar)
        except OSError as exc:
            cls.log.warning("Could not write session timeout sidecar: %s", exc)
        return {"session_timeout_stats": snapshot}

    def run_flow(self) -> None:
        cfg = self.cfg
        if cfg.chat.standalone:
            self.page_think()
            self.chat_flow()
        elif cfg.health.standalone:
            self.page_think()
            health_flow(self)
        else:
            homepage_flow(self)
            self.page_think()
            wafr_flow(self, self.wafr_think)
            if cfg.health.enabled:
                self.page_think()
                health_flow(self)
            if cfg.chat.enabled:
                self.page_think()
                self.chat_flow()

    def wafr_think(self) -> None:
        self.think(self.cfg.wafr_think_time_min, self.cfg.wafr_think_time_max)

    def chat_flow(self) -> None:
        s = self.cfg.chat_settings
        if not s.ws_base_url:
            self.log.error("[%s] chat.ws_base_url is not configured; skipping Chat flow.", self.label)
            return
        if not s.tenant_scope:
            self.log.error("[%s] No tenants loaded from the tenants file; skipping Chat flow.", self.label)
            return
        url = f"{s.ws_base_url}?Authorization={self.token}&OrganizationId={self.org_id}&agentic=true"
        session = ChatSession(s, self.fire_metric, self.transcript, self.label)
        self.session_timeouts.add(self.label, session.run(url))

    def fire_metric(self, name: str, start: float, exception: BaseException | None) -> None:
        """Record a pseudo-request in Locust's stats (custom-client pattern)."""
        self.environment.events.request.fire(
            request_type="WS", name=name, response_time=(time.monotonic() - start) * 1000,
            response_length=0, exception=exception, context={},
        )
