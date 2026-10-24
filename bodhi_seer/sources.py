"""Source connectors for broader biotech monitoring beyond the core GDELT API.

This module intentionally keeps the integration lightweight and terminal-friendly:
all sources log progress to the console, and unsupported sources fail gracefully
instead of crashing the app.
"""
from __future__ import annotations

import asyncio
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Callable, Iterable

import requests

from . import gdelt

DEFAULT_SOURCES = [
    "gdelt",
    "clinicaltrials",
    "chicagobusiness_rss",
    "google_patents",
    "crunchbase",
]


def _logger(logger: Callable[[str], None] | None, msg: str) -> None:
    if logger is None:
        print(f"[sources] {msg}")
    else:
        logger(f"[sources] {msg}")


def list_sources() -> list[dict]:
    return [
        {"name": "gdelt", "kind": "news", "description": "Public GDELT DOC API for broad event monitoring."},
        {"name": "clinicaltrials", "kind": "trials", "description": "ClinicalTrials.gov public search API for biotech trials."},
        {"name": "chicagobusiness_rss", "kind": "news", "description": "RSS feed for Chicago business and biotech coverage."},
        {"name": "google_patents", "kind": "patents", "description": "Google Patents search; requires a browser/API adapter in production."},
        {"name": "crunchbase", "kind": "funding", "description": "Crunchbase; requires a paid API key or partner access."},
    ]


def _gdelt_source(topic: str, location: str, days: int, logger: Callable[[str], None] | None) -> list[dict]:
    end = gdelt.utcnow()
    start = end - timedelta(days=max(days, 1))
    terms = [topic, f'{topic} {location}', location]
    query = ' OR '.join(f'"{term}"' for term in terms if term and term.strip())
    query = f'({query}) sourcelang:english'
    _logger(logger, f"gdelt: searching {query!r} from {start:%Y-%m-%d} to {end:%Y-%m-%d}")
    items = []
    for art in gdelt.fetch(query, start, end):
        items.append({
            "title": art["title"],
            "url": art["url"],
            "source": "gdelt",
            "location": location,
            "published_at": art["seen_at"],
        })
    _logger(logger, f"gdelt: collected {len(items)} items")
    return items


def _clinicaltrials_source(topic: str, location: str, days: int, logger: Callable[[str], None] | None) -> list[dict]:
    query_term = " ".join(part for part in [topic.strip(), location.strip()] if part and part.strip())
    params = {"query.term": query_term or "clinical trials", "pageSize": 10}
    url = "https://clinicaltrials.gov/api/v2/studies"
    _logger(logger, f"clinicaltrials: querying v2 term search {query_term!r}")
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # pragma: no cover - network path
        _logger(logger, f"clinicaltrials: failed to fetch results ({exc})")
        _logger(logger, "clinicaltrials: the public API endpoint may be unavailable or may require a different query contract; no results returned")
        return []

    studies = payload.get("studies", [])
    items = []
    seen = set()
    location_filter = (location or "").strip().lower()

    for study in studies:
        protocol = study.get("protocolSection", {})
        id_module = protocol.get("identificationModule", {})
        status_module = protocol.get("statusModule", {})
        title = id_module.get("briefTitle") or id_module.get("officialTitle") or "Clinical trial"
        nct_id = id_module.get("nctId") or ""
        if not nct_id:
            continue
        key = (nct_id, title)
        if key in seen:
            continue
        seen.add(key)

        loc_values = []
        for loc in (study.get("locationModule") or {}).get("locations", []) or []:
            facility = loc.get("facility") or {}
            address = facility.get("address") or {}
            for value in (address.get("city"), address.get("state"), address.get("country"), loc.get("city"), loc.get("state"), loc.get("country")):
                if value:
                    loc_values.append(str(value))

        if location_filter and loc_values and not any(location_filter in str(value).lower() for value in loc_values):
            continue

        url = f"https://clinicaltrials.gov/study/{nct_id}"
        published_at = status_module.get("primaryCompletionDate") or status_module.get("studyFirstSubmitDate") or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        items.append({
            "title": title,
            "url": url,
            "source": "clinicaltrials",
            "location": ", ".join(loc_values) if loc_values else location or "",
            "published_at": published_at,
        })

    _logger(logger, f"clinicaltrials: collected {len(items)} items")
    return items


def _rss_items(feed_urls: Iterable[str], topic: str, logger: Callable[[str], None] | None) -> list[dict]:
    term = topic.lower()
    all_items: list[dict] = []
    for feed_url in feed_urls:
        _logger(logger, f"chicagobusiness_rss: fetching {feed_url}")
        try:
            resp = requests.get(feed_url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:  # pragma: no cover - network path
            _logger(logger, f"chicagobusiness_rss: {feed_url} failed ({exc})")
            continue
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            _logger(logger, f"chicagobusiness_rss: {feed_url} returned invalid XML ({exc})")
            continue
        for item in root.findall("./channel/item") or root.findall(".//item"):
            title = item.findtext("title", default="")
            link = item.findtext("link", default="")
            published = item.findtext("pubDate", default="")
            if not title:
                continue
            text = f"{title} {link}".lower()
            if term not in text:
                continue
            all_items.append({
                "title": title,
                "url": link,
                "source": "chicagobusiness_rss",
                "published_at": published or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    _logger(logger, f"chicagobusiness_rss: collected {len(all_items)} items")
    return all_items


def _chicagobusiness_rss_source(topic: str, location: str, days: int, logger: Callable[[str], None] | None) -> list[dict]:
    _logger(logger, "chicagobusiness_rss: this feed is currently blocked with HTTP 403; a browser-based scrape or an approved partner feed is needed")
    return []


def _google_patents_source(topic: str, location: str, days: int, logger: Callable[[str], None] | None) -> list[dict]:
    query = urllib.parse.quote(f'{topic} {location}')
    _logger(logger, f"google_patents: query page is not a public JSON API; using a browser-based search path for {query!r}")
    _logger(logger, "google_patents: no results collected; add a browser adapter or API key for production use")
    return []


def _crunchbase_source(topic: str, location: str, days: int, logger: Callable[[str], None] | None) -> list[dict]:
    _logger(logger, "crunchbase: requires a Crunchbase API key or partner access; no public dataset is enabled for this app")
    return []


_SOURCE_FACTORIES = {
    "gdelt": "_gdelt_source",
    "clinicaltrials": "_clinicaltrials_source",
    "chicagobusiness_rss": "_chicagobusiness_rss_source",
    "google_patents": "_google_patents_source",
    "crunchbase": "_crunchbase_source",
}


def _collect_one_source(name: str, topic: str, location: str, days: int, logger):
    factory_name = _SOURCE_FACTORIES.get(name)
    fn = globals().get(factory_name)
    if fn is None:
        _logger(logger, f"unknown source {name!r}; skipping")
        return []
    _logger(logger, f"--- {name} ---")
    try:
        items = fn(topic, location, days, logger)
        _logger(logger, f"{name}: complete ({len(items)} items)")
        return items
    except Exception as exc:  # pragma: no cover - network path
        _logger(logger, f"{name}: raised an exception ({exc})")
        return []


async def _collect_one_source_async(name: str, topic: str, location: str, days: int, logger):
    return await asyncio.to_thread(_collect_one_source, name, topic, location, days, logger)


async def _collect_sources_async(topic: str, location: str = "Chicago", days: int = 30, include: list[str] | None = None, logger: Callable[[str], None] | None = None) -> list[dict]:
    if include is None:
        include = DEFAULT_SOURCES

    results: list[dict] = []
    parallel_names = [name for name in include if name != "gdelt"]

    if "gdelt" in include:
        results.extend(_collect_one_source("gdelt", topic, location, days, logger))

    if not parallel_names:
        return results

    tasks = [asyncio.create_task(_collect_one_source_async(name, topic, location, days, logger)) for name in parallel_names]
    for task in tasks:
        results.extend(await task)
    return results


def collect_sources(topic: str, location: str = "Chicago", days: int = 30, include: list[str] | None = None, logger: Callable[[str], None] | None = None) -> list[dict]:
    return asyncio.run(_collect_sources_async(topic, location, days, include, logger))
