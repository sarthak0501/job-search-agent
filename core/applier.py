from __future__ import annotations
"""
applier.py – automated job application via Playwright.
Supports Greenhouse-hosted forms (Reddit, Airbnb, Stripe, Databricks, Lyft, etc.)
"""
import asyncio
import os
import tempfile
from pathlib import Path

import anthropic
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

# ── Applicant profile ──────────────────────────────────────────────────────────
PROFILE = {
    "first_name":           "Sarthak",
    "last_name":            "Bichhawa",
    "email":                "sarthaksgsits@gmail.com",
    "phone":                "3128383536",
    "location":             "Redmond, WA",
    "current_company":      "Microsoft",
    "years_experience":     "8",
    "work_authorized":      "Yes",   # H1-B — authorized to work in US
    "requires_sponsorship": "Yes",   # H1-B — needs sponsorship
    "resume_path":          str(Path.home() / "Downloads" / "BichhawaSarthakResume_2026.pdf"),
}

RESUME_SUMMARY = """
Senior Data Scientist (8+ years) at Microsoft building production ML systems for
reliability, attribution, and customer analytics. Key work: adaptive stress-testing
for Azure Storage (prevented 10+ Sev1/Sev2 incidents), multi-touch partner attribution
(+10% MoM attributed revenue), LLM-powered customer health agent (50% reduction in
aging CRIs). Previously at Walmart Labs (+7% revenue lift via planogram optimization)
and Integral Ad Science (PySpark NLP, ~1M URLs/day). Skills: Python, SQL, PySpark,
Azure, ML, statistical modeling, NLP, LLM agents, experiment design.
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
                messages=[{"role": "user", "content": f"""Write a concise cover letter (3 paragraphs, ~200 words) for:
Job: {job_title} at {company}
Description: {job_description[:1200]}
Applicant: {RESUME_SUMMARY}
Rules: specific achievements, no "I am writing to express my interest", output body only (no header/signature)"""}],
            )
            return msg.content[0].text.strip()
        except Exception as exc:
            print(f"[applier] Claude cover letter failed ({exc}), using generic")

    return (
        f"I'm excited to apply for the {job_title} role at {company}. "
        f"With 8+ years building production ML and data systems at Microsoft — including "
        f"adaptive stress-testing for Azure Storage (prevented 10+ Sev1/Sev2 incidents), "
        f"multi-touch partner attribution pipelines (+10% MoM revenue), and an LLM-powered "
        f"customer health agent that cut aging CRIs by 50% — I bring a strong track record "
        f"of turning complex data problems into measurable business outcomes.\n\n"
        f"I thrive owning end-to-end pipelines from experimentation through production, and "
        f"I'm energized by the opportunity to bring this depth of experience to {company}. "
        f"I'd love to connect and learn more about the team."
    )


# ── Combobox helper ───────────────────────────────────────────────────────────
async def select_combobox(page: Page, field_id: str, option_text: str, timeout: int = 5000):
    """Click a Greenhouse React-Select combobox and pick the matching option."""
    try:
        inp = await page.wait_for_selector(f'[id="{field_id}"]', timeout=timeout)
        await inp.click()
        await asyncio.sleep(0.4)
        await inp.fill(option_text)
        await asyncio.sleep(0.5)
        options = await page.query_selector_all('[role="option"]')
        for opt in options:
            try:
                if not await opt.is_visible():
                    continue
                text = (await opt.inner_text()).strip()
                if text.lower() == option_text.lower():
                    await opt.click()
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
        return False
    except Exception as exc:
        print(f"[applier] combobox [id={field_id!r}] → {option_text!r} failed: {exc}")
        return False


async def select_combobox_first(page: Page, field_id: str, timeout: int = 5000):
    """Open a combobox and pick the first available option (for EEOC decline)."""
    try:
        inp = await page.wait_for_selector(f"#{field_id}", timeout=timeout)
        await inp.click()
        await asyncio.sleep(0.5)
        option = await page.wait_for_selector('[role="option"]', timeout=3000)
        await option.click()
        await asyncio.sleep(0.3)
        return True
    except Exception as exc:
        print(f"[applier] combobox first #{field_id} failed: {exc}")
        return False


async def select_combobox_containing(page: Page, field_id: str, partial: str, timeout: int = 5000):
    """Pick the first visible option whose text contains `partial` (case-insensitive)."""
    try:
        inp = await page.wait_for_selector(f'[id="{field_id}"]', timeout=timeout)
        await inp.click()
        await asyncio.sleep(0.4)
        await inp.fill(partial)
        await asyncio.sleep(0.6)
        options = await page.query_selector_all('[role="option"]')
        for opt in options:
            try:
                if not await opt.is_visible():
                    continue
                text = (await opt.inner_text()).strip().lower()
                if partial.lower() in text:
                    await opt.click()
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
        # fallback: click first visible
        for opt in options:
            try:
                if await opt.is_visible():
                    await opt.click()
                    await asyncio.sleep(0.3)
                    return True
            except Exception:
                continue
        return True
    except Exception as exc:
        print(f"[applier] combobox containing [id={field_id!r}] → {partial!r} failed: {exc}")
        return False


# ── Greenhouse form ───────────────────────────────────────────────────────────
async def _apply_greenhouse(page: Page, job, cover_letter: str) -> bool:
    print("[applier] filling Greenhouse form …")

    # Basic fields
    for field_id, value in [
        ("first_name",  PROFILE["first_name"]),
        ("last_name",   PROFILE["last_name"]),
        ("email",       PROFILE["email"]),
        ("phone",       PROFILE["phone"]),
    ]:
        try:
            el = await page.wait_for_selector(f"#{field_id}", timeout=4000)
            await el.fill(value)
        except PWTimeout:
            print(f"[applier] field #{field_id} not found, skipping")

    # Location
    try:
        loc = await page.wait_for_selector("#candidate-location", timeout=3000)
        await loc.fill(PROFILE["location"])
        await asyncio.sleep(0.6)
        # dismiss autocomplete if it appears
        await page.keyboard.press("Escape")
    except PWTimeout:
        pass

    # Resume upload
    resume_path = PROFILE["resume_path"]
    if os.path.exists(resume_path):
        try:
            file_input = await page.wait_for_selector("#resume", timeout=4000)
            await file_input.set_input_files(resume_path)
            print("[applier] resume uploaded")
        except PWTimeout:
            print("[applier] resume input not found")
    else:
        print(f"[applier] resume not found at {resume_path}")

    # Cover letter — write to temp file and upload
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                      prefix="cover_letter_", delete=False) as tmp:
        tmp.write(cover_letter)
        cl_path = tmp.name
    try:
        cl_input = await page.wait_for_selector("#cover_letter", timeout=3000)
        await cl_input.set_input_files(cl_path)
        print("[applier] cover letter uploaded")
    except PWTimeout:
        print("[applier] cover letter input not found, skipping")
    finally:
        os.unlink(cl_path)

    # ── Custom questions ───────────────────────────────────────────────────────
    # Scan all question_* fields by their label and fill appropriately
    question_inputs = await page.query_selector_all("input[id^='question_']")
    for inp in question_inputs:
        qid = await inp.get_attribute("id")
        label = await page.evaluate(f"""() => {{
            const lbl = document.querySelector("label[for='{qid}']");
            return lbl ? lbl.innerText.toLowerCase() : "";
        }}""")

        if "linkedin" in label:
            pass  # skip — not available
        elif "hear about" in label or "how did you" in label:
            await select_combobox_containing(page, qid, "job board")
        elif "current" in label and "company" in label:
            try:
                el = await page.wait_for_selector(f"#{qid}", timeout=2000)
                await el.fill(PROFILE["current_company"])
            except PWTimeout:
                pass
        elif "years" in label and "experience" in label:
            try:
                el = await page.wait_for_selector(f"#{qid}", timeout=2000)
                await el.fill(PROFILE["years_experience"])
            except PWTimeout:
                pass
        elif "authorized" in label or "work in the u" in label:
            await select_combobox(page, qid, PROFILE["work_authorized"])
        elif "sponsor" in label or "immigration" in label or "visa" in label:
            await select_combobox(page, qid, PROFILE["requires_sponsorship"])
        elif "privacy" in label or "i agree" in label or "candidate privacy" in label:
            await select_combobox_containing(page, qid, "I agree")

    # EEOC / DEI questions (numeric IDs: gender, transgender, orientation, disability, veteran)
    eeoc_ids = ["430", "431", "432", "433", "434"]
    decline_terms = ["decline", "prefer not", "i don't wish", "choose not"]
    for eid in eeoc_ids:
        try:
            # CSS IDs starting with digits need attribute selector
            el = await page.query_selector(f'[id="{eid}"]')
            if not el:
                continue
            await el.click()
            await asyncio.sleep(0.5)
            options = await page.query_selector_all('[role="option"]')
            declined = False
            for opt in options:
                text = (await opt.inner_text()).strip().lower()
                if any(t in text for t in decline_terms):
                    await opt.click()
                    declined = True
                    break
            if not declined and options:
                await options[-1].click()
            await asyncio.sleep(0.3)
        except Exception as exc:
            print(f"[applier] EEOC [id={eid!r}] failed: {exc}")

    await asyncio.sleep(1)

    # Submit
    for sel in ["button[type='submit']", "input[type='submit']",
                "button:has-text('Submit application')", "button:has-text('Submit')"]:
        try:
            btn = await page.wait_for_selector(sel, timeout=3000)
            await btn.scroll_into_view_if_needed()
            await asyncio.sleep(0.5)
            await btn.click()
            print("[applier] submit clicked, waiting for confirmation …")
            await page.wait_for_load_state("networkidle", timeout=15000)
            await asyncio.sleep(2)
            # Screenshot for verification
            screenshot_path = f"/tmp/apply_{job.company}_{job.id}.png"
            await page.screenshot(path=screenshot_path, full_page=False)
            print(f"[applier] screenshot saved: {screenshot_path}")
            # Check for success indicators
            content = (await page.content()).lower()
            if any(w in content for w in ["thank you", "application received",
                                           "successfully submitted", "we'll be in touch"]):
                print("[applier] success confirmed")
                return True
            print("[applier] submitted — check screenshot to verify")
            return True  # assume success if no error
        except PWTimeout:
            continue

    print("[applier] submit button not found")
    return False


# ── Lever form ────────────────────────────────────────────────────────────────
async def _apply_lever(page: Page, job, cover_letter: str) -> bool:
    print("[applier] filling Lever form …")
    for sel, value in [
        ("input[name='name']",  f"{PROFILE['first_name']} {PROFILE['last_name']}"),
        ("input[name='email']", PROFILE["email"]),
        ("input[name='phone']", PROFILE["phone"]),
    ]:
        try:
            el = await page.wait_for_selector(sel, timeout=3000)
            await el.fill(value)
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


# ── Main ──────────────────────────────────────────────────────────────────────
def _is_greenhouse(url: str) -> bool:
    return any(h in url for h in ["greenhouse.io", "careerpuck.com", "gh_jid="])

def _is_lever(url: str) -> bool:
    return "lever.co" in url


async def _do_apply(job) -> dict:
    cover_letter = generate_cover_letter(
        job_title=job.title,
        company=job.company,
        job_description=job.description or "",
    )

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
            print(f"[applier] navigating to {job.url}")
            await page.goto(job.url, wait_until="networkidle", timeout=25000)

            if _is_greenhouse(job.url):
                success = await _apply_greenhouse(page, job, cover_letter)
            elif _is_lever(job.url):
                success = await _apply_lever(page, job, cover_letter)
            else:
                print(f"[applier] unknown platform, leaving browser open for manual fill")
                await asyncio.sleep(60)
                success = False

            await asyncio.sleep(3)
            return {"success": success, "cover_letter": cover_letter}
        except Exception as exc:
            print(f"[applier] error: {exc}")
            return {"success": False, "error": str(exc), "cover_letter": cover_letter}
        finally:
            await browser.close()


def apply_to_job(job) -> dict:
    """Synchronous wrapper — called from FastAPI background task."""
    return asyncio.run(_do_apply(job))
