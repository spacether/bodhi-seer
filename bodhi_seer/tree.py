"""Place articles into the tree: topic -> subtopic -> story -> article.

With an API key, Claude assigns each batch of new articles to existing
branches or creates new ones. Without one, a keyword fallback groups
articles by the most distinctive word in their titles.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from . import db, llm

BATCH = 60
MAX_STORIES_SHOWN = 25

STOPWORDS = set("""
a an and are as at be been but by can could did do does for from had has have he her his how i if in
into is it its just may might more most new news not now of on or our out over said says she so some
than that the their them then there these they this to up us was we were what when where which who why
will with would you your after about against amid among before being between both during each first
get gets got here him its last make makes many much near next off once only other own same see show
shows still such take takes than too under until very via vs while year years day days week report
reports update updates live video photos watch read latest today yesterday tomorrow one two three
""".split())

SYSTEM = """You organize news coverage of one topic into a tree.
Level 1 is subtopics: broad angles on the topic. Aim for 3 to 8 in total across the whole tree.
Level 2 is stories: specific developments, events or announcements within a subtopic.
Reuse an existing subtopic or story label exactly, character for character, whenever an article fits it.
Create a new label only when an article clearly does not fit any existing one.
Labels are short (2 to 6 words), sentence case, neutral and specific."""


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z'-]{2,}", text.lower()) if w not in STOPWORDS]


def _existing_tree(conn, topic_id: int) -> dict:
    """{subtopic_label: {"id": id, "stories": {story_label: id}}}"""
    tree: dict = {}
    rows = conn.execute(
        """SELECT s.id sid, s.label slabel, st.id tid, st.label tlabel,
                  (SELECT COUNT(*) FROM nodes a WHERE a.parent_id = st.id) n,
                  st.seen_at
           FROM nodes s LEFT JOIN nodes st ON st.parent_id = s.id AND st.kind = 'story'
           WHERE s.topic_id = ? AND s.kind = 'subtopic'
           ORDER BY s.seen_at, st.seen_at""",
        (topic_id,),
    ).fetchall()
    for r in rows:
        sub = tree.setdefault(r["slabel"], {"id": r["sid"], "stories": {}, "counts": {}})
        if r["tid"]:
            sub["stories"][r["tlabel"]] = r["tid"]
            sub["counts"][r["tlabel"]] = r["n"]
    return tree


def _tree_prompt(tree: dict) -> str:
    if not tree:
        return "(empty: create the first subtopics and stories)"
    lines = []
    for sub, info in tree.items():
        lines.append(f"- {sub}")
        stories = sorted(info["stories"], key=lambda s: -info["counts"].get(s, 0))[:MAX_STORIES_SHOWN]
        for s in stories:
            lines.append(f"    - {s} ({info['counts'].get(s, 0)} articles)")
    return "\n".join(lines)


def _assign_llm(topic_name: str, tree: dict, batch: list[dict]) -> dict[int, tuple[str, str]]:
    lines = [f"{i} | {a['seen_at'][:16]} | {a['domain']} | {a['title']}" for i, a in enumerate(batch)]
    prompt = f"""Topic: {topic_name}

Existing tree:
{_tree_prompt(tree)}

New articles (id | time UTC | source | title):
{chr(10).join(lines)}

Return {{"assignments": [{{"id": <article id>, "subtopic": "<label>", "story": "<label>"}}, ...]}}
with one entry for every article id."""
    out = llm.complete_json(SYSTEM, prompt, max_tokens=6000)
    result = {}
    for item in out.get("assignments", []):
        try:
            i = int(item["id"])
            sub, story = str(item["subtopic"]).strip(), str(item["story"]).strip()
        except (KeyError, ValueError, TypeError):
            continue
        if 0 <= i < len(batch) and sub and story:
            result[i] = (sub[:80], story[:100])
    return result


def _assign_keywords(conn, topic, tree: dict, batch: list[dict]) -> dict[int, tuple[str, str]]:
    query_words = set(_words(topic["query"])) | set(_words(topic["name"]))
    titles = [r["label"] for r in db.articles(conn, topic["id"])] + [a["title"] for a in batch]
    counts = Counter(w for t in titles for w in set(_words(t)) if w not in query_words)
    ranked = [w for w, _ in counts.most_common(8)]
    existing = {label.lower(): label for label in tree}
    result = {}
    for i, a in enumerate(batch):
        words = set(_words(a["title"]))
        sub = next((existing[w] for w in words if w in existing), None)
        if not sub:
            kw = next((w for w in ranked if w in words), None)
            sub = kw.capitalize() if kw else "Other coverage"
        day = datetime.strptime(a["seen_at"][:10], "%Y-%m-%d")
        result[i] = (sub, f"{sub}: {day:%b} {day.day}")
    return result


def _match(label: str, options: dict) -> str | None:
    """Match a returned label to an existing one, ignoring case and spacing."""
    key = " ".join(label.lower().split())
    for existing in options:
        if " ".join(existing.lower().split()) == key:
            return existing
    return None


def ingest(conn, topic, new_articles: list[dict]) -> int:
    """Insert articles not already stored. Returns the number added."""
    topic_id = topic["id"]
    have_urls = {r[0] for r in conn.execute(
        "SELECT url FROM nodes WHERE topic_id = ? AND url IS NOT NULL", (topic_id,))}
    have_titles = {_norm_title(r[0]) for r in conn.execute(
        "SELECT label FROM nodes WHERE topic_id = ? AND kind = 'article'", (topic_id,))}
    fresh = []
    for a in new_articles:
        nt = _norm_title(a["title"])
        if a["url"] in have_urls or nt in have_titles:
            continue  # syndicated copies of the same headline add noise, not information
        have_urls.add(a["url"])
        have_titles.add(nt)
        fresh.append(a)
    fresh.sort(key=lambda a: a["seen_at"])
    if not fresh:
        return 0

    root = db.root_node(conn, topic_id)
    use_llm = llm.available()
    added = 0
    for start in range(0, len(fresh), BATCH):
        batch = fresh[start:start + BATCH]
        tree = _existing_tree(conn, topic_id)
        assignments = {}
        if use_llm and not all(a.get("story") for a in batch):
            try:
                assignments = _assign_llm(topic["name"], tree, batch)
            except Exception as e:  # keep going with the fallback rather than losing the batch
                print(f"  Claude assignment failed ({e}); using keywords for this batch")
        for i, a in enumerate(batch):
            if a.get("subtopic") and a.get("story"):  # pre-labelled (used by the demo)
                assignments[i] = (a["subtopic"], a["story"])
        missing = [i for i in range(len(batch)) if i not in assignments]
        if missing:
            fallback = _assign_keywords(conn, topic, tree, batch)
            for i in missing:
                assignments[i] = fallback[i]

        ts = db.now_iso()
        with conn:
            for i, a in enumerate(batch):
                sub_label, story_label = assignments[i]
                sub_key = _match(sub_label, tree)
                if sub_key is None:
                    cur = conn.execute(
                        "INSERT INTO nodes (topic_id, parent_id, kind, label, seen_at, added_at) "
                        "VALUES (?, ?, 'subtopic', ?, ?, ?)",
                        (topic_id, root["id"], sub_label, a["seen_at"], ts))
                    tree[sub_label] = {"id": cur.lastrowid, "stories": {}, "counts": {}}
                    sub_key = sub_label
                sub = tree[sub_key]
                story_key = _match(story_label, sub["stories"])
                if story_key is None:
                    cur = conn.execute(
                        "INSERT INTO nodes (topic_id, parent_id, kind, label, seen_at, added_at) "
                        "VALUES (?, ?, 'story', ?, ?, ?)",
                        (topic_id, sub["id"], story_label, a["seen_at"], ts))
                    sub["stories"][story_label] = cur.lastrowid
                    story_key = story_label
                sub["counts"][story_key] = sub["counts"].get(story_key, 0) + 1
                conn.execute(
                    "INSERT OR IGNORE INTO nodes (topic_id, parent_id, kind, label, url, domain, "
                    "country, language, image, seen_at, added_at) "
                    "VALUES (?, ?, 'article', ?, ?, ?, ?, ?, ?, ?, ?)",
                    (topic_id, sub["stories"][story_key], a["title"], a["url"], a["domain"],
                     a["country"], a["language"], a["image"], a["seen_at"], ts))
                added += 1
        print(f"  placed {min(start + BATCH, len(fresh))}/{len(fresh)} new articles")
    db.refresh_branch_dates(conn, topic_id)
    return added
