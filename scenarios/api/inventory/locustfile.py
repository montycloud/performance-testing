"""Inventory summary load test.

    locust -f scenarios/api/inventory/locustfile.py --headless --users 5 --spawn-rate 5 \\
      --csv reports/inventory --html reports/inventory.html

Options: --mc-config <file> (or MCPERF_CONFIG) to use another config file.
Flow logic lives in mcperf/api/inventory.py; settings in config.yaml next to this file.
"""

from pathlib import Path

from dotenv import load_dotenv

from mcperf.api.inventory import InventoryConfig, InventoryUser
from mcperf.locust import register_scenario

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env", override=False)

register_scenario(InventoryUser, _HERE / "config.yaml", InventoryConfig)
