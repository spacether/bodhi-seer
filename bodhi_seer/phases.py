"""Split a topic's coverage into growth phases, explain each, and summarize the tree.

Phases come from the article-volume curve: articles are binned over time and
the series is cut into segments of roughly constant rate (optimal
segmentation by dynamic programming, with a penalty that stops it from
over-splitting). Each phase is then linked to every node it touched, so the
interface can highlight that part of the graph.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import db, llm

MAX_PHASES = 6
MAX_BINS = 60


def _dt(s: str) -> datetime:
    if not s:
        raise ValueError("empty datetime value")
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Unsupported datetime value: {s!r}") from exc


def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def segment(counts: list[float], max_k: int = MAX_PHASES, min_len: int = 2,
            min_articles: int = 3) -> list[tuple[int, int]]:
    """Cut counts into contiguous [start, end) segments with roughly constant level."""
    n = len(counts)
    if n < 2 * min_len:
        return [(0, n)]
    raw = counts
    counts = [math.sqrt(c) for c in counts]  # steadies the variance of count data
    s1 = [0.0]
    s2 = [0.0]
    for c in counts:
        s1.append(s1[-1] + c)
        s2.append(s2[-1] + c * c)

    def cost(i: int, j: int) -> float:
        m = j - i
        tot = s1[j] - s1[i]
        return (s2[j] - s2[i]) - tot * tot / m

    inf = float("inf")
    max_k = max(1, min(max_k, n // min_len))
    best = [[inf] * (n + 1) for _ in range(max_k + 1)]
    back = [[0] * (n + 1) for _ in range(max_k + 1)]
    best[0][0] = 0.0
    for k in range(1, max_k + 1):
        for j in range(k * min_len, n + 1):
            for i in range((k - 1) * min_len, j - min_len + 1):
                if best[k - 1][i] == inf:
                    continue
                v = best[k - 1][i] + cost(i, j)
                if v < best[k][j]:
                    best[k][j], back[k][j] = v, i

    # Noise level from neighbouring-bin differences; the penalty grows with it.
    diffs = [counts[i + 1] - counts[i] for i in range(n - 1)]
    noise = max(sum(d * d for d in diffs) / (2 * len(diffs)), 0.25)
    penalty = 1.5 * noise * math.log(n)
    k_best = min(range(1, max_k + 1), key=lambda k: best[k][n] + penalty * k)

    cuts, j = [], n
    for k in range(k_best, 0, -1):
        i = back[k][j]
        cuts.append((i, j))
        j = i
    cuts = cuts[::-1]

    # Fold segments with almost no articles into a neighbour.
    merged: list[list[int]] = []
    for i, j in cuts:
        if merged and sum(raw[i:j]) < min_articles:
            merged[-1][1] = j
        else:
            merged.append([i, j])
    if len(merged) > 1 and sum(raw[merged[0][0]:merged[0][1]]) < min_articles:
        merged[1][0] = merged[0][0]
        merged.pop(0)
    return [(i, j) for i, j in merged]


def _sample(items: list, k: int) -> list:
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def _fmt_day(d: datetime) -> str:
    return f"{d:%b} {d.day}"


def analyze(conn, topic) -> int:
    """Recompute phases and the summary for a topic. Returns the number of phases."""
    topic_id = topic["id"]
    arts = db.articles(conn, topic_id)
    if not arts:
        raise RuntimeError("This topic has no articles yet. Run `update` first.")
    nodes = {r["id"]: r for r in conn.execute("SELECT * FROM nodes WHERE topic_id = ?", (topic_id,))}
    root = db.root_node(conn, topic_id)

    first, last = _dt(arts[0]["seen_at"]), _dt(arts[-1]["seen_at"])
    span = max((last - first).total_seconds(), 3600.0)
    nbins = max(4, min(MAX_BINS, len(arts) // 4))
    width = span / nbins
    counts = [0.0] * nbins
    for a in arts:
        b = min(int((_dt(a["seen_at"]) - first).total_seconds() / width), nbins - 1)
        counts[b] += 1
    segs = segment(counts)

    phases = []
    for idx, (i, j) in enumerate(segs):
        start = first + timedelta(seconds=i * width)
        end = first + timedelta(seconds=j * width) if j < nbins else last + timedelta(seconds=1)
        members = [a for a in arts if start <= _dt(a["seen_at"]) < end]
        touched: set[int] = {root["id"]}
        for a in members:
            touched.add(a["id"])
            p = a["parent_id"]
            while p is not None and p not in touched:
                touched.add(p)
                p = nodes[p]["parent_id"]
        new_branches = {
            nid for nid in touched
            if nodes[nid]["kind"] in ("subtopic", "story") and start <= _dt(nodes[nid]["seen_at"]) < end
        }
        phases.append({"idx": idx, "start": start, "end": end, "members": members,
                       "touched": touched, "new": new_branches})

    use_llm = llm.available()
    previous: list[dict] = []
    for ph in phases:
        title, text = None, None
        if use_llm:
            try:
                title, text = _explain_llm(topic, nodes, ph, previous, len(phases))
            except Exception as e:
                print(f"  Claude explanation failed for phase {ph['idx'] + 1} ({e}); using statistics")
        if not text:
            title, text = _explain_stats(nodes, ph)
        ph["title"], ph["explanation"] = title, text
        previous.append(ph)
        print(f"  phase {ph['idx'] + 1}: {title}")

    summary = None
    if use_llm:
        try:
            summary = _summary_llm(topic, nodes, phases, len(arts))
        except Exception as e:
            print(f"  Claude summary failed ({e}); using statistics")
    if not summary:
        summary = _summary_stats(nodes, phases, arts)

    with conn:
        conn.execute("DELETE FROM phases WHERE topic_id = ?", (topic_id,))
        for ph in phases:
            cur = conn.execute(
                "INSERT INTO phases (topic_id, idx, start_at, end_at, article_count, title, explanation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (topic_id, ph["idx"], _iso(ph["start"]), _iso(ph["end"]), len(ph["members"]),
                 ph["title"], ph["explanation"]))
            conn.executemany(
                "INSERT INTO phase_nodes (phase_id, node_id, is_new) VALUES (?, ?, ?)",
                [(cur.lastrowid, nid, int(nid in ph["new"])) for nid in ph["touched"]])
        conn.execute("UPDATE topics SET summary = ?, analyzed_at = ? WHERE id = ?",
                     (summary, db.now_iso(), topic_id))
    return len(phases)


def _branch_counts(nodes, ph) -> Counter:
    c: Counter = Counter()
    for a in ph["members"]:
        c[a["parent_id"]] += 1
    return c


def _explain_stats(nodes, ph) -> tuple[str, str]:
    members = ph["members"]
    sources = Counter(a["domain"] for a in members if a["domain"])
    stories = _branch_counts(nodes, ph)
    sub_counts: Counter = Counter()
    for sid, k in stories.items():
        sub_counts[nodes[sid]["parent_id"]] += k
    new_subs = [nodes[n]["label"] for n in sorted(
        (n for n in ph["new"] if nodes[n]["kind"] == "subtopic"), key=lambda n: -sub_counts[n])]
    new_stories = [nodes[n]["label"] for n in sorted(
        (n for n in ph["new"] if nodes[n]["kind"] == "story"), key=lambda n: -stories[n])]
    lead = nodes[stories.most_common(1)[0][0]]["label"] if stories else None
    title = new_subs[0] if new_subs else (lead or f"Phase {ph['idx'] + 1}")
    parts = [f"{len(members)} articles from {len(sources)} sources between "
             f"{_fmt_day(ph['start'])} and {_fmt_day(ph['end'])}."]
    if new_subs:
        parts.append("New angles: " + ", ".join(new_subs[:4]) + ".")
    if new_stories:
        parts.append(f"{len(new_stories)} new stories began, including " + ", ".join(new_stories[:3]) + ".")
    if lead:
        parts.append(f"Most coverage went to {lead} ({stories.most_common(1)[0][1]} articles).")
    if sources:
        parts.append("Leading sources: " + ", ".join(d for d, _ in sources.most_common(3)) + ".")
    return title, " ".join(parts)


def _explain_llm(topic, nodes, ph, previous, total) -> tuple[str, str]:
    stories = _branch_counts(nodes, ph)
    active = "\n".join(
        f"- {nodes[nodes[sid]['parent_id']]['label']} > {nodes[sid]['label']}: {n} articles"
        + (" (new)" if sid in ph["new"] else "")
        for sid, n in stories.most_common(15))
    titles = "\n".join(f"- {a['seen_at'][:16]} {a['domain']}: {a['label']}"
                       for a in _sample(ph["members"], 50))
    before = "\n".join(f"Phase {p['idx'] + 1} ({_fmt_day(p['start'])} to {_fmt_day(p['end'])}): "
                       f"{p['title']}. {p['explanation']}" for p in previous[-3:]) or "(this is the first phase)"
    prompt = f"""Topic: {topic['name']}
This is phase {ph['idx'] + 1} of {total}, from {ph['start']:%Y-%m-%d %H:%M} to {ph['end']:%Y-%m-%d %H:%M} UTC,
with {len(ph['members'])} articles.

Earlier phases:
{before}

Stories active in this phase:
{active}

Sample of headlines in this phase:
{titles}

Return {{"title": "...", "explanation": "..."}}.
The title names what defined this phase in 3 to 7 words.
The explanation is 2 to 4 plain sentences: what happened, what changed from the previous phase,
and which threads grew or appeared. Describe the coverage; don't speculate beyond the headlines."""
    out = llm.complete_json(
        "You explain how news coverage of a topic developed over time, phase by phase. "
        "Be concrete and neutral.", prompt, max_tokens=800)
    return str(out["title"]).strip(), str(out["explanation"]).strip()


def _tree_outline(nodes) -> str:
    children = defaultdict(list)
    for n in nodes.values():
        if n["parent_id"] is not None:
            children[n["parent_id"]].append(n)
    size = {nid: len(children[nid]) for nid in nodes}
    lines = []
    for n in nodes.values():
        if n["kind"] == "subtopic":
            stories = sorted(children[n["id"]], key=lambda s: -size[s["id"]])
            total = sum(size[s["id"]] for s in stories)
            lines.append(f"- {n['label']} ({total} articles)")
            lines += [f"    - {s['label']} ({size[s['id']]})" for s in stories[:8]]
    return "\n".join(lines)


def _summary_llm(topic, nodes, phases, n_articles) -> str:
    phase_text = "\n".join(f"{p['idx'] + 1}. {_fmt_day(p['start'])} to {_fmt_day(p['end'])}, "
                           f"{len(p['members'])} articles: {p['title']}. {p['explanation']}" for p in phases)
    prompt = f"""Topic: {topic['name']} ({n_articles} articles)

Coverage tree (subtopics and their largest stories):
{_tree_outline(nodes)}

Growth phases:
{phase_text}

Write a summary of this coverage in 1 or 2 short paragraphs of plain prose: what the story is,
its main threads, and how it grew. No headings or lists."""
    return llm.complete("You summarize news coverage neutrally and concretely.", prompt, 700)


def _summary_stats(nodes, phases, arts) -> str:
    subs = [n for n in nodes.values() if n["kind"] == "subtopic"]
    sources = {a["domain"] for a in arts}
    busiest = max(phases, key=lambda p: len(p["members"]))
    return (f"{len(arts)} articles from {len(sources)} sources, grouped into {len(subs)} subtopics "
            f"and {sum(1 for n in nodes.values() if n['kind'] == 'story')} stories. "
            f"Coverage ran from {_fmt_day(phases[0]['start'])} to {_fmt_day(phases[-1]['end'])} "
            f"in {len(phases)} phases; the busiest was phase {busiest['idx'] + 1} "
            f"({busiest['title']}) with {len(busiest['members'])} articles. "
            "Add an Anthropic API key to get written summaries.")
