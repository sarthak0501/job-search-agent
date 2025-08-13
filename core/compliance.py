from __future__ import annotations
import time, threading, urllib.parse
from urllib import robotparser
from typing import Dict, Tuple, Optional

class RateLimiter:
    """Simple per-domain token bucket (requests per minute)."""
    def __init__(self): 
        self._lock = threading.Lock()
        self._buckets: Dict[str, Tuple[int,float,int]] = {}  # domain -> (tokens, last_refill_ts, cap)

    def allow(self, domain: str, per_minute: int) -> bool:
        now = time.time()
        with self._lock:
            tokens, last, cap = self._buckets.get(domain, (per_minute, now, per_minute))
            # refill
            if now - last >= 60:
                tokens = per_minute
                last = now
            if tokens <= 0:
                self._buckets[domain] = (tokens, last, per_minute)
                return False
            tokens -= 1
            self._buckets[domain] = (tokens, last, per_minute)
            return True

class ComplianceGate:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.user_agent = self.cfg.get("user_agent", "job-search-agent/1.0")
        self.obey_robots = bool(self.cfg.get("obey_robots_txt", True))
        self.allow = set(self.cfg.get("allow_domains") or [])
        self.deny = set(self.cfg.get("deny_domains") or [])
        rl_cfg = self.cfg.get("rate_limits", {}) or {}
        self.default_per_min = int(rl_cfg.get("default_per_minute", 30))
        self.overrides: Dict[str,int] = rl_cfg.get("overrides", {}) or {}
        self._robots_cache: Dict[str, robotparser.RobotFileParser] = {}
        self._limiter = RateLimiter()

    def _domain(self, url: str) -> str:
        return urllib.parse.urlparse(url).netloc.lower()

    def _robots(self, domain: str) -> Optional[robotparser.RobotFileParser]:
        if domain in self._robots_cache:
            return self._robots_cache[domain]
        rp = robotparser.RobotFileParser()
        rp.set_url(f"https://{domain}/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None
        self._robots_cache[domain] = rp
        return rp

    def check_url(self, url: str) -> Tuple[bool, str]:
        d = self._domain(url)
        # deny list
        if d in self.deny:
            return False, f"Denied by config.deny_domains: {d}"
        # allow list (if present)
        if self.allow and d not in self.allow:
            return False, f"Not in allow_domains: {d}"

        # robots
        if self.obey_robots:
            rp = self._robots(d)
            if rp and not rp.can_fetch(self.user_agent, url):
                return False, f"Blocked by robots.txt for {d}"

        # rate limit
        per_min = self.overrides.get(d, self.default_per_min)
        if not self._limiter.allow(d, per_min):
            return False, f"Rate limit exceeded for {d} ({per_min}/min)"
        return True, "OK"
