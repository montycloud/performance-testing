"""MontyCloudUser — the lifecycle every MontyCloud Locust scenario shares.

Template method: the base class owns claim credentials → sign in → think →
run_flow() → count iterations → StopUser. A scenario subclasses it and
implements only run_flow().

    class InventoryUser(MontyCloudUser):
        def run_flow(self) -> None:
            self.run_batch([...])

Configuration is injected with configure() (see mcperf.locust.scenario), never
read at import time, so flow classes can be imported and unit-tested freely.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Iterable
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

    @classmethod
    def configure(cls, cfg: ScenarioConfig) -> None:
        """Bind a loaded config to this user class and load its credentials."""
        cls.cfg = cfg
        cls.host = cfg.api.base_url
        cls.claimer = UserClaimer(load_users_csv(cfg.load.users_csv, cfg.load.user_count))

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
        self.sign_in()

    @task
    def journey(self) -> None:
        if not self.token:
            self.sign_in()
            if not self.token:
                self.log.error("[%s] No token available; removing user from pool.", self.email)
                raise StopUser()

        load = self.cfg.load
        self.think(load.think_time_min, load.think_time_max)
        self.run_flow()

        if load.is_single_journey:
            self.iterations_done += 1
            if self.iterations_done >= load.iterations:
                raise StopUser()
            self.log.info(
                "[%s] journey %d/%d complete", self.email, self.iterations_done, load.iterations,
            )

    def run_flow(self) -> None:
        """One journey through the page under test. Subclasses implement this."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Helpers for flows
    # ------------------------------------------------------------------

    @property
    def email(self) -> str:
        return self.creds.get("Email", "unknown")

    def sign_in(self) -> None:
        api = self.cfg.api
        self.token, self.org_id = signin(self.client, self.creds, api.mc_debug_mode, api.timeout, self.log)

    def api_get(self, path: str, name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return authenticated_get(
            self.client, self.token, path, name, self.cfg.api.timeout, self.log, params=params,
        )

    def run_batch(self, callables: Iterable[Callable[[], object]]) -> None:
        run_batch(callables)

    def think(self, low: float, high: float) -> None:
        gevent.sleep(random.uniform(low, high))

    def batch_think(self) -> None:
        load = self.cfg.load
        self.think(load.batch_think_time_min, load.batch_think_time_max)
