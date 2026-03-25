from __future__ import annotations
"""
page_classifier.py – Classify pages and buttons during the apply loop.

Provides:
  classify_page(page, frame) -> PageType
  classify_button(text)      -> StepIntent
  find_unresolved_required_fields(frame) -> list[dict]
"""

import re
from typing import Optional

from playwright.sync_api import Page, Frame

from core.outcome import PageType, StepIntent


# ---------------------------------------------------------------------------
# URL pattern helpers
# ---------------------------------------------------------------------------

_SUCCESS_URL_PATTERNS = [
    r"thank[_-]?you",
    r"confirmation",
    r"confirmed",
    r"success",
    r"applied",
    r"application[_-]?received",
    r"submitted",
    r"complete",
]

_LOGIN_URL_PATTERNS = [
    r"/login",
    r"/signin",
    r"/sign[_-]?in",
    r"/auth",
    r"/account/login",
]


def _url_matches(url: str, patterns: list[str]) -> bool:
    url_lower = url.lower()
    for pat in patterns:
        if re.search(pat, url_lower):
            return True
    return False


# ---------------------------------------------------------------------------
# Text classifiers
# ---------------------------------------------------------------------------

_SUCCESS_TEXT_PATTERNS = [
    "thank you",
    "application received",
    "we'll be in touch",
    "we will be in touch",
    "successfully submitted",
    "your application has been received",
    "your application has been submitted",
    "application submitted",
    "you've applied",
    "you have applied",
    "you applied",
    "submission complete",
    "you're all set",
    "we got your application",
]

_LOGIN_TEXT_PATTERNS = [
    "sign in to apply",
    "log in to apply",
    "create an account to apply",
    "please log in",
    "please sign in",
    "sign in to continue",
    "log in to continue",
    "enter your password",
    "sign in with",
    "log in with",
    "create an account",
    "register to continue",
    "verify your email",
    "confirm your email address",
]

_CAPTCHA_TEXT_PATTERNS = [
    "prove you're human",
    "prove you are human",
    "complete the captcha",
    "robot check",
    "verify you are human",
    "verify that you are human",
    "i am not a robot",
    "i'm not a robot",
    "recaptcha",
    "hcaptcha",
    "human verification",
    "security check",
    "bot protection",
]

_REVIEW_TEXT_PATTERNS = [
    "review your application",
    "please review",
    "confirm your information",
    "review and submit",
    "review application",
    "verify your information",
    "check your answers",
    "application summary",
    "review your answers",
]

_ERROR_TEXT_PATTERNS = [
    "something went wrong",
    "an error occurred",
    "page not found",
    "404",
    "500 error",
    "internal server error",
    "service unavailable",
    "503",
]


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _text_matches(haystack: str, patterns: list[str]) -> bool:
    h = _norm_text(haystack)
    for pat in patterns:
        if pat in h:
            return True
    return False


def _get_page_text(page: Page, frame: Frame) -> str:
    """Get visible text from the page safely."""
    parts = []
    for scope in (frame, page.main_frame if frame != page.main_frame else None):
        if scope is None:
            continue
        try:
            text = scope.evaluate("() => document.body?.innerText || ''")
            if text:
                parts.append(str(text))
        except Exception:
            pass
    return " ".join(parts)


def _get_headings(frame: Frame) -> list[str]:
    """Get visible headings from the frame."""
    try:
        return frame.evaluate(r"""() => {
            return Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]'))
                .map(el => el.innerText.trim())
                .filter(Boolean)
                .slice(0, 10);
        }""")
    except Exception:
        return []


def _get_button_texts(frame: Frame) -> list[str]:
    """Get all visible button texts."""
    try:
        return frame.evaluate(r"""() => {
            return Array.from(document.querySelectorAll(
                'button, input[type="submit"], a[role="button"]'
            ))
            .filter(el => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
            })
            .map(el => el.innerText.trim() || el.getAttribute('value') || '')
            .filter(Boolean)
            .slice(0, 15);
        }""")
    except Exception:
        return []


def _has_form_fields(frame: Frame) -> bool:
    """Check if there are visible, interactive form fields."""
    try:
        return frame.evaluate(r"""() => {
            const inputs = document.querySelectorAll(
                'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]),' +
                'textarea, select, [role="combobox"], [role="textbox"]'
            );
            for (const el of inputs) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) return true;
            }
            return false;
        }""")
    except Exception:
        return False


# ---------------------------------------------------------------------------
# classify_page
# ---------------------------------------------------------------------------

def classify_page(page: Page, frame: Frame) -> PageType:
    """
    Classify the current page state for routing in the apply loop.

    Returns a PageType enum value.
    """
    # 1. URL-based checks (fastest)
    try:
        current_url = page.url or ""
    except Exception:
        current_url = ""

    if _url_matches(current_url, _SUCCESS_URL_PATTERNS):
        return PageType.SUCCESS

    if _url_matches(current_url, _LOGIN_URL_PATTERNS):
        return PageType.LOGIN_WALL

    # 2. Get page text for content analysis
    page_text = _get_page_text(page, frame)
    headings = _get_headings(frame)
    button_texts = _get_button_texts(frame)

    all_text = page_text + " " + " ".join(headings) + " " + " ".join(button_texts)

    # 3. Success detection
    if _text_matches(all_text, _SUCCESS_TEXT_PATTERNS):
        return PageType.SUCCESS

    # 4. Login wall detection
    if _text_matches(all_text, _LOGIN_TEXT_PATTERNS):
        return PageType.LOGIN_WALL

    # 5. CAPTCHA detection
    if _text_matches(all_text, _CAPTCHA_TEXT_PATTERNS):
        return PageType.CAPTCHA

    # 6. Site error detection
    if _text_matches(all_text, _ERROR_TEXT_PATTERNS):
        return PageType.SITE_ERROR

    # 7. Review page detection
    if _text_matches(all_text, _REVIEW_TEXT_PATTERNS):
        # Also check for submit-type buttons on review pages
        btn_lower = " ".join(button_texts).lower()
        if any(w in btn_lower for w in ["submit", "finish"]):
            return PageType.FINAL_SUBMIT
        return PageType.REVIEW

    # 8. Button-based classification
    for btn_text in button_texts:
        intent = classify_button(btn_text)
        if intent == StepIntent.CLICK_SUBMIT:
            return PageType.FINAL_SUBMIT
        if intent in (StepIntent.CLICK_NEXT, StepIntent.CLICK_CONTINUE):
            return PageType.FILL
        if intent == StepIntent.CLICK_REVIEW:
            return PageType.REVIEW

    # 9. If there are visible fields, it's a fill page
    if _has_form_fields(frame):
        return PageType.FILL

    return PageType.UNKNOWN


# ---------------------------------------------------------------------------
# classify_button
# ---------------------------------------------------------------------------

def classify_button(text: str) -> StepIntent:
    """
    Map button text to a StepIntent.

    Args:
        text: Button label text (case-insensitive).

    Returns:
        StepIntent enum value.
    """
    t = _norm_text(text)

    # Abort / auth triggers — check before submit to avoid false matches
    if any(w in t for w in ["sign in", "log in", "login", "create account", "register", "verify"]):
        return StepIntent.ABORT

    # Submit / finish
    if any(w in t for w in [
        "submit application", "submit your application",
        "submit and apply", "apply now", "finish application",
        "complete application", "submit", "finish",
        "send application", "apply",
    ]):
        # Extra guard: don't classify "save and continue" as submit
        if "continue" in t or "save" in t and "submit" not in t:
            return StepIntent.CLICK_CONTINUE
        return StepIntent.CLICK_SUBMIT

    # Review
    if any(w in t for w in ["review application", "review your application", "review"]):
        return StepIntent.CLICK_REVIEW

    # Next / continue / proceed
    if any(w in t for w in ["next", "continue", "proceed", "save & continue", "save and continue", "next step"]):
        return StepIntent.CLICK_NEXT

    # Save / draft
    if any(w in t for w in ["save draft", "save for later", "save progress"]):
        return StepIntent.WAIT

    # Default: treat as next for unknown buttons on fill pages
    return StepIntent.CLICK_NEXT


# ---------------------------------------------------------------------------
# find_unresolved_required_fields
# ---------------------------------------------------------------------------

def find_unresolved_required_fields(frame: Frame) -> list[dict]:
    """
    Return a list of visible required fields that appear to be empty/unfilled.

    Each dict: {label, selector, type, element_id, name}
    """
    try:
        return frame.evaluate(r"""() => {
            const unresolved = [];
            const inputs = document.querySelectorAll(
                'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]),' +
                'textarea, select, [role="combobox"], [role="textbox"]'
            );

            for (const el of inputs) {
                // Must be visible
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;

                // Must be required
                const required = el.required ||
                    el.getAttribute('aria-required') === 'true' ||
                    el.closest('[data-required="true"]') !== null ||
                    el.closest('.required') !== null;
                if (!required) continue;

                // Must be empty or unchecked
                const tag = el.tagName.toLowerCase();
                const type = (el.getAttribute('type') || tag).toLowerCase();
                let isEmpty = false;

                if (type === 'checkbox' || type === 'radio') {
                    isEmpty = !el.checked;
                } else if (tag === 'select') {
                    isEmpty = !el.value || el.selectedIndex <= 0;
                } else if (el.getAttribute('role') === 'combobox') {
                    isEmpty = !el.value || el.value.trim() === '';
                } else {
                    isEmpty = !el.value || el.value.trim() === '';
                }

                if (!isEmpty) continue;

                // Find label
                let label = el.getAttribute('aria-label') || '';
                const id = el.id || '';
                if (!label && id) {
                    const lEl = document.querySelector('label[for=' + JSON.stringify(id) + ']');
                    if (lEl) label = lEl.innerText.trim();
                }
                if (!label) {
                    const wrapper = el.closest('.form-group, fieldset, [class*="question"], [class*="field"]');
                    if (wrapper) {
                        const lEl = wrapper.querySelector('label, legend');
                        if (lEl) label = lEl.innerText.trim();
                    }
                }

                // Build selector
                const name = el.getAttribute('name') || '';
                let selector = '';
                if (id) selector = '#' + id;
                else if (name) selector = '[name=' + JSON.stringify(name) + ']';
                else selector = tag + '[type=' + JSON.stringify(type) + ']';

                unresolved.push({
                    label: label.substring(0, 150),
                    selector,
                    type,
                    element_id: id,
                    name,
                });
            }
            return unresolved;
        }""")
    except Exception as exc:
        print(f"[page_classifier] find_unresolved_required_fields error: {exc}")
        return []
