from __future__ import annotations
import re
from typing import Dict, List, Set


def _tokens(text: str) -> Set[str]:
    return set(re.findall(r"\b[a-z][a-z0-9+#.]*\b", text.lower()))


def score_job(job: Dict, profile: Dict) -> float:
    """
    Hybrid keyword scorer — returns 0‥100.

    Breakdown:
      40 pts  title keywords match (target_titles)
      40 pts  skills / keywords found anywhere in posting
      20 pts  location preference match
    """
    target_titles: List[str] = [t.lower() for t in profile.get("target_titles", [])]
    skills:        Set[str]  = {s.lower() for s in profile.get("skills", [])}
    locations:     List[str] = [l.lower() for l in profile.get("locations", [])]

    job_title_tok   = _tokens(job.get("title", ""))
    job_all_tok     = _tokens(f"{job.get('title', '')} {job.get('description', '')}")
    job_location    = job.get("location", "").lower()

    # ── title score (40 pts) ──────────────────────────────────────────────────
    title_score = 0.0
    if target_titles:
        best = 0.0
        for target in target_titles:
            target_tok = _tokens(target)
            if target_tok:
                overlap = len(target_tok & job_title_tok) / len(target_tok)
                best = max(best, overlap)
        title_score = best * 40

    # ── skills score (40 pts) ─────────────────────────────────────────────────
    skill_score = 0.0
    if skills:
        matched = sum(1 for s in skills if s in job_all_tok)
        skill_score = (matched / len(skills)) * 40

    # ── location score (20 pts) ───────────────────────────────────────────────
    loc_score = 0.0
    if not locations:
        loc_score = 20.0          # no preference → full credit
    else:
        for pref in locations:
            if pref in job_location or job_location in pref or "remote" in job_location:
                loc_score = 20.0
                break

    return round(min(title_score + skill_score + loc_score, 100.0), 1)
