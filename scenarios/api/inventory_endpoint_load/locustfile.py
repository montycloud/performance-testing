"""Inventory endpoint-targeted load test.

    locust -f scenarios/api/inventory_endpoint_load/locustfile.py --headless \\
      --users 10 --spawn-rate 10 --run-time 5m \\
      --csv reports/endpoint_load --html reports/endpoint_load.html

Options: --mc-config <file> (or MCPERF_CONFIG) to use another config file,
--env to target a MontyCloud environment.
Flow logic lives in mcperf/api/inventory_endpoint_load.py; settings in
config.yaml next to this file.
"""

from pathlib import Path

from dotenv import load_dotenv

from mcperf.api.inventory_endpoint_load import (
    ENDPOINT_LOAD_USER_CLASSES,
    EndpointLoadConfig,
    GovernanceLoadUser,
    InventorySummaryLoadUser,
    ResourcesLoadUser,
    ResourcesPaginatedLoadUser,
)
from mcperf.locust import register_scenario

# Locust discovers User classes by scanning this module's own top-level names,
# so each class must be imported directly here (not just via the
# ENDPOINT_LOAD_USER_CLASSES tuple) even though it's unused beyond that.
__all__ = [
    "GovernanceLoadUser",
    "InventorySummaryLoadUser",
    "ResourcesLoadUser",
    "ResourcesPaginatedLoadUser",
]

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env", override=False)

register_scenario(ENDPOINT_LOAD_USER_CLASSES, _HERE / "config.yaml", EndpointLoadConfig)
