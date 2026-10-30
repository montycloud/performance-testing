"""Main user journey load test: Home Page → WAFR → optional Health Events → optional AI Chat.

    locust -f scenarios/api/journey/locustfile.py --headless --users 10 --spawn-rate 2 \\
      --csv reports/journey --html reports/journey.html

Options: --mc-config <file> (or MCPERF_CONFIG), --env <name>, --report-name <name>.
Chat query sweeps: python -m mcperf.runners.chat_sweep --manifest scenarios/api/journey/executions/wafr.yaml
Flow logic lives in mcperf/api/journey.py and mcperf/api/pages/; settings in config.yaml next to this file.
"""

from pathlib import Path

from dotenv import load_dotenv

from mcperf.api.journey import JourneyConfig, JourneyUser
from mcperf.locust import register_scenario

_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE / ".env", override=False)

register_scenario(JourneyUser, _HERE / "config.yaml", JourneyConfig)
