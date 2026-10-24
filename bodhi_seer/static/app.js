/* Bodhi Seer viewer.
   The panel explains the story section by section; whichever section is active
   highlights the part of the 3D tree it describes. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const els = {
    app: $("app"), select: $("topic-select"), volume: $("volume"), sections: $("sections"),
    graph: $("graph"), detail: $("detail"), empty: $("empty"), legend: $("legend"),
    sideToggle: $("side-toggle"), fit: $("fit"),
  };

  const SIZE = { topic: 26, subtopic: 9, story: 3, article: 1 };
  const state = {
    data: null, byId: new Map(), children: new Map(), origin: new Map(),
    phaseSets: [], newSets: [], active: -1, focus: null, suppressObserver: 0, colors: null,
  };

  // ---------- helpers ----------
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const parse = (iso) => new Date(iso);
  const fmtDay = (iso) => parse(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const fmtTime = (iso) => parse(iso).toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const range = (a, b) => (fmtDay(a) === fmtDay(b) ? fmtDay(a) : `${fmtDay(a)} to ${fmtDay(b)}`);
  const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

  function readColors() {
    const css = getComputedStyle(document.documentElement);
    const v = (name) => css.getPropertyValue(name).trim();
    state.colors = {
      stage: v("--stage"), ink: v("--ink"), dim: v("--dim-node"), faint: v("--faint-node"),
      hues: [0, 1, 2, 3, 4, 5].map((i) => v(`--p${i}`)),
    };
  }
  const hue = (i) => state.colors.hues[((i % 6) + 6) % 6];
  function withAlpha(color, a) {
    if (color.startsWith("#")) {
      const h = color.length === 4 ? [...color.slice(1)].map((c) => c + c).join("") : color.slice(1);
      const n = parseInt(h, 16);
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
    }
    const m = color.match(/rgba?\(([^)]+)\)/);
    if (!m) return color;
    const [r, g, b] = m[1].split(",").map((x) => x.trim());
    return `rgba(${r},${g},${b},${a})`;
  }

  // ---------- graph ----------
  const Graph = new ForceGraph3D(els.graph, { controlType: "orbit" })
    .dagMode("radialout")
    .dagLevelDistance(70)
    .nodeId("id")
    .nodeRelSize(3)
    .nodeOpacity(1)
    .linkOpacity(1)
    .enableNodeDrag(false)
    .showNavInfo(false)
    .nodeLabel((n) => {
      const when = n.kind === "article" ? fmtTime(n.seen_at) : `since ${fmtDay(n.seen_at)}`;
      const src = n.domain ? `${esc(n.domain)}, ` : "";
      return `<b>${esc(n.label)}</b><br><span style="opacity:.75">${src}${when}</span>`;
    })
    .nodeVal((n) => {
      const isNew = state.active >= 0 && state.newSets[state.active].has(n.id);
      return SIZE[n.kind] * (isNew ? 1.8 : 1);
    })
    .nodeColor((n) => colorFor(n.id, n.kind === "article" ? 0.85 : 1))
    .linkColor((l) => colorFor(idOf(l.target), 0.55, true))
    .linkWidth((l) => (visibility(idOf(l.target)) === "on" ? 0.6 : 0))
    .onNodeClick(showDetail)
    .onBackgroundClick(hideDetail)
    .cooldownTicks(200);

  const idOf = (x) => (typeof x === "object" ? x.id : x);

  // "on": part of the active section; "before": already existed; "after": not yet there.
  function visibility(id) {
    if (state.focus) return state.focus.has(id) ? "on" : "after";
    if (state.active < 0) return "all";
    if (state.phaseSets[state.active].has(id)) return "on";
    const o = state.origin.get(id) ?? 0;
    return o < state.active ? "before" : "after";
  }

  function colorFor(id, alpha, isLink = false) {
    const node = state.byId.get(id);
    if (node && node.kind === "topic") return state.colors.ink;
    const vis = visibility(id);
    const c = state.colors;
    if (vis === "all") return withAlpha(hue(state.origin.get(id) ?? 0), isLink ? 0.35 : alpha);
    if (vis === "on") {
      const h = state.focus ? hue(state.origin.get(id) ?? 0) : hue(state.active);
      return withAlpha(h, isLink ? 0.7 : 1);
    }
    if (vis === "before") return isLink ? withAlpha(c.dim, 0.18) : c.dim;
    return isLink ? withAlpha(c.faint, 0.04) : c.faint;
  }

  function refreshGraph() {
    Graph.nodeColor(Graph.nodeColor()).linkColor(Graph.linkColor())
      .linkWidth(Graph.linkWidth()).nodeVal(Graph.nodeVal());
  }

  function fitTo(set, ms = 900) {
    if (set) Graph.zoomToFit(ms, 60, (n) => set.has(n.id));
    else Graph.zoomToFit(ms, 40);
  }

  const resize = new ResizeObserver(() => {
    Graph.width(els.graph.clientWidth).height(els.graph.clientHeight);
  });
  resize.observe(els.graph);

  // ---------- loading ----------
  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(`${url} returned ${r.status}`);
    return r.json();
  }

  async function init() {
    readColors();
    Graph.backgroundColor(state.colors.stage);
    try {
      if (localStorage.getItem("bodhi-seer-side") === "right") setSide("right");
    } catch (_) { /* storage unavailable: keep default */ }

    let topics;
    try {
      topics = await getJSON("/api/topics");
    } catch (e) {
      return showEmpty("Can't reach the local server",
        "Start it with <code>python -m bodhi_seer serve</code> and reload this page.");
    }
    if (!topics.length) {
      return showEmpty("No topics yet",
        'Add one with <code>python -m bodhi_seer add "Topic" --query \'"key phrase"\'</code>, ' +
        "or load sample data with <code>python -m bodhi_seer demo</code>.");
    }
    if (els.select) {
      els.select.innerHTML = topics.map((t) =>
        `<option value="${t.id}">${esc(t.name)} (${t.articles})</option>`).join("");
      const fromHash = Number(location.hash.slice(1));
      const start = topics.find((t) => t.id === fromHash) || topics[0];
      els.select.value = start.id;
      els.select.addEventListener("change", () => loadTopic(Number(els.select.value)));
      loadTopic(start.id);
      return;
    }

    const fromHash = Number(location.hash.slice(1));
    const start = topics.find((t) => t.id === fromHash) || topics[0];
    loadTopic(start.id);
  }

  async function loadTopic(id) {
    hideDetail();
    let data;
    try {
      data = await getJSON(`/api/topics/${id}/graph`);
    } catch (e) {
      return showEmpty("Couldn't load this topic", esc(e.message));
    }
    history.replaceState(null, "", `#${id}`);
    els.empty.hidden = true;
    state.data = data;
    state.byId = new Map(data.nodes.map((n) => [n.id, n]));
    state.children = new Map();
    data.nodes.forEach((n) => {
      if (n.parent_id != null) {
        if (!state.children.has(n.parent_id)) state.children.set(n.parent_id, []);
        state.children.get(n.parent_id).push(n.id);
      }
    });
    state.phaseSets = data.phases.map((p) => new Set(p.node_ids));
    state.newSets = data.phases.map((p) => new Set(p.new_node_ids));
    state.origin = new Map();
    data.phases.forEach((p, i) => p.node_ids.forEach((nid) => {
      if (!state.origin.has(nid)) state.origin.set(nid, i);
    }));
    state.active = -1;
    state.focus = null;

    const links = data.nodes.filter((n) => n.parent_id != null)
      .map((n) => ({ source: n.parent_id, target: n.id }));
    Graph.graphData({ nodes: data.nodes.map((n) => ({ ...n })), links });
    Graph.onEngineStop(() => { fitTo(null, 600); Graph.onEngineStop(() => {}); });

    renderSections();
    renderVolume();
    renderLegend();
    els.sections.scrollTop = 0;
  }

  function showEmpty(title, html) {
    els.empty.innerHTML = `<div><h2>${esc(title)}</h2><p>${html}</p></div>`;
    els.empty.hidden = false;
    els.sections.innerHTML = "";
    els.volume.innerHTML = "";
  }

  // ---------- panel ----------
  function renderSections() {
    const { topic, phases, nodes } = state.data;
    const arts = nodes.filter((n) => n.kind === "article");
    const sources = new Set(arts.map((a) => a.domain)).size;
    const first = arts.length ? arts.reduce((a, b) => (a.seen_at < b.seen_at ? a : b)).seen_at : null;
    const last = arts.length ? arts.reduce((a, b) => (a.seen_at > b.seen_at ? a : b)).seen_at : null;

    const summary = topic.summary
      ? topic.summary.split(/\n\s*\n/).map((p) => `<p class="text">${esc(p)}</p>`).join("")
      : `<p class="text">This topic hasn't been analyzed yet. Run
         <code>python -m bodhi_seer analyze "${esc(topic.name)}"</code> to split it into explained sections.</p>`;

    let html = `
      <article class="section overview active" data-index="-1" tabindex="0" style="--hue: var(--ink)">
        <p class="meta">${plural(arts.length, "article")} from ${plural(sources, "source")}${
          first ? `, ${range(first, last)}` : ""}</p>
        <h2>${esc(topic.name)}</h2>
        ${summary}
      </article>`;

    phases.forEach((p, i) => {
      const newBranches = p.new_node_ids.map((id) => state.byId.get(id))
        .filter((n) => n && (n.kind === "subtopic" || n.kind === "story"))
        .sort((a, b) => (a.kind === b.kind ? 0 : a.kind === "subtopic" ? -1 : 1));
      const shown = newBranches.slice(0, 4).map((n) => `<b>${esc(n.label)}</b>`).join(", ");
      const more = newBranches.length > 4 ? ` and ${newBranches.length - 4} more` : "";
      html += `
        <article class="section" data-index="${i}" tabindex="0" style="--hue: var(--p${i % 6})">
          <p class="meta"><span class="num">Phase ${i + 1}</span>&ensp;${range(p.start_at, p.end_at)},
            ${plural(p.article_count, "article")}</p>
          <h2>${esc(p.title)}</h2>
          <p class="text">${esc(p.explanation)}</p>
          ${newBranches.length ? `<p class="new">New here: ${shown}${more}</p>` : ""}
        </article>`;
    });
    observer.disconnect();
    els.sections.innerHTML = html;

    els.sections.querySelectorAll(".section").forEach((card) => {
      const i = Number(card.dataset.index);
      card.addEventListener("click", () => setActive(i, { scroll: true }));
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setActive(i, { scroll: true }); }
      });
      observer.observe(card);
    });
  }

  // Activate whichever section crosses a line a third of the way down the panel.
  const observer = new IntersectionObserver((entries) => {
    if (Date.now() < state.suppressObserver) return;
    const hit = entries.filter((e) => e.isIntersecting)
      .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (hit) setActive(Number(hit.target.dataset.index));
  }, { root: els.sections, rootMargin: "-30% 0px -65% 0px" });

  function setActive(i, { scroll = false } = {}) {
    const max = state.data ? state.data.phases.length - 1 : -1;
    i = Math.max(-1, Math.min(max, i));
    const changed = i !== state.active || state.focus;
    state.active = i;
    state.focus = null;
    els.sections.querySelectorAll(".section").forEach((c) =>
      c.classList.toggle("active", Number(c.dataset.index) === i));
    if (scroll) {
      state.suppressObserver = Date.now() + 900;
      const card = els.sections.querySelector(`.section[data-index="${i}"]`);
      if (card) els.sections.scrollTo({ top: card.offsetTop - els.sections.offsetTop - 8 });
    }
    if (!changed) return;
    refreshGraph();
    renderVolume();
    renderLegend();
    fitTo(i >= 0 ? state.phaseSets[i] : null);
  }

  function renderVolume() {
    const { phases, nodes } = state.data;
    const arts = nodes.filter((n) => n.kind === "article");
    if (!arts.length || !phases.length) { els.volume.innerHTML = ""; return; }
    const t0 = parse(phases[0].start_at).getTime();
    const t1 = parse(phases[phases.length - 1].end_at).getTime();
    const W = 360, H = 64, base = 50, BINS = 48;
    const bins = new Array(BINS).fill(0);
    arts.forEach((a) => {
      const b = Math.min(BINS - 1, Math.max(0, Math.floor((parse(a.seen_at) - t0) / (t1 - t0) * BINS)));
      bins[b] += 1;
    });
    const peak = Math.max(...bins, 1);
    const x = (t) => ((t - t0) / (t1 - t0)) * W;
    const phaseAt = (t) => phases.findIndex((p) => t < parse(p.end_at).getTime());

    let svg = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
      aria-label="Articles per period, colored by phase">`;
    phases.forEach((p, i) => {
      const a = x(parse(p.start_at).getTime()), b = x(parse(p.end_at).getTime());
      const on = state.active === i;
      svg += `<rect class="band" data-index="${i}" x="${a}" y="0" width="${Math.max(b - a, 1)}" height="${base}"
        fill="var(--p${i % 6})" opacity="${on ? 0.16 : 0}"><title>Phase ${i + 1}: ${esc(p.title)}</title></rect>`;
    });
    const bw = W / BINS;
    bins.forEach((v, b) => {
      if (!v) return;
      const mid = t0 + (b + 0.5) * (t1 - t0) / BINS;
      const pi = Math.max(0, phaseAt(mid));
      const h = (v / peak) * (base - 4);
      const op = state.active < 0 || state.active === pi ? 1 : 0.28;
      svg += `<rect x="${b * bw + 0.5}" y="${base - h}" width="${bw - 1}" height="${h}" rx="1"
        fill="var(--p${pi % 6})" opacity="${op}" pointer-events="none"/>`;
    });
    svg += `<text class="axis" x="0" y="${H - 2}">${fmtDay(phases[0].start_at)}</text>
      <text class="axis" x="${W}" y="${H - 2}" text-anchor="end">${fmtDay(phases[phases.length - 1].end_at)}</text>`;
    svg += "</svg>";
    els.volume.innerHTML = svg;
    els.volume.querySelectorAll(".band").forEach((r) =>
      r.addEventListener("click", () => setActive(Number(r.dataset.index), { scroll: true })));
  }

  function renderLegend() {
    const phases = state.data ? state.data.phases : [];
    if (state.focus) {
      els.legend.innerHTML = `<span style="--hue: var(--p0)">Highlighted branch</span>`;
    } else if (state.active < 0) {
      els.legend.innerHTML = phases.map((p, i) =>
        `<span style="--hue: var(--p${i % 6})">Phase ${i + 1}</span>`).join("") +
        (phases.length ? "<span style=\"--hue: transparent\">Color shows when each part first appeared</span>" : "");
    } else {
      els.legend.innerHTML = `<span style="--hue: var(--p${state.active % 6})">In this phase</span>
        <span style="--hue: var(--dim-node)">Earlier</span>
        <span style="--hue: var(--faint-node)">Later</span>`;
    }
  }

  // ---------- node detail ----------
  function subtree(id) {
    const out = new Set();
    const walk = (n) => { out.add(n); (state.children.get(n) || []).forEach(walk); };
    walk(id);
    let p = state.byId.get(id)?.parent_id;
    while (p != null) { out.add(p); p = state.byId.get(p)?.parent_id; }
    return out;
  }
  const countArticles = (id) => [...subtree(id)].filter((n) => {
    const node = state.byId.get(n);
    return node.kind === "article" && n !== id;
  }).length;

  function showDetail(node) {
    const n = state.byId.get(node.id);
    const dist = n.kind === "article" ? 45 : 110;
    const r = Math.hypot(node.x, node.y, node.z) || 1;
    Graph.cameraPosition(
      { x: node.x * (1 + dist / r), y: node.y * (1 + dist / r), z: node.z * (1 + dist / r) }, node, 900);

    const parent = state.byId.get(n.parent_id);
    let body;
    if (n.kind === "article") {
      body = `<p class="meta">${esc(n.domain || "")}${n.domain ? ", " : ""}${fmtTime(n.seen_at)}<br>
          In ${esc(parent?.label)}</p>
        <div class="row"><a href="${esc(n.url)}" target="_blank" rel="noopener">Open article</a>
          <button type="button" data-act="branch" data-id="${parent?.id}">Highlight its story</button>
          <button type="button" class="close" data-act="close">Close</button></div>`;
    } else if (n.kind === "topic") {
      body = `<p class="meta">${plural(countArticles(n.id), "article")}</p>
        <div class="row"><button type="button" class="close" data-act="close">Close</button></div>`;
    } else {
      const kind = n.kind === "story" ? `Story in ${esc(parent?.label)}` : "Subtopic";
      body = `<p class="meta">${kind}, ${plural(countArticles(n.id), "article")}, since ${fmtDay(n.seen_at)}</p>
        <div class="row"><button type="button" data-act="branch" data-id="${n.id}">Highlight this branch</button>
          <button type="button" class="close" data-act="close">Close</button></div>`;
    }
    els.detail.innerHTML = `<h3>${esc(n.label)}</h3>${body}`;
    els.detail.hidden = false;
    els.detail.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.act === "close") return hideDetail();
      state.focus = subtree(Number(b.dataset.id));
      refreshGraph();
      renderLegend();
      fitTo(state.focus);
    }));
  }

  function hideDetail() {
    els.detail.hidden = true;
    if (state.focus) {
      state.focus = null;
      refreshGraph();
      renderLegend();
    }
  }

  // ---------- controls ----------
  function setSide(side) {
    els.app.classList.toggle("right", side === "right");
    els.sideToggle.textContent = side === "right" ? "Move panel left" : "Move panel right";
    try { localStorage.setItem("bodhi-seer-side", side); } catch (_) { /* ignore */ }
  }
  els.sideToggle.addEventListener("click", () =>
    setSide(els.app.classList.contains("right") ? "left" : "right"));
  els.fit.addEventListener("click", () =>
    fitTo(state.focus || (state.active >= 0 ? state.phaseSets[state.active] : null)));

  document.addEventListener("keydown", (e) => {
    if (!state.data || e.target.tagName === "SELECT" || e.metaKey || e.ctrlKey) return;
    if (e.key === "ArrowDown" || e.key === "j") { e.preventDefault(); setActive(state.active + 1, { scroll: true }); }
    if (e.key === "ArrowUp" || e.key === "k") { e.preventDefault(); setActive(state.active - 1, { scroll: true }); }
    if (e.key === "Escape") { hideDetail(); setActive(-1, { scroll: true }); }
  });

  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    readColors();
    Graph.backgroundColor(state.colors.stage);
    refreshGraph();
  });

  init();
})();
