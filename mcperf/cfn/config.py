"""Load and persist the cfn config (scenarios/cfn/config.yaml, or $MCPERF_CFN_CONFIG)."""

import os
from pathlib import Path

import yaml

_DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "scenarios" / "cfn" / "config.yaml"
CONFIG_FILE = Path(os.environ.get("MCPERF_CFN_CONFIG", _DEFAULT_CONFIG))


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
