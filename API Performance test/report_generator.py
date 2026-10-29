#!/usr/bin/env python3
"""
Compatibility shim — the report generator now lives in mcperf.reporting.locust_report.

Kept so locustfile.py, cost_dashboard_locustfile.py and day2sdk_locustfile.py
keep writing reports to "API Performance test/reports/" until they are migrated
onto mcperf (restructure phase 3). Do not add code here.
"""

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcperf.reporting import locust_report as _impl  # noqa: E402


def generate(*args, **kwargs) -> Path:
    kwargs.setdefault("reports_dir", _HERE / "reports")
    return _impl.generate(*args, **kwargs)


if __name__ == "__main__":
    args = _impl._build_parser().parse_args()
    out = generate(Path(args.csv), Path(args.output) if args.output else None)
    print(f"Report written to: {out}")
