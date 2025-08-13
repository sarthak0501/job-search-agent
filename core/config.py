from __future__ import annotations
import os, yaml

class AppConfig(dict):
    @property
    def compliance(self): return self.get("compliance", {})

def load_config(path: str = "config.yaml") -> AppConfig:
    if not os.path.exists(path):
        return AppConfig()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig(data)
