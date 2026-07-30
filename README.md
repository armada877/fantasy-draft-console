# Live Auction Draft Console

A dynamic, data-driven command console for a **fantasy football auction draft**, with a
thin LLM advisor. It re-prices every remaining player in real time from the calibrated
bidding tendencies of each opponent, tracks your build against a target roster, surfaces
value/scarcity as the board moves, and (optionally) calls Claude for a live read of the
room after every pick.

Built for a specific keeper league, but the framework is **bring-your-own-league**: the
code is tracked here; your data, projections, and league-specific advisor briefing stay
local (see [What's ignored](#whats-ignored)).

![console](docs/console.png)

## What it does

- **On the block** — type the nominated player → **Worth**, live **Will go ≈** (predicted
  sale price from opponents' calibrated bids × market inflation), and a **dynamic Max bid**
  that adjusts all draft long for value, scarcity, market trend, your budget and roster.
- **Value board / Projections** — best-available ranked by worth, or raw projections
  (FPTS / VORP / tiers) filterable by position **and tier**, with live scarcity/cliffs.
  Toggle "hide picked" to keep drafted players visible (faded + struck through).
- **Your build vs target** — roster (starters + bench) tracked against a target split.
- **Advisor (LLM)** — ask anything, or let it auto-post a read after each pick. Pick the
  model (Haiku / Sonnet / Opus). Answers accumulate in a scrollable feed.

## Architecture

```
ESPN league data ─┐
                  ├─► analysis/ (lib.py + a1..a20) ─► draft_sheets/tool_data.json ─┐
projection sheets ┘                                                                 │
                                                                inject into template ▼
draft_sheets/draft_tool_template.html  ──────────────────►  draft_app/static/index.html
                                                                                    │
                              draft_app/server.py  (FastAPI: serves console + /api/advise)
```

- **Pipeline** (`scraping/`, `analysis/`, `draft_sheets/`): scrape league history →
  analyze tendencies → export `tool_data.json` (players + per-manager bidding profiles).
- **App** (`draft_app/`): FastAPI serves the console and a `/api/advise` endpoint that
  calls Claude with a strategy briefing + the live draft state.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r draft_app/requirements.txt

# 1) Provide data: generate tool_data.json from your own pipeline (see below),
#    or drop your own draft_sheets/tool_data.json (see the shape in the template's /*DATA*/).
# 2) Inject the data into the console template:
python3 -c "tpl=open('draft_sheets/draft_tool_template.html').read(); \
data=open('draft_sheets/tool_data.json').read(); \
open('draft_app/static/index.html','w').write(tpl.replace('/*DATA*/', data))"
cp draft_sheets/tool_data.json draft_app/static/data.json

# 3) (Optional) enable the advisor
cp draft_app/briefing.example.md draft_app/briefing.md   # then customize it
export ANTHROPIC_API_KEY=sk-ant-...

# 4) Run
cd draft_app && uvicorn server:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

Without `ANTHROPIC_API_KEY` the whole console still works client-side; only the Advisor
panel is disabled.

## The advisor

`POST /api/advise` sends `{question, state, model}` to Claude. The **system prompt** is
loaded from `draft_app/briefing.md` (gitignored — put your league's opponent tendencies
and plan there; start from `briefing.example.md`). The **live state** — every team's
budget, needs and roster, best-available, inflation, and the player on the block — is
posted on every call, so the advisor tracks the evolving draft. Model is chosen from the
dropdown (allow-listed in `server.py`).

**Eval:** `python3 draft_app/eval_advisor.py` runs a tendency-driven mock auction and
probes the advisor at checkpoints, checking it stays grounded (only names available
players, respects your budget, cites real team budgets) and on-strategy. Use it to tune
the briefing.

## Deploy on Railway

Push to GitHub, then **New Project → Deploy from GitHub repo** pointing at `draft_app/`.
Nixpacks detects Python and installs `requirements.txt`; the `Procfile` binds `$PORT`.
Set `ANTHROPIC_API_KEY` (and, if you want the advisor sharp, commit a `briefing.md` **to
a private repo only** or set `STRATEGY_BRIEFING_PATH`). See `draft_app/README.md`.

## Refresh data for a new season

Re-run `analysis/a20_export_tool_data.py` to regenerate `draft_sheets/tool_data.json`,
then re-run the inject step above. Update projection sheets via
`draft_sheets/extract_elboberto_master.py`.

## What's ignored

`.gitignore` keeps league-specific and private files **local** (never pushed): scraped
data (`scraping/raw/`), projection sheets (`draft_sheets/*.xlsm`, `*_projections.json`),
generated payloads (`tool_data.json`, `static/index.html`, `static/data.json`), the
**analysis pipeline** (`analysis/` — its scripts carry real manager names) and its
outputs (`reports/`), private notes (`league/`), the advisor's `briefing.md`, and secrets
(`.env`, `scraping/.espn_auth.json`). The reusable app, scrapers, template, and docs are
tracked. (If you'd rather publish the analysis too, sanitize the names in `analysis/lib.py`
into a config first.)
