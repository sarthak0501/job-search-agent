from __future__ import annotations
import os
import pathlib

import yaml

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

class AppConfig(dict):
    @property
    def compliance(self): return self.get("compliance", {})

def resolve_config_path(path: str | os.PathLike | None = None) -> pathlib.Path:
    if path is None:
        return DEFAULT_CONFIG_PATH

    resolved = pathlib.Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = PROJECT_ROOT / resolved
    return resolved


def load_config(path: str | os.PathLike | None = None) -> AppConfig:
    config_path = resolve_config_path(path)
    if not config_path.exists():
        return AppConfig()
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig(data)
