# Bodhi Seer

A 3D tree of news coverage on a topic, built from GDELT and stored in SQLite.
The tree grows topic → subtopics → stories → articles. Bodhi Seer splits the
coverage into growth phases, explains each phase, and summarizes the whole
tree. In the viewer, the explanations sit in a side panel; as you scroll or
step through them, the part of the graph each one describes lights up.

## Setup (macOS)

Needs Python 3.10 or newer (`brew install python` if `python3 --version` is older).

```bash
cd bodhi-seer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

The default setup is local Ollama/Qwen 2.5 3B. If you want to use Anthropic instead, set `BODHI_SEER_LLM_PROVIDER=anthropic` and your `ANTHROPIC_API_KEY`.

Try it with sample data first, no network needed:

```bash
python -m bodhi_seer demo
python -m bodhi_seer serve      # open http://localhost:8000
```

If you want to use a local model such as Qwen 2.5 3B through Ollama, start the container first:

```bash
docker compose up -d
```

Then configure the model endpoint in your shell or `.env`:

```bash
export BODHI_SEER_LLM_PROVIDER=ollama
export BODHI_SEER_MODEL=qwen2.5:3b
export BODHI_SEER_BASE_URL=http://localhost:11434/v1
```

Pull the model once:

```bash
docker compose exec ollama ollama pull qwen2.5:3b
```

## Tracking a real topic

```bash
python -m bodhi_seer add "Harbor bridge" --days 14
python -m bodhi_seer update "Harbor bridge"     # fetch what's new since last time, re-analyze
python -m bodhi_seer update --all               # every topic
python -m bodhi_seer analyze "Harbor bridge"    # recompute phases and explanations only
python -m bodhi_seer list
python -m bodhi_seer remove "Harbor bridge"
```

If you want a different GDELT query than the topic title, you can still override it:

```bash
python -m bodhi_seer add "Harbor bridge" --query '"harbor bridge" sourcelang:english' --days 14
```

The `--query` uses GDELT DOC API syntax: quote phrases, combine with `OR` in
parentheses, and filter with operators such as `sourcelang:english`,
`sourcecountry:US` or `domain:reuters.com`. GDELT keeps about 90 days, and it
rejects very short or very common search terms.

To keep a topic current, schedule `update --all` with cron (`crontab -e`):

```
0 * * * * cd /path/to/bodhi-seer && .venv/bin/python -m bodhi_seer update --all >> update.log 2>&1
```

## Using the viewer

- Scroll the panel, press ↑/↓ (or j/k), or click a card or a band on the volume chart to move between phases. Esc returns to the overview.
- In a phase, its nodes take the phase color, branches that first appeared in it are drawn larger, earlier parts are gray, and later parts fade out.
- In the overview, each node is colored by the phase in which it first appeared.
- Click a node to fly to it. Articles link to the source; branches can be highlighted on their own.
- Drag to rotate, scroll to zoom, right-drag to pan. "Move panel right" switches sides.

## With and without an API key

With `BODHI_SEER_LLM_PROVIDER=ollama` and a local model such as `qwen2.5:3b`, the app can use the local model to place each batch of new articles into the tree, write a title and explanation for each phase, and generate the overall summary.

With Anthropic configured instead, the same workflow also works using the Anthropic API. The app falls back to statistical descriptions when no model is available, so everything still runs even without a model backend.

Without a model backend, articles are grouped by distinctive title keywords, and phases get statistical descriptions (volume, new branches, leading sources). Adding a model later and running `analyze` rewrites the explanations, but articles already placed keep their keyword branches; to rebuild the tree, `remove` the topic and `add` it again.

## Multi-source monitoring

For local biotech and cluster analysis, the app now includes a source registry that can fan out to multiple sources and log progress to the terminal. The built-in source list includes:

- GDELT (broad event feed)
- ClinicalTrials.gov (trial search)
- Chicago Business RSS (local business coverage)
- Google Patents (browser/search-stage integration; no public API key required)
- Crunchbase (requires API credentials or partner access)

You can run the multi-source scan like this:

```bash
python -m bodhi_seer sources "biotech" --location "Chicago" --days 30
```

Each source logs progress as it runs, so you can see which connector is fetching data, how many items it collected, and which sources are intentionally unavailable until an API key or browser adapter is added.

## How it works

| File | Role |
| --- | --- |
| `bodhi_seer/gdelt.py` | Walks the time range in windows, splits windows that hit GDELT's 250-article cap, spaces requests 5 seconds apart. |
| `bodhi_seer/tree.py` | Skips duplicate URLs and syndicated duplicate headlines, then assigns articles to subtopics and stories. |
| `bodhi_seer/phases.py` | Bins articles over time and cuts the volume curve into segments of steady rate (dynamic programming with a penalty, at most 6 phases). Links each phase to every node it touched, then explains it. |
| `bodhi_seer/db.py` | SQLite schema: `topics`, `nodes` (the tree, via `parent_id`), `phases`, `phase_nodes`. |
| `bodhi_seer/server.py` | FastAPI: `/api/topics` and `/api/topics/{id}/graph`. |
| `bodhi_seer/static/` | The viewer, using [3d-force-graph](https://github.com/vasturiano/3d-force-graph) in radial tree layout. |

The database file defaults to `bodhi_seer.db` in the repo folder (`BODHI_SEER_DB` changes it) and you can open it with any SQLite tool.

## License

MIT. See [LICENSE](LICENSE).
