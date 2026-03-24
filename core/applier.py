from __future__ import annotations
"""
applier.py – orchestrates job application.

Profile is loaded fresh from profile.json at apply time.
Form filling is delegated to ai_filler.ai_fill_form (Claude-powered loop).
"""
import asyncio
import os

import anthropic
from playwright.async_api import async_playwright, Frame, Page, TimeoutError as PWTimeout

from core.profile_store import load_profile
from core.ai_filler import ai_fill_form

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
                    f"Rules: specific achievements, no clichés, output body only"
                )}],
            )
            return msg.content[0].text.strip()
        except Exception as exc:
            print(f"[applier] Claude cover letter failed ({exc}), using fallback")

    return (
        f"I'm excited to apply for the {job_title} role at {company}. "
        f"With 8+ years building production ML and data systems at Microsoft — "
        f"adaptive stress-testing for Azure Storage (10+ Sev1/Sev2 incidents prevented), "
        f"multi-touch attribution (+10% MoM revenue), and an LLM-powered customer health "
        f"agent (50% reduction in aging CRIs) — I bring a consistent record of turning "
        f"complex data problems into measurable outcomes.\n\n"
        f"I'd love to bring this depth of experience to {company}."
    )


# ── Platform helpers ──────────────────────────────────────────────────────────
def _is_greenhouse(url: str) -> bool:
    return any(h in url for h in ["greenhouse.io", "careerpuck.com", "gh_jid="])

def _is_lever(url: str) -> bool:
    return "lever.co" in url


async def _click_apply_button(page: Page) -> None:
    """Click Apply / Apply Now if present (company career pages before the form loads)."""
    for sel in [
        "a:has-text('Apply Now')", "button:has-text('Apply Now')",
        "a:has-text('Apply for this job')", "button:has-text('Apply for this job')",
        "a:has-text('Apply')", "button:has-text('Apply')",
    ]:
        try:
            btn = await page.wait_for_selector(sel, timeout=3000)
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=8000)
            await asyncio.sleep(1)
            return
        except PWTimeout:
            continue


async def _get_greenhouse_frame(page: Page, timeout: int = 8000) -> Frame:
    """Return the Greenhouse form frame (embedded iframe or main page)."""
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        for f in page.frames:
            if "greenhouse.io/embed" in f.url or "greenhouse.io/job_app" in f.url:
                return f
        await asyncio.sleep(0.4)
    return page.main_frame  # form is directly on the page


# ── Main entry ────────────────────────────────────────────────────────────────
async def _do_apply(job) -> dict:
    profile = load_profile()
    if not profile:
        return {"success": False, "error": "No profile.json — complete setup at /setup"}

    cover_letter = generate_cover_letter(
        job.title, job.company, job.description or ""
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
            print(f"[applier] → {job.url}")
            await page.goto(job.url, wait_until="networkidle", timeout=25000)

            await _click_apply_button(page)

            if _is_greenhouse(job.url):
                frame = await _get_greenhouse_frame(page)
            else:
                frame = page.main_frame

            print(f"[applier] frame: {frame.url[:80]}")
            success = await ai_fill_form(page, frame, profile, cover_letter)

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
    """Synchronous wrapper — called from FastAPI background task."""
    return asyncio.run(_do_apply(job))
