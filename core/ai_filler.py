from __future__ import annotations
"""
ai_filler.py – Claude-powered form-fill loop (sync Playwright API).

Uses sync_playwright so it works cleanly in FastAPI background threads
without asyncio event-loop conflicts.
"""
import base64
import json
import os
import tempfile
import time

import anthropic
from playwright.sync_api import Page, Frame, TimeoutError as PWTimeout

MAX_ATTEMPTS = 5


# ── DOM extraction ─────────────────────────────────────────────────────────────
def _extract_form_fields(frame: Frame) -> list[dict]:
    return frame.evaluate("""() => {
        const fields = [];
        const seen = new Set();
        const inputs = document.querySelectorAll(
            'input:not([type=hidden]):not([type=submit]):not([type=button]),' +
            'textarea, select'
        );
        inputs.forEach(el => {
            const id   = el.id   || '';
            const name = el.name || '';
            const key  = id || name;
            if (!key || seen.has(key)) return;
            seen.add(key);

            let label = '';
            if (id) {
                const lEl = document.querySelector('label[for=' + JSON.stringify(id) + ']');
                if (lEl) label = lEl.innerText.trim();
            }
            if (!label) {
                let p = el.parentElement;
                while (p && p.tagName !== 'FORM') {
                    if (p.tagName === 'LABEL') { label = p.innerText.trim(); break; }
                    const sib = p.querySelector('label');
                    if (sib && sib !== el) { label = sib.innerText.trim(); break; }
                    p = p.parentElement;
                }
            }
            const options = el.tagName === 'SELECT'
                ? Array.from(el.options).map(o => o.text.trim()).filter(Boolean)
                : [];
            fields.push({
                id, name,
                type: el.type || el.tagName.toLowerCase(),
                tag: el.tagName.toLowerCase(),
                label: label.replace(/\\s+/g, ' ').substring(0, 200),
                placeholder: el.placeholder || '',
                required: el.required,
                options,
                role: el.getAttribute('role') || '',
            });
        });
        return fields;
    }""")


def _screenshot_b64(page: Page) -> str:
    data = page.screenshot(type="jpeg", quality=55, full_page=False)
    return base64.standard_b64encode(data).decode()


# ── Claude prompt ──────────────────────────────────────────────────────────────
def _build_prompt(fields: list[dict], profile: dict,
                  prior_errors: list[str], attempt: int) -> str:
    errors_block = ""
    if prior_errors:
        errors_block = (
            f"\n\nPREVIOUS ATTEMPT {attempt - 1} VALIDATION ERRORS — fix these:\n"
            + "\n".join(f"  • {e}" for e in prior_errors)
        )
    p = {k: v for k, v in profile.items() if not k.startswith("_")}
    return f"""You are filling a job application form for this applicant.

APPLICANT PROFILE:
{json.dumps(p, indent=2)}

FORM FIELDS (id, type, label, options):
{json.dumps(fields, indent=2)}
{errors_block}

Return ONLY a valid JSON array of action objects. No explanation, no markdown fences.

Action types:
  fill         — text/email/tel/textarea:  {{"type":"fill","id":"<id>","value":"<str>"}}
  select       — native <select>:          {{"type":"select","id":"<id>","value":"<option text>"}}
  combobox     — React-Select/custom:      {{"type":"combobox","id":"<id>","value":"<option text>"}}
  upload       — file input:               {{"type":"upload","id":"<id>","value":"<abs path>"}}
  check        — checkbox:                 {{"type":"check","id":"<id>"}}
  click_submit — submit the form:          {{"type":"click_submit"}}

Rules:
- Always end with click_submit.
- Resume upload (id=resume): use profile.resume_path.
- Cover letter upload (id=cover_letter): use profile._cover_letter_path if present, else skip.
- Phone country-code combobox (id=country): use combobox action with value "United States".
- Phone (id=phone): fill with raw digits from profile.phone (no dashes or spaces).
- For EEOC fields (gender, race, disability, veteran, orientation, transgender):
  use profile value if it matches an option, otherwise pick "decline / prefer not to answer".
- Salary: use salary_min, salary_max, or salary_range as the label suggests.
- Skip LinkedIn if profile.linkedin is empty.
- For open-ended textarea questions, write a brief professional answer from the applicant's background.
- Fields with role=combobox need the combobox action type.
- If a field id starts with a digit, still use it as-is in the id field.
"""


# ── Action executor ────────────────────────────────────────────────────────────
def _execute_actions(frame: Frame, actions: list[dict]) -> None:
    for action in actions:
        atype = action.get("type")
        aid   = str(action.get("id", ""))
        value = str(action.get("value", ""))
        sel   = f'[id="{aid}"]' if aid else ""

        try:
            if atype == "fill":
                el = frame.wait_for_selector(sel, timeout=4000)
                el.fill(value)

            elif atype == "select":
                el = frame.wait_for_selector(sel, timeout=4000)
                try:
                    el.select_option(label=value)
                except Exception:
                    el.select_option(value=value)

            elif atype == "combobox":
                inp = frame.wait_for_selector(sel, timeout=4000)
                inp.click()
                time.sleep(0.35)
                inp.fill(value)
                time.sleep(0.6)
                options = frame.query_selector_all('[role="option"]')
                matched = False
                for opt in options:
                    try:
                        if not opt.is_visible():
                            continue
                        text = opt.inner_text().strip()
                        if value.lower() in text.lower():
                            opt.click()
                            matched = True
                            break
                    except Exception:
                        continue
                if not matched:
                    for opt in reversed(options):
                        try:
                            if opt.is_visible():
                                opt.click()
                                break
                        except Exception:
                            continue
                time.sleep(0.3)

            elif atype == "upload":
                if os.path.exists(value):
                    el = frame.wait_for_selector(sel, timeout=4000)
                    el.set_input_files(value)
                else:
                    print(f"[ai_filler] upload skipped — file not found: {value}")

            elif atype == "check":
                el = frame.wait_for_selector(sel, timeout=4000)
                el.check()

            elif atype == "click_submit":
                for btn_sel in [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button:has-text('Submit application')",
                    "button:has-text('Submit')",
                ]:
                    try:
                        btn = frame.wait_for_selector(btn_sel, timeout=3000)
                        btn.scroll_into_view_if_needed()
                        time.sleep(0.5)
                        btn.click()
                        print("[ai_filler] submit clicked")
                        time.sleep(5)
                        break
                    except PWTimeout:
                        continue

        except Exception as exc:
            print(f"[ai_filler] action {action} → {exc}")


# ── Outcome detection ──────────────────────────────────────────────────────────
def _detect_outcome(frame: Frame) -> tuple[str, list[str]]:
    content = frame.content().lower()
    if any(w in content for w in [
        "thank you", "application received", "successfully submitted",
        "we'll be in touch", "application submitted", "you've applied",
        "your application has been",
    ]):
        return "success", []

    error_els = frame.query_selector_all(
        '[class*="error"]:not([class*="error-boundary"]),'
        '[class*="invalid"],[aria-invalid="true"],'
        '.field_with_errors,[class*="validation"]'
    )
    errors = []
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


# ── Main entry (sync) ──────────────────────────────────────────────────────────
def ai_fill_form(page: Page, frame: Frame,
                 profile: dict, cover_letter: str) -> bool:
    """Claude-powered retry loop. Returns True if form was submitted."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ai_filler] ANTHROPIC_API_KEY not set — skipping AI fill")
        return False

    client = anthropic.Anthropic(api_key=api_key)
    prior_errors: list[str] = []

    cl_tmp = None
    if cover_letter:
        fd, cl_tmp = tempfile.mkstemp(suffix=".txt", prefix="cover_letter_")
        with os.fdopen(fd, "w") as f:
            f.write(cover_letter)
        profile = {**profile, "_cover_letter_path": cl_tmp}

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"[ai_filler] attempt {attempt}/{MAX_ATTEMPTS}")

            fields = _extract_form_fields(frame)
            if not fields:
                print("[ai_filler] no fields found — giving up")
                return False

            screenshot_b64 = _screenshot_b64(page)
            prompt = _build_prompt(fields, profile, prior_errors, attempt)

            try:
                msg = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": screenshot_b64,
                            }},
                            {"type": "text", "text": prompt},
                        ],
                    }],
                )
                raw = msg.content[0].text.strip()
                if raw.startswith("```"):
                    raw = "\n".join(
                        l for l in raw.splitlines()
                        if not l.strip().startswith("```")
                    )
                actions = json.loads(raw)
            except Exception as exc:
                print(f"[ai_filler] Claude error attempt {attempt}: {exc}")
                break

            print(f"[ai_filler] executing {len(actions)} actions")
            _execute_actions(frame, actions)

            outcome, errors = _detect_outcome(frame)
            print(f"[ai_filler] outcome={outcome} errors={errors}")

            if outcome == "success":
                return True
            if outcome == "errors":
                prior_errors = errors
                continue
            if any(a.get("type") == "click_submit" for a in actions):
                print("[ai_filler] submit executed, outcome unknown — treating as success")
                return True
            break

    finally:
        if cl_tmp:
            try:
                os.unlink(cl_tmp)
            except OSError:
                pass

    return False
