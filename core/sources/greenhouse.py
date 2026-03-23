from __future__ import annotations
import re
from typing import List, Dict
import httpx
from .base import JobSource


class GreenhouseSource(JobSource):
    """Fetches public job postings from Greenhouse boards API."""

    BASE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    def __init__(self, company_slug: str):
        self.slug = company_slug

    def fetch(self) -> List[Dict]:
        url = self.BASE.format(slug=self.slug)
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[greenhouse:{self.slug}] HTTP error: {exc}")
            return []

        jobs: List[Dict] = []
        for j in resp.json().get("jobs", []):
            raw_desc = j.get("content", "") or ""
            description = re.sub(r"<[^>]+>", " ", raw_desc).strip()
            jobs.append({
                "external_id": f"greenhouse_{j['id']}",
                "source":      "greenhouse",
                "company":     self.slug,
                "title":       j.get("title", ""),
                "location":    (j.get("location") or {}).get("name", ""),
                "url":         j.get("absolute_url", ""),
                "description": description,
            })
        return jobs
