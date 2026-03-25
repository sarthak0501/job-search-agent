from __future__ import annotations
"""
debug_artifacts.py – Write structured debug artifacts for every apply attempt.

Writes to /tmp/apply_debug/{job_id}/{timestamp}/
Files:
  fields.json     – extracted field metadata from the page
  mappings.json   – canonical key mappings (deterministic + LLM)
  actions.json    – actions that were executed
  states.json     – full state-tracker log
  failure.txt     – failure reason (if any)

Screenshots are taken at key moments and stored as PNG files.
Keeps the last MAX_DEBUG_DIRS directories per job_id (rotates old ones).
"""

import json
import os
import pathlib
import shutil
import time
from typing import Optional

MAX_DEBUG_DIRS = 10
DEBUG_ROOT = pathlib.Path("/tmp/apply_debug")


def _job_dir(job_id: str | int) -> pathlib.Path:
    return DEBUG_ROOT / str(job_id)


def _rotate(job_dir: pathlib.Path) -> None:
    """Remove oldest debug dirs if more than MAX_DEBUG_DIRS exist."""
    if not job_dir.exists():
        return
    entries = sorted(
        [d for d in job_dir.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime,
    )
    while len(entries) >= MAX_DEBUG_DIRS:
        oldest = entries.pop(0)
        try:
            shutil.rmtree(oldest)
        except Exception:
            pass


def _safe_write_json(path: pathlib.Path, data) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as exc:
        print(f"[debug_artifacts] failed to write {path}: {exc}")


def _safe_write_text(path: pathlib.Path, text: str) -> None:
    try:
        with path.open("w", encoding="utf-8") as f:
            f.write(text)
    except Exception as exc:
        print(f"[debug_artifacts] failed to write {path}: {exc}")


class DebugArtifacts:
    """
    Collects debug data during an apply attempt and writes it to disk.

    Usage:
        dbg = DebugArtifacts(job_id=42)
        dbg.record_fields(extracted_fields)
        dbg.record_mappings(canonical_mappings)
        dbg.record_actions(actions_executed)
        dbg.record_states(state_log)
        dbg.take_screenshot(page, "before_submit")
        dbg.write(failure_reason="stuck on step 2")
    """

    def __init__(self, job_id: str | int, attempt: int = 1) -> None:
        self.job_id = str(job_id)
        self.attempt = attempt
        self._timestamp = time.strftime("%Y%m%d_%H%M%S")
        self._dir: Optional[pathlib.Path] = None

        self._fields: list[dict] = []
        self._mappings: list[dict] = []
        self._actions: list[dict] = []
        self._states: list[dict] = []
        self._screenshots: list[str] = []
        self._failure_reason: str = ""

    def _ensure_dir(self) -> pathlib.Path:
        if self._dir is None:
            job_dir = _job_dir(self.job_id)
            _rotate(job_dir)
            run_dir = job_dir / f"{self._timestamp}_attempt{self.attempt}"
            run_dir.mkdir(parents=True, exist_ok=True)
            self._dir = run_dir
        return self._dir

    # ── Collection methods ────────────────────────────────────────────────────

    def record_fields(self, fields: list[dict]) -> None:
        self._fields = list(fields)

    def record_mappings(self, mappings: list[dict]) -> None:
        """
        mappings: list of {field_id, label, canonical_key, profile_value, confidence, source}
        source = "deterministic" | "llm"
        """
        self._mappings = list(mappings)

    def record_actions(self, actions: list[dict]) -> None:
        self._actions = list(actions)

    def record_states(self, state_log: list[dict]) -> None:
        self._states = list(state_log)

    def set_failure(self, reason: str) -> None:
        self._failure_reason = reason

    def take_screenshot(self, page, label: str = "screenshot") -> str:
        """Take a screenshot and return the path. Never raises."""
        try:
            d = self._ensure_dir()
            safe_label = label.replace("/", "_").replace(" ", "_")
            path = str(d / f"{safe_label}.png")
            page.screenshot(path=path, full_page=False)
            self._screenshots.append(path)
            print(f"[debug_artifacts] screenshot -> {path}")
            return path
        except Exception as exc:
            print(f"[debug_artifacts] screenshot failed ({label}): {exc}")
            return ""

    # ── Write ─────────────────────────────────────────────────────────────────

    def write(self, failure_reason: str = "") -> str:
        """Write all collected data to disk. Returns the output directory path."""
        if failure_reason:
            self._failure_reason = failure_reason

        d = self._ensure_dir()

        _safe_write_json(d / "fields.json", self._fields)
        _safe_write_json(d / "mappings.json", self._mappings)
        _safe_write_json(d / "actions.json", self._actions)
        _safe_write_json(d / "states.json", self._states)

        if self._failure_reason:
            _safe_write_text(d / "failure.txt", self._failure_reason)

        # Write a summary manifest
        manifest = {
            "job_id": self.job_id,
            "attempt": self.attempt,
            "timestamp": self._timestamp,
            "field_count": len(self._fields),
            "mapping_count": len(self._mappings),
            "action_count": len(self._actions),
            "state_count": len(self._states),
            "screenshots": self._screenshots,
            "failure_reason": self._failure_reason,
        }
        _safe_write_json(d / "manifest.json", manifest)

        print(f"[debug_artifacts] wrote artifacts to {d}")
        return str(d)
