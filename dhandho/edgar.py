"""SEC EDGAR client: rate limited, every raw response cached to disk (gzipped).

Cached responses are never refetched unless refresh=True.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

import requests

log = logging.getLogger(__name__)

DATA_BASE = "https://data.sec.gov"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class EdgarError(RuntimeError):
    pass


class RateLimiter:
    """Enforces a minimum spacing between calls, so throughput never exceeds max_per_second."""

    def __init__(
        self,
        max_per_second: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if max_per_second <= 0:
            raise ValueError("max_per_second must be positive")
        self.interval = 1.0 / max_per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_allowed = float("-inf")

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next_allowed:
                self._sleep(self._next_allowed - now)
                now = self._next_allowed
            self._next_allowed = now + self.interval


def normalize_ticker(ticker: str) -> str:
    """SEC uses dashes for share classes: BRK.B / brk/b -> BRK-B."""
    return ticker.strip().upper().replace(".", "-").replace("/", "-")


class EdgarClient:
    def __init__(
        self,
        user_agent: str,
        cache_dir: str | Path,
        rate_limiter: RateLimiter,
        session: requests.Session | None = None,
        refresh: bool = False,
        timeout: float = 30,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if "@" not in user_agent:
            raise EdgarError("User-Agent must include a contact email")
        self.cache_dir = Path(cache_dir)
        self.limiter = rate_limiter
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.refresh = refresh
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        self._refreshed: set[Path] = set()  # with --refresh, refetch each URL at most once per run
        self.network_calls = 0

    # ---- cache -------------------------------------------------------------
    def _cache_path(self, kind: str, name: str) -> Path:
        return self.cache_dir / kind / f"{name}.json.gz"

    @staticmethod
    def _read_cache(path: Path) -> dict[str, Any]:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def _write_cache(path: Path, body: bytes, url: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with gzip.open(tmp, "wb") as fh:
            fh.write(body)
        tmp.replace(path)
        meta = {"url": url, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "bytes": len(body)}
        path.with_name(path.name.replace(".json.gz", ".meta.json")).write_text(json.dumps(meta, indent=2))

    def cache_meta(self, kind: str, name: str) -> dict[str, Any] | None:
        p = self._cache_path(kind, name)
        m = p.with_name(p.name.replace(".json.gz", ".meta.json"))
        return json.loads(m.read_text()) if m.exists() else None

    # ---- network -----------------------------------------------------------
    def _fetch(self, url: str) -> bytes:
        last_err: str = ""
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            self.network_calls += 1
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    return resp.content
                if resp.status_code == 404:
                    raise EdgarError(f"404 Not Found: {url}")
                last_err = f"HTTP {resp.status_code}"
                if resp.status_code not in RETRY_STATUSES:
                    raise EdgarError(f"{last_err} for {url}")
            if attempt < self.max_retries:
                delay = self.backoff_base * (2**attempt)
                log.warning("fetch %s failed (%s); retry %d in %.0fs", url, last_err, attempt + 1, delay)
                self._sleep(delay)
        raise EdgarError(f"giving up on {url} after {self.max_retries + 1} attempts: {last_err}")

    def get_json(self, url: str, kind: str, name: str) -> dict[str, Any]:
        path = self._cache_path(kind, name)
        use_cache = path.exists() and (not self.refresh or path in self._refreshed)
        if use_cache:
            log.debug("cache hit %s", path)
            return self._read_cache(path)
        log.info("GET %s", url)
        body = self._fetch(url)
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise EdgarError(f"invalid JSON from {url}: {exc}") from exc
        self._write_cache(path, body, url)
        self._refreshed.add(path)
        return data

    # ---- endpoints ---------------------------------------------------------
    def ticker_map(self) -> dict[str, tuple[int, str]]:
        """ticker -> (cik, company name)."""
        raw = self.get_json(TICKERS_URL, "reference", "company_tickers")
        out: dict[str, tuple[int, str]] = {}
        for row in raw.values():
            out[normalize_ticker(row["ticker"])] = (int(row["cik_str"]), row["title"])
        return out

    def companyfacts(self, cik: int) -> dict[str, Any]:
        name = f"CIK{cik:010d}"
        return self.get_json(f"{DATA_BASE}/api/xbrl/companyfacts/{name}.json", "companyfacts", name)

    def submissions(self, cik: int) -> dict[str, Any]:
        name = f"CIK{cik:010d}"
        return self.get_json(f"{DATA_BASE}/submissions/{name}.json", "submissions", name)


def client_from_config(cfg, refresh: bool = False) -> EdgarClient:
    sec = cfg.sec
    return EdgarClient(
        user_agent=sec["user_agent"],
        cache_dir=cfg.path("cache_dir"),
        rate_limiter=RateLimiter(sec["max_requests_per_second"]),
        refresh=refresh,
        timeout=sec.get("timeout_seconds", 30),
        max_retries=sec.get("max_retries", 4),
        backoff_base=sec.get("backoff_base_seconds", 2),
    )
