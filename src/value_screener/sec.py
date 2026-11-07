"""SEC EDGAR client: required User-Agent, <= sec_max_rps across threads, retries, cache.

Adapted from ``stock_research/edgar.py``. Responses are cached under
``data/cache/<name>.json.gz`` for ``cache_ttl_days``; a ``transform`` lets callers
keep only the slice they need (companyfacts files are megabytes each).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

import requests

from . import cache

TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

_RETRY_STATUS = {429, 500, 502, 503, 504}


def cik10(cik) -> str:
    """Zero-pad a CIK to the 10-digit form EDGAR URLs expect."""
    return str(int(cik)).zfill(10)


class RateLimiter:
    """Thread-safe spacing of request start times to at most ``rps`` per second."""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)


class SecClient:
    def __init__(self, user_agent: str, cache_dir: Path, *, rps: float = 8,
                 ttl_days: float = 7, attempts: int = 4, base_delay: float = 1.0,
                 session: requests.Session | None = None):
        if not user_agent or "<" in user_agent or "@" not in user_agent:
            raise ValueError("set sec_user_agent in config/settings.yaml to 'Name email@domain'")
        self.cache_dir = Path(cache_dir)
        self.ttl_days = ttl_days
        self.attempts = attempts
        self.base_delay = base_delay
        self.limiter = RateLimiter(rps)
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent,
                                     "Accept-Encoding": "gzip, deflate"})
        self.calls = 0                       # network requests made (cache misses)
        self._count_lock = threading.Lock()

    def get_json(self, url: str, name: str,
                 transform: Callable[[dict], object] | None = None):
        """Cached JSON (optionally transformed) for ``url``; None when SEC has no such file."""
        path = self.cache_dir / f"{name}.json.gz"
        hit = cache.read_json(path, self.ttl_days)
        if hit is not None:
            return None if hit == cache.MISSING else hit
        payload = self._fetch(url)
        obj = cache.MISSING if payload is None else (transform(payload) if transform else payload)
        cache.write_json(path, obj)
        return None if payload is None else obj

    def get_records(self, url: str, name: str, transform: Callable[[str], list[dict]],
                    ttl_days: float | None = None):
        """A large text file (an EDGAR index) cached only as the records ``transform`` keeps."""
        path = self.cache_dir / f"{name}.json.gz"
        hit = cache.read_json(path, self.ttl_days if ttl_days is None else ttl_days)
        if hit is not None:
            return None if hit == cache.MISSING else hit
        text = self._fetch(url, lambda resp: resp.text)
        obj = cache.MISSING if text is None else transform(text)
        cache.write_json(path, obj)
        return None if text is None else obj

    def _fetch(self, url: str, parse: Callable = lambda resp: resp.json()):
        delay = self.base_delay
        last: Exception | None = None
        for attempt in range(self.attempts):
            self.limiter.wait()
            with self._count_lock:
                self.calls += 1
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 404:
                    return None
                if resp.status_code in _RETRY_STATUS:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                resp.raise_for_status()
                return parse(resp)
            except (requests.RequestException, ValueError) as exc:
                last = exc
                if attempt == self.attempts - 1:
                    break
                time.sleep(delay * (4 if "429" in str(exc) else 1))
                delay *= 2
        assert last is not None
        raise last

    # Convenience endpoints -------------------------------------------------- #
    def tickers_exchange(self) -> dict:
        return self.get_json(TICKERS_EXCHANGE_URL, "company_tickers_exchange")

    def submissions(self, cik, transform=None):
        return self.get_json(SUBMISSIONS_URL.format(cik10=cik10(cik)),
                             f"submissions/CIK{cik10(cik)}", transform)

    def company_facts(self, cik, transform=None, variant: str | None = None):
        """``variant`` names the transform's cache, so changing it never reuses stale slices."""
        sub = f"facts/{variant}" if variant else "facts"
        return self.get_json(FACTS_URL.format(cik10=cik10(cik)),
                             f"{sub}/CIK{cik10(cik)}", transform)


def client_from_settings(settings) -> SecClient:
    return SecClient(settings.sec_user_agent, settings.cache_dir / "sec",
                     rps=settings.sec_max_rps, ttl_days=settings.cache_ttl_days)
