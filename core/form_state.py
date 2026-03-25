from __future__ import annotations
"""
form_state.py – Page-state tracking to detect loops during multi-step apply.

Uses a fingerprint (hash of stable page signals) to detect when the applier
is stuck on the same page state or oscillating between a fixed set of states.

Progress-aware: does not mark stuck if visible field count decreased, page
classification changed, or URL path changed (indicating real progress was made).
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
      "new"                         – fingerprint not seen before
      "repeated"                    – fingerprint seen before (possible loop)
      "stuck"                       – fingerprint seen STUCK_REPEAT_THRESHOLD+ times OR
                                      total attempts >= STUCK_TOTAL_THRESHOLD
      "stuck_same_page_no_progress" – same fp + same field_count + no URL change
      "cycling_between_steps"       – oscillating between exactly 2 known fingerprints
      "repeated_validation_errors"  – same fp with error_texts present repeatedly

    Progress-aware: a "stuck" classification is NOT issued if:
      - visible field count decreased (form is making progress)
      - the URL path changed meaningfully
      - page classification changed (handled by caller via reset())
    """

    def __init__(self) -> None:
        self._history: list[dict] = []
        self._fingerprint_counts: dict[str, int] = {}

    @staticmethod
    def _url_path(url: str) -> str:
        try:
            from urllib.parse import urlparse
            return urlparse(url).path
        except Exception:
            return url

    def _made_progress(self, state: FormState) -> bool:
        """
        Return True if we can observe forward progress vs. the most recent entry.
        Progress means: fewer visible fields OR URL path changed.
        """
        if not self._history:
            return False
        prev = self._history[-1]
        prev_field_count = prev.get("field_count", -1)
        prev_url_path = self._url_path(prev.get("url", ""))
        curr_url_path = self._url_path(state.url)
        field_decreased = state.field_count < prev_field_count and prev_field_count > 0
        url_changed = curr_url_path != prev_url_path and curr_url_path not in ("", "/")
        return field_decreased or url_changed

    def _detect_cycling(self, fp: str) -> bool:
        """Return True if we are oscillating between exactly 2 fingerprints."""
        if len(self._history) < 4:
            return False
        recent = [e["fingerprint"] for e in self._history[-4:]]
        unique = set(recent)
        if len(unique) == 2:
            # All 4 recent entries alternate between 2 fps
            return recent[0] == recent[2] and recent[1] == recent[3]
        return False

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

        # ── Progress check: never declare stuck if we see measurable forward motion ──
        made_progress = self._made_progress(state)

        # ── Total threshold ──
        if total >= STUCK_TOTAL_THRESHOLD and not made_progress:
            entry["classification"] = "stuck"
            entry["stuck_type"] = "stuck_same_page_no_progress"
            return "stuck"

        # ── Cycling detection ──
        if self._detect_cycling(fp) and not made_progress:
            entry["classification"] = "stuck"
            entry["stuck_type"] = "cycling_between_steps"
            return "stuck"

        # ── Repeat threshold ──
        if count >= STUCK_REPEAT_THRESHOLD:
            if made_progress:
                # Progress detected despite same fingerprint — keep going
                entry["classification"] = "repeated"
                return "repeated"
            # Check sub-type
            has_errors = bool(state.error_texts)
            if has_errors and count >= 2:
                entry["classification"] = "stuck"
                entry["stuck_type"] = "repeated_validation_errors"
                return "stuck"
            entry["classification"] = "stuck"
            entry["stuck_type"] = "stuck_same_page_no_progress"
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
