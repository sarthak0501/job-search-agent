from __future__ import annotations
"""
fetch_and_store()  – pull jobs from all enabled sources, score, and persist.
Called by the scheduler and also exposed as a manual trigger via the API.
"""
import datetime
from core.config import load_config
from core.compliance import ComplianceGate
from core.models import Job, get_engine, init_db, make_session_factory
from core.scoring import score_job
from core.sources import GreenhouseSource, LeverSource

_SOURCE_MAP = {
    "greenhouse": GreenhouseSource,
    "lever":      LeverSource,
}


def fetch_and_store(config_path: str = "config.yaml") -> dict:
    cfg    = load_config(config_path)
    gate   = ComplianceGate(cfg.compliance)
    engine = get_engine()
    init_db(engine)
    Session = make_session_factory(engine)
    session = Session()

    profile   = cfg.get("profile", {})
    threshold = cfg.get("scoring", {}).get("threshold", 70)

    added = skipped = blocked = 0

    try:
        for src_cfg in cfg.get("sources", []):
            if not src_cfg.get("enabled"):
                continue
            plugin    = src_cfg.get("plugin", "")
            companies = src_cfg.get("companies", [])
            klass     = _SOURCE_MAP.get(plugin)
            if not klass:
                print(f"[fetcher] unknown plugin: {plugin}")
                continue

            for company in companies:
                print(f"[fetcher] fetching {plugin}/{company} …")
                try:
                    jobs_raw = klass(company).fetch()
                except Exception as exc:
                    print(f"[fetcher] {plugin}/{company} error: {exc}")
                    continue

                for jd in jobs_raw:
                    # Compliance check
                    ok, reason = gate.check_url(jd.get("url", ""))
                    if not ok:
                        blocked += 1
                        continue

                    # Dedup
                    if session.query(Job).filter_by(external_id=jd["external_id"]).first():
                        skipped += 1
                        continue

                    s = score_job(jd, profile)
                    if s < threshold:
                        skipped += 1
                        continue

                    session.add(Job(
                        external_id = jd["external_id"],
                        source      = jd["source"],
                        company     = jd["company"],
                        title       = jd["title"],
                        location    = jd.get("location", ""),
                        url         = jd.get("url", ""),
                        description = jd.get("description", ""),
                        score       = s,
                        fetched_at  = datetime.datetime.utcnow(),
                    ))
                    added += 1

        session.commit()
    finally:
        session.close()

    result = {"added": added, "skipped": skipped, "blocked": blocked}
    print(f"[fetcher] done: {result}")
    return result
