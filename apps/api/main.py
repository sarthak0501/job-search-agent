from __future__ import annotations
import datetime
import json
import pathlib
from contextlib import asynccontextmanager
from typing import Generator

from fastapi import FastAPI, Request, Depends, HTTPException, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from core.models import Job, get_engine, init_db, make_session_factory
from core.scheduler import start_scheduler, stop_scheduler
from core.fetcher import fetch_and_store
from core.applier import apply_to_job, get_apply_readiness
from core.profile_store import profile_exists, load_profile, save_profile
from core.outcome import ApplyResult, FailureType

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR      = pathlib.Path(__file__).resolve().parents[1]
STATIC_DIR    = BASE_DIR / "ui" / "static"
TEMPLATES_DIR = BASE_DIR / "ui" / "templates"

# ── db ─────────────────────────────────────────────────────────────────────────
_engine         = get_engine()
init_db(_engine)
_SessionFactory = make_session_factory(_engine)


def get_db() -> Generator[Session, None, None]:
    db = _SessionFactory()
    try:
        yield db
    finally:
        db.close()


# ── lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.has_profile = profile_exists()
    start_scheduler(interval_hours=1)
    yield
    stop_scheduler()


# ── app ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Job Search Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Expose has_profile as a Jinja2 global so base.html can read it without
# needing it passed explicitly in every TemplateResponse context.
templates.env.globals["has_profile"] = lambda: app.state.has_profile


# ── setup guard ────────────────────────────────────────────────────────────────
def _require_profile() -> RedirectResponse | None:
    if not app.state.has_profile:
        return RedirectResponse("/setup", status_code=302)
    return None


# ── setup routes ───────────────────────────────────────────────────────────────
@app.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request):
    existing = load_profile() or {}
    return templates.TemplateResponse(request, "setup.html",
                                      {"profile": existing, "error": None})


@app.post("/setup")
def setup_post(
    request:              Request,
    first_name:           str = Form(...),
    last_name:            str = Form(...),
    email:                str = Form(...),
    phone:                str = Form(...),
    phone_country_code:   str = Form(default="+1"),
    address:              str = Form(default=""),
    city:                 str = Form(default=""),
    state:                str = Form(default=""),
    zip:                  str = Form(default=""),
    country:              str = Form(default="United States"),
    location:             str = Form(default=""),
    current_company:      str = Form(default=""),
    current_title:        str = Form(default=""),
    years_experience:     str = Form(default=""),
    linkedin:             str = Form(default=""),
    salary_min:           str = Form(default=""),
    salary_max:           str = Form(default=""),
    salary_range:         str = Form(default=""),
    work_authorized:      str = Form(default="Yes"),
    requires_sponsorship: str = Form(default="No"),
    resume_path:          str = Form(...),
    gender:               str = Form(default=""),
    gender_eeoc:          str = Form(default=""),
    transgender:          str = Form(default="No"),
    orientation:          str = Form(default=""),
    disability:           str = Form(default="No"),
    veteran:              str = Form(default="I am not a protected veteran"),
    referral_source:      str = Form(default="Job board"),
    ethnicity:            str = Form(default=""),
):
    profile_data = {
        "first_name": first_name, "last_name": last_name,
        "email": email, "phone": phone,
        "phone_country_code": phone_country_code,
        "address": address, "city": city, "state": state,
        "zip": zip, "country": country, "location": location,
        "current_company": current_company, "current_title": current_title,
        "years_experience": years_experience, "linkedin": linkedin,
        "salary_min": salary_min, "salary_max": salary_max,
        "salary_range": salary_range,
        "work_authorized": work_authorized,
        "requires_sponsorship": requires_sponsorship,
        "resume_path": resume_path,
        "gender": gender, "gender_eeoc": gender_eeoc,
        "transgender": transgender, "orientation": orientation,
        "disability": disability, "veteran": veteran,
        "ethnicity": ethnicity, "referral_source": referral_source,
    }

    if not pathlib.Path(resume_path).exists():
        return templates.TemplateResponse(
            request, "setup.html",
            {"profile": profile_data,
             "error": f"Resume file not found at: {resume_path}"},
            status_code=422,
        )

    save_profile(profile_data)
    app.state.has_profile = True
    return RedirectResponse("/queue", status_code=303)


# ── pages ──────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    if redir := _require_profile():
        return redir
    stats = {
        "total":    db.query(Job).count(),
        "new":      db.query(Job).filter_by(status="new").count(),
        "approved": db.query(Job).filter_by(status="approved").count(),
        "applied":  db.query(Job).filter_by(status="applied").count(),
    }
    return templates.TemplateResponse(request, "index.html", {"stats": stats})


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, status: str = "new",
               db: Session = Depends(get_db)):
    if redir := _require_profile():
        return redir
    jobs = (
        db.query(Job)
        .filter_by(status=status)
        .order_by(Job.score.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "queue.html", {"jobs": jobs, "current_status": status},
    )


# ── health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "has_profile": app.state.has_profile}


# ── REST API ───────────────────────────────────────────────────────────────────
@app.get("/api/jobs")
def list_jobs(status: str | None = None, limit: int = 50,
              db: Session = Depends(get_db)):
    q = db.query(Job)
    if status:
        q = q.filter_by(status=status)
    return [j.to_dict() for j in q.order_by(Job.score.desc()).limit(limit).all()]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    d = job.to_dict()
    d["description"] = job.description
    return d


@app.post("/api/jobs/{job_id}/approve")
def approve_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.status = "approved"
    db.commit()
    return {"id": job_id, "status": "approved"}


@app.post("/api/jobs/{job_id}/reject")
def reject_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job.status = "rejected"
    db.commit()
    return {"id": job_id, "status": "rejected"}


@app.post("/api/jobs/{job_id}/apply")
def apply_job(job_id: int, background_tasks: BackgroundTasks,
              db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in ("new", "approved"):
        raise HTTPException(400, f"Cannot apply to a job with status '{job.status}'")

    readiness = get_apply_readiness(check_browser=False)
    if not readiness["ready"]:
        job.last_error = readiness["error"]
        db.commit()
        raise HTTPException(400, readiness["error"])

    job.status = "applying"
    job.last_error = ""
    db.commit()

    def _run(job_id: int):
        bg = _SessionFactory()
        try:
            bg_job = bg.get(Job, job_id)
            result = apply_to_job(bg_job)

            # ApplyResult -> job status mapping
            if isinstance(result, ApplyResult):
                if result.success:
                    bg_job.status = "applied"
                    bg_job.applied_at = datetime.datetime.utcnow()
                    bg_job.last_error = ""
                    print(f"[apply] job {job_id} applied successfully")
                elif result.failure_type == FailureType.UNRESOLVED_REQUIRED_FIELDS:
                    # Could not fill required fields — needs human review
                    bg_job.status = "needs_review"
                    bg_job.last_error = json.dumps({
                        "failure_type": result.failure_type.value,
                        "reason": result.failure_reason,
                        "unresolved_fields": result.unresolved_fields,
                        "debug_dir": result.debug_dir,
                    })
                    print(f"[apply] job {job_id} needs_review: {result.failure_reason}")
                elif result.failure_type in (
                    FailureType.LOGIN_REQUIRED,
                    FailureType.CAPTCHA_OR_HUMAN_VERIFICATION,
                    FailureType.HUMAN_VERIFICATION_BLOCKED,
                    FailureType.AUTH_SESSION_EXPIRED,
                ):
                    # Requires manual human action
                    bg_job.status = "blocked"
                    bg_job.last_error = json.dumps({
                        "failure_type": result.failure_type.value,
                        "reason": result.failure_reason,
                        "debug_dir": result.debug_dir,
                    })
                    print(f"[apply] job {job_id} blocked: {result.failure_reason}")
                elif result.retryable:
                    # Transient failure — keep as approved for retry
                    bg_job.status = "approved"
                    bg_job.last_error = json.dumps({
                        "failure_type": result.failure_type.value if result.failure_type else None,
                        "reason": result.failure_reason,
                        "debug_dir": result.debug_dir,
                    })
                    print(f"[apply] job {job_id} retryable failure: {result.failure_reason}")
                else:
                    # Permanent failure
                    bg_job.status = "failed"
                    bg_job.last_error = json.dumps({
                        "failure_type": result.failure_type.value if result.failure_type else None,
                        "reason": result.failure_reason,
                        "debug_dir": result.debug_dir,
                    })
                    print(f"[apply] job {job_id} failed: {result.failure_reason}")
            else:
                # Legacy dict result fallback
                if result.get("success"):
                    bg_job.status = "applied"
                    bg_job.applied_at = datetime.datetime.utcnow()
                    bg_job.last_error = ""
                elif result.get("failed_permanently"):
                    bg_job.status = "failed"
                    bg_job.last_error = result.get("error", "Apply loop detected")
                else:
                    bg_job.status = "approved"
                    bg_job.last_error = result.get("error", "Unknown apply error")
            bg.commit()
        except Exception as exc:
            print(f"[apply] job {job_id} exception: {exc}")
            try:
                bg_job = bg.get(Job, job_id)
                bg_job.status = "approved"
                bg_job.last_error = str(exc)
                bg.commit()
            except Exception:
                pass
        finally:
            bg.close()

    background_tasks.add_task(_run, job_id)
    return {"id": job_id, "status": "applying",
            "message": "Browser opened — AI is filling the form"}


@app.get("/api/jobs/{job_id}/apply-status")
def get_apply_status(job_id: int, db: Session = Depends(get_db)):
    """Return detailed apply status for a job including structured failure info."""
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    d = job.to_dict()
    # Parse structured last_error if it's JSON
    if job.last_error:
        try:
            d["last_error_detail"] = json.loads(job.last_error)
        except (json.JSONDecodeError, ValueError):
            d["last_error_detail"] = {"reason": job.last_error}
    else:
        d["last_error_detail"] = None
    return d


@app.get("/api/apply-readiness")
def apply_readiness():
    """Return structured readiness breakdown."""
    return get_apply_readiness(check_browser=False)


@app.post("/api/fetch")
def trigger_fetch(background_tasks: BackgroundTasks):
    background_tasks.add_task(fetch_and_store)
    return {"message": "Fetch started in background"}
