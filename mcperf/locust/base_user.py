"""MontyCloudUser — the lifecycle every MontyCloud Locust scenario shares.

Template method: the base class owns claim credentials → authenticate → think →
run_flow() → count iterations → StopUser. A scenario subclasses it and
implements only run_flow().

    class InventoryUser(MontyCloudUser):
        def run_flow(self) -> None:
            self.run_batch([self.call("/org/inventory-summary", "[Inventory] ...")])

Hooks a subclass may override:
  load_credentials(cfg)   where credentials come from (default: users CSV)
  authenticate()          how a user signs in (default: /auth/signin password flow)
  api_get(...)            how a GET is sent (default: session token header)
  think_before_flow       pause between sign-in and run_flow() (default: True)
  report_extras(prefix)   extra keyword arguments for the HTML report

Configuration is injected with configure() (see mcperf.locust.scenario), never
read at import time, so flow classes can be imported and unit-tested freely.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any, ClassVar

import gevent
from locust import HttpUser, constant, task
from locust.exception import StopUser
from requests.adapters import HTTPAdapter

from mcperf.core.config import ScenarioConfig
from mcperf.core.credentials import UserClaimer, load_users_csv
from mcperf.locust.batch import run_batch
from mcperf.locust.http import authenticated_get, signin


class MontyCloudUser(HttpUser):
    abstract = True
    wait_time = constant(0)

    cfg: ClassVar[ScenarioConfig | None] = None
    claimer: ClassVar[UserClaimer | None] = None
    log: ClassVar[logging.Logger] = logging.getLogger("perf_test")
    # Max concurrent connections per user; must cover the widest batch.
    pool_maxsize: ClassVar[int] = 20
    think_before_flow: ClassVar[bool] = True

    # ------------------------------------------------------------------
    # Class-level wiring
    # ------------------------------------------------------------------

    @classmethod
    def configure(cls, cfg: ScenarioConfig) -> None:
        """Bind a loaded config to this user class and load its credentials."""
        cls.cfg = cfg
        cls.host = cfg.api.base_url
        cls.claimer = UserClaimer(cls.load_credentials(cfg))

    @classmethod
    def load_credentials(cls, cfg: ScenarioConfig) -> list[dict[str, str]]:
        return load_users_csv(cfg.load.users_csv, cfg.load.user_count)

    @classmethod
    def report_extras(cls, csv_prefix: str) -> dict[str, Any]:
        """Extra keyword arguments for mcperf.reporting.locust_report.generate()."""
        return {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        if self.cfg is None or self.claimer is None:
            raise RuntimeError(
                f"{type(self).__name__} is not configured. Call "
                "mcperf.locust.scenario.register_scenario() in the locustfile."
            )
        self.creds: dict[str, str] = self.claimer.claim()
        self.token: str | None = None
        self.org_id: str = ""
        self.iterations_done: int = 0
        adapter = HTTPAdapter(pool_connections=1, pool_maxsize=self.pool_maxsize)
        self.client.mount("https://", adapter)
        self.client.mount("http://", adapter)
        self.authenticated = self.authenticate()

    @task
    def journey(self) -> None:
        if not self.authenticated:
            self.authenticated = self.authenticate()
            if not self.authenticated:
                self.log.error("[%s] Authentication failed; removing user from pool.", self.label)
                raise StopUser()

        load = self.cfg.load
        if self.think_before_flow:
            self.think(load.think_time_min, load.think_time_max)
        self.run_flow()

        if load.is_single_journey:
            self.iterations_done += 1
            if self.iterations_done >= load.iterations:
                raise StopUser()
            self.log.info(
                "[%s] journey %d/%d complete", self.label, self.iterations_done, load.iterations,
            )

    def run_flow(self) -> None:
        """One journey through the page under test. Subclasses implement this."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Overridable strategies
    # ------------------------------------------------------------------

    def authenticate(self) -> bool:
        """Password sign-in. Sets self.token and self.org_id; returns True on success."""
        api = self.cfg.api
        self.token, self.org_id = signin(self.client, self.creds, api.mc_debug_mode, api.timeout, self.log)
        return bool(self.token)

    def api_get(self, path: str, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return authenticated_get(
            self.client, self.token, path, name, self.cfg.api.timeout, self.log, params=params,
        )

    @property
    def label(self) -> str:
        """How this user is identified in log lines."""
        return self.creds.get("Email", "unknown")

    # ------------------------------------------------------------------
    # Helpers for flows
    # ------------------------------------------------------------------

    def call(self, path: str, name: str, params: dict[str, Any] | None = None) -> Callable[[], dict[str, Any]]:
        """A not-yet-sent GET, for building batches without lambdas."""
        return partial(self.api_get, path, name, params)

    def run_batch(self, callables: Iterable[Callable[[], object]]) -> None:
        run_batch(callables)

    def think(self, low: float, high: float) -> None:
        gevent.sleep(random.uniform(low, high))

    def page_think(self) -> None:
        load = self.cfg.load
        self.think(load.think_time_min, load.think_time_max)

    def batch_think(self) -> None:
        load = self.cfg.load
        self.think(load.batch_think_time_min, load.batch_think_time_max)
