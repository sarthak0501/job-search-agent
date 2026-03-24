from __future__ import annotations
import json
import os
import pathlib
import tempfile
from typing import Optional

PROFILE_PATH = pathlib.Path(__file__).resolve().parents[1] / "profile.json"


def profile_exists() -> bool:
    return PROFILE_PATH.exists()


def load_profile() -> Optional[dict]:
    if not PROFILE_PATH.exists():
        return None
    with PROFILE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_profile(data: dict) -> None:
    """Atomically write profile.json."""
    dir_ = PROFILE_PATH.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, str(PROFILE_PATH))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
