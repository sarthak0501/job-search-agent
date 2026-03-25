from __future__ import annotations
"""
form_state.py – Page-state tracking to detect loops during multi-step apply.

Uses a fingerprint (hash of stable page signals) to detect when the applier
is stuck on the same page state or oscillating between a fixed set of states.
"""

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import Frame, Page

# Maximum number of times the same fingerprint can appear before declaring "stuck"
STUCK_REPEAT_THRESHOLD = 3
# Maximum total attempts before declaring "stuck" regardless of fingerprints
STUCK_TOTAL_THRESHOLD = 8


# ---------------------------------------------------------------------------
# FormState
# ---------------------------------------------------------------------------

@dataclass
class FormState:
    url: str
    headings: list[str]
    question_labels: list[str]
    field_count: int
    error_texts: list[str]
    button_texts: list[str]
    fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.fingerprint = self._compute_fingerprint()

    def _compute_fingerprint(self) -> str:
        """Stable hash of the page state — URL path + sorted labels + field count."""
        # Use only the URL path (not full URL with query params that may rotate)
        try:
            from urllib.parse import urlparse
            url_part = urlparse(self.url).path
        except Exception:
            url_part = self.url

        # Normalise: lower-case, strip whitespace
        def norm(s: str) -> str:
            return re.sub(r"\s+", " ", s.lower().strip())

        parts = [
            url_part,
            "|".join(sorted(norm(h) for h in self.headings)),
            "|".join(sorted(norm(q) for q in self.question_labels)),
            str(self.field_count),
            "|".join(sorted(norm(b) for b in self.button_texts)),
        ]
        raw = "::".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "headings": self.headings,
            "question_labels": self.question_labels,
            "field_count": self.field_count,
            "error_texts": self.error_texts,
            "button_texts": self.button_texts,
            "fingerprint": self.fingerprint,
        }


# ---------------------------------------------------------------------------
# extract_state
# ---------------------------------------------------------------------------

def extract_state(page: Page, frame: Frame) -> FormState:
    """
    Extract a FormState snapshot from the current Playwright page/frame.
    Safe to call at any point — never raises.
    """
    url = ""
    headings: list[str] = []
    question_labels: list[str] = []
    field_count = 0
    error_texts: list[str] = []
    button_texts: list[str] = []

    try:
        url = page.url or ""
    except Exception:
        pass

    try:
        data = frame.evaluate(r"""() => {
            const getText = el => (el ? el.innerText.trim().replace(/\s+/g, ' ') : '');

            // Headings
            const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,[role="heading"]'))
                .map(getText).filter(Boolean).slice(0, 10);

            // Labels for form fields
            const labels = Array.from(document.querySelectorAll('label, legend, [class*="question-title"], [class*="field-label"]'))
                .map(getText).filter(Boolean).slice(0, 40);

            // Visible interactive field count
            const fields = document.querySelectorAll(
                'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]),' +
                'textarea, select, [role="combobox"], [role="textbox"]'
            );
            let fieldCount = 0;
            for (const el of fields) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) fieldCount++;
            }

            // Error texts
            const errors = Array.from(document.querySelectorAll(
                '[class*="error"]:not([class*="error-boundary"]), [class*="invalid"], [aria-invalid="true"], .field_with_errors'
            )).map(getText).filter(t => t && t.length < 200).slice(0, 10);

            // Button texts
            const buttons = Array.from(document.querySelectorAll('button, input[type="submit"], a[role="button"]'))
                .map(getText).filter(Boolean).slice(0, 10);

            return {headings, labels, fieldCount, errors, buttons};
        }""")

        headings = data.get("headings", [])
        question_labels = data.get("labels", [])
        field_count = data.get("fieldCount", 0)
        error_texts = data.get("errors", [])
        button_texts = data.get("buttons", [])
    except Exception as exc:
        print(f"[form_state] extract error: {exc}")

    return FormState(
        url=url,
        headings=headings,
        question_labels=question_labels,
        field_count=field_count,
        error_texts=error_texts,
        button_texts=button_texts,
    )


# ---------------------------------------------------------------------------
# StateTracker
# ---------------------------------------------------------------------------

class StateTracker:
    """
    Records FormState snapshots and detects whether the apply loop is stuck.

    record(state) returns one of:
      "new"      – fingerprint not seen before
      "repeated" – fingerprint seen before (possible loop)
      "stuck"    – fingerprint seen STUCK_REPEAT_THRESHOLD+ times OR
                   total attempts >= STUCK_TOTAL_THRESHOLD
    """

    def __init__(self) -> None:
        self._history: list[dict] = []
        self._fingerprint_counts: dict[str, int] = {}

    def record(self, state: FormState) -> str:
        """Record a state snapshot and classify it."""
        fp = state.fingerprint
        count = self._fingerprint_counts.get(fp, 0) + 1
        self._fingerprint_counts[fp] = count

        total = len(self._history) + 1
        entry = {
            "attempt": total,
            "fingerprint": fp,
            "count_this_fp": count,
            "url": state.url,
            "field_count": state.field_count,
            "headings": state.headings[:3],
            "errors": state.error_texts[:3],
            "buttons": state.button_texts[:3],
            "timestamp": time.time(),
        }
        self._history.append(entry)

        # Determine classification
        if total >= STUCK_TOTAL_THRESHOLD:
            entry["classification"] = "stuck"
            return "stuck"
        if count >= STUCK_REPEAT_THRESHOLD:
            entry["classification"] = "stuck"
            return "stuck"
        if count > 1:
            entry["classification"] = "repeated"
            return "repeated"

        entry["classification"] = "new"
        return "new"

    def get_log(self) -> list[dict]:
        """Return full history for debug artifacts."""
        return list(self._history)

    def reset(self) -> None:
        """Reset tracker (e.g. when a new distinct page is detected)."""
        self._history.clear()
        self._fingerprint_counts.clear()

    @property
    def total_attempts(self) -> int:
        return len(self._history)
