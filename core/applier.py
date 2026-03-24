from __future__ import annotations
"""
applier.py – automated Greenhouse job application via Playwright.

Works for:
  - Direct Greenhouse boards  (job-boards.greenhouse.io / boards.greenhouse.io)
  - Embedded Greenhouse forms  (careers.airbnb.com, stripe.com, etc.)
  - Lever boards               (api.lever.co)
"""
import asyncio
import os
import tempfile

import anthropic
from playwright.async_api import async_playwright, Frame, Page, TimeoutError as PWTimeout

from core.profile import PROFILE

RESUME_SUMMARY = """
Senior Data Scientist (8+ years) at Microsoft building production ML systems for
reliability, attribution, and customer analytics. Key projects: adaptive stress-testing
for Azure Storage (prevented 10+ Sev1/Sev2 incidents), multi-touch partner attribution
(+10% MoM revenue), LLM-powered customer health agent (50% reduction in aging CRIs).
Walmart Labs: planogram optimisation (+7% revenue, led 4 DS). Skills: Python, SQL,
PySpark, Azure, ML/statistical modelling, NLP, LLM agents, experiment design.
"""


# ── Cover letter ──────────────────────────────────────────────────────────────
def generate_cover_letter(job_title: str, company: str, job_description: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=500,
                messages=[{"role": "user", "content": (
                    f"Write a concise cover letter (3 paragraphs, ~200 words) for:\n"
                    f"Job: {job_title} at {company}\n"
                    f"Description: {job_description[:1200]}\n"
                    f"Applicant: {RESUME_SUMMARY}\n"
                    f"Rules: specific achievements, no clichés, output body only (no header/signature)"
                )}],
            )
            return msg.content[0].text.strip()
        except Exception as exc:
            print(f"[applier] Claude cover letter failed ({exc}), using fallback")

    return (
        f"I'm excited to apply for the {job_title} role at {company}. "
        f"With 8+ years building production ML and data systems at Microsoft — including "
        f"adaptive stress-testing for Azure Storage (10+ Sev1/Sev2 incidents prevented), "
        f"multi-touch attribution pipelines (+10% MoM revenue), and an LLM-powered "
        f"customer health agent (50% reduction in aging CRIs) — I bring a consistent "
        f"record of turning complex data problems into measurable outcomes.\n\n"
        f"I thrive owning end-to-end pipelines from experimentation through production "
        f"and would love to bring this depth of experience to {company}."
    )


def answer_custom_question(question: str, job_title: str, company: str) -> str:
    """Use Claude to answer a job-specific custom question, or return empty string."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return ""
    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": (
                f"Answer this job application question concisely (2-4 sentences) for "
                f"{job_title} at {company}:\n\nQuestion: {question}\n\n"
                f"Applicant background: {RESUME_SUMMARY}\n\n"
                f"Answer directly and specifically. No fluff."
            )}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return ""


# ── Combobox helpers (React-Select / Greenhouse custom dropdowns) ─────────────
async def _combobox_select(frame: Frame, field_id: str, option_text: str, timeout: int = 5000) -> bool:
    """Type into a combobox and click the first visible option matching option_text."""
    try:
        inp = await frame.wait_for_selector(f'[id="{field_id}"]', timeout=timeout)
        await inp.click()
        await asyncio.sleep(0.3)
        await inp.fill(option_text)
        await asyncio.sleep(0.6)
        options = await frame.query_selector_all('[role="option"]')
        for opt in options:
            try:
                if not await opt.is_visible():
                    continue
                text = (await opt.inner_text()).strip()
                if option_text.lower() in text.lower():
                    await opt.click()
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
        # fallback: first visible option
        for opt in options:
            try:
                if await opt.is_visible():
                    await opt.click()
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
    except Exception as exc:
        print(f"[applier] combobox [id={field_id!r}] → {option_text!r} failed: {exc}")
    return False


async def _combobox_decline(frame: Frame, field_id: str, timeout: int = 4000) -> bool:
    """Open a combobox and pick the 'decline / prefer not to answer' option."""
    decline_terms = ["decline", "prefer not", "i don't wish", "choose not", "no response"]
    try:
        inp = await frame.wait_for_selector(f'[id="{field_id}"]', timeout=timeout)
        await inp.click()
        await asyncio.sleep(0.5)
        options = await frame.query_selector_all('[role="option"]')
        for opt in options:
            try:
                if not await opt.is_visible():
                    continue
                text = (await opt.inner_text()).strip().lower()
                if any(t in text for t in decline_terms):
                    await opt.click()
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
        # fallback: last visible option (usually decline)
        visible = [o for o in options]
        if visible:
            await visible[-1].click()
            await asyncio.sleep(0.3)
            return True
    except Exception as exc:
        print(f"[applier] combobox decline [id={field_id!r}] failed: {exc}")
    return False


# ── Phone country code ────────────────────────────────────────────────────────
async def _set_phone_country(frame: Frame):
    """Select United States (+1) in the phone country-code picker."""
    try:
        country_inp = await frame.wait_for_selector('[id="country"]', timeout=4000)
        await country_inp.click()
        await asyncio.sleep(0.3)
        await country_inp.fill("United States")
        await asyncio.sleep(0.6)
        options = await frame.query_selector_all('[role="option"]')
        for opt in options:
            try:
                if not await opt.is_visible():
                    continue
                text = (await opt.inner_text()).strip()
                if "United States" in text:
                    await opt.click()
                    await asyncio.sleep(0.3)
                    return
            except Exception:
                continue
    except Exception as exc:
        print(f"[applier] phone country code failed: {exc}")


# ── Core form filler (works in any Frame — page or iframe) ───────────────────
async def _fill_greenhouse_form(frame: Frame, job, cover_letter: str) -> bool:
    # ── Basic fields ──────────────────────────────────────────────────────────
    for fid, val in [
        ("first_name", PROFILE["first_name"]),
        ("last_name",  PROFILE["last_name"]),
        ("email",      PROFILE["email"]),
        ("phone",      PROFILE["phone"]),
    ]:
        try:
            el = await frame.wait_for_selector(f'#{fid}', timeout=4000)
            await el.fill(val)
        except PWTimeout:
            pass

    # Phone country code → +1
    await _set_phone_country(frame)
    # Re-fill phone (country picker sometimes clears it)
    try:
        ph = await frame.wait_for_selector("#phone", timeout=3000)
        await ph.fill(PROFILE["phone"])
    except PWTimeout:
        pass

    # Location
    try:
        loc = await frame.wait_for_selector("#candidate-location", timeout=3000)
        await loc.fill(PROFILE["location"])
        await asyncio.sleep(0.5)
        await frame.keyboard.press("Escape")
    except PWTimeout:
        pass

    # ── Resume ────────────────────────────────────────────────────────────────
    resume_path = PROFILE["resume_path"]
    if os.path.exists(resume_path):
        try:
            fi = await frame.wait_for_selector("#resume", timeout=4000)
            await fi.set_input_files(resume_path)
            print("[applier] resume uploaded")
        except PWTimeout:
            print("[applier] resume input not found")
    else:
        print(f"[applier] resume not found at {resume_path}")

    # ── Cover letter (file upload) ────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                     prefix="cover_letter_", delete=False) as tmp:
        tmp.write(cover_letter)
        cl_path = tmp.name
    try:
        cl_fi = await frame.wait_for_selector("#cover_letter", timeout=3000)
        await cl_fi.set_input_files(cl_path)
        print("[applier] cover letter uploaded")
    except PWTimeout:
        pass
    finally:
        os.unlink(cl_path)

    # ── Custom questions ──────────────────────────────────────────────────────
    question_inputs = await frame.query_selector_all("input[id^='question_'], textarea[id^='question_']")
    for inp in question_inputs:
        qid   = await inp.get_attribute("id")
        label = await inp.evaluate(
            'el => { const l = document.querySelector("label[for=" + JSON.stringify(el.id) + "]"); '
            'return l ? l.innerText.trim().toLowerCase() : ""; }'
        )
        tag   = await inp.evaluate("el => el.tagName")

        if "linkedin" in label:
            pass  # skip — not available

        elif "hear about" in label or "how did you" in label:
            await _combobox_select(frame, qid, PROFILE["referral_source"])

        elif "current" in label and "company" in label:
            try:
                await inp.fill(PROFILE["current_company"])
            except Exception:
                pass

        elif "years" in label and "experience" in label:
            try:
                await inp.fill(PROFILE["years_experience"])
            except Exception:
                pass

        elif "authorized" in label or ("work" in label and ("country" in label or "u.s" in label or "united states" in label)):
            await _combobox_select(frame, qid, PROFILE["work_authorized"])

        elif "sponsor" in label or "immigration" in label or "visa" in label:
            await _combobox_select(frame, qid, PROFILE["requires_sponsorship"])

        elif "privacy" in label or "i agree" in label or "candidate privacy" in label:
            await _combobox_select(frame, qid, "I agree")

        elif "gender" in label:
            await _combobox_select(frame, qid, PROFILE["gender_eeoc"])

        elif "veteran" in label:
            await _combobox_decline(frame, qid)

        elif "race" in label or "ethnicity" in label:
            await _combobox_decline(frame, qid)

        elif "disability" in label:
            await _combobox_decline(frame, qid)

        elif "transgender" in label or "sexual" in label or "orientation" in label:
            await _combobox_decline(frame, qid)

        elif tag == "TEXTAREA":
            # Job-specific open-ended question — ask Claude
            answer = answer_custom_question(label, job.title, job.company)
            if answer:
                try:
                    await inp.fill(answer)
                except Exception:
                    pass

    # ── "Decline all EEOC" checkbox (Airbnb pattern) ─────────────────────────
    # Some forms have a single "I decline to answer" checkbox instead of dropdowns
    try:
        decline_cb = await frame.query_selector("input[type='checkbox'][id*='question_']")
        if decline_cb:
            label = await decline_cb.evaluate(
                'el => { const l = document.querySelector("label[for=" + JSON.stringify(el.id) + "]"); '
                'return l ? l.innerText.trim().toLowerCase() : ""; }'
            )
            if "decline" in label or "prefer not" in label:
                await decline_cb.check()
                await asyncio.sleep(0.3)
    except Exception:
        pass

    # ── EEOC numeric-id fields (Reddit pattern: #430 etc.) ───────────────────
    for eid in ["430", "431", "432", "433", "434"]:
        try:
            el = await frame.query_selector(f'[id="{eid}"]')
            if el:
                await _combobox_decline(frame, eid)
        except Exception:
            pass

    await asyncio.sleep(1)

    # ── Submit ────────────────────────────────────────────────────────────────
    for sel in [
        "button[type='submit']",
        "input[type='submit']",
        "button:has-text('Submit application')",
        "button:has-text('Submit')",
    ]:
        try:
            btn = await frame.wait_for_selector(sel, timeout=3000)
            await btn.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
            await btn.click()
            print("[applier] submit clicked, waiting …")
            # wait on the parent page, not the frame
            await asyncio.sleep(5)
            content = (await frame.content()).lower()
            if any(w in content for w in ["thank you", "application received",
                                           "successfully submitted", "we'll be in touch"]):
                print("[applier] success confirmed via page content")
                return True
            print("[applier] submitted — verify via screenshot")
            return True
        except PWTimeout:
            continue

    print("[applier] submit button not found")
    return False


# ── Platform detection & orchestration ───────────────────────────────────────
def _is_greenhouse(url: str) -> bool:
    return any(h in url for h in ["greenhouse.io", "careerpuck.com", "gh_jid="])

def _is_lever(url: str) -> bool:
    return "lever.co" in url


async def _get_greenhouse_frame(page: Page, timeout: int = 8000) -> Frame:
    """Return the Greenhouse form frame (iframe or main page)."""
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        for f in page.frames:
            if "greenhouse.io/embed" in f.url or "greenhouse.io/job_app" in f.url:
                return f
        await asyncio.sleep(0.5)
    # No iframe found — form is on the main page
    return page.main_frame


async def _click_apply_button(page: Page):
    """Click an Apply / Apply Now button if present (company career pages)."""
    for sel in [
        "a:has-text('Apply Now')",
        "button:has-text('Apply Now')",
        "a:has-text('Apply for this job')",
        "button:has-text('Apply for this job')",
        "a:has-text('Apply')",
        "button:has-text('Apply')",
    ]:
        try:
            btn = await page.wait_for_selector(sel, timeout=3000)
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=8000)
            await asyncio.sleep(1)
            return
        except PWTimeout:
            continue


async def _apply_lever(page: Page, job, cover_letter: str) -> bool:
    print("[applier] filling Lever form …")
    for sel, val in [
        ("input[name='name']",  f"{PROFILE['first_name']} {PROFILE['last_name']}"),
        ("input[name='email']", PROFILE["email"]),
        ("input[name='phone']", PROFILE["phone_country_code"] + PROFILE["phone"]),
    ]:
        try:
            el = await page.wait_for_selector(sel, timeout=3000)
            await el.fill(val)
        except PWTimeout:
            pass

    resume_path = PROFILE["resume_path"]
    if os.path.exists(resume_path):
        try:
            fi = await page.wait_for_selector("input[type='file']", timeout=3000)
            await fi.set_input_files(resume_path)
        except PWTimeout:
            pass

    try:
        ta = await page.wait_for_selector("textarea[name='comments']", timeout=2000)
        await ta.fill(cover_letter)
    except PWTimeout:
        pass

    for sel in ["button[type='submit']", "button:has-text('Submit')", "input[type='submit']"]:
        try:
            btn = await page.wait_for_selector(sel, timeout=3000)
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=12000)
            return True
        except PWTimeout:
            continue
    return False


# ── Main entry ────────────────────────────────────────────────────────────────
async def _do_apply(job) -> dict:
    cover_letter = generate_cover_letter(job.title, job.company, job.description or "")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()
        try:
            print(f"[applier] → {job.url}")
            await page.goto(job.url, wait_until="networkidle", timeout=25000)

            if _is_greenhouse(job.url):
                # Some URLs land on a company career page with an Apply button
                await _click_apply_button(page)
                # Get the form frame (iframe embed or main page)
                frame = await _get_greenhouse_frame(page)
                print(f"[applier] form frame: {frame.url[:70]}")
                success = await _fill_greenhouse_form(frame, job, cover_letter)

            elif _is_lever(job.url):
                success = await _apply_lever(page, job, cover_letter)

            else:
                print("[applier] unknown platform — leaving browser open 60s for manual fill")
                await asyncio.sleep(60)
                success = False

            # Screenshot for verification
            screenshot_path = f"/tmp/apply_{job.company}_{job.id}.png"
            await page.screenshot(path=screenshot_path, full_page=False)
            print(f"[applier] screenshot → {screenshot_path}")

            await asyncio.sleep(3)
            return {"success": success, "cover_letter": cover_letter}

        except Exception as exc:
            print(f"[applier] error: {exc}")
            return {"success": False, "error": str(exc), "cover_letter": cover_letter}
        finally:
            await browser.close()


def apply_to_job(job) -> dict:
    return asyncio.run(_do_apply(job))
