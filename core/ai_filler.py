from __future__ import annotations
"""
ai_filler.py – Deterministic + AI-powered form-fill loop (sync Playwright API).

Architecture:
  1. extract_page_data  – HTML + structured fields + scoped combobox options
  2. map_fields_deterministically – uses question_map.CATALOG for high-confidence fills
  3. Phase 1 LLM (once per page) – analyze form HTML -> structured field list
  4. Phase 2 LLM – generate actions ONLY for unmapped / low-confidence fields
  5. Merge deterministic + LLM actions and execute
  6. Check outcome via FormState fingerprint (loop/stuck detection)
  7. On stuck -> abort with debug artifacts

Combobox interaction is ALWAYS scoped to each field's listbox to prevent
cross-dropdown pollution.
"""

import json
import os
import re
import subprocess
import tempfile
import time
from typing import Optional

from playwright.sync_api import Page, Frame, TimeoutError as PWTimeout

from core.question_map import match_field, CATALOG
from core.form_state import FormState, StateTracker, extract_state
from core.debug_artifacts import DebugArtifacts

MAX_ATTEMPTS = 8
MAX_HTML_LENGTH = 50_000
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")

# Confidence threshold above which we skip LLM for a field
DETERMINISTIC_CONFIDENCE = 0.75


# ══════════════════════════════════════════════════════════════════════════════
# HTML / field extraction
# ══════════════════════════════════════════════════════════════════════════════

def _extract_clean_html(frame: Frame) -> str:
    """Return a cleaned, compact version of the frame HTML for LLM analysis."""
    raw = frame.evaluate(r"""() => {
        const clone = document.documentElement.cloneNode(true);

        ['script','style','svg','noscript','link','meta','img',
         'video','audio','canvas','path','head','footer','nav',
         'header'].forEach(tag => {
            clone.querySelectorAll(tag).forEach(el => el.remove());
        });

        clone.querySelectorAll(
            '[style*="display: none"],[style*="display:none"],[hidden]'
        ).forEach(el => el.remove());

        const keep = new Set([
            'id','name','type','value','for','role',
            'aria-label','aria-labelledby','aria-required',
            'aria-describedby','aria-checked','aria-selected',
            'aria-owns','aria-controls',
            'placeholder','required','class','href','action','method',
            'checked','selected','disabled','multiple','accept',
            'data-automation-id','data-testid'
        ]);
        clone.querySelectorAll('*').forEach(el => {
            [...el.attributes].forEach(a => {
                if (!keep.has(a.name)) el.removeAttribute(a.name);
            });
            if (el.className && typeof el.className === 'string' && el.className.length > 80)
                el.className = el.className.substring(0, 80);
        });

        const forms = clone.querySelectorAll('form');
        if (forms.length) return Array.from(forms).map(f => f.outerHTML).join('\n');
        return clone.querySelector('body')?.innerHTML || clone.innerHTML;
    }""")

    if len(raw) > MAX_HTML_LENGTH:
        raw = raw[:MAX_HTML_LENGTH] + "\n<!-- truncated -->"
    return raw


def _extract_form_fields(frame: Frame) -> list[dict]:
    """Grab structured field metadata including label, options, aria attributes."""
    return frame.evaluate(r"""() => {
        const fields = [];
        const seen = new Set();
        const inputs = document.querySelectorAll(
            'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]),' +
            'textarea, select, [role="combobox"], [role="textbox"]'
        );
        inputs.forEach(el => {
            const id   = el.id   || '';
            const name = el.getAttribute('name') || '';
            const key  = id || name || el.tagName + Math.random();
            if (seen.has(key)) return;
            seen.add(key);

            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return;

            // Find label: for attribute, closest wrapper, or aria-label
            let label = el.getAttribute('aria-label') || '';
            if (!label && id) {
                const lEl = document.querySelector('label[for=' + JSON.stringify(id) + ']');
                if (lEl) label = lEl.innerText.trim();
            }
            if (!label) {
                const wrapper = el.closest('.field-wrapper, .select__container, fieldset, .form-group, [class*="question"], [class*="field-container"]');
                if (wrapper) {
                    const l = wrapper.querySelector('label, legend');
                    if (l) label = l.innerText.trim();
                }
            }

            // Labelledby resolution
            if (!label) {
                const lblby = el.getAttribute('aria-labelledby') || '';
                if (lblby) {
                    const lblEl = document.getElementById(lblby);
                    if (lblEl) label = lblEl.innerText.trim();
                }
            }

            let options = [];
            if (el.tagName === 'SELECT') {
                options = Array.from(el.options).map(o => o.text.trim()).filter(Boolean);
            } else if (el.getAttribute('type') === 'radio') {
                options = Array.from(document.querySelectorAll('input[name=' + JSON.stringify(name) + ']'))
                    .map(r => r.value).filter(Boolean);
            }

            const role = el.getAttribute('role') || '';
            const required = el.required || el.getAttribute('aria-required') === 'true';

            fields.push({
                id, name,
                type: el.getAttribute('type') || el.tagName.toLowerCase(),
                tag: el.tagName.toLowerCase(),
                label: label.replace(/\s+/g, ' ').substring(0, 200),
                placeholder: el.getAttribute('placeholder') || '',
                required,
                options,
                role,
                value: el.value || '',
                aria_label: el.getAttribute('aria-label') || '',
                aria_labelledby: el.getAttribute('aria-labelledby') || '',
            });
        });
        return fields;
    }""")


def _extract_combobox_options(frame: Frame) -> dict[str, list[str]]:
    """
    Click each combobox, read its SCOPED options, close it.
    Returns {combobox_id: [option_texts]}.
    Cross-pollution is prevented by only reading options from the
    specific listbox associated with each combobox.
    """
    comboboxes = frame.query_selector_all('[role="combobox"]')
    result: dict[str, list[str]] = {}

    for cb in comboboxes:
        cb_id = cb.get_attribute("id") or ""
        if not cb_id:
            continue
        try:
            cb.click()
            time.sleep(0.6)

            opts: list[str] = []

            # Strategy 1: React-Select scoped listbox
            listbox_sel = f'#react-select-{cb_id}-listbox [role="option"]'
            option_els = frame.query_selector_all(listbox_sel)

            # Strategy 2: aria-owns / aria-controls
            if not option_els:
                owns = cb.get_attribute("aria-owns") or cb.get_attribute("aria-controls") or ""
                if owns:
                    option_els = frame.query_selector_all(f'#{owns} [role="option"]')

            # Strategy 3: aria-expanded listbox sibling
            if not option_els:
                parent = frame.evaluate(
                    f'() => document.getElementById({json.dumps(cb_id)})?.closest("[data-testid], form, .field-wrapper")?.id || ""'
                )
                if parent:
                    option_els = frame.query_selector_all(f'#{parent} [role="option"]')

            for o in option_els[:30]:
                try:
                    t = o.inner_text().strip()
                    if t:
                        opts.append(t)
                except Exception:
                    pass

            if opts:
                result[cb_id] = opts

            # Close
            frame.page.keyboard.press("Escape")
            time.sleep(0.3)
        except Exception as exc:
            print(f"[ai_filler] combobox {cb_id} option extraction error: {exc}")
            try:
                frame.page.keyboard.press("Escape")
            except Exception:
                pass

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Deterministic field mapping
# ══════════════════════════════════════════════════════════════════════════════

def _map_fields_deterministically(
    fields: list[dict],
    combobox_options: dict[str, list[str]],
    profile: dict,
) -> list[dict]:
    """
    Map each extracted field against the CATALOG via question_map.match_field.

    Returns a list of mapping dicts:
    {
        field: <original field dict>,
        canonical_key: str,
        profile_value: str,
        confidence: float,
        source: "deterministic" | "needs_llm",
        options: [str],   # merged from field.options + combobox_options
    }
    """
    mappings = []
    for f in fields:
        # Merge static options with combobox options (scoped)
        cb_opts = combobox_options.get(f.get("id", ""), [])
        all_opts = f.get("options", []) + [o for o in cb_opts if o not in f.get("options", [])]

        ckey, pval, conf = match_field(
            label=f.get("label", ""),
            placeholder=f.get("placeholder", ""),
            name=f.get("name", ""),
            id=f.get("id", ""),
            aria_label=f.get("aria_label", ""),
            options=all_opts,
            profile=profile,
        )

        mappings.append({
            "field": f,
            "canonical_key": ckey,
            "profile_value": pval,
            "confidence": conf,
            "source": "deterministic" if conf >= DETERMINISTIC_CONFIDENCE else "needs_llm",
            "options": all_opts,
        })

    return mappings


def _build_deterministic_actions(mappings: list[dict], profile: dict) -> list[dict]:
    """
    Convert high-confidence deterministic mappings into Playwright actions.
    Skips mappings with empty profile_value (e.g. github when it's "").
    """
    actions = []
    for m in mappings:
        if m["source"] != "deterministic":
            continue
        if not m["profile_value"]:
            continue

        f = m["field"]
        pval = m["profile_value"]
        fid = f.get("id", "")
        fname = f.get("name", "")
        selector = f'[id={json.dumps(fid)}]' if fid else f'[name={json.dumps(fname)}]'
        ftype = f.get("type", "")
        frole = f.get("role", "")
        tag = f.get("tag", "")

        # Upload fields
        if m["canonical_key"] in ("resume_upload", "cover_letter_upload") or ftype == "file":
            if os.path.exists(pval):
                actions.append({"type": "upload", "selector": selector, "value": pval})
            continue

        # Radio
        if ftype == "radio":
            # Build selector targeting the specific radio with matching value
            # Try value-based selector; if options_map resolved the value, trust it
            radio_val = pval
            radio_sel = f'input[name={json.dumps(fname)}][value={json.dumps(radio_val)}]'
            # Fallback: just the name-based selection — LLM will handle ambiguous ones
            actions.append({"type": "radio", "selector": radio_sel, "value": pval})
            continue

        # Native select
        if tag == "select":
            actions.append({"type": "select", "selector": selector, "value": pval})
            continue

        # Combobox (React-Select / custom)
        if frole == "combobox" or "combobox" in ftype:
            actions.append({"type": "combobox", "selector": selector, "value": pval})
            continue

        # Checkbox
        if ftype == "checkbox":
            # Only check if profile value is affirmative
            if str(pval).lower() in ("yes", "true", "1", "i agree", "agree"):
                actions.append({"type": "check", "selector": selector})
            continue

        # Default: fill (text, email, tel, textarea, etc.)
        actions.append({"type": "fill", "selector": selector, "value": pval})

    return actions


# ══════════════════════════════════════════════════════════════════════════════
# LLM helpers
# ══════════════════════════════════════════════════════════════════════════════

def _call_claude_cli(prompt: str) -> str:
    """Call the local claude CLI. Uses a temp file for large prompts."""
    if len(prompt) > 100_000:
        fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="claude_prompt_")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(prompt)
            with open(tmp) as stdin:
                result = subprocess.run(
                    [CLAUDE_BIN, "-p", "-"],
                    stdin=stdin,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
        finally:
            os.unlink(tmp)
    else:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )

    if result.returncode != 0:
        raise RuntimeError(f"claude CLI error: {result.stderr.strip()}")
    return result.stdout.strip()


def _parse_json_array(raw: str) -> list[dict]:
    """Extract a JSON array from LLM response, tolerating markdown fences."""
    text = raw.strip()
    if "```" in text:
        text = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("```")
        )
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        return json.loads(match.group())
    return json.loads(text)


def _detect_step_type(headings: list[str], button_texts: list[str]) -> str:
    """
    Heuristic: classify current page as fill | review | success | unknown.
    """
    all_text = " ".join(headings + button_texts).lower()
    if any(w in all_text for w in ["thank you", "application submitted", "successfully submitted", "you've applied"]):
        return "success"
    if any(w in all_text for w in ["review", "confirm", "summary"]):
        return "review"
    if any(w in all_text for w in ["submit", "next", "continue", "save"]):
        return "fill"
    return "unknown"


def _build_analysis_prompt(html: str, fields: list[dict],
                           combobox_options: dict[str, list[str]]) -> str:
    return f"""You are analyzing a job application form. Study the HTML and field
metadata below, then output a STRUCTURED ANALYSIS of every interactive field.

═══ PAGE HTML (cleaned) ═══
{html}

═══ STRUCTURED FIELDS ═══
{json.dumps(fields, indent=2)}

═══ COMBOBOX OPTIONS (scoped per field id) ═══
{json.dumps(combobox_options, indent=2)}

═══ TASK ═══
For EACH visible interactive field on the form, output ONE line in this exact format:

FIELD: <css_selector> | TYPE: <fill|select|combobox|upload|check|radio|click> | LABEL: <what it asks> | OPTIONS: <comma-separated if applicable> | NOTES: <any special handling>

After listing all fields, output:
SUBMIT_SELECTOR: <css selector for the submit/next button>
MULTI_PAGE: <yes|no — does this look like a multi-page form?>

Be thorough. Include EVERY visible interactive field. For combobox fields,
list the EXACT option values from the COMBOBOX OPTIONS section.
Do NOT include hidden or non-interactive fields.
"""


def _build_action_prompt(
    analysis: str,
    unmapped_fields: list[dict],
    profile: dict,
    prior_errors: list[str],
    attempt: int,
    step_type: str,
) -> str:
    errors_block = ""
    if prior_errors:
        errors_block = (
            f"\n\nPREVIOUS ATTEMPT {attempt - 1} VALIDATION ERRORS — fix these:\n"
            + "\n".join(f"  - {e}" for e in prior_errors)
        )

    p = {k: v for k, v in profile.items() if not k.startswith("_")}
    cl_path = profile.get("_cover_letter_path", "")
    resume_path = profile.get("resume_path", "")

    unmapped_summary = json.dumps(unmapped_fields, indent=2) if unmapped_fields else "None — all fields were mapped deterministically."

    return f"""You are filling a job application form.
Current step type: {step_type}

A prior analysis of the form has been done. Many fields were already mapped
deterministically. You only need to generate actions for the UNMAPPED fields listed below.

═══ FORM ANALYSIS ═══
{analysis}

═══ UNMAPPED / AMBIGUOUS FIELDS (fill these ONLY) ═══
{unmapped_summary}

═══ APPLICANT PROFILE ═══
{json.dumps(p, indent=2)}

═══ FILES ═══
Resume:       {resume_path}
Cover letter: {cl_path or "(none)"}
{errors_block}

═══ OUTPUT FORMAT ═══
Return ONLY a valid JSON array. No markdown fences, no explanation.

ACTION TYPES:
  fill         {{"type":"fill",       "selector":"#id_or_css", "value":"text"}}
  select       {{"type":"select",     "selector":"#id_or_css", "value":"option text"}}
  combobox     {{"type":"combobox",   "selector":"#id_or_css", "value":"exact option text"}}
  upload       {{"type":"upload",     "selector":"#id_or_css", "value":"/abs/path"}}
  check        {{"type":"check",      "selector":"#id_or_css"}}
  radio        {{"type":"radio",      "selector":"input[name='x'][value='y']"}}
  click        {{"type":"click",      "selector":"css selector"}}
  click_submit {{"type":"click_submit"}}

═══ CRITICAL RULES ═══
1. ALWAYS end the array with {{"type":"click_submit"}} for fill/unknown steps.
   On review steps: use {{"type":"click_submit"}} only after confirming it's a submit.
   On success steps: return [].

2. GENDER — applicant is MALE.
   Select "Male", "Man", "He/Him", "M" or masculine option.
   NEVER select "Female", "Woman", "She/Her", "F".
   Fallback: "Decline to self-identify".

3. EEOC fields: use profile values. If no match → "Decline" / "Prefer not to say".

4. For combobox: value MUST be the EXACT text of one of the options listed in the analysis.

5. Phone: raw digits only (no dashes/spaces/country code).
   Phone country combobox: "United States".

6. Resume upload: "{resume_path}". Cover letter: "{cl_path}" (skip if empty).

7. LinkedIn: "{p.get('linkedin', '')}". Skip if empty.

8. Work authorization:
   - "Are you authorized to work in the US?" → Yes (profile: work_authorized={p.get('work_authorized')})
   - "Will you require sponsorship?" → Yes (profile: requires_sponsorship={p.get('requires_sponsorship')}, visa_status={p.get('visa_status')})

9. Consent / "I agree" checkboxes — always check/agree.

10. Open-ended textareas — write 2-3 professional sentences relevant to the question.

11. If a field is already filled correctly, SKIP it.

12. Do NOT re-fill fields that were already mapped deterministically — only handle the UNMAPPED fields listed above.
"""


# ══════════════════════════════════════════════════════════════════════════════
# Element targeting and action execution
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_target(frame: Frame, action: dict, timeout: int = 4000):
    """Find the target element via CSS selector, falling back to id/name attrs."""
    selector = action.get("selector", "").strip()
    if selector:
        try:
            return frame.wait_for_selector(selector, timeout=timeout)
        except Exception:
            pass

    for key in ("id", "name"):
        value = str(action.get(key, "")).strip()
        if not value:
            continue
        quoted = json.dumps(value)
        for sel in (f"[id={quoted}]", f"[name={quoted}]"):
            try:
                return frame.wait_for_selector(sel, timeout=timeout)
            except Exception:
                continue

    raise ValueError(f"Could not locate element for action: {action}")


def _interact_combobox(page: Page, frame: Frame, action: dict, value: str) -> None:
    """
    Open a combobox, find its SCOPED options, pick the matching one.
    Scoping prevents cross-dropdown pollution.
    """
    page.keyboard.press("Escape")
    time.sleep(0.2)
    inp = _resolve_target(frame, action)

    cb_id = inp.get_attribute("id") or ""
    inp.click()
    time.sleep(0.8)

    def _scoped_options() -> list:
        if cb_id:
            # React-Select pattern
            opts = frame.query_selector_all(
                f'#react-select-{cb_id}-listbox [role="option"]'
            )
            if opts:
                return opts
            # aria-owns / aria-controls
            owns = inp.get_attribute("aria-owns") or inp.get_attribute("aria-controls") or ""
            if owns:
                opts = frame.query_selector_all(f'#{owns} [role="option"]')
                if opts:
                    return opts
        # Fallback: only VISIBLE options (reduced cross-pollution risk)
        return [o for o in frame.query_selector_all('[role="option"]') if o.is_visible()]

    def _pick(opts, val: str) -> bool:
        val_lower = val.lower()
        for opt in opts:
            try:
                text = opt.inner_text().strip()
                if not text:
                    continue
                if val_lower == text.lower():
                    opt.click()
                    return True
            except Exception:
                continue
        # Partial / substring match
        for opt in opts:
            try:
                text = opt.inner_text().strip()
                if not text:
                    continue
                if val_lower in text.lower() or text.lower() in val_lower:
                    opt.click()
                    return True
            except Exception:
                continue
        return False

    # Strategy 1: full list, no typing
    matched = _pick(_scoped_options(), value)

    # Strategy 2: type to filter, then pick
    if not matched:
        try:
            inp.fill(value)
            time.sleep(0.8)
            matched = _pick(_scoped_options(), value)
        except Exception:
            pass

    # Strategy 3: first visible option as last-resort fallback
    if not matched:
        opts = _scoped_options()
        if opts:
            print(f"[ai_filler] combobox {cb_id}: no match for '{value}', picking first option")
            try:
                opts[0].click()
            except Exception:
                pass

    time.sleep(0.3)


def _execute_actions(page: Page, frame: Frame, actions: list[dict]) -> None:
    """Execute all actions in sequence. Logs each action; never raises."""
    for action in actions:
        atype = action.get("type")
        value = str(action.get("value", ""))

        try:
            if atype == "fill":
                el = _resolve_target(frame, action)
                el.click()
                el.fill(value)

            elif atype == "select":
                el = _resolve_target(frame, action)
                try:
                    el.select_option(label=value)
                except Exception:
                    try:
                        el.select_option(value=value)
                    except Exception:
                        # Try partial match on option text
                        el.evaluate(f"""
                            el => {{
                                const val = {json.dumps(value.lower())};
                                for (const opt of el.options) {{
                                    if (opt.text.toLowerCase().includes(val) || val.includes(opt.text.toLowerCase())) {{
                                        el.value = opt.value;
                                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                                        break;
                                    }}
                                }}
                            }}
                        """)

            elif atype == "combobox":
                _interact_combobox(page, frame, action, value)

            elif atype == "upload":
                if os.path.exists(value):
                    el = _resolve_target(frame, action)
                    el.set_input_files(value)
                else:
                    print(f"[ai_filler] upload skipped — file not found: {value}")

            elif atype == "check":
                el = _resolve_target(frame, action)
                if not el.is_checked():
                    el.check()

            elif atype == "radio":
                el = _resolve_target(frame, action)
                el.check()

            elif atype == "click":
                el = _resolve_target(frame, action)
                el.scroll_into_view_if_needed()
                el.click()
                time.sleep(0.3)

            elif atype in ("click_submit", "click_next", "click_continue", "click_review"):
                # Try ordered list of button selectors
                submit_selectors = [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button:has-text('Submit application')",
                    "button:has-text('Submit Application')",
                    "button:has-text('Submit')",
                    "button:has-text('Next')",
                    "button:has-text('Continue')",
                    "button:has-text('Review')",
                    "a:has-text('Next')",
                    "a:has-text('Continue')",
                ]
                clicked = False
                for btn_sel in submit_selectors:
                    try:
                        btn = frame.wait_for_selector(btn_sel, timeout=3000)
                        btn.scroll_into_view_if_needed()
                        time.sleep(0.5)
                        btn.click()
                        print(f"[ai_filler] clicked submit: {btn_sel}")
                        time.sleep(5)
                        clicked = True
                        break
                    except PWTimeout:
                        continue
                if not clicked:
                    print("[ai_filler] click_submit: no button found")

            elif atype == "wait":
                secs = float(action.get("value", 2))
                time.sleep(min(secs, 10))

        except Exception as exc:
            print(f"[ai_filler] action {action} -> {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Page outcome detection
# ══════════════════════════════════════════════════════════════════════════════

def _has_visible_fields(frame: Frame) -> bool:
    """Return True if the frame still has visible, interactive form fields."""
    return frame.evaluate(r"""() => {
        const inputs = document.querySelectorAll(
            'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]),' +
            'textarea, select, [role="combobox"]'
        );
        for (const el of inputs) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) return true;
        }
        return false;
    }""")


def _detect_outcome(page: Page, frame: Frame) -> tuple[str, list[str]]:
    """
    Returns (outcome, errors) where outcome is one of:
      "success"  – thank-you / application submitted page
      "errors"   – validation errors on the form
      "unknown"  – neither
    """
    contents = []
    seen_urls: set[str] = set()

    for scope in (frame, page.main_frame):
        try:
            scope_url = getattr(scope, "url", "")
            if scope_url in seen_urls:
                continue
            seen_urls.add(scope_url)
            contents.append(scope.content().lower())
        except Exception:
            continue

    current_urls = " ".join(
        part.lower()
        for part in [page.url, getattr(frame, "url", "")]
        if part
    )

    haystack = "\n".join(contents + [current_urls])
    if any(w in haystack for w in [
        "thank you", "application received", "successfully submitted",
        "we'll be in touch", "application submitted", "you've applied",
        "your application has been", "thank_you", "submitted successfully",
    ]):
        return "success", []

    errors: list[str] = []
    for scope in (frame, page.main_frame):
        try:
            error_els = scope.query_selector_all(
                '[class*="error"]:not([class*="error-boundary"]),'
                '[class*="invalid"],[aria-invalid="true"],'
                '.field_with_errors,[class*="validation"]'
            )
        except Exception:
            continue

        for el in error_els:
            try:
                if el.is_visible():
                    txt = el.inner_text().strip()
                    if txt and len(txt) < 300:
                        errors.append(txt)
            except Exception:
                pass

    if errors:
        return "errors", list(dict.fromkeys(errors))

    return "unknown", []


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY (sync)
# ══════════════════════════════════════════════════════════════════════════════

def ai_fill_form(
    page: Page,
    frame: Frame,
    profile: dict,
    cover_letter: str,
    debug: Optional[DebugArtifacts] = None,
) -> bool:
    """
    Fill a job application form using deterministic mapping + LLM for edge cases.

    Args:
        page:         Playwright Page object
        frame:        Frame to operate on (main or iframe)
        profile:      Applicant profile dict
        cover_letter: Cover letter text (will be written to temp file)
        debug:        Optional DebugArtifacts instance for writing artifacts

    Returns True on detected success, False otherwise.
    """
    if not os.path.exists(CLAUDE_BIN):
        print(f"[ai_filler] claude CLI not found at {CLAUDE_BIN}")
        return False

    prior_errors: list[str] = []
    tracker = StateTracker()
    all_mappings: list[dict] = []
    all_actions: list[dict] = []

    # Write cover letter to temp file so it can be uploaded
    cl_tmp = None
    if cover_letter:
        fd, cl_tmp = tempfile.mkstemp(suffix=".txt", prefix="cover_letter_")
        with os.fdopen(fd, "w") as f:
            f.write(cover_letter)
        profile = {
            **profile,
            "_cover_letter_path": cl_tmp,
            "_cover_letter_text": cover_letter,
        }

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"\n[ai_filler] ══ attempt {attempt}/{MAX_ATTEMPTS} ══")

            # ── State snapshot & loop detection ──
            state = extract_state(page, frame)
            classification = tracker.record(state)
            print(f"[ai_filler] state fingerprint={state.fingerprint} classification={classification}")

            if classification == "stuck":
                failure_msg = (
                    f"Stuck on fingerprint {state.fingerprint} "
                    f"after {tracker.total_attempts} attempts. "
                    f"Headings: {state.headings[:2]}. "
                    f"Errors: {state.error_texts[:3]}."
                )
                print(f"[ai_filler] STUCK — aborting. {failure_msg}")
                if debug:
                    debug.record_states(tracker.get_log())
                    debug.set_failure(failure_msg)
                return False

            # Take page-load screenshot
            if debug:
                debug.take_screenshot(page, f"attempt{attempt}_page_load")

            # ── Extract page data ──
            html = _extract_clean_html(frame)
            fields = _extract_form_fields(frame)
            print(f"[ai_filler] extracted {len(html)} chars HTML, {len(fields)} fields")

            if not fields and "<form" not in html.lower():
                # Check if we're already on a success page
                outcome, _ = _detect_outcome(page, frame)
                if outcome == "success":
                    print("[ai_filler] success page detected (no form)")
                    return True
                print("[ai_filler] no form content found — giving up")
                return False

            # ── Extract combobox options (scoped) ──
            print("[ai_filler] extracting combobox options...")
            cb_options = _extract_combobox_options(frame)
            for cb_id, opts in cb_options.items():
                print(f"[ai_filler]   combobox {cb_id}: {opts[:5]}{'...' if len(opts) > 5 else ''}")

            # ── Deterministic field mapping ──
            mappings = _map_fields_deterministically(fields, cb_options, profile)
            det_mapped = [m for m in mappings if m["source"] == "deterministic" and m["profile_value"]]
            needs_llm = [m for m in mappings if m["source"] == "needs_llm"]

            print(f"[ai_filler] mapped deterministically: {len(det_mapped)}, needs LLM: {len(needs_llm)}")
            for m in det_mapped:
                print(f"  [DET] {m['field'].get('label','?')[:40]:40s} -> {m['canonical_key']:20s} = {str(m['profile_value'])[:30]}")

            # Record mappings for debug
            mapping_records = []
            for m in mappings:
                mapping_records.append({
                    "field_id": m["field"].get("id", ""),
                    "label": m["field"].get("label", ""),
                    "canonical_key": m["canonical_key"],
                    "profile_value": m["profile_value"],
                    "confidence": m["confidence"],
                    "source": m["source"],
                })
            all_mappings.extend(mapping_records)

            # ── Build deterministic actions ──
            det_actions = _build_deterministic_actions(mappings, profile)

            # ── Phase 1 LLM: Analyze the form ──
            print("[ai_filler] PHASE 1: analyzing form...")
            try:
                analysis_prompt = _build_analysis_prompt(html, fields, cb_options)
                analysis = _call_claude_cli(analysis_prompt)
                print(f"[ai_filler] analysis ({len(analysis)} chars):")
                for line in analysis.splitlines()[:25]:
                    print(f"  | {line}")
                if len(analysis.splitlines()) > 25:
                    print(f"  | ... ({len(analysis.splitlines())} total lines)")
            except Exception as exc:
                print(f"[ai_filler] Phase 1 error: {exc}")
                # Degrade gracefully: execute only deterministic actions
                analysis = "(Phase 1 LLM failed — deterministic only)"
                needs_llm = []

            # ── Phase 2 LLM: Generate actions for unmapped fields ──
            llm_actions: list[dict] = []
            step_type = _detect_step_type(state.headings, state.button_texts)

            if needs_llm or prior_errors:
                print(f"[ai_filler] PHASE 2: generating LLM actions for {len(needs_llm)} unmapped fields...")
                unmapped_field_data = [
                    {
                        "id": m["field"].get("id", ""),
                        "name": m["field"].get("name", ""),
                        "label": m["field"].get("label", ""),
                        "type": m["field"].get("type", ""),
                        "placeholder": m["field"].get("placeholder", ""),
                        "options": m["options"],
                    }
                    for m in needs_llm
                ]

                try:
                    action_prompt = _build_action_prompt(
                        analysis, unmapped_field_data, profile,
                        prior_errors, attempt, step_type
                    )
                    raw = _call_claude_cli(action_prompt)
                    llm_actions = _parse_json_array(raw)
                    print(f"[ai_filler] LLM generated {len(llm_actions)} actions")
                except Exception as exc:
                    print(f"[ai_filler] Phase 2 error: {exc}")
                    # Degrade: add a click_submit at minimum
                    llm_actions = [{"type": "click_submit"}]
            else:
                # All fields mapped — just need a submit
                llm_actions = [{"type": "click_submit"}]

            # ── Merge: deterministic first, then LLM, deduplicate selectors ──
            # Deterministic actions take priority; LLM actions fill the gaps
            # but we remove any LLM action whose selector was already handled
            det_selectors = {a.get("selector", "") for a in det_actions if a.get("selector")}
            filtered_llm = []
            for a in llm_actions:
                sel = a.get("selector", "")
                atype = a.get("type", "")
                if atype == "click_submit":
                    filtered_llm.append(a)
                elif sel and sel not in det_selectors:
                    filtered_llm.append(a)
                elif not sel:
                    filtered_llm.append(a)

            # Ensure exactly one click_submit at the end
            merged = det_actions + [a for a in filtered_llm if a.get("type") != "click_submit"]
            merged.append({"type": "click_submit"})

            print(f"[ai_filler] executing {len(merged)} merged actions ({len(det_actions)} det + {len(filtered_llm)} llm)")
            for a in merged:
                print(f"  > {a.get('type'):12s} {a.get('selector',''):35s} {str(a.get('value',''))[:40]}")

            # Take pre-submit screenshot
            if debug:
                debug.take_screenshot(page, f"attempt{attempt}_before_submit")

            all_actions.extend(merged)
            _execute_actions(page, frame, merged)

            # Take post-submit screenshot
            if debug:
                debug.take_screenshot(page, f"attempt{attempt}_after_submit")

            # ── Check outcome ──
            outcome, errors = _detect_outcome(page, frame)
            print(f"[ai_filler] outcome={outcome} errors={errors}")

            if outcome == "success":
                if debug:
                    debug.record_fields(fields)
                    debug.record_mappings(all_mappings)
                    debug.record_actions(all_actions)
                    debug.record_states(tracker.get_log())
                return True

            if outcome == "errors":
                prior_errors = errors
                continue

            # Multi-page: check for new fields after submit
            time.sleep(2)
            if _has_visible_fields(frame):
                print("[ai_filler] new page detected — continuing (multi-page form)")
                prior_errors = []
                continue

            print("[ai_filler] submit done, no more visible fields — treating as success")
            return True

        # Exhausted all attempts
        if debug:
            debug.record_fields([])
            debug.record_mappings(all_mappings)
            debug.record_actions(all_actions)
            debug.record_states(tracker.get_log())
            debug.set_failure(f"Exhausted {MAX_ATTEMPTS} attempts without success")

        return False

    finally:
        if cl_tmp:
            try:
                os.unlink(cl_tmp)
            except OSError:
                pass
