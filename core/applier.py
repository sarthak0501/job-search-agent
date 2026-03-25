from __future__ import annotations
"""
applier.py – Orchestrates job application using sync Playwright.

Sync API avoids asyncio conflicts when called from FastAPI background threads.

Pipeline:
  1. check_readiness()          -> ReadinessReport (abort if not ready)
  2. build browser context      (with optional storage_state)
  3. open URL + navigation
  4. discover_application_surface() -> (frame, surface_info)
  5. detect_blocking_page()     -> optional FailureType (login/captcha)
  6. ai_fill_form()             -> ApplyResult
  7. write debug artifacts
  8. return ApplyResult
"""
import os
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright, Page, Frame, TimeoutError as PWTimeout

from core.profile_store import load_profile
from core.ai_filler import ai_fill_form, CLAUDE_BIN
from core.debug_artifacts import DebugArtifacts
from core.outcome import (
    ApplyResult, FailureType, ReadinessReport,
    make_failure, RETRYABLE_FAILURES,
)

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


# ── Readiness check ───────────────────────────────────────────────────────────

def check_readiness(profile: dict | None = None) -> ReadinessReport:
    """
    Structured readiness check. Returns a ReadinessReport with per-item status.
    """
    if profile is None:
        profile = load_profile()

    resume_path = (profile or {}).get("resume_path", "")
    checks = {
        "profile": bool(profile),
        "resume": bool(resume_path and os.path.exists(resume_path)),
        "claude": os.path.exists(CLAUDE_BIN),
    }

    first_failure: FailureType | None = None
    error = ""

    if not checks["profile"]:
        first_failure = FailureType.MISSING_PROFILE
        error = "Profile is missing. Complete setup at /setup first."
    elif not checks["resume"]:
        first_failure = FailureType.MISSING_RESUME
        error = f"Resume file not found: {resume_path or 'not configured'}"
    elif not checks["claude"]:
        first_failure = FailureType.MISSING_CLAUDE
        error = f"Claude CLI not found at {CLAUDE_BIN}"

    return ReadinessReport(
        ready=first_failure is None,
        checks=checks,
        first_failure=first_failure,
        error=error,
    )


def get_apply_readiness(check_browser: bool = False) -> dict:
    """
    Legacy compatibility wrapper. Returns a dict with 'ready', 'checks', 'error'.
    Also runs browser check if requested.
    """
    profile = load_profile()
    report = check_readiness(profile)
    result = report.to_dict()

    if check_browser and report.ready:
        browser_ok = True
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=_should_launch_headless())
                browser.close()
        except Exception:
            browser_ok = False
        result["checks"]["browser"] = browser_ok
        if not browser_ok:
            result["ready"] = False
            result["error"] = "Playwright could not launch Chromium in this environment."
            result["first_failure"] = FailureType.BROWSER_LAUNCH_FAILED.value
    else:
        result["checks"]["browser"] = None

    return result


# ── Application surface discovery ─────────────────────────────────────────────

# CTA button text patterns to try (in order)
_APPLY_CTA_TEXTS = [
    "Apply Now",
    "Apply for this job",
    "Easy Apply",
    "Start application",
    "Start your application",
    "Submit application",
    "Continue application",
    "I'm interested",
    "Apply",
    "Quick Apply",
]

_ATS_FRAME_KEYWORDS = [
    "greenhouse.io", "lever.co", "workday.com", "bamboohr.com",
    "job_app", "apply", "jobs", "careers", "application",
]


def _score_frame(frame: Frame) -> int:
    """Score a frame for likelihood of being the application form."""
    score = 0
    url = (frame.url or "").lower()
    for kw in _ATS_FRAME_KEYWORDS:
        if kw in url:
            score += 10 if kw in ("greenhouse.io", "lever.co", "workday.com", "bamboohr.com") else 3

    try:
        n_inputs = frame.evaluate(r"""() => {
            return document.querySelectorAll(
                'input:not([type=hidden]):not([type=submit]):not([type=button]),' +
                'textarea, select'
            ).length;
        }""")
        if n_inputs > 3:
            score += 5
        elif n_inputs == 0:
            score -= 5
    except Exception:
        pass

    return score


def _click_apply_cta(page: Page) -> bool:
    """Click an apply CTA button if present. Returns True if clicked."""
    for text in _APPLY_CTA_TEXTS:
        for tag in ("button", "a"):
            sel = f"{tag}:has-text('{text}')"
            try:
                btn = page.wait_for_selector(sel, timeout=2500)
                if btn and btn.is_visible():
                    btn.click()
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    time.sleep(1)
                    return True
            except PWTimeout:
                continue
            except Exception:
                continue
    return False


def discover_application_surface(page: Page) -> tuple[Frame, dict]:
    """
    Find the frame most likely to contain the application form.

    Steps:
    1. Try clicking apply CTA buttons
    2. Handle popup / new tab
    3. Score all frames and return highest-scoring one
    4. Return (frame, surface_info dict)
    """
    surface_info: dict = {"cta_clicked": False, "new_tab_opened": False}

    # Step 1: Click apply CTA
    pages_before = len(page.context.pages)
    cta_clicked = _click_apply_cta(page)
    surface_info["cta_clicked"] = cta_clicked

    # Step 2: Handle new tab
    if cta_clicked:
        time.sleep(1.5)
        pages_after = page.context.pages
        if len(pages_after) > pages_before:
            new_page = pages_after[-1]
            try:
                new_page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            page = new_page
            surface_info["new_tab_opened"] = True
            surface_info["final_page"] = new_page
        else:
            surface_info["final_page"] = page
    else:
        surface_info["final_page"] = page

    # Step 3: Score frames
    actual_page = surface_info.get("final_page", page)
    frames = actual_page.frames
    if not frames:
        return actual_page.main_frame, surface_info

    best_frame = actual_page.main_frame
    best_score = _score_frame(actual_page.main_frame)

    for f in frames:
        if f == actual_page.main_frame:
            continue
        score = _score_frame(f)
        if score > best_score:
            best_score = score
            best_frame = f

    surface_info["frame_url"] = best_frame.url
    surface_info["frame_score"] = best_score
    print(f"[applier] selected frame: {best_frame.url[:80]} (score={best_score})")

    return best_frame, surface_info


# ── Login/captcha detection ───────────────────────────────────────────────────

def detect_blocking_page(page: Page, frame: Frame) -> FailureType | None:
    """
    Check if the current page/frame is a login wall or captcha.

    Returns the appropriate FailureType or None if no blocking detected.
    """
    try:
        from core.page_classifier import classify_page
        from core.outcome import PageType
        page_type = classify_page(page, frame)
        if page_type == PageType.LOGIN_WALL:
            return FailureType.LOGIN_REQUIRED
        if page_type == PageType.CAPTCHA:
            return FailureType.CAPTCHA_OR_HUMAN_VERIFICATION
        if page_type == PageType.SITE_ERROR:
            return FailureType.SITE_ERROR
    except Exception as exc:
        print(f"[applier] detect_blocking_page error: {exc}")
    return None


# ── Browser context builder ────────────────────────────────────────────────────

def _build_browser_context(playwright, profile: dict):
    """Launch browser and create context, applying profile settings."""
    headless = _should_launch_headless()
    # Profile can override headless
    if "headless" in profile:
        headless = bool(profile["headless"])

    slow_mo = int(profile.get("slow_mo", 0))
    user_agent = profile.get(
        "browser_user_agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36",
    )
    viewport = {
        "width": int(profile.get("viewport_width", 1280)),
        "height": int(profile.get("viewport_height", 900)),
    }

    browser = playwright.chromium.launch(headless=headless, slow_mo=slow_mo)

    # Session persistence via storage_state
    storage_state_path = profile.get("storage_state_path", "")
    ctx_kwargs: dict = {
        "user_agent": user_agent,
        "viewport": viewport,
    }
    if storage_state_path and os.path.exists(storage_state_path):
        ctx_kwargs["storage_state"] = storage_state_path
        print(f"[applier] loading storage state from {storage_state_path}")

    ctx = browser.new_context(**ctx_kwargs)
    return browser, ctx


# ── Main entry ────────────────────────────────────────────────────────────────

def apply_to_job(job) -> ApplyResult:
    """
    Synchronous — safe to call from any thread.

    Returns ApplyResult with:
      success: bool
      failure_type: FailureType | None
      failure_reason: str
      retryable: bool
      debug_dir: str
      attempts: int
      unresolved_fields: list
    """
    # 1. Readiness check
    profile = load_profile() or {}
    readiness = check_readiness(profile)
    if not readiness.ready:
        return make_failure(
            readiness.first_failure or FailureType.MISSING_PROFILE,
            reason=readiness.error,
        )

    cover_letter = generate_cover_letter(
        job.title, job.company, job.description or ""
    )

    # Initialise debug artifact writer
    job_id = getattr(job, "id", "unknown")
    debug = DebugArtifacts(job_id=job_id, attempt=1)

    try:
        with sync_playwright() as p:
            browser, ctx = _build_browser_context(p, profile)
            page = ctx.new_page()
            try:
                print(f"[applier] → {job.url}")

                # Take screenshot after page load
                debug.take_screenshot(page, "page_load")

                try:
                    page.goto(job.url, wait_until="networkidle", timeout=25000)
                except PWTimeout:
                    page.goto(job.url, wait_until="domcontentloaded", timeout=20000)

                # 4. Discover application surface
                frame, surface_info = discover_application_surface(page)
                # If a new tab was opened, use the new page's main frame
                actual_page = surface_info.get("final_page", page)
                if actual_page is not page:
                    page = actual_page

                print(f"[applier] frame: {frame.url[:80]}")

                # Take post-navigation screenshot
                debug.take_screenshot(page, "post_navigation")

                # 5. Detect login/captcha
                blocking = detect_blocking_page(page, frame)
                if blocking is not None:
                    debug.take_screenshot(page, "blocking_page")
                    debug_dir = debug.write(failure_reason=blocking.value)
                    return make_failure(
                        blocking,
                        reason=f"Blocking page detected: {blocking.value}",
                        debug_dir=debug_dir,
                        attempts=1,
                    )

                # 6. Fill the form
                result = ai_fill_form(page, frame, profile, cover_letter, debug=debug)

                # Take final screenshot
                debug.take_screenshot(page, "final_state")

                # 7. Write debug artifacts
                if isinstance(result, ApplyResult):
                    failure_reason = "" if result.success else result.failure_reason
                    debug_dir = debug.write(failure_reason=failure_reason)
                    result.debug_dir = debug_dir
                    result.attempts = 1
                    print(f"[applier] debug artifacts → {debug_dir}")
                    return result
                else:
                    # Legacy bool return from old ai_fill_form
                    success = bool(result)
                    failure_reason = "" if success else "ai_fill_form returned False"
                    debug_dir = debug.write(failure_reason=failure_reason)
                    print(f"[applier] debug artifacts → {debug_dir}")

                    if success:
                        return ApplyResult(
                            success=True,
                            debug_dir=debug_dir,
                            attempts=1,
                        )

                    # Check if stuck
                    state_log = debug._states
                    stuck = any(
                        s.get("classification") == "stuck"
                        for s in state_log
                    )
                    stuck_type_str = ""
                    if stuck:
                        for s in state_log:
                            if s.get("classification") == "stuck":
                                stuck_type_str = s.get("stuck_type", "stuck_same_page_no_progress")
                                break

                    ft_map = {
                        "stuck_same_page_no_progress": FailureType.STUCK_SAME_PAGE_NO_PROGRESS,
                        "cycling_between_steps": FailureType.CYCLING_BETWEEN_STEPS,
                        "repeated_validation_errors": FailureType.REPEATED_VALIDATION_ERRORS,
                    }
                    ft = ft_map.get(stuck_type_str, FailureType.STUCK_SAME_PAGE_NO_PROGRESS) if stuck else FailureType.SUBMISSION_NOT_CONFIRMED

                    return make_failure(
                        ft,
                        reason=failure_reason,
                        debug_dir=debug_dir,
                        attempts=1,
                    )

            except Exception as exc:
                print(f"[applier] error: {exc}")
                try:
                    debug.take_screenshot(page, "error_state")
                    debug_dir = debug.write(failure_reason=str(exc))
                except Exception:
                    debug_dir = ""
                return make_failure(
                    FailureType.SITE_ERROR,
                    reason=str(exc),
                    debug_dir=debug_dir,
                    attempts=1,
                )
            finally:
                browser.close()

    except Exception as outer_exc:
        print(f"[applier] browser launch error: {outer_exc}")
        try:
            debug_dir = debug.write(failure_reason=str(outer_exc))
        except Exception:
            debug_dir = ""
        return make_failure(
            FailureType.BROWSER_LAUNCH_FAILED,
            reason=str(outer_exc),
            debug_dir=debug_dir,
        )
