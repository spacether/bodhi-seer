"""Command line: python -m bodhi_seer <command> ..."""
from __future__ import annotations

import argparse
import math
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from . import db, gdelt, phases, sources, tree


def load_env(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _need_topic(conn, name):
    t = db.get_topic(conn, name)
    if not t:
        sys.exit(f"No topic named {name!r}. Run `python -m bodhi_seer list` to see topics.")
    return t


def estimate_fetch_volume(sample_count: int, *, days: float, probe_days: float) -> float:
    if sample_count <= 0 or days <= 0 or probe_days <= 0:
        return 0.0
    return sample_count * (days / probe_days)


def _probe_fetch(query: str, end: datetime, probe_days: float):
    probe_start = end - timedelta(days=probe_days)
    sample = list(gdelt.fetch(query, probe_start, end, allow_retries=False))
    return len(sample), probe_start


def _maybe_confirm_full_fetch(topic_name: str, query: str, days: float, *, prompt: bool = True) -> bool:
    if not prompt:
        return True
    end = gdelt.utcnow()
    probe_days = min(days, max(1.0, min(3.0, days / 5.0)))
    probe_count, _ = _probe_fetch(query, end, probe_days)
    estimated_total = estimate_fetch_volume(probe_count, days=days, probe_days=probe_days)
    request_gap = gdelt.REQUEST_GAP
    estimated_requests = max(1, math.ceil(estimated_total / gdelt.MAX_RECORDS))
    estimated_minutes = (estimated_requests * request_gap) / 60.0
    print(
        f"Probe: {probe_count} articles in the last {probe_days:.1f} days for {topic_name!r}. "
        f"Estimated total over {days:.1f} days: ~{estimated_total:.0f} articles. "
        f"This uses GDELT's throttled fetch path, which pauses {request_gap:.1f}s between requests; "
        f"the rough estimate is {estimated_minutes:.1f} minutes for about {estimated_requests} requests."
    )
    print(f"Query being probed: {query!r}")
    answer = input("Continue with the full fetch? [Y/n]: ").strip().lower()
    return answer not in {"n", "no", "cancel", "abort"}


def _fetch(conn, topic, days: float | None, *, confirm: bool = True) -> int:
    end = gdelt.utcnow()
    if topic["last_fetched"] and days is None:
        # Overlap the last window a little; duplicates are skipped on insert.
        start = datetime.strptime(topic["last_fetched"], "%Y-%m-%dT%H:%M:%SZ") - timedelta(hours=1)
    else:
        start = end - timedelta(days=days or 7)
    if confirm and days is not None and days > 0:
        if not _maybe_confirm_full_fetch(topic["name"], topic["query"], days):
            raise SystemExit(f"Cancelled full fetch for {topic['name']!r}.")
    print(f"Fetching GDELT for {topic['name']!r}: {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC")
    arts = list(gdelt.fetch(topic["query"], start, end))
    print(f"  {len(arts)} articles returned")
    added = tree.ingest(conn, topic, arts)
    with conn:
        conn.execute("UPDATE topics SET last_fetched = ? WHERE id = ?",
                     (end.strftime("%Y-%m-%dT%H:%M:%SZ"), topic["id"]))
    print(f"  {added} new articles added to the tree")
    return added


def _analyze(conn, topic) -> None:
    print(f"Analyzing growth phases for {topic['name']!r}")
    n = phases.analyze(conn, topic)
    print(f"  {n} phases written")


def _normalize_source_item(item: dict) -> dict:
    url = str(item.get("url") or "").strip()
    published = item.get("published_at") or item.get("seen_at") or datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    domain = item.get("domain") or (urlparse(url).netloc if url else item.get("source") or "source")
    return {
        "title": item.get("title") or item.get("headline") or "Untitled",
        "url": url or f"https://{domain}/source",
        "domain": domain,
        "country": item.get("location") or item.get("country") or "Unknown",
        "language": item.get("language") or "English",
        "image": item.get("image"),
        "seen_at": published,
    }


def _fetch_extra_sources(topic_name: str, location: str | None, days: float | None, *, logger=print, include: list[str] | None = None) -> list[dict]:
    location = (location or "").strip()
    if include is None:
        include = [name["name"] for name in sources.list_sources() if name["name"] != "gdelt"]
    try:
        items = sources.collect_sources(topic_name, location=location, days=int(days or 30), include=include, logger=logger)
    except Exception as exc:  # pragma: no cover - network path
        logger(f"[sources] failed while collecting {location!r} sources for {topic_name!r}: {exc}")
        return []
    normalized = [_normalize_source_item(item) for item in items]
    if normalized:
        logger(f"[sources] coordinated {len(normalized)} extra source items into the topic tree")
    return normalized


def _run_topic_fetch_and_sources(conn, topic, days, location, *, yes: bool, source_include: list[str] | None = None):
    with ThreadPoolExecutor(max_workers=2) as executor:
        gdelt_future = executor.submit(_fetch, conn, topic, days, confirm=not yes)
        source_future = executor.submit(_fetch_extra_sources, topic["name"], location, days, logger=print, include=source_include)
        try:
            fetch_count = gdelt_future.result()
        except Exception as exc:
            print(f"[warning] GDELT fetch failed for {topic['name']!r}: {exc}")
            print("[warning] Continuing with the other sources without the GDELT results.")
            fetch_count = 0
        try:
            source_items = source_future.result()
        except Exception as exc:
            print(f"[sources] failed while collecting {location!r} sources for {topic['name']!r}: {exc}")
            source_items = []
    return fetch_count, source_items


def _default_query(name: str, source_lang: str | None = None) -> str:
    query = name.strip()
    if not source_lang:
        source_lang = "english"
    if query:
        return f'"{query}" sourcelang:{source_lang}'
    return f"sourcelang:{source_lang}"


def cmd_add(a):
    conn = db.connect()
    existing = db.get_topic(conn, a.name)
    if existing:
        print(f"Topic {a.name!r} already exists; refreshing it instead of creating a duplicate.")
        topic = dict(existing)
        if a.query:
            with conn:
                conn.execute("UPDATE topics SET query = ? WHERE id = ?", (a.query, topic["id"]))
            topic["query"] = a.query
        elif a.sourcelang:
            default_query = _default_query(a.name, a.sourcelang)
            with conn:
                conn.execute("UPDATE topics SET query = ? WHERE id = ?", (default_query, topic["id"]))
            topic["query"] = default_query
    else:
        topic = db.create_topic(conn, a.name, a.query or _default_query(a.name, a.sourcelang))

    fetch_count, source_items = _run_topic_fetch_and_sources(conn, topic, a.days, getattr(a, "location", None), yes=a.yes)

    if fetch_count:
        _analyze(conn, db.get_topic(conn, topic["id"]))
    if source_items:
        tree.ingest(conn, topic, source_items)
        _analyze(conn, db.get_topic(conn, topic["id"]))


def cmd_update(a):
    conn = db.connect()
    names = [r["name"] for r in conn.execute("SELECT name FROM topics")] if a.all else [a.name]
    for name in names:
        topic = db.get_topic(conn, name)
        if not topic:
            print(f"Topic {name!r} does not exist yet; creating it now.")
            topic = db.create_topic(conn, name, a.query or _default_query(name, getattr(a, "sourcelang", None)))
        fetch_count, source_items = _run_topic_fetch_and_sources(conn, topic, a.days, getattr(a, "location", None), yes=a.yes)
        added = fetch_count
        if source_items:
            tree.ingest(conn, topic, source_items)
            added += len(source_items)
        if (added or a.reanalyze) and not a.no_analyze:
            _analyze(conn, db.get_topic(conn, topic["id"]))


def cmd_analyze(a):
    conn = db.connect()
    _analyze(conn, _need_topic(conn, a.name))


def cmd_list(a):
    conn = db.connect()
    rows = conn.execute(
        """SELECT t.*, (SELECT COUNT(*) FROM nodes n WHERE n.topic_id = t.id AND n.kind='article') n
           FROM topics t ORDER BY name""").fetchall()
    if not rows:
        print("No topics yet. Add one with: python -m bodhi_seer add \"Topic name\" --query '...'")
    for r in rows:
        print(f"{r['id']:>3}  {r['name']}  ({r['n']} articles, fetched to {r['last_fetched'] or 'never'})")
        print(f"     query: {r['query']}")


def cmd_remove(a):
    conn = db.connect()
    topic = _need_topic(conn, a.name)
    with conn:
        conn.execute("DELETE FROM topics WHERE id = ?", (topic["id"],))
    print(f"Removed {topic['name']!r}")


def cmd_serve(a):
    import uvicorn

    print(f"Open http://localhost:{a.port}")
    uvicorn.run("bodhi_seer.server:app", host=a.host, port=a.port, reload=a.reload)


def cmd_sources(a):
    print(f"Scanning data sources for {a.topic!r} in {a.location!r} over the last {a.days} days")
    items = sources.collect_sources(a.topic, location=a.location, days=a.days, logger=print)
    print(f"Total collected: {len(items)} items")
    for item in items[:10]:
        print(f"  - {item.get('source', 'source')}: {item.get('title', 'Untitled')} -> {item.get('url', '')}")
    if len(items) > 10:
        print(f"  ... and {len(items) - 10} more")


def cmd_demo(a):
    """Synthetic topic for trying the viewer without network access."""
    conn = db.connect()
    name = "Demo: harbor bridge closure"
    old = db.get_topic(conn, name)
    if old:
        with conn:
            conn.execute("DELETE FROM topics WHERE id = ?", (old["id"],))
    topic = db.create_topic(conn, name, '"harbor bridge"')
    rng = random.Random(7)
    start = gdelt.utcnow() - timedelta(days=12)
    # (day offset, articles that day, [(subtopic, story, headline), ...])
    script = [
        (0, 4, [("Safety and engineering", "Welds found cracked", "Inspection finds cracked welds on Harbor Bridge")]),
        (1, 22, [("Closure and traffic", "Bridge closed indefinitely", "Harbor Bridge closed indefinitely after inspection"),
                 ("Closure and traffic", "Commuter detours", "Commuters face long detours after bridge closure"),
                 ("Local economy", "Ferries add sailings", "Ferry operators add sailings as bridge stays shut")]),
        (2, 30, [("Closure and traffic", "Downtown gridlock", "Traffic gridlock grips downtown after closure"),
                 ("Local economy", "East-bank shops lose customers", "East-bank shop owners report foot traffic drop"),
                 ("Local economy", "Temporary ferry proposal", "City weighs temporary ferry across the harbor")]),
        (3, 12, [("Repairs and funding", "Mayor promises fast repairs", "Mayor promises fast repairs to Harbor Bridge"),
                 ("Local economy", "Relief fund request", "Shop owners ask city for relief fund")]),
        (5, 6, [("Accountability", "2019 audit flagged welds", "State audit flagged bridge welds in 2019"),
                ("Accountability", "Funding cuts questioned", "Lawmakers question cuts to bridge repair budget")]),
        (6, 18, [("Accountability", "Legislative hearing", "Legislature opens hearing on bridge funding"),
                 ("Accountability", "Funding cuts questioned", "Audit shows repair money was cut twice"),
                 ("Accountability", "Transport chief testifies", "Transport chief testifies on bridge welds")]),
        (8, 7, [("Repairs and funding", "Eight-month repair estimate", "Engineers estimate eight months of repairs"),
                ("Repairs and funding", "Contractor named", "Contractor named for Harbor Bridge repairs")]),
        (10, 15, [("Local economy", "Temporary ferry launches", "Temporary ferry launches across the harbor"),
                  ("Local economy", "Temporary ferry launches", "Ferry ridership beats early forecasts"),
                  ("Local economy", "East-bank shops lose customers", "Some customers return to east-bank shops")]),
    ]
    domains = ["harborgazette.com", "coastdispatch.com", "rivertonledger.com", "metrowire.com",
               "statepost.org", "citybeat.news", "bayreport.com"]
    arts = []
    for day, n, items in script:
        for i in range(n):
            t = start + timedelta(days=day, hours=rng.uniform(5, 23))
            sub_label, story, headline = rng.choice(items)
            arts.append({"url": f"https://example.com/demo/{day}-{i}",
                         "title": f"{headline}, {rng.choice(domains).split('.')[0]} reports ({day}-{i})",
                         "domain": rng.choice(domains), "country": "United States", "language": "English",
                         "image": None, "seen_at": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "subtopic": sub_label, "story": story})
    tree.ingest(conn, topic, arts)
    _analyze(conn, db.get_topic(conn, topic["id"]))
    print("Demo topic ready. Run: python -m bodhi_seer serve")


def main(argv=None):
    load_env()
    p = argparse.ArgumentParser(prog="bodhi-seer", description="3D tree of news coverage from GDELT")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("add", help="create a topic and fetch its first articles")
    s.add_argument("name")
    s.add_argument("--query", help="Optional override for the full GDELT query. If omitted, the app searches for the topic name in the selected source language.")
    s.add_argument("--sourcelang", default="english", help="GDELT source language filter, default: english")
    s.add_argument("--days", type=float, default=7, help="how far back to fetch (GDELT keeps about 90 days)")
    s.add_argument("--location", default=None, help="Optional location to include in external-source scans (for example: Chicago)")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt after the probe estimate")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("update", help="fetch articles since the last update, then re-analyze")
    s.add_argument("name", nargs="?")
    s.add_argument("--all", action="store_true", help="update every topic")
    s.add_argument("--days", type=float, help="refetch this many days back instead of since last update")
    s.add_argument("--location", default=None, help="Optional location to include in external-source scans (for example: Chicago)")
    s.add_argument("--no-analyze", action="store_true")
    s.add_argument("--yes", action="store_true", help="skip the confirmation prompt after the probe estimate")
    s.add_argument("--reanalyze", action="store_true", help="re-analyze even if nothing new arrived")
    s.set_defaults(func=cmd_update)

    s = sub.add_parser("analyze", help="recompute growth phases and summaries")
    s.add_argument("name")
    s.set_defaults(func=cmd_analyze)

    sub.add_parser("list", help="show topics").set_defaults(func=cmd_list)

    s = sub.add_parser("remove", help="delete a topic and its tree")
    s.add_argument("name")
    s.set_defaults(func=cmd_remove)

    s = sub.add_parser("serve", help="run the viewer")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--reload", action="store_true", default=True)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("sources", help="scan a set of external data sources and log progress to the terminal")
    s.add_argument("topic", nargs="?", default="biotech")
    s.add_argument("--location", default="Chicago")
    s.add_argument("--days", type=int, default=30)
    s.set_defaults(func=cmd_sources)

    sub.add_parser("demo", help="load a synthetic topic to try the viewer").set_defaults(func=cmd_demo)

    a = p.parse_args(argv)
    if a.cmd == "update" and not (a.name or a.all):
        p.error("update needs a topic name or --all")
    a.func(a)
