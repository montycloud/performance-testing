#!/usr/bin/env python3
"""
Compatibility shim — these helpers now live in the mcperf package:

  load_config         → mcperf.core.config.load_yaml
  load_users          → mcperf.core.credentials.load_users_csv
  UserClaimer         → mcperf.core.credentials.UserClaimer
  load_env_credentials→ mcperf.core.credentials.load_env_credentials
  redact_ws_url       → mcperf.core.redact.redact_ws_url
  run_batch           → mcperf.locust.batch.run_batch
  signin / authenticated_get / raw_header_get → mcperf.locust.http

Kept so locustfile.py, cost_dashboard_locustfile.py and day2sdk_locustfile.py
run unchanged until they are migrated (restructure phase 3). New code imports
from mcperf directly. Do not add code here.
"""

import sys
from pathlib import Path
from typing import Dict, List

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcperf.core import credentials as _credentials  # noqa: E402
from mcperf.core.config import load_yaml as load_config  # noqa: E402,F401
from mcperf.core.credentials import UserClaimer  # noqa: E402,F401
from mcperf.core.redact import redact_ws_url  # noqa: E402,F401
from mcperf.locust.batch import run_batch  # noqa: E402,F401
from mcperf.locust.http import authenticated_get, raw_header_get, signin  # noqa: E402,F401


def load_users(csv_path: Path, limit: int, logger=None) -> List[Dict[str, str]]:
    return _credentials.load_users_csv(csv_path, limit)


def load_env_credentials(prefix: str, logger=None) -> List[Dict[str, str]]:
    return _credentials.load_env_credentials(prefix)
