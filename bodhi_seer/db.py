"""SQLite storage for topics, the coverage tree, and growth phases."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    query         TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    last_fetched  TEXT,              -- end of the last GDELT window fetched
    summary       TEXT,
    analyzed_at   TEXT
);

-- One tree per topic: topic -> subtopic -> story -> article.
CREATE TABLE IF NOT EXISTS nodes (
    id         INTEGER PRIMARY KEY,
    topic_id   INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    parent_id  INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL CHECK (kind IN ('topic','subtopic','story','article')),
    label      TEXT NOT NULL,
    url        TEXT,
    domain     TEXT,
    country    TEXT,
    language   TEXT,
    image      TEXT,
    seen_at    TEXT NOT NULL,        -- ISO UTC; for branches, their earliest article
    added_at   TEXT NOT NULL,        -- when this row was written locally
    UNIQUE (topic_id, url)
);
CREATE INDEX IF NOT EXISTS nodes_topic ON nodes(topic_id, kind);
CREATE INDEX IF NOT EXISTS nodes_parent ON nodes(parent_id);

CREATE TABLE IF NOT EXISTS phases (
    id            INTEGER PRIMARY KEY,
    topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    idx           INTEGER NOT NULL,
    start_at      TEXT NOT NULL,
    end_at        TEXT NOT NULL,
    article_count INTEGER NOT NULL,
    title         TEXT NOT NULL,
    explanation   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS phase_nodes (
    phase_id  INTEGER NOT NULL REFERENCES phases(id) ON DELETE CASCADE,
    node_id   INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    is_new    INTEGER NOT NULL DEFAULT 0,   -- branch first appeared in this phase
    PRIMARY KEY (phase_id, node_id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or os.environ.get("BODHI_SEER_DB", "bodhi_seer.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def get_topic(conn, name_or_id) -> sqlite3.Row | None:
    if str(name_or_id).isdigit():
        row = conn.execute("SELECT * FROM topics WHERE id = ?", (int(name_or_id),)).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM topics WHERE name = ?", (str(name_or_id),)).fetchone()


def create_topic(conn, name: str, query: str) -> sqlite3.Row:
    ts = now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO topics (name, query, created_at) VALUES (?, ?, ?)", (name, query, ts)
        )
        conn.execute(
            "INSERT INTO nodes (topic_id, parent_id, kind, label, seen_at, added_at) "
            "VALUES (?, NULL, 'topic', ?, ?, ?)",
            (cur.lastrowid, name, ts, ts),
        )
    return get_topic(conn, cur.lastrowid)


def root_node(conn, topic_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM nodes WHERE topic_id = ? AND kind = 'topic'", (topic_id,)
    ).fetchone()


def branches(conn, topic_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM nodes WHERE topic_id = ? AND kind IN ('subtopic','story') ORDER BY seen_at",
        (topic_id,),
    ).fetchall()


def articles(conn, topic_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM nodes WHERE topic_id = ? AND kind = 'article' ORDER BY seen_at",
        (topic_id,),
    ).fetchall()


def refresh_branch_dates(conn, topic_id: int) -> None:
    """A branch's seen_at is the time of its earliest article; the root's is the earliest overall."""
    with conn:
        conn.execute(
            """UPDATE nodes SET seen_at = (
                   SELECT MIN(a.seen_at) FROM nodes a WHERE a.parent_id = nodes.id)
               WHERE topic_id = ? AND kind = 'story'
                 AND EXISTS (SELECT 1 FROM nodes a WHERE a.parent_id = nodes.id)""",
            (topic_id,),
        )
        conn.execute(
            """UPDATE nodes SET seen_at = (
                   SELECT MIN(s.seen_at) FROM nodes s WHERE s.parent_id = nodes.id)
               WHERE topic_id = ? AND kind = 'subtopic'
                 AND EXISTS (SELECT 1 FROM nodes s WHERE s.parent_id = nodes.id)""",
            (topic_id,),
        )
        conn.execute(
            """UPDATE nodes SET seen_at = COALESCE((
                   SELECT MIN(a.seen_at) FROM nodes a
                   WHERE a.topic_id = nodes.topic_id AND a.kind = 'article'), seen_at)
               WHERE topic_id = ? AND kind = 'topic'""",
            (topic_id,),
        )
