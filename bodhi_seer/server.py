"""Local web server: JSON API plus the 3D viewer."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, sources


def _ensure_topic_summary(conn, topic):
    topic_row = db.get_topic(conn, topic["id"])
    if topic_row is None:
        return
    if topic_row["summary"]:
        return
    article_count = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE topic_id = ? AND kind = 'article'",
        (topic_row["id"],),
    ).fetchone()[0]
    if article_count > 0:
        from . import cli
        cli._analyze(conn, topic_row)

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Bodhi Seer")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path in {"/", "/topics"} or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/topics")
def topics_page():
    return FileResponse(STATIC / "topics.html")


def _source_scope_to_include(source_mode: str | None) -> list[str]:
    mode = (source_mode or "news").strip().lower()
    extra = [entry["name"] for entry in sources.list_sources() if entry["name"] != "gdelt"]
    if mode == "all":
        return extra
    return [entry["name"] for entry in sources.list_sources() if entry["name"] != "gdelt" and entry["kind"] == "news"]


def create_topic_from_form(*, name: str, days: float = 7, language: str | None = None,
                          source_mode: str | None = None, location: str | None = None):
    if not name or not str(name).strip():
        raise ValueError("Topic name is required")
    from . import cli

    clean_name = str(name).strip()
    lang = (language or "english").strip() or "english"
    include = _source_scope_to_include(source_mode)

    conn = db.connect()
    existing = db.get_topic(conn, clean_name)
    if existing:
        topic = dict(existing)
        query = cli._default_query(clean_name, lang)
        with conn:
            conn.execute("UPDATE topics SET query = ? WHERE id = ?", (query, topic["id"]))
        topic["query"] = query
    else:
        topic = db.create_topic(conn, clean_name, cli._default_query(clean_name, lang))

    fetch_count, source_items = cli._run_topic_fetch_and_sources(conn, topic, days, location, yes=True, source_include=include)
    if source_items:
        tree = __import__("bodhi_seer.tree", fromlist=["ingest"]).ingest
        tree(conn, topic, source_items)
    if fetch_count or source_items:
        cli._analyze(conn, db.get_topic(conn, topic["id"]))
    else:
        _ensure_topic_summary(conn, topic)
    return db.get_topic(conn, topic["id"])


@app.get("/api/topics")
def topics():
    conn = db.connect()
    rows = conn.execute(
        """SELECT t.id, t.name, t.query, t.summary, t.last_fetched, t.analyzed_at,
                  (SELECT COUNT(*) FROM nodes n WHERE n.topic_id = t.id AND n.kind = 'article') articles
           FROM topics t ORDER BY t.name""").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/topics")
def create_topic(payload: dict):
    name = (payload or {}).get("name")
    days = (payload or {}).get("days", 7)
    language = (payload or {}).get("language")
    source_mode = (payload or {}).get("source_mode", "news")
    location = (payload or {}).get("location")
    try:
        topic = create_topic_from_form(
            name=name,
            days=float(days),
            language=language,
            source_mode=source_mode,
            location=location,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    topic_dict = dict(topic)
    return {
        "id": topic_dict["id"],
        "name": topic_dict["name"],
        "query": topic_dict["query"],
        "summary": topic_dict.get("summary"),
        "analyzed_at": topic_dict.get("analyzed_at"),
        "source_mode": source_mode,
    }


@app.delete("/api/topics/{topic_id}")
def delete_topic(topic_id: int):
    conn = db.connect()
    topic = db.get_topic(conn, topic_id)
    if not topic:
        raise HTTPException(404, "No topic with that id")
    with conn:
        conn.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
    return {"deleted": True, "id": topic_id}


@app.get("/api/topics/{topic_id}/graph")
def graph(topic_id: int):
    conn = db.connect()
    topic = db.get_topic(conn, topic_id)
    if not topic:
        raise HTTPException(404, "No topic with that id")
    nodes = [dict(r) for r in conn.execute(
        """SELECT id, parent_id, kind, label, url, domain, country, seen_at
           FROM nodes WHERE topic_id = ?""", (topic_id,))]
    phases = []
    for p in conn.execute("SELECT * FROM phases WHERE topic_id = ? ORDER BY idx", (topic_id,)):
        members = conn.execute("SELECT node_id, is_new FROM phase_nodes WHERE phase_id = ?", (p["id"],)).fetchall()
        phases.append({
            "idx": p["idx"], "title": p["title"], "explanation": p["explanation"],
            "start_at": p["start_at"], "end_at": p["end_at"], "article_count": p["article_count"],
            "node_ids": [m["node_id"] for m in members],
            "new_node_ids": [m["node_id"] for m in members if m["is_new"]],
        })
    return {
        "topic": {"id": topic["id"], "name": topic["name"], "query": topic["query"],
                  "summary": topic["summary"], "analyzed_at": topic["analyzed_at"],
                  "last_fetched": topic["last_fetched"]},
        "nodes": nodes,
        "phases": phases,
    }
