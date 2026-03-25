from __future__ import annotations
"""
ai_filler.py – Deterministic + AI-powered form-fill loop (sync Playwright API).

Architecture:
  1. extract_fields         – comprehensive FieldMeta via field_extractor
  2. classify_page          – detect success/login/captcha/fill/review
  3. map_fields_deterministically – uses question_map.CATALOG
  4. find_unresolved_required_fields – detect empty required fields
  5. Phase 1 LLM (once per page) – analyze form HTML -> structured field list
  6. Phase 2 LLM – generate actions ONLY for unmapped fields
  7. Validate LLM actions – reject those with bad selectors/options/files
  8. Merge deterministic + validated LLM actions and execute with interaction.py
  9. Pre-submit check: if unresolved required fields remain -> DO NOT submit
  10. Determine step intent from page classifier
  11. Check state fingerprint for loops (progress-aware)
  12. Loop or return ApplyResult

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
from core.field_extractor import extract_fields, FieldMeta
from core.page_classifier import (
    classify_page, classify_button, find_unresolved_required_fields,
)
from core.interaction import (
    fill_field, select_option, interact_combobox, check_radio,
    toggle_checkbox, upload_file, InteractionResult,
)
from core.outcome import (
    ApplyResult, FailureType, PageType, StepIntent, make_failure,
)

MAX_ATTEMPTS = 8
MAX_HTML_LENGTH = 50_000
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")

# Confidence threshold above which we skip LLM for a field
DETERMINISTIC_CONFIDENCE = 0.75


# ══════════════════════════════════════════════════════════════════════════════
# HTML extraction (for LLM prompts)
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


# ══════════════════════════════════════════════════════════════════════════════
# Combobox option extraction (scoped)
# ══════════════════════════════════════════════════════════════════════════════

def _extract_combobox_options(frame: Frame) -> dict[str, list[str]]:
    """
    Click each combobox, read its SCOPED options, close it.
    Returns {combobox_id: [option_texts]}.
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

            # Strategy 4: nearest visible listbox
            if not option_els:
                listboxes = frame.query_selector_all('[role="listbox"]')
                for lb in listboxes:
                    if lb.is_visible():
                        option_els = lb.query_selector_all('[role="option"]')
                        if option_els:
                            break

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
    fields: list[FieldMeta],
    combobox_options: dict[str, list[str]],
    profile: dict,
) -> list[dict]:
    """
    Map each extracted field against the CATALOG via question_map.match_field.

    Returns list of mapping dicts:
    {
        field: FieldMeta,
        canonical_key: str,
        profile_value: str,
        confidence: float,
        source: "deterministic" | "needs_llm",
        options: [str],
    }
    """
    mappings = []
    for f in fields:
        # Merge static options with combobox options (scoped)
        cb_opts = combobox_options.get(f.id, [])
        static_opts = [o.get("label", "") for o in f.options]
        all_opts = static_opts + [o for o in cb_opts if o not in static_opts]

        ckey, pval, conf = match_field(
            label=f.label,
            placeholder=f.placeholder,
            name=f.name,
            id=f.id,
            aria_label=f.aria_label,
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
    Convert high-confidence deterministic mappings into action dicts.
    Skips mappings with empty profile_value.
    """
    actions = []
    for m in mappings:
        if m["source"] != "deterministic":
            continue
        if not m["profile_value"]:
            continue

        f: FieldMeta = m["field"]
        pval = m["profile_value"]
        selector = f.selector_candidates[0] if f.selector_candidates else ""
        if not selector:
            if f.id:
                selector = f'#{f.id}'
            elif f.name:
                selector = f'[name={json.dumps(f.name)}]'
            else:
                continue

        ftype = f.type
        frole = f.role
        tag = f.tag

        # Upload fields
        if m["canonical_key"] in ("resume_upload", "cover_letter_upload") or ftype == "file":
            if os.path.exists(pval):
                actions.append({
                    "type": "upload",
                    "selector": selector,
                    "value": pval,
                    "selector_candidates": f.selector_candidates,
                })
            continue

        # Radio
        if ftype == "radio":
            actions.append({
                "type": "radio",
                "selector": selector,
                "value": pval,
                "name": f.name,
                "options": m["options"],
                "field_meta": f,
            })
            continue

        # Native select
        if tag == "select":
            actions.append({
                "type": "select",
                "selector": selector,
                "value": pval,
                "options": f.options,
                "selector_candidates": f.selector_candidates,
            })
            continue

        # Combobox (React-Select / custom)
        if frole == "combobox" or "combobox" in ftype or f.widget_type in ("react_select", "custom_listbox"):
            actions.append({
                "type": "combobox",
                "selector": selector,
                "value": pval,
                "field_meta": f,
            })
            continue

        # Checkbox
        if ftype == "checkbox":
            if str(pval).lower() in ("yes", "true", "1", "i agree", "agree"):
                actions.append({
                    "type": "check",
                    "selector": selector,
                    "label": f.label,
                    "selector_candidates": f.selector_candidates,
                })
            continue

        # Default: fill
        actions.append({
            "type": "fill",
            "selector": selector,
            "value": pval,
            "selector_candidates": f.selector_candidates,
        })

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
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        return json.loads(m.group())
    return json.loads(text)


def _build_analysis_prompt(html: str, fields: list[FieldMeta],
                            combobox_options: dict[str, list[str]]) -> str:
    fields_data = [f.to_dict() for f in fields]
    return f"""You are analyzing a job application form. Study the HTML and field
metadata below, then output a STRUCTURED ANALYSIS of every interactive field.

═══ PAGE HTML (cleaned) ═══
{html}

═══ STRUCTURED FIELDS ═══
{json.dumps(fields_data, indent=2)}

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

═══ CRITICAL RULES ═══
1. DO NOT include click_submit — navigation is handled separately.

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
# LLM action validation
# ══════════════════════════════════════════════════════════════════════════════

def _validate_llm_actions(
    frame: Frame,
    actions: list[dict],
    fields: list[FieldMeta],
    combobox_options: dict[str, list[str]],
) -> tuple[list[dict], list[dict]]:
    """
    Validate LLM-proposed actions before execution.

    Returns (valid_actions, rejected_actions).
    rejected_actions: [{action, reason}]
    """
    # Build option index: selector -> list of option texts
    option_index: dict[str, list[str]] = {}
    for f in fields:
        key = f.selector_candidates[0] if f.selector_candidates else ""
        if f.id:
            key = f"#{f.id}"
        elif f.name:
            key = f"[name={json.dumps(f.name)}]"
        opt_labels = [o.get("label", "") for o in f.options]
        opt_labels += combobox_options.get(f.id, [])
        if opt_labels:
            option_index[key] = opt_labels

    valid = []
    rejected = []

    for action in actions:
        atype = action.get("type", "")
        selector = action.get("selector", "").strip()
        value = str(action.get("value", ""))

        # click_submit is filtered out from LLM actions; ignore if present
        if atype in ("click_submit", "click_next", "click_continue", "click_review"):
            rejected.append({"action": action, "reason": "navigation_action_from_llm_rejected"})
            continue

        # Validate selector resolves
        if selector and atype not in ("wait",):
            try:
                el = frame.query_selector(selector)
                if el is None:
                    rejected.append({"action": action, "reason": f"selector_not_found: {selector}"})
                    continue
            except Exception as e:
                rejected.append({"action": action, "reason": f"selector_error: {e}"})
                continue

        # Validate upload: file must exist
        if atype == "upload":
            if not os.path.exists(value):
                rejected.append({"action": action, "reason": f"file_not_found: {value}"})
                continue

        # Validate combobox/select: proposed option must appear in extracted options
        if atype in ("combobox", "select") and value:
            # Find the options for this selector
            opts = option_index.get(selector, [])
            # Also try partial key matching
            if not opts:
                for k, v in option_index.items():
                    if selector and (selector in k or k in selector):
                        opts = v
                        break
            if opts:
                from core.interaction import find_best_option_match
                matched = find_best_option_match(value, opts, threshold=0.7)
                if matched is None:
                    rejected.append({
                        "action": action,
                        "reason": f"option_not_in_extracted_options: '{value}' not in {opts[:5]}",
                    })
                    continue

        # Validate radio: proposed value must match a visible radio label
        if atype == "radio":
            # Find the radio group for this selector name
            name_match = re.search(r'\[name=(["\'])(.*?)\1\]', selector)
            if name_match:
                radio_name = name_match.group(2)
                radio_field = next(
                    (f for f in fields if f.name == radio_name and f.type == "radio"),
                    None,
                )
                if radio_field:
                    opt_labels = [o.get("label", "") for o in radio_field.options]
                    from core.interaction import find_best_option_match
                    matched = find_best_option_match(value, opt_labels, threshold=0.5)
                    if matched is None:
                        # also check option values
                        opt_values = [o.get("value", "") for o in radio_field.options]
                        matched = find_best_option_match(value, opt_values, threshold=0.5)
                        if matched is None:
                            rejected.append({
                                "action": action,
                                "reason": f"radio_value_not_matched: '{value}' not in labels {opt_labels}",
                            })
                            continue

        valid.append(action)

    return valid, rejected


# ══════════════════════════════════════════════════════════════════════════════
# Action execution (using interaction.py primitives)
# ══════════════════════════════════════════════════════════════════════════════

def _execute_actions(
    page: Page,
    frame: Frame,
    actions: list[dict],
    fields: list[FieldMeta],
) -> list[dict]:
    """
    Execute all actions using interaction.py primitives.
    Returns list of {action, result: InteractionResult.to_dict()} per action.
    """
    executed = []
    # Build field lookup: selector -> FieldMeta
    field_by_selector: dict[str, FieldMeta] = {}
    for f in fields:
        for s in f.selector_candidates:
            if s:
                field_by_selector[s] = f
        if f.id:
            field_by_selector[f"#{f.id}"] = f
        if f.name:
            field_by_selector[f"[name={json.dumps(f.name)}]"] = f

    for action in actions:
        atype = action.get("type", "")
        value = str(action.get("value", ""))
        selector = action.get("selector", "").strip()
        selector_candidates = action.get("selector_candidates") or ([selector] if selector else [])
        field_meta = action.get("field_meta") or field_by_selector.get(selector)

        result_obj = InteractionResult(success=True, actual_value="")
        try:
            if atype == "fill":
                result_obj = fill_field(frame, selector_candidates, value)

            elif atype == "select":
                options = action.get("options") or (field_meta.options if field_meta else [])
                result_obj = select_option(frame, selector_candidates, value, options)

            elif atype == "combobox":
                if field_meta:
                    result_obj = interact_combobox(page, frame, field_meta, value)
                else:
                    # Build a minimal FieldMeta for the interaction
                    import re as _re
                    id_match = _re.search(r'#([^\s\[]+)', selector)
                    name_match = _re.search(r'\[name=["\']([^"\']+)["\']', selector)
                    tmp = FieldMeta(
                        id=id_match.group(1) if id_match else "",
                        name=name_match.group(1) if name_match else "",
                        selector_candidates=selector_candidates,
                    )
                    result_obj = interact_combobox(page, frame, tmp, value)

            elif atype == "upload":
                result_obj = upload_file(frame, selector_candidates, value)

            elif atype == "check":
                label = action.get("label", field_meta.label if field_meta else "")
                result_obj = toggle_checkbox(frame, selector_candidates, True, label=label)

            elif atype == "radio":
                if field_meta and field_meta.type == "radio":
                    result_obj = check_radio(frame, field_meta, value)
                else:
                    # Fallback: direct selector check
                    try:
                        el = frame.wait_for_selector(selector, timeout=3000)
                        if el:
                            el.check()
                            result_obj = InteractionResult(success=True, actual_value=value)
                        else:
                            result_obj = InteractionResult(success=False, error_message=f"radio selector not found: {selector}")
                    except Exception as e:
                        result_obj = InteractionResult(success=False, error_message=str(e))

            elif atype == "click":
                try:
                    el = frame.wait_for_selector(selector, timeout=4000)
                    if el:
                        el.scroll_into_view_if_needed()
                        el.click()
                        time.sleep(0.3)
                        result_obj = InteractionResult(success=True)
                    else:
                        result_obj = InteractionResult(success=False, error_message=f"click: element not found: {selector}")
                except Exception as e:
                    result_obj = InteractionResult(success=False, error_message=str(e))

            elif atype == "wait":
                secs = float(value) if value else 2.0
                time.sleep(min(secs, 10))
                result_obj = InteractionResult(success=True)

        except Exception as exc:
            result_obj = InteractionResult(success=False, error_message=str(exc))

        if not result_obj.success:
            print(f"[ai_filler] action failed: {action} -> {result_obj.error_message}")
        else:
            print(f"[ai_filler] action ok: type={atype} sel={selector[:40]} val={value[:30]}")

        executed.append({
            "action": {k: v for k, v in action.items() if k != "field_meta"},
            "result": {
                "success": result_obj.success,
                "actual_value": result_obj.actual_value,
                "error_message": result_obj.error_message,
            },
        })

    return executed


# ══════════════════════════════════════════════════════════════════════════════
# Navigation (step intent execution)
# ══════════════════════════════════════════════════════════════════════════════

def _execute_navigation(page: Page, frame: Frame, intent: StepIntent) -> bool:
    """
    Execute the navigation action determined by the page classifier.
    Returns True if a button was clicked, False otherwise.
    """
    if intent == StepIntent.CLICK_SUBMIT:
        selectors = [
            "button:has-text('Submit Application')",
            "button:has-text('Submit application')",
            "button:has-text('Submit')",
            "input[type='submit']",
            "button[type='submit']",
            "button:has-text('Finish')",
            "button:has-text('Apply')",
        ]
    elif intent == StepIntent.CLICK_REVIEW:
        selectors = [
            "button:has-text('Review Application')",
            "button:has-text('Review')",
            "button:has-text('Preview')",
        ]
    elif intent in (StepIntent.CLICK_NEXT, StepIntent.CLICK_CONTINUE):
        selectors = [
            "button:has-text('Next')",
            "button:has-text('Continue')",
            "button:has-text('Proceed')",
            "a:has-text('Next')",
            "a:has-text('Continue')",
            "button[type='submit']",
            "input[type='submit']",
        ]
    else:
        return False

    for sel in selectors:
        try:
            btn = frame.wait_for_selector(sel, timeout=3000)
            if btn and btn.is_visible():
                btn.scroll_into_view_if_needed()
                time.sleep(0.5)
                btn.click()
                print(f"[ai_filler] navigation: clicked {sel}")
                time.sleep(3)
                return True
        except PWTimeout:
            continue
        except Exception:
            continue

    # Fallback: try on main page
    for sel in selectors:
        try:
            btn = page.wait_for_selector(sel, timeout=2000)
            if btn and btn.is_visible():
                btn.scroll_into_view_if_needed()
                btn.click()
                print(f"[ai_filler] navigation (page): clicked {sel}")
                time.sleep(3)
                return True
        except (PWTimeout, Exception):
            continue

    print(f"[ai_filler] navigation: no button found for intent={intent}")
    return False


# ══════════════════════════════════════════════════════════════════════════════
# Determine step intent from page context
# ══════════════════════════════════════════════════════════════════════════════

def _get_step_intent(page_type: PageType, button_texts: list[str]) -> StepIntent:
    """Determine what navigation action to take based on page type and buttons."""
    if page_type == PageType.SUCCESS:
        return StepIntent.WAIT
    if page_type == PageType.FINAL_SUBMIT:
        return StepIntent.CLICK_SUBMIT
    if page_type == PageType.REVIEW:
        return StepIntent.CLICK_SUBMIT
    # For fill pages, look at button texts
    for btn_text in button_texts:
        intent = classify_button(btn_text)
        if intent != StepIntent.CLICK_NEXT:
            return intent
        return StepIntent.CLICK_NEXT
    return StepIntent.CLICK_NEXT


# ══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY (sync)
# ══════════════════════════════════════════════════════════════════════════════

def ai_fill_form(
    page: Page,
    frame: Frame,
    profile: dict,
    cover_letter: str,
    debug: Optional[DebugArtifacts] = None,
    **kwargs,
) -> ApplyResult:
    """
    Fill a job application form using deterministic mapping + LLM for edge cases.

    Args:
        page:         Playwright Page object
        frame:        Frame to operate on (main or iframe)
        profile:      Applicant profile dict
        cover_letter: Cover letter text (will be written to temp file)
        debug:        Optional DebugArtifacts instance for writing artifacts

    Returns ApplyResult (success=True on confirmed submission, failure otherwise).
    """
    if not os.path.exists(CLAUDE_BIN):
        msg = f"Claude CLI not found at {CLAUDE_BIN}"
        print(f"[ai_filler] {msg}")
        return make_failure(FailureType.MISSING_CLAUDE, reason=msg)

    prior_errors: list[str] = []
    tracker = StateTracker()
    all_mappings: list[dict] = []
    all_executed: list[dict] = []
    all_rejected: list[dict] = []

    # Write cover letter to temp file so it can be uploaded
    cl_tmp: Optional[str] = None
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

            if debug:
                debug.record_state_fingerprint(attempt, state.fingerprint)

            if classification == "stuck":
                # Determine stuck type
                log = tracker.get_log()
                stuck_type = "stuck_same_page_no_progress"
                for entry in reversed(log):
                    if entry.get("classification") == "stuck":
                        stuck_type = entry.get("stuck_type", stuck_type)
                        break

                ft_map = {
                    "stuck_same_page_no_progress": FailureType.STUCK_SAME_PAGE_NO_PROGRESS,
                    "cycling_between_steps": FailureType.CYCLING_BETWEEN_STEPS,
                    "repeated_validation_errors": FailureType.REPEATED_VALIDATION_ERRORS,
                }
                ft = ft_map.get(stuck_type, FailureType.STUCK_SAME_PAGE_NO_PROGRESS)

                failure_msg = (
                    f"Stuck ({stuck_type}) on fingerprint {state.fingerprint} "
                    f"after {tracker.total_attempts} attempts. "
                    f"Headings: {state.headings[:2]}. "
                    f"Errors: {state.error_texts[:3]}."
                )
                print(f"[ai_filler] STUCK — aborting. {failure_msg}")
                if debug:
                    debug.record_states(tracker.get_log())
                    debug.record_executed_actions(all_executed)
                    debug.record_rejected_actions(all_rejected)
                    debug.set_structured_failure(ft.value, failure_msg)
                return make_failure(ft, reason=failure_msg, attempts=attempt)

            # Take page-load screenshot
            if debug:
                debug.take_screenshot(page, f"attempt{attempt}_page_load")

            # ── Classify page ──
            page_type = classify_page(page, frame)
            print(f"[ai_filler] page_type={page_type}")

            if debug:
                debug.record_page_classification(attempt, page_type.value)

            # Success page?
            if page_type == PageType.SUCCESS:
                print("[ai_filler] success page detected")
                if debug:
                    debug.record_states(tracker.get_log())
                    debug.record_mappings(all_mappings)
                    debug.record_executed_actions(all_executed)
                return ApplyResult(success=True, attempts=attempt)

            # Blocking pages
            if page_type == PageType.LOGIN_WALL:
                if debug:
                    debug.take_screenshot(page, f"attempt{attempt}_login_wall")
                    debug.record_states(tracker.get_log())
                return make_failure(
                    FailureType.LOGIN_REQUIRED,
                    reason="Login wall detected during form fill",
                    attempts=attempt,
                )

            if page_type == PageType.CAPTCHA:
                if debug:
                    debug.take_screenshot(page, f"attempt{attempt}_captcha")
                    debug.record_states(tracker.get_log())
                return make_failure(
                    FailureType.CAPTCHA_OR_HUMAN_VERIFICATION,
                    reason="CAPTCHA detected during form fill",
                    attempts=attempt,
                )

            # ── Extract fields ──
            fields = extract_fields(frame)
            html = _extract_clean_html(frame)
            print(f"[ai_filler] extracted {len(html)} chars HTML, {len(fields)} fields")

            if debug:
                debug.record_fields([f.to_dict() for f in fields])
                debug.record_html_snapshot(html, step=attempt)

            if not fields and "<form" not in html.lower():
                # Recheck page type after HTML analysis
                if page_type == PageType.SUCCESS:
                    return ApplyResult(success=True, attempts=attempt)
                if any(w in html.lower() for w in [
                    "thank you", "application received", "successfully submitted",
                    "you've applied", "application submitted",
                ]):
                    return ApplyResult(success=True, attempts=attempt)
                print("[ai_filler] no form content found — giving up")
                return make_failure(
                    FailureType.UNKNOWN_POST_SUBMIT_STATE,
                    reason="No form fields found and not on success page",
                    attempts=attempt,
                )

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
                f_label = m["field"].label[:40] if hasattr(m["field"], "label") else str(m["field"])[:40]
                print(f"  [DET] {f_label:40s} -> {m['canonical_key']:20s} = {str(m['profile_value'])[:30]}")

            # Record mappings for debug
            mapping_records = []
            for m in mappings:
                fld = m["field"]
                mapping_records.append({
                    "field_id": fld.id if hasattr(fld, "id") else "",
                    "label": fld.label if hasattr(fld, "label") else "",
                    "canonical_key": m["canonical_key"],
                    "profile_value": m["profile_value"],
                    "confidence": m["confidence"],
                    "source": m["source"],
                })
            all_mappings.extend(mapping_records)

            if debug:
                debug.record_mappings(mapping_records)

            # ── Build deterministic actions ──
            det_actions = _build_deterministic_actions(mappings, profile)

            # ── Phase 1 LLM: Analyze the form ──
            print("[ai_filler] PHASE 1: analyzing form...")
            analysis = "(Phase 1 LLM skipped)"
            try:
                analysis_prompt = _build_analysis_prompt(html, fields, cb_options)
                analysis = _call_claude_cli(analysis_prompt)
                print(f"[ai_filler] analysis ({len(analysis)} chars):")
                for line in analysis.splitlines()[:25]:
                    print(f"  | {line}")
                if len(analysis.splitlines()) > 25:
                    print(f"  | ... ({len(analysis.splitlines())} total lines)")
            except Exception as exc:
                print(f"[ai_filler] Phase 1 error: {exc} — continuing with deterministic only")
                needs_llm = []

            # ── Phase 2 LLM: Generate actions for unmapped fields ──
            llm_actions: list[dict] = []
            step_type_str = page_type.value

            if needs_llm or prior_errors:
                print(f"[ai_filler] PHASE 2: generating LLM actions for {len(needs_llm)} unmapped fields...")
                unmapped_field_data = []
                for m in needs_llm:
                    fld = m["field"]
                    unmapped_field_data.append({
                        "id": fld.id,
                        "name": fld.name,
                        "label": fld.label,
                        "type": fld.type,
                        "placeholder": fld.placeholder,
                        "options": m["options"],
                        "widget_type": fld.widget_type,
                    })

                try:
                    action_prompt = _build_action_prompt(
                        analysis, unmapped_field_data, profile,
                        prior_errors, attempt, step_type_str,
                    )
                    raw = _call_claude_cli(action_prompt)
                    llm_actions = _parse_json_array(raw)
                    print(f"[ai_filler] LLM generated {len(llm_actions)} actions")

                    if debug:
                        debug.record_proposed_actions(llm_actions)

                except Exception as exc:
                    print(f"[ai_filler] Phase 2 error: {exc} — using deterministic actions only")
                    llm_actions = []

            # ── Validate LLM actions ──
            valid_llm, rejected_llm = _validate_llm_actions(
                frame, llm_actions, fields, cb_options,
            )
            all_rejected.extend(rejected_llm)
            if rejected_llm:
                print(f"[ai_filler] rejected {len(rejected_llm)} LLM actions:")
                for r in rejected_llm:
                    print(f"  REJECT: {r['action']} -> {r['reason']}")
            if debug:
                debug.record_rejected_actions(all_rejected)

            # ── Merge: deterministic first, then validated LLM ──
            det_selectors = {a.get("selector", "") for a in det_actions if a.get("selector")}
            filtered_llm = []
            for a in valid_llm:
                sel = a.get("selector", "")
                atype = a.get("type", "")
                if sel and sel not in det_selectors:
                    filtered_llm.append(a)
                elif not sel:
                    filtered_llm.append(a)

            merged = det_actions + filtered_llm

            print(f"[ai_filler] executing {len(merged)} merged actions ({len(det_actions)} det + {len(filtered_llm)} llm)")
            for a in merged:
                print(f"  > {a.get('type'):12s} {str(a.get('selector',''))[:35]} {str(a.get('value',''))[:40]}")

            # Take pre-fill screenshot
            if debug:
                debug.take_screenshot(page, f"attempt{attempt}_pre_fill")

            # ── Execute fill actions ──
            executed = _execute_actions(page, frame, merged, fields)
            all_executed.extend(executed)

            if debug:
                debug.record_executed_actions(all_executed)

            # Take post-fill screenshot
            if debug:
                debug.take_screenshot(page, f"attempt{attempt}_post_fill")

            # ── Pre-submit check: find unresolved required fields ──
            unresolved = find_unresolved_required_fields(frame)
            print(f"[ai_filler] unresolved required fields: {len(unresolved)}")
            for u in unresolved:
                print(f"  UNRESOLVED: {u.get('label', '?')} [{u.get('type', '?')}] {u.get('selector', '')}")

            if debug and unresolved:
                debug.record_unresolved_fields(unresolved)

            if unresolved:
                # Do NOT submit if required fields are empty
                print("[ai_filler] BLOCKING SUBMIT: unresolved required fields present")
                # If we have prior_errors too, we're stuck in validation loop
                if prior_errors and len(unresolved) == len(prior_errors):
                    if debug:
                        debug.record_states(tracker.get_log())
                    return make_failure(
                        FailureType.UNRESOLVED_REQUIRED_FIELDS,
                        reason=f"Cannot fill required fields: {[u.get('label', '?') for u in unresolved]}",
                        attempts=attempt,
                        unresolved_fields=unresolved,
                    )
                # Record as prior errors and retry
                prior_errors = [f"Required field '{u.get('label','?')}' is empty" for u in unresolved]
                continue

            # ── Determine navigation intent ──
            button_texts = state.button_texts
            step_intent = _get_step_intent(page_type, button_texts)
            print(f"[ai_filler] step_intent={step_intent}")

            # Take pre-submit screenshot
            if debug:
                debug.take_screenshot(page, f"attempt{attempt}_pre_submit")

            # ── Execute navigation ──
            nav_clicked = _execute_navigation(page, frame, step_intent)

            # Take post-submit screenshot
            if debug:
                debug.take_screenshot(page, f"attempt{attempt}_post_submit")

            # ── Check outcome ──
            time.sleep(2)
            post_page_type = classify_page(page, frame)
            print(f"[ai_filler] post-navigation page_type={post_page_type}")

            if post_page_type == PageType.SUCCESS:
                if debug:
                    debug.record_states(tracker.get_log())
                    debug.record_mappings(all_mappings)
                    debug.record_executed_actions(all_executed)
                return ApplyResult(success=True, attempts=attempt)

            if post_page_type == PageType.LOGIN_WALL:
                return make_failure(
                    FailureType.LOGIN_REQUIRED,
                    reason="Login wall after navigation",
                    attempts=attempt,
                )

            if post_page_type == PageType.CAPTCHA:
                return make_failure(
                    FailureType.CAPTCHA_OR_HUMAN_VERIFICATION,
                    reason="CAPTCHA after navigation",
                    attempts=attempt,
                )

            # Check for validation errors
            error_elements: list[str] = []
            for scope in (frame, page.main_frame):
                try:
                    errs = scope.query_selector_all(
                        '[class*="error"]:not([class*="error-boundary"]),'
                        '[class*="invalid"],[aria-invalid="true"],'
                        '.field_with_errors,[class*="validation"]'
                    )
                    for el in errs:
                        try:
                            if el.is_visible():
                                txt = el.inner_text().strip()
                                if txt and len(txt) < 300:
                                    error_elements.append(txt)
                        except Exception:
                            pass
                except Exception:
                    pass

            if error_elements:
                prior_errors = list(dict.fromkeys(error_elements))
                print(f"[ai_filler] validation errors: {prior_errors[:3]}")
                continue

            # Multi-page: if there are new fields, continue
            new_fields = extract_fields(frame)
            if new_fields:
                print("[ai_filler] new page with fields detected — continuing (multi-page form)")
                prior_errors = []
                continue

            # No fields, no success page detected explicitly — treat as success
            # if we navigated (submitted)
            if nav_clicked and step_intent == StepIntent.CLICK_SUBMIT:
                print("[ai_filler] submit clicked, no more fields — treating as success")
                if debug:
                    debug.record_states(tracker.get_log())
                return ApplyResult(success=True, attempts=attempt)

            # No progress
            print("[ai_filler] no progress detected after navigation — will retry")
            prior_errors = []

        # Exhausted all attempts
        if debug:
            debug.record_states(tracker.get_log())
            debug.record_mappings(all_mappings)
            debug.record_executed_actions(all_executed)
            debug.set_structured_failure(
                FailureType.STUCK_SAME_PAGE_NO_PROGRESS.value,
                f"Exhausted {MAX_ATTEMPTS} attempts without success",
            )

        return make_failure(
            FailureType.STUCK_SAME_PAGE_NO_PROGRESS,
            reason=f"Exhausted {MAX_ATTEMPTS} attempts without success",
            attempts=MAX_ATTEMPTS,
        )

    finally:
        if cl_tmp:
            try:
                os.unlink(cl_tmp)
            except OSError:
                pass
