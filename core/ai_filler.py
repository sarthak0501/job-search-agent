from __future__ import annotations
"""
ai_filler.py – Claude-powered form-fill loop (sync Playwright API).

Uses the local `claude` CLI (Claude Code) as the AI backend — no API key
required. The user just needs a Claude Code terminal open.
"""
import json
import os
import subprocess
import tempfile
import time

from playwright.sync_api import Page, Frame, TimeoutError as PWTimeout

MAX_ATTEMPTS = 5
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")


# ── DOM extraction ─────────────────────────────────────────────────────────────
def _extract_form_fields(frame: Frame) -> list[dict]:
    return frame.evaluate("""() => {
        const fields = [];
        const seen = new Set();
        const inputs = document.querySelectorAll(
            'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]),' +
            'textarea, select'
        );
        inputs.forEach(el => {
            const id   = el.id   || '';
            const name = el.name || '';
            const key  = id || name;
            if (!key || seen.has(key)) return;
            seen.add(key);

            // Skip elements with zero dimensions (hidden/collapsed)
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return;

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
- Use the field's id when present. If id is blank, put the field name in the id slot.
- Resume upload (id=resume): use profile.resume_path.
- Cover letter upload (id=cover_letter): use profile._cover_letter_path if present, else skip.
- Phone country-code combobox (id=country): use combobox action with value "United States".
- Phone (id=phone): fill with raw digits from profile.phone (no dashes or spaces).
- For EEOC fields (gender, race, ethnicity, disability, veteran, orientation, transgender,
  sexual orientation): use the profile value if it matches an option. If no match, pick
  the option containing "decline", "prefer not", "choose not", or "I don't wish".
  If none of those exist, pick the LAST option in the list (usually the decline option).
  These fields are often required — always pick something.
- Salary: use salary_min, salary_max, or salary_range as the label suggests.
- Skip LinkedIn if profile.linkedin is empty.
- For open-ended textarea questions, write a brief professional answer from the applicant's background.
- Fields with role=combobox need the combobox action type.
- If a field id starts with a digit, still use it as-is in the id field.
"""


# ── Action executor ────────────────────────────────────────────────────────────
def _selector_candidates(action: dict) -> list[str]:
    values = []
    for key in ("id", "name"):
        value = str(action.get(key, "")).strip()
        if value and value not in values:
            values.append(value)

    selectors: list[str] = []
    for value in values:
        quoted = json.dumps(value)
        selectors.append(f"[id={quoted}]")
        selectors.append(f"[name={quoted}]")
    return selectors


def _wait_for_action_target(frame: Frame, action: dict, timeout: int = 4000):
    last_exc = None
    for selector in _selector_candidates(action):
        try:
            return frame.wait_for_selector(selector, timeout=timeout)
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise ValueError(f"No selector candidates found for action: {action}")


def _execute_actions(page: Page, frame: Frame, actions: list[dict]) -> None:
    for action in actions:
        atype = action.get("type")
        value = str(action.get("value", ""))

        try:
            if atype == "fill":
                el = _wait_for_action_target(frame, action)
                el.fill(value)

            elif atype == "select":
                el = _wait_for_action_target(frame, action)
                try:
                    el.select_option(label=value)
                except Exception:
                    el.select_option(value=value)

            elif atype == "combobox":
                # Close any open dropdown first
                page.keyboard.press("Escape")
                time.sleep(0.2)
                inp = _wait_for_action_target(frame, action)
                inp.click()
                time.sleep(0.8)

                def _pick_option(opts, val):
                    """Return True if a matching option was found and clicked."""
                    for opt in opts:
                        try:
                            text = opt.inner_text().strip()
                            if not text:
                                continue
                            if val.lower() in text.lower() or text.lower() in val.lower():
                                opt.click()
                                return True
                        except Exception:
                            continue
                    return False

                # Strategy 1: scan full list (no typing) — works for fixed-option dropdowns
                # Don't filter by is_visible() so we can find options scrolled out of view
                options = frame.query_selector_all('[role="option"]')
                matched = _pick_option(options, value)

                # Strategy 2: type to filter — works for searchable dropdowns
                if not matched:
                    inp.fill(value)
                    time.sleep(0.8)
                    options = frame.query_selector_all('[role="option"]')
                    matched = _pick_option(options, value)

                # Fallback: pick first visible option (accept anything over leaving it blank)
                if not matched:
                    all_opts = [o for o in frame.query_selector_all('[role="option"]') if o.is_visible()]
                    if all_opts:
                        all_opts[0].click()

                time.sleep(0.3)

            elif atype == "upload":
                if os.path.exists(value):
                    el = _wait_for_action_target(frame, action)
                    el.set_input_files(value)
                else:
                    print(f"[ai_filler] upload skipped — file not found: {value}")

            elif atype == "check":
                el = _wait_for_action_target(frame, action)
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
def _detect_outcome(page: Page, frame: Frame) -> tuple[str, list[str]]:
    contents = []
    seen_urls = set()

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
        "your application has been", "thank_you",
    ]):
        return "success", []

    errors = []
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


def _call_claude_cli(prompt: str) -> str:
    """Call the local claude CLI and return its output."""
    result = subprocess.run(
        [CLAUDE_BIN, "-p", prompt],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude CLI error: {result.stderr.strip()}")
    return result.stdout.strip()


# ── Main entry (sync) ──────────────────────────────────────────────────────────
def ai_fill_form(page: Page, frame: Frame,
                 profile: dict, cover_letter: str) -> bool:
    """Claude-powered retry loop using local claude CLI. Returns True if submitted."""
    if not os.path.exists(CLAUDE_BIN):
        print(f"[ai_filler] claude CLI not found at {CLAUDE_BIN}")
        return False

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

            prompt = _build_prompt(fields, profile, prior_errors, attempt)

            try:
                raw = _call_claude_cli(prompt)
                # Strip markdown fences if Claude wraps output
                if "```" in raw:
                    raw = "\n".join(
                        l for l in raw.splitlines()
                        if not l.strip().startswith("```")
                    )
                actions = json.loads(raw)
            except Exception as exc:
                print(f"[ai_filler] Claude error attempt {attempt}: {exc}")
                break

            print(f"[ai_filler] executing {len(actions)} actions")
            _execute_actions(page, frame, actions)

            outcome, errors = _detect_outcome(page, frame)
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
