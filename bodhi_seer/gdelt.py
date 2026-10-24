"""Fetch articles from the GDELT DOC 2.0 API.

The DOC API returns at most 250 articles per request and covers roughly the
last three months. To get fuller coverage we walk the time range in windows
and split any window that hits the 250 cap. GDELT asks for at most one
request every 5 seconds, so requests are spaced out.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import requests


def _log(msg: str):
    print(f"[gdelt] {msg}")

API = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RECORDS = 250
MIN_WINDOW = timedelta(minutes=30)
REQUEST_GAP = 5.5  # seconds between requests
RATE_LIMIT_BASE_DELAY = 15.0
RATE_LIMIT_CAP = 60.0
RATE_LIMIT_TOTAL_BUDGET = 60.0

_last_request = 0.0


def _calculate_retry_delay(attempt: int, *, base: float = RATE_LIMIT_BASE_DELAY, cap: float = RATE_LIMIT_CAP) -> float:
    if attempt < 0:
        attempt = 0
    return min(cap, base * (2 ** attempt))


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


def parse_seendate(s: str) -> str:
    """'20261024T121500Z' -> '2026-10-24T12:15:00Z'"""
    dt = datetime.strptime(s, "%Y%m%dT%H%M%SZ")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _request(query: str, start: datetime, end: datetime, retries: int = 3, *, fail_fast_on_rate_limit: bool = False) -> list[dict]:
    global _last_request
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": MAX_RECORDS,
        "sort": "DateAsc",
        "startdatetime": _fmt(start),
        "enddatetime": _fmt(end),
    }
    _log(f"request query={query!r} window={start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} maxrecords={MAX_RECORDS}")
    total_rate_limit_delay = 0.0
    for attempt in range(retries):
        wait = REQUEST_GAP - (time.time() - _last_request)
        if wait > 0:
            _log(f"sleeping {wait:.1f}s before request to honor GDELT throttling")
            time.sleep(wait)
        _last_request = time.time()
        try:
            r = requests.get(API, params=params, timeout=60)
        except requests.RequestException as e:
            delay = _calculate_retry_delay(attempt, base=10.0, cap=300.0)
            _log(f"network error ({e}); retrying in {delay:.0f}s (attempt {attempt + 1}/{retries})")
            time.sleep(delay)
            continue
        text = r.text.strip()
        if r.status_code == 429 or "limit requests" in text.lower():
            if fail_fast_on_rate_limit:
                raise RuntimeError("GDELT rate-limited during the probe window; no retry performed.")
            delay = _calculate_retry_delay(attempt)
            if total_rate_limit_delay + delay > RATE_LIMIT_TOTAL_BUDGET:
                raise RuntimeError(
                    f"GDELT rate-limited and the retry budget reached 60s; stopping instead of retrying endlessly. "
                    f"Backoff used: {total_rate_limit_delay:.0f}s. Try again later."
                )
            total_rate_limit_delay += delay
            _log(f"GDELT rate-limited this request; backing off {delay:.0f}s before retry {attempt + 1}/{retries} "
                 f"(total rate-limit backoff so far: {total_rate_limit_delay:.0f}s / {RATE_LIMIT_TOTAL_BUDGET:.0f}s)")
            time.sleep(delay)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"GDELT returned {r.status_code}: {text[:200]}")
        if not text:
            _log("GDELT returned empty body for this query")
            return []
        try:
            data = r.json().get("articles", []) or []
            _log(f"GDELT returned {len(data)} articles for query={query!r} in window {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M}")
            return data
        except ValueError:
            # GDELT reports query problems as plain text, e.g. a phrase that is too short.
            raise RuntimeError(f"GDELT rejected the query: {text[:300]}")
    raise RuntimeError(f"GDELT kept rate-limiting; stopped after {total_rate_limit_delay:.0f}s of retry backoff. Try again later.")


def fetch(query: str, start: datetime, end: datetime, window: timedelta = timedelta(days=1), *, allow_retries: bool = True):
    """Yield article dicts between start and end, oldest window first."""
    seen: set[str] = set()
    pending = []
    t = start
    while t < end:
        pending.append((t, min(t + window, end)))
        t += window

    while pending:
        a, b = pending.pop(0)
        _log(f"fetching window {a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M} for query={query!r}")
        batch = _request(query, a, b, retries=3 if allow_retries else 1, fail_fast_on_rate_limit=not allow_retries)
        if len(batch) >= MAX_RECORDS and (b - a) > MIN_WINDOW:
            mid = a + (b - a) / 2
            _log(f"window hit {MAX_RECORDS} article cap; splitting {a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M} at {mid:%Y-%m-%d %H:%M}")
            pending[0:0] = [(a, mid), (mid, b)]
            continue
        label = f"{a:%Y-%m-%d %H:%M} to {b:%Y-%m-%d %H:%M}"
        print(f"  {label}: {len(batch)} articles" + (" (capped)" if len(batch) >= MAX_RECORDS else ""))
        for art in batch:
            url = art.get("url")
            if not url or url in seen or not art.get("title"):
                continue
            seen.add(url)
            yield {
                "url": url,
                "title": " ".join(art["title"].split()),
                "domain": art.get("domain"),
                "country": art.get("sourcecountry"),
                "language": art.get("language"),
                "image": art.get("socialimage") or None,
                "seen_at": parse_seendate(art["seendate"]),
            }


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
