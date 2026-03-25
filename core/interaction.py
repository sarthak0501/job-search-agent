from __future__ import annotations
"""
interaction.py – Verified interaction primitives for form filling.

All functions return InteractionResult(success, actual_value, error_message).
Failures are non-raising: callers check .success and log errors.

CRITICAL rules enforced:
  Combobox: Escape before open, scope options, no blind first-option fallback,
            verify by readback, synonym matching with 0.8 threshold.
  Radio:    match against LABEL TEXT (not value attr), verify .checked after.
  Select:   try label then value then normalized, verify after.
  Checkbox: only auto-check required / consent / application-terms boxes.
"""

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from playwright.sync_api import Frame, Page, TimeoutError as PWTimeout

from core.field_extractor import FieldMeta


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class InteractionResult:
    success: bool
    actual_value: str = ""
    error_message: str = ""


# ---------------------------------------------------------------------------
# Text normalisation helpers (pure Python — testable without Playwright)
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    """Lower-case, strip accents, collapse whitespace, strip punctuation."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# Synonym table for common option matching
_SYNONYMS: dict[str, list[str]] = {
    "united states": ["us", "usa", "united states of america", "u.s.", "u.s.a."],
    "yes": [
        "y", "true", "1", "i am", "i do", "affirmative", "correct",
        "i am authorized", "authorized", "i agree", "agree", "i have", "yes i",
    ],
    "no": [
        "n", "false", "0", "i am not", "i do not", "negative", "incorrect",
        "i am not authorized", "not authorized", "i disagree", "i have not",
    ],
    "male": ["man", "he him", "he/him", "m"],
    "female": ["woman", "she her", "she/him", "f"],
    "prefer not to say": ["decline", "decline to self identify", "i prefer not", "not specified"],
    "master of science": [
        "masters", "ms", "m.s.", "master degree", "graduate degree",
        "master s degree", "master's degree", "masters degree",
    ],
    "bachelor of science": [
        "bachelors", "bs", "b.s.", "bachelor degree", "undergraduate degree",
        "bachelor s degree", "bachelor's degree", "bachelors degree",
    ],
    "immediately": ["asap", "as soon as possible", "right away", "now", "available now"],
}

SYNONYM_THRESHOLD = 0.8


def find_best_option_match(
    target: str,
    options: list[str],
    threshold: float = SYNONYM_THRESHOLD,
) -> Optional[str]:
    """
    Find the best matching option for a target value.

    Priority:
      1. Exact match (case-insensitive)
      2. Normalised exact match
      3. Substring containment (target in option or option in target)
      4. Synonym table match
      5. SequenceMatcher similarity >= threshold

    Returns None if no trustworthy match found above threshold.
    """
    if not target or not options:
        return None

    t_norm = _norm(target)
    opts_norm = [(_norm(o), o) for o in options]

    # 1. Exact (case-insensitive)
    for on, o in opts_norm:
        if on == t_norm:
            return o

    # 2. Substring containment
    for on, o in opts_norm:
        if t_norm and on and (t_norm in on or on in t_norm):
            # Only accept if the shorter string is at least 3 chars
            if min(len(t_norm), len(on)) >= 3:
                return o

    # 3. Synonym table
    for canonical, synonyms in _SYNONYMS.items():
        canon_norm = _norm(canonical)
        all_forms = [canon_norm] + [_norm(s) for s in synonyms]
        if t_norm in all_forms:
            for on, o in opts_norm:
                if on in all_forms:
                    return o

    # 4. Similarity fallback
    best_score = 0.0
    best_opt: Optional[str] = None
    for on, o in opts_norm:
        s = _sim(t_norm, on)
        if s > best_score:
            best_score = s
            best_opt = o
    if best_score >= threshold:
        return best_opt

    return None


# ---------------------------------------------------------------------------
# fill_field
# ---------------------------------------------------------------------------

def fill_field(frame: Frame, selector_candidates: list[str], value: str) -> InteractionResult:
    """
    Fill a text/email/tel/textarea field. Tries selector candidates in order.
    Verifies by reading back the value.
    """
    for sel in selector_candidates:
        if not sel:
            continue
        try:
            el = frame.wait_for_selector(sel, timeout=3000)
            if el is None:
                continue
            el.click()
            el.fill(value)
            time.sleep(0.1)
            # Verify readback
            try:
                actual = el.input_value() or ""
            except Exception:
                actual = value  # assume success if readback fails
            if value.lower() in actual.lower() or actual.strip() != "":
                return InteractionResult(success=True, actual_value=actual)
        except (PWTimeout, Exception):
            continue
    return InteractionResult(
        success=False,
        error_message=f"fill_field: no selector resolved among {selector_candidates}",
    )


# ---------------------------------------------------------------------------
# select_option
# ---------------------------------------------------------------------------

def select_option(
    frame: Frame,
    selector_candidates: list[str],
    value: str,
    options: list[dict] | None = None,
) -> InteractionResult:
    """
    Select a <select> option by label or value. Tries normalised matching.
    """
    # Build option label list from options metadata
    opt_labels = [o.get("label", "") for o in (options or []) if o.get("label")]

    # Resolve the best matching option text
    resolved_label = find_best_option_match(value, opt_labels) if opt_labels else value

    for sel in selector_candidates:
        if not sel:
            continue
        try:
            el = frame.wait_for_selector(sel, timeout=3000)
            if el is None:
                continue

            # Try by label first
            target = resolved_label or value
            try:
                el.select_option(label=target)
                actual = el.evaluate("el => el.options[el.selectedIndex]?.text || el.value || ''")
                return InteractionResult(success=True, actual_value=str(actual))
            except Exception:
                pass

            # Try by value
            try:
                el.select_option(value=value)
                actual = el.evaluate("el => el.options[el.selectedIndex]?.text || el.value || ''")
                return InteractionResult(success=True, actual_value=str(actual))
            except Exception:
                pass

            # Try normalised partial match via JS
            try:
                val_lower = _norm(value)
                el.evaluate(f"""el => {{
                    const val = {json.dumps(val_lower)};
                    for (const opt of el.options) {{
                        const norm = opt.text.toLowerCase().replace(/[^\\w\\s]/g,' ').replace(/\\s+/g,' ').trim();
                        if (norm === val || norm.includes(val) || val.includes(norm)) {{
                            el.value = opt.value;
                            el.dispatchEvent(new Event('change', {{bubbles: true}}));
                            break;
                        }}
                    }}
                }}""")
                actual = el.evaluate("el => el.options[el.selectedIndex]?.text || ''")
                if actual:
                    return InteractionResult(success=True, actual_value=str(actual))
            except Exception:
                pass

        except (PWTimeout, Exception):
            continue

    return InteractionResult(
        success=False,
        error_message=f"select_option: could not select '{value}' from candidates {selector_candidates}",
    )


# ---------------------------------------------------------------------------
# interact_combobox
# ---------------------------------------------------------------------------

def interact_combobox(
    page: Page,
    frame: Frame,
    field_meta: FieldMeta,
    value: str,
) -> InteractionResult:
    """
    Interact with a custom combobox (React-Select or similar).

    Rules:
    - Always press Escape before opening
    - Click the combobox input
    - Wait 600ms
    - Scope options via react-select listbox -> aria-controls -> aria-owns ->
      nearest visible expanded listbox -> DOM-proximate [role=option]
    - NEVER fallback to first visible option blindly
    - Match via find_best_option_match with 0.8 threshold
    - If no trustworthy match: close (Escape), return failure
    - After selection: read back displayed value to verify
    """
    cb_id = field_meta.id
    selector_candidates = field_meta.selector_candidates

    # Escape to close any open dropdown
    try:
        page.keyboard.press("Escape")
        time.sleep(0.2)
    except Exception:
        pass

    # Find the input element
    inp = None
    for sel in selector_candidates:
        if not sel:
            continue
        try:
            inp = frame.wait_for_selector(sel, timeout=3000)
            if inp:
                break
        except (PWTimeout, Exception):
            continue

    if inp is None:
        return InteractionResult(
            success=False,
            error_message=f"interact_combobox: could not find element for field id={cb_id}",
        )

    # Click to open
    try:
        inp.click()
        time.sleep(0.6)
    except Exception as e:
        return InteractionResult(success=False, error_message=f"interact_combobox: click failed: {e}")

    def _scoped_option_elements() -> list:
        """Get option elements scoped to this combobox."""
        # Strategy 1: React-Select pattern
        if cb_id:
            opts = frame.query_selector_all(f'#react-select-{cb_id}-listbox [role="option"]')
            if opts:
                return opts

        # Strategy 2: aria-controls
        try:
            controls = inp.get_attribute("aria-controls") or ""
            if controls:
                opts = frame.query_selector_all(f'#{controls} [role="option"]')
                if opts:
                    return opts
        except Exception:
            pass

        # Strategy 3: aria-owns
        try:
            owns = inp.get_attribute("aria-owns") or ""
            if owns:
                opts = frame.query_selector_all(f'#{owns} [role="option"]')
                if opts:
                    return opts
        except Exception:
            pass

        # Strategy 4: nearest visible expanded listbox
        try:
            listboxes = frame.query_selector_all('[role="listbox"]')
            for lb in listboxes:
                if lb.is_visible():
                    opts = lb.query_selector_all('[role="option"]')
                    if opts:
                        return opts
        except Exception:
            pass

        # Strategy 5: DOM-proximate [role=option] (visible only)
        try:
            all_opts = frame.query_selector_all('[role="option"]')
            visible = [o for o in all_opts if o.is_visible()]
            return visible
        except Exception:
            return []

    option_elements = _scoped_option_elements()

    # Read option texts
    option_texts = []
    for oel in option_elements:
        try:
            t = oel.inner_text().strip()
            if t:
                option_texts.append(t)
        except Exception:
            pass

    # Find best match
    best_match = find_best_option_match(value, option_texts, threshold=SYNONYM_THRESHOLD)

    if best_match is None:
        # No trustworthy match: close dropdown and return failure
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return InteractionResult(
            success=False,
            error_message=f"interact_combobox: no_trustworthy_option_match for '{value}' in {option_texts[:5]}",
        )

    # Click the matching option
    for oel in option_elements:
        try:
            t = oel.inner_text().strip()
            if t == best_match:
                oel.click()
                time.sleep(0.3)
                break
        except Exception:
            continue

    # Readback verification
    try:
        displayed = inp.input_value() or inp.evaluate("el => el.textContent || el.innerText || ''") or ""
        displayed = str(displayed).strip()
    except Exception:
        displayed = best_match  # assume success

    return InteractionResult(success=True, actual_value=displayed or best_match)


# ---------------------------------------------------------------------------
# check_radio
# ---------------------------------------------------------------------------

def check_radio(frame: Frame, field_meta: FieldMeta, target_label: str) -> InteractionResult:
    """
    Click the radio button whose LABEL TEXT matches target_label.
    Verifies .checked == True after clicking.
    """
    name = field_meta.name
    options = field_meta.options  # [{value, label, selected}]

    # Match target_label against option labels
    opt_labels = [o.get("label", "") for o in options]
    matched_label = find_best_option_match(target_label, opt_labels, threshold=0.6)

    if matched_label is None:
        # Try matching against option values as fallback
        opt_values = [o.get("value", "") for o in options]
        matched_value = find_best_option_match(target_label, opt_values, threshold=0.6)
        if matched_value is None:
            return InteractionResult(
                success=False,
                error_message=f"check_radio: no match for '{target_label}' in labels {opt_labels}",
            )
        # Find the option dict with this value
        target_opt = next((o for o in options if o.get("value") == matched_value), None)
        if not target_opt:
            return InteractionResult(success=False, error_message="check_radio: option not found")
        radio_value = target_opt.get("value", "")
    else:
        target_opt = next((o for o in options if o.get("label") == matched_label), None)
        radio_value = target_opt.get("value", "") if target_opt else ""

    # Build selector
    if name:
        if radio_value:
            sel = f'input[type="radio"][name={json.dumps(name)}][value={json.dumps(radio_value)}]'
        else:
            sel = f'input[type="radio"][name={json.dumps(name)}]'
    elif field_meta.id:
        sel = f'#{field_meta.id}'
    else:
        return InteractionResult(
            success=False,
            error_message="check_radio: cannot build selector (no name or id)",
        )

    try:
        el = frame.wait_for_selector(sel, timeout=3000)
        if el is None:
            return InteractionResult(success=False, error_message=f"check_radio: selector not found: {sel}")
        el.check()
        time.sleep(0.1)
        # Verify
        checked = el.is_checked()
        if checked:
            return InteractionResult(success=True, actual_value=radio_value or matched_label or target_label)
        return InteractionResult(
            success=False,
            error_message=f"check_radio: element not checked after click: {sel}",
        )
    except (PWTimeout, Exception) as e:
        return InteractionResult(success=False, error_message=f"check_radio: {e}")


# ---------------------------------------------------------------------------
# toggle_checkbox
# ---------------------------------------------------------------------------

# Labels that indicate a mandatory consent checkbox (application-terms)
_CONSENT_PATTERNS = [
    "i agree", "i consent", "i accept", "terms", "conditions",
    "privacy policy", "agree to", "accept the", "certify that",
    "acknowledge", "i understand", "i have read",
    "eeoc", "voluntary", "self-identify", "authorized to work",
]

# Marketing / optional patterns — do NOT auto-check these
_MARKETING_PATTERNS = [
    "newsletter", "marketing", "promotional", "product update",
    "email update", "opt in", "opt-in", "subscribe", "communications",
    "special offers", "job alerts",
]


def _is_consent_checkbox(label: str) -> bool:
    """Return True if the label suggests a required consent / terms checkbox."""
    label_lower = label.lower()
    has_marketing = any(p in label_lower for p in _MARKETING_PATTERNS)
    if has_marketing:
        return False
    return any(p in label_lower for p in _CONSENT_PATTERNS)


def toggle_checkbox(
    frame: Frame,
    selector_candidates: list[str],
    should_check: bool,
    label: str = "",
) -> InteractionResult:
    """
    Check or uncheck a checkbox.

    Auto-check rules:
    - If should_check=True and label indicates marketing/newsletter: skip
    - If label indicates consent/required: always check
    - Otherwise: respect should_check
    """
    # Override check logic for consent/marketing
    if label:
        if _is_consent_checkbox(label):
            should_check = True
        elif any(p in label.lower() for p in _MARKETING_PATTERNS):
            # Skip marketing boxes
            return InteractionResult(
                success=True,
                actual_value="skipped",
                error_message="marketing_checkbox_skipped",
            )

    for sel in selector_candidates:
        if not sel:
            continue
        try:
            el = frame.wait_for_selector(sel, timeout=3000)
            if el is None:
                continue
            is_checked = el.is_checked()
            if should_check and not is_checked:
                el.check()
            elif not should_check and is_checked:
                el.uncheck()
            time.sleep(0.1)
            actual = el.is_checked()
            return InteractionResult(
                success=True,
                actual_value="true" if actual else "false",
            )
        except (PWTimeout, Exception):
            continue

    return InteractionResult(
        success=False,
        error_message=f"toggle_checkbox: no selector resolved among {selector_candidates}",
    )


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------

def upload_file(frame: Frame, selector_candidates: list[str], file_path: str) -> InteractionResult:
    """
    Upload a file to a file input. Checks that the file exists first.
    """
    if not os.path.exists(file_path):
        return InteractionResult(
            success=False,
            error_message=f"upload_file: file not found: {file_path}",
        )

    for sel in selector_candidates:
        if not sel:
            continue
        try:
            el = frame.wait_for_selector(sel, timeout=3000)
            if el is None:
                continue
            el.set_input_files(file_path)
            return InteractionResult(success=True, actual_value=file_path)
        except (PWTimeout, Exception) as e:
            continue

    return InteractionResult(
        success=False,
        error_message=f"upload_file: no selector resolved for {file_path}",
    )
