from __future__ import annotations
"""
ai_filler.py – Claude-powered form-fill loop.

Given a Playwright Page + Frame and a user profile dict, Claude looks at the
form (screenshot + DOM field list) and returns a list of actions to execute.
The loop retries up to MAX_ATTEMPTS times, feeding validation errors back to
Claude so it can correct mistakes.
"""
import asyncio
import base64
import json
import os
import tempfile
from typing import Any

import anthropic
from playwright.async_api import Frame, Page, TimeoutError as PWTimeout

MAX_ATTEMPTS = 5


# ── DOM extraction ─────────────────────────────────────────────────────────────
async def _extract_form_fields(frame: Frame) -> list[dict]:
    """Walk the frame DOM and return structured field info for Claude."""
    return await frame.evaluate("""() => {
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


async def _take_screenshot_b64(page: Page) -> str:
    data = await page.screenshot(type="jpeg", quality=55, full_page=False)
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

    # Build a clean profile summary (exclude internal keys)
    p = {k: v for k, v in profile.items() if not k.startswith("_")}

    return f"""You are filling a job application form for this applicant.

APPLICANT PROFILE:
{json.dumps(p, indent=2)}

FORM FIELDS (id, type, label, options):
{json.dumps(fields, indent=2)}
{errors_block}

Return ONLY a valid JSON array of action objects. No explanation, no markdown fences.

Action types:
  fill        — text/email/tel/textarea:  {{"type":"fill","id":"<id>","value":"<str>"}}
  select      — native <select>:          {{"type":"select","id":"<id>","value":"<option text>"}}
  combobox    — React-Select/custom:      {{"type":"combobox","id":"<id>","value":"<option text>"}}
  upload      — file input:               {{"type":"upload","id":"<id>","value":"<abs path>"}}
  check       — checkbox:                 {{"type":"check","id":"<id>"}}
  click_submit — submit the form:         {{"type":"click_submit"}}

Rules:
- Always end with click_submit.
- Resume upload: use profile.resume_path.
- Cover letter upload (id=cover_letter): use profile._cover_letter_path if present.
- Phone: fill the raw digits from profile.phone (no dashes). For the country-code
  combobox (id=country), use combobox action with value "United States".
- Work auth / sponsorship: match profile.work_authorized and profile.requires_sponsorship.
- EEOC fields (gender, race, disability, veteran, orientation, transgender):
  use the profile value if it matches an option, otherwise pick the "decline /
  prefer not to answer" option.
- Salary: use salary_min, salary_max, or salary_range as the label suggests.
- Skip LinkedIn if profile.linkedin is empty.
- For custom open-ended textarea questions, write a brief professional answer
  drawing on the applicant's background.
- Combobox fields with role="combobox" need the combobox action type.
- If a field id starts with a digit, still use it as-is in the id field.
"""


# ── Action executor ────────────────────────────────────────────────────────────
async def _execute_actions(frame: Frame, actions: list[dict]) -> None:
    for action in actions:
        atype = action.get("type")
        aid   = str(action.get("id", ""))
        value = str(action.get("value", ""))
        sel   = f'[id="{aid}"]' if aid else ""

        try:
            if atype == "fill":
                el = await frame.wait_for_selector(sel, timeout=4000)
                await el.fill(value)

            elif atype == "select":
                el = await frame.wait_for_selector(sel, timeout=4000)
                try:
                    await el.select_option(label=value)
                except Exception:
                    await el.select_option(value=value)

            elif atype == "combobox":
                inp = await frame.wait_for_selector(sel, timeout=4000)
                await inp.click()
                await asyncio.sleep(0.35)
                await inp.fill(value)
                await asyncio.sleep(0.6)
                options = await frame.query_selector_all('[role="option"]')
                matched = False
                for opt in options:
                    if not await opt.is_visible():
                        continue
                    text = (await opt.inner_text()).strip()
                    if value.lower() in text.lower():
                        await opt.click()
                        matched = True
                        break
                if not matched:
                    # pick last visible option (usually "prefer not to answer")
                    for opt in reversed(options):
                        try:
                            if await opt.is_visible():
                                await opt.click()
                                break
                        except Exception:
                            continue
                await asyncio.sleep(0.3)

            elif atype == "upload":
                if os.path.exists(value):
                    el = await frame.wait_for_selector(sel, timeout=4000)
                    await el.set_input_files(value)
                else:
                    print(f"[ai_filler] upload skipped — file not found: {value}")

            elif atype == "check":
                el = await frame.wait_for_selector(sel, timeout=4000)
                await el.check()

            elif atype == "click_submit":
                for btn_sel in [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button:has-text('Submit application')",
                    "button:has-text('Submit')",
                ]:
                    try:
                        btn = await frame.wait_for_selector(btn_sel, timeout=3000)
                        await btn.scroll_into_view_if_needed()
                        await asyncio.sleep(0.5)
                        await btn.click()
                        print("[ai_filler] submit clicked")
                        await asyncio.sleep(5)
                        break
                    except PWTimeout:
                        continue

        except Exception as exc:
            print(f"[ai_filler] action {action} → {exc}")


# ── Outcome detection ──────────────────────────────────────────────────────────
async def _detect_outcome(frame: Frame) -> tuple[str, list[str]]:
    content = (await frame.content()).lower()
    if any(w in content for w in [
        "thank you", "application received", "successfully submitted",
        "we'll be in touch", "application submitted", "you've applied",
        "your application has been",
    ]):
        return "success", []

    error_els = await frame.query_selector_all(
        '[class*="error"]:not([class*="error-boundary"]), '
        '[class*="invalid"], [aria-invalid="true"], '
        '.field_with_errors, [class*="validation"]'
    )
    errors = []
    for el in error_els:
        try:
            if await el.is_visible():
                txt = (await el.inner_text()).strip()
                if txt and len(txt) < 300:
                    errors.append(txt)
        except Exception:
            pass

    if errors:
        return "errors", list(dict.fromkeys(errors))  # dedupe

    return "unknown", []


# ── Main entry ─────────────────────────────────────────────────────────────────
async def ai_fill_form(page: Page, frame: Frame,
                       profile: dict, cover_letter: str) -> bool:
    """
    Claude-powered retry loop. Returns True if form was successfully submitted.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[ai_filler] ANTHROPIC_API_KEY not set — skipping AI fill")
        return False

    client = anthropic.Anthropic(api_key=api_key)
    prior_errors: list[str] = []

    # Write cover letter to temp file so Claude can reference it
    cl_tmp = None
    if cover_letter:
        fd, cl_tmp = tempfile.mkstemp(suffix=".txt", prefix="cover_letter_")
        with os.fdopen(fd, "w") as f:
            f.write(cover_letter)
        profile = {**profile, "_cover_letter_path": cl_tmp}

    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"[ai_filler] attempt {attempt}/{MAX_ATTEMPTS}")

            fields = await _extract_form_fields(frame)
            if not fields:
                print("[ai_filler] no fields found — giving up")
                return False

            screenshot_b64 = await _take_screenshot_b64(page)
            prompt = _build_prompt(fields, profile, prior_errors, attempt)

            try:
                msg = client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=2048,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": screenshot_b64,
                                },
                            },
                            {"type": "text", "text": prompt},
                        ],
                    }],
                )
                raw = msg.content[0].text.strip()
                # Strip markdown fences if Claude wraps output
                if raw.startswith("```"):
                    raw = "\n".join(
                        l for l in raw.splitlines()
                        if not l.strip().startswith("```")
                    )
                actions = json.loads(raw)
            except Exception as exc:
                print(f"[ai_filler] Claude error on attempt {attempt}: {exc}")
                break

            print(f"[ai_filler] executing {len(actions)} actions")
            await _execute_actions(frame, actions)

            outcome, errors = await _detect_outcome(frame)
            print(f"[ai_filler] outcome={outcome} errors={errors}")

            if outcome == "success":
                return True

            if outcome == "errors":
                prior_errors = errors
                continue

            # "unknown" — if we clicked submit, assume success
            if any(a.get("type") == "click_submit" for a in actions):
                print("[ai_filler] submit executed, outcome unknown — treating as success")
                return True

            break  # no submit attempted and no errors — something went wrong

    finally:
        if cl_tmp:
            try:
                os.unlink(cl_tmp)
            except OSError:
                pass

    return False
