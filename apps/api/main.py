from __future__ import annotations
import datetime
import pathlib
from contextlib import asynccontextmanager
from typing import Generator

from fastapi import FastAPI, Request, Depends, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from core.models import Job, get_engine, init_db, make_session_factory
from core.scheduler import start_scheduler, stop_scheduler
from core.fetcher import fetch_and_store

# ── paths ─────────────────────────────────────────────────────────────────────
BASE_DIR      = pathlib.Path(__file__).resolve().parents[1]   # apps/
STATIC_DIR    = BASE_DIR / "ui" / "static"
TEMPLATES_DIR = BASE_DIR / "ui" / "templates"

# ── db setup ──────────────────────────────────────────────────────────────────
_engine         = get_engine()
init_db(_engine)
_SessionFactory = make_session_factory(_engine)


def get_db() -> Generator[Session, None, None]:
    db = _SessionFactory()
    try:
        yield db
    finally:
        db.close()


# ── lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    start_scheduler(interval_hours=1)
    yield
    stop_scheduler()


# ── app ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Job Search Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ── pages ─────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    stats = {
        "total":    db.query(Job).count(),
        "new":      db.query(Job).filter_by(status="new").count(),
        "approved": db.query(Job).filter_by(status="approved").count(),
        "applied":  db.query(Job).filter_by(status="applied").count(),
    }
    return templates.TemplateResponse(request, "index.html", {"stats": stats})


@app.get("/queue", response_class=HTMLResponse)
def queue_page(
    request: Request,
    status: str = "new",
    db: Session = Depends(get_db),
):
    jobs = (
        db.query(Job)
        .filter_by(status=status)
        .order_by(Job.score.desc())
        .all()
    )
    return templates.TemplateResponse(
        request, "queue.html", {"jobs": jobs, "current_status": status}
    )


# ── health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}


# ── REST API ──────────────────────────────────────────────────────────────────
@app.get("/api/jobs")
def list_jobs(
    status: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(Job)
    if status:
        q = q.filter_by(status=status)
    jobs = q.order_by(Job.score.desc()).limit(limit).all()
    return [j.to_dict() for j in jobs]


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
def apply_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status not in ("new", "approved"):
        raise HTTPException(400, f"Cannot apply to a job with status '{job.status}'")
    job.status     = "applied"
    job.applied_at = datetime.datetime.utcnow()
    db.commit()
    return {"id": job_id, "status": "applied", "applied_at": job.applied_at.isoformat()}


@app.post("/api/fetch")
def trigger_fetch(background_tasks: BackgroundTasks):
    """Manually trigger a job-fetch cycle."""
    background_tasks.add_task(fetch_and_store)
    return {"message": "Fetch started in background"}
