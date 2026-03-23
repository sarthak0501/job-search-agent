from __future__ import annotations
from typing import List, Dict
import httpx
from .base import JobSource


class LeverSource(JobSource):
    """Fetches public job postings from Lever postings API."""

    BASE = "https://api.lever.co/v0/postings/{slug}?mode=json"

    def __init__(self, company_slug: str):
        self.slug = company_slug

    def fetch(self) -> List[Dict]:
        url = self.BASE.format(slug=self.slug)
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.get(url)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[lever:{self.slug}] HTTP error: {exc}")
            return []

        jobs: List[Dict] = []
        for j in resp.json():
            description = j.get("descriptionPlain", "") or ""
            for lst in j.get("lists", []):
                description += "\n" + lst.get("text", "") + "\n"
                for item in lst.get("content", "").split("<li>"):
                    description += item.replace("</li>", "").strip() + " "
            jobs.append({
                "external_id": f"lever_{j['id']}",
                "source":      "lever",
                "company":     self.slug,
                "title":       j.get("text", ""),
                "location":    (j.get("categories") or {}).get("location", ""),
                "url":         j.get("hostedUrl", ""),
                "description": description.strip(),
            })
        return jobs
