"""Load and persist per-account config files under cfn/Configs/."""

from pathlib import Path

import yaml

CONFIGS_DIR = Path(__file__).parent / "Configs"
CONFIG_FILE = CONFIGS_DIR / "config.yaml"


def _resolve_path(config_arg: str | None) -> Path:
    if not config_arg:
        return CONFIG_FILE
    path = Path(config_arg)
    if path.is_absolute() or path.parent != Path("."):
        return path
    return CONFIGS_DIR / config_arg


def load_config(config_arg: str | None = None) -> dict:
    with open(_resolve_path(config_arg)) as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    path = _resolve_path(cfg.get("_config_path"))
    cfg_to_write = {k: v for k, v in cfg.items() if k != "_config_path"}
    with open(path, "w") as f:
        yaml.dump(cfg_to_write, f, default_flow_style=False, sort_keys=False)
