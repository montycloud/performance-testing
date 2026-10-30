"""Cost Dashboard load test.

    locust -f scenarios/api/cost_dashboard/locustfile.py --headless --users 5 --spawn-rate 5 \\
      --csv reports/cost_dashboard --html reports/cost_dashboard.html

Options: --mc-config <file> (or MCPERF_CONFIG), --env <name>, --report-name <name>.
Flow logic lives in mcperf/api/cost_dashboard.py; settings in config.yaml next to this file.
"""

from pathlib import Path

from dotenv import load_dotenv

from mcperf.api.cost_dashboard import CostDashboardConfig, CostDashboardUser
from mcperf.locust import register_scenario

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env", override=False)

register_scenario(CostDashboardUser, _HERE / "config.yaml", CostDashboardConfig)
