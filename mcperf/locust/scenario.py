"""Wire one or more MontyCloudUser subclasses into a Locust run.

register_scenario() is the only call a locustfile needs. It:
  1. adds --mc-config (env MCPERF_CONFIG) so sweeps can point at another config
     file without rewriting one in place, --env to pick a MontyCloud
     environment (overrides api.base_url), and --report-name;
  2. loads and validates the config on Locust's init event, then configures the
     user class(es) — pass a list of classes when a locustfile defines several
     independently-weighted User classes sharing one config (e.g. one class
     per endpoint, each with its own `weight`/`wait_time`);
  3. in single_journey mode, quits once every user has finished;
  4. after the run, writes the custom HTML report next to the --csv output.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from pathlib import Path

import gevent
from locust import events
from locust.runners import STATE_CLEANUP, STATE_STOPPED, STATE_STOPPING, WorkerRunner
from locust.stats import CSV_STATS_FLUSH_INTERVAL_SEC, CSV_STATS_INTERVAL_SEC

from mcperf.core import environments
from mcperf.core.config import ScenarioConfig
from mcperf.locust.base_user import MontyCloudUser

logger = logging.getLogger(__name__)

_FINISHED_STATES = (STATE_STOPPING, STATE_STOPPED, STATE_CLEANUP)


def _as_classes(
    user_cls: type[MontyCloudUser] | Sequence[type[MontyCloudUser]],
) -> list[type[MontyCloudUser]]:
    """Normalize register_scenario()'s *user_cls* to a list: a bare class becomes
    a single-item list, a list/tuple of classes passes through unchanged."""
    return [user_cls] if isinstance(user_cls, type) else list(user_cls)


def register_scenario(
    user_cls: type[MontyCloudUser] | Sequence[type[MontyCloudUser]],
    default_config: Path,
    config_cls: type[ScenarioConfig] = ScenarioConfig,
) -> None:
    """Wire one locustfile. *user_cls* is usually a single class; pass a list/tuple
    when a locustfile defines several independently-weighted User classes that
    should share the same config file (e.g. one class per endpoint) — every
    class in the sequence is configure()'d from the same loaded config.
    """
    user_classes = _as_classes(user_cls)
    primary = user_classes[0]

    @events.init_command_line_parser.add_listener
    def _add_args(parser, **_kwargs) -> None:
        parser.add_argument(
            "--mc-config",
            type=str,
            env_var="MCPERF_CONFIG",
            default=str(default_config),
            help=f"Scenario config YAML (default: {default_config})",
        )
        parser.add_argument(
            "--env",
            type=str,
            default=None,
            help=f"MontyCloud environment, overrides api.base_url ({', '.join(environments.ENVIRONMENTS)})",
        )
        parser.add_argument(
            "--report-name",
            type=str,
            default=None,
            help="Custom report name segment (custom_<name>_<timestamp>.html), overrides report.report_name",
        )

    @events.init.add_listener
    def _configure(environment, **_kwargs) -> None:
        opts = getattr(environment, "parsed_options", None)
        path = Path(getattr(opts, "mc_config", None) or default_config)
        env = getattr(opts, "env", None)
        cfg = config_cls.load_file(path).with_overrides(env=env, report_name=getattr(opts, "report_name", None))
        if env and environments.is_production(env):
            logger.warning("--env %s is PRODUCTION (%s).", env, cfg.api.base_url)
        for cls in user_classes:
            cls.configure(cfg)
        logger.info("Loaded scenario config %s → %s", path, cfg.api.base_url)

    install_single_journey_watchdog(lambda: primary.cfg is not None and primary.cfg.load.is_single_journey)
    install_report_on_stop(lambda: primary.cfg, primary.report_extras)


def install_single_journey_watchdog(is_single_journey: Callable[[], bool]) -> None:
    """Stop the run once every spawned user has raised StopUser."""

    @events.test_start.add_listener
    def _on_test_start(environment, **_kwargs) -> None:
        if not is_single_journey():
            return
        runner = environment.runner
        if runner is None or isinstance(runner, WorkerRunner):
            return  # workers follow the master; nothing to watch locally

        def _quit_when_all_users_done() -> None:
            # Phase 1: wait for the first user to spawn (ramp-up has begun).
            while runner.user_count == 0:
                if runner.state in _FINISHED_STATES:
                    return
                gevent.sleep(1)
            # Phase 2: wait for the last user to finish its journey.
            while runner.user_count > 0:
                if runner.state in _FINISHED_STATES:
                    return
                gevent.sleep(1)
            logger.info("single_journey: all users have finished — stopping the test.")
            opts = getattr(environment, "parsed_options", None)
            if opts and getattr(opts, "headless", False):
                runner.quit()  # headless: exit the process
            else:
                runner.stop()  # web UI: stop the run, keep the UI alive

        gevent.spawn(_quit_when_all_users_done)


def install_report_on_stop(
    get_config: Callable[[], ScenarioConfig | None],
    get_extras: Callable[[str], dict] = lambda _prefix: {},
) -> None:
    """Write the custom HTML report when the run was started with --csv."""

    @events.test_stop.add_listener
    def _on_test_stop(environment, **_kwargs) -> None:
        opts = getattr(environment, "parsed_options", None)
        csv_prefix = getattr(opts, "csv_prefix", None) if opts else None
        if not csv_prefix:
            logger.info("No --csv prefix configured; skipping custom HTML report.")
            return

        # Locust rewrites the stats CSV every CSV_STATS_INTERVAL_SEC and flushes
        # it every CSV_STATS_FLUSH_INTERVAL_SEC; wait a full cycle so the last
        # requests are on disk before reading it.
        gevent.sleep(CSV_STATS_INTERVAL_SEC + CSV_STATS_FLUSH_INTERVAL_SEC + 1)

        stats_csv = Path(csv_prefix + "_stats.csv")
        if not stats_csv.exists():
            logger.warning("Expected Locust stats CSV at %s — not found; skipping custom report.", stats_csv)
            return

        cfg = get_config()
        try:
            from mcperf.reporting.locust_report import generate  # noqa: PLC0415

            out = generate(
                stats_csv,
                description=cfg.report.description if cfg else "",
                config_data=cfg.raw if cfg else None,
                report_name=cfg.report.report_name if cfg else "",
                **get_extras(csv_prefix),
            )
            logger.info("Custom HTML report  →  %s", out)
        except Exception as exc:
            logger.error("Failed to generate custom HTML report: %s", exc, exc_info=True)
