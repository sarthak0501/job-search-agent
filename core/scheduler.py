from __future__ import annotations
from apscheduler.schedulers.background import BackgroundScheduler
from core.fetcher import fetch_and_store

_scheduler: BackgroundScheduler | None = None


def start_scheduler(interval_hours: int = 1) -> BackgroundScheduler:
    global _scheduler
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(
        fetch_and_store,
        trigger="interval",
        hours=interval_hours,
        id="job_fetch",
        replace_existing=True,
    )
    _scheduler.start()
    print(f"[scheduler] started — fetching every {interval_hours}h")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        print("[scheduler] stopped")
