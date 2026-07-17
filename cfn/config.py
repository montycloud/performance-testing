"""Load and persist config.yaml."""

from pathlib import Path

import yaml

CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
