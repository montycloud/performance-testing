"""Day2SDK public API load test.

    locust -f scenarios/api/day2sdk/locustfile.py --headless --users 5 --spawn-rate 2 --env stg \\
      --csv reports/day2sdk --html reports/day2sdk.html

Needs DAY2SDK_ACCESS_KEY_<N> / DAY2SDK_SECRET_KEY_<N> in the environment or in
.env next to this file (see .env.example).
Options: --mc-config <file> (or MCPERF_CONFIG), --env <name>, --report-name <name>.
Flow logic lives in mcperf/api/day2sdk.py; settings in config.yaml next to this file.
"""

from pathlib import Path

from dotenv import load_dotenv

from mcperf.api.day2sdk import Day2SDKConfig, Day2SDKUser
from mcperf.locust import register_scenario

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env", override=False)

register_scenario(Day2SDKUser, _HERE / "config.yaml", Day2SDKConfig)
