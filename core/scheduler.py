from __future__ import annotations
from apscheduler.schedulers.background import BackgroundScheduler
from core.config import load_config, resolve_config_path
from core.fetcher import fetch_and_store

_scheduler: BackgroundScheduler | None = None


def start_scheduler(interval_hours: int = 1, config_path: str | None = None) -> BackgroundScheduler:
    global _scheduler
    cfg = load_config(config_path)
    timezone = cfg.get("app", {}).get("timezone", "UTC")
    job_kwargs = {}
    if config_path is not None:
        job_kwargs["config_path"] = str(resolve_config_path(config_path))

    _scheduler = BackgroundScheduler(timezone=timezone)
    _scheduler.add_job(
        fetch_and_store,
        trigger="interval",
        hours=interval_hours,
        kwargs=job_kwargs,
        id="job_fetch",
        replace_existing=True,
    )
    _scheduler.start()
    print(f"[scheduler] started — fetching every {interval_hours}h ({timezone})")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        print("[scheduler] stopped")
