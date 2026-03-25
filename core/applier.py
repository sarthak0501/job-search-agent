from __future__ import annotations
"""
applier.py – orchestrates job application using sync Playwright.

Sync API avoids asyncio conflicts when called from FastAPI background threads.
Integrates:
  - StateTracker (loop/stuck detection)
  - DebugArtifacts (structured debug output per attempt)
"""
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright, Page, Frame, TimeoutError as PWTimeout

from core.profile_store import load_profile
from core.ai_filler import ai_fill_form, CLAUDE_BIN
from core.debug_artifacts import DebugArtifacts

RESUME_SUMMARY = """
Senior Data Scientist (8+ years) at Microsoft — adaptive stress-testing for Azure
Storage (10+ Sev1/Sev2 incidents prevented), multi-touch attribution (+10% MoM
revenue), LLM-powered customer health agent (50% reduction in aging CRIs).
Walmart Labs: planogram optimisation (+7% revenue). Skills: Python, SQL, PySpark,
Azure, ML, NLP, LLM agents, experiment design.
"""


# ── Cover letter ──────────────────────────────────────────────────────────────
def generate_cover_letter(job_title: str, company: str, job_description: str) -> str:
    if os.path.exists(CLAUDE_BIN):
        try:
            prompt = (
                f"Write a concise cover letter (3 paragraphs, ~200 words) for:\n"
                f"Job: {job_title} at {company}\n"
                f"Description: {job_description[:1200]}\n"
                f"Applicant: {RESUME_SUMMARY}\n"
                f"Rules: specific achievements, no clichés, body only (no header/signature)"
            )
            result = subprocess.run(
                [CLAUDE_BIN, "-p", prompt],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except Exception as exc:
            print(f"[applier] cover letter generation failed ({exc}), using fallback")

    return (
        f"I'm excited to apply for the {job_title} role at {company}. "
        f"With 8+ years building production ML and data systems at Microsoft — "
        f"adaptive stress-testing for Azure Storage (10+ Sev1/Sev2 incidents prevented), "
        f"multi-touch attribution pipelines (+10% MoM revenue), and an LLM-powered "
        f"customer health agent (50% reduction in aging CRIs) — I bring a consistent "
        f"record of turning complex data problems into measurable business outcomes.\n\n"
        f"I'd love to bring this experience to {company}."
    )


# ── Platform helpers ──────────────────────────────────────────────────────────
def _is_greenhouse(url: str) -> bool:
    return any(h in url for h in ["greenhouse.io", "careerpuck.com", "gh_jid="])

def _is_lever(url: str) -> bool:
    return "lever.co" in url


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _should_launch_headless() -> bool:
    forced = _env_flag("JOB_SEARCH_HEADLESS")
    if forced is not None:
        return forced

    if sys.platform == "darwin":
        return False

    return not any(os.environ.get(name) for name in ("DISPLAY", "WAYLAND_DISPLAY"))


def get_apply_readiness(check_browser: bool = False) -> dict:
    profile = load_profile()
    resume_path = (profile or {}).get("resume_path", "")
    checks = {
        "profile": bool(profile),
        "resume": bool(resume_path and os.path.exists(resume_path)),
        "claude": os.path.exists(CLAUDE_BIN),
        "browser": None,
    }

    if check_browser:
        checks["browser"] = True
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=_should_launch_headless())
                browser.close()
        except Exception:
            checks["browser"] = False

    error = None
    if not checks["profile"]:
        error = "Profile is missing. Complete setup at /setup first."
    elif not checks["resume"]:
        error = f"Resume file not found: {resume_path or 'not configured'}"
    elif not checks["claude"]:
        error = f"Claude CLI not found at {CLAUDE_BIN}"
    elif checks["browser"] is False:
        error = "Playwright could not launch Chromium in this environment."

    return {"ready": error is None, "checks": checks, "error": error}


def _click_apply_button(page: Page) -> None:
    """Click Apply / Apply Now if present (company career pages)."""
    for sel in [
        "a:has-text('Apply Now')", "button:has-text('Apply Now')",
        "a:has-text('Apply for this job')", "button:has-text('Apply for this job')",
        "a:has-text('Easy Apply')", "button:has-text('Easy Apply')",
    ]:
        try:
            btn = page.wait_for_selector(sel, timeout=2500)
            btn.click()
            page.wait_for_load_state("networkidle", timeout=8000)
            time.sleep(1)
            return
        except PWTimeout:
            continue


def _get_greenhouse_frame(page: Page, timeout: float = 8.0) -> Frame:
    """Return the Greenhouse embed iframe, or the main frame if not found."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for f in page.frames:
            if "greenhouse.io/embed" in f.url or "greenhouse.io/job_app" in f.url:
                return f
        time.sleep(0.4)
    return page.main_frame


# ── Main entry ────────────────────────────────────────────────────────────────
def apply_to_job(job) -> dict:
    """
    Synchronous — safe to call from any thread.

    Returns dict with:
      success: bool
      cover_letter: str
      error: str (if failure)
      debug_dir: str (path to debug artifacts)
    """
    readiness = get_apply_readiness(check_browser=False)
    if not readiness["ready"]:
        return {"success": False, "error": readiness["error"]}

    profile = load_profile() or {}

    cover_letter = generate_cover_letter(
        job.title, job.company, job.description or ""
    )

    # Initialise debug artifact writer
    job_id = getattr(job, "id", "unknown")
    debug = DebugArtifacts(job_id=job_id, attempt=1)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=_should_launch_headless())
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        try:
            print(f"[applier] → {job.url}")
            page.goto(job.url, wait_until="networkidle", timeout=25000)

            _click_apply_button(page)

            if _is_greenhouse(job.url):
                frame = _get_greenhouse_frame(page)
            else:
                frame = page.main_frame

            print(f"[applier] frame: {frame.url[:80]}")

            # Take initial screenshot
            debug.take_screenshot(page, "initial_page")

            success = ai_fill_form(page, frame, profile, cover_letter, debug=debug)

            # Take final screenshot
            debug.take_screenshot(page, "final_state")

            # Write all debug artifacts
            failure_reason = "" if success else "ai_fill_form returned False"
            debug_dir = debug.write(failure_reason=failure_reason)
            print(f"[applier] debug artifacts → {debug_dir}")

            if not success:
                # If stuck/loop was detected, mark as failed (not re-queueable)
                state_log = debug._states
                stuck = any(
                    s.get("classification") == "stuck"
                    for s in state_log
                )
                if stuck:
                    return {
                        "success": False,
                        "error": f"Apply loop detected (stuck). Debug: {debug_dir}",
                        "cover_letter": cover_letter,
                        "debug_dir": debug_dir,
                        "failed_permanently": True,
                    }

            time.sleep(3)
            return {
                "success": success,
                "cover_letter": cover_letter,
                "debug_dir": debug_dir,
            }

        except Exception as exc:
            print(f"[applier] error: {exc}")
            try:
                debug.take_screenshot(page, "error_state")
                debug_dir = debug.write(failure_reason=str(exc))
            except Exception:
                debug_dir = ""
            return {
                "success": False,
                "error": str(exc),
                "cover_letter": cover_letter,
                "debug_dir": debug_dir,
            }
        finally:
            browser.close()
