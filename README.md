# Live Auction Draft Console

A dynamic, data-driven command console for a **fantasy football auction draft**, with a
thin LLM advisor. It re-prices every remaining player in real time from the calibrated
bidding tendencies of each opponent, tracks your build against a target roster, surfaces
value/scarcity as the board moves, and (optionally) calls Claude for a live read of the
room after every pick.

Built for a specific keeper league, but the framework is **bring-your-own-league**: the
code and the universal projection baseline are tracked here; your league config, secrets,
and league-specific advisor briefing stay local (see [What's ignored](#whats-ignored)).

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
projections .xlsm  ─┐  (checked-in universal baseline)
                    ├─►  build_tool_data.py  ─►  draft_sheets/tool_data.json ─┐
ESPN league (scrape)┘   (settings + managers)         ▲                       │
config/tendencies.json ───────────────────────────────┘        inject into template ▼
  (optional calibrated opponents)                                                   │
draft_sheets/draft_tool_template.html  ──────────────────►  draft_app/static/index.html
                                                                                    │
                              draft_app/server.py  (FastAPI: serves console + /api/advise)
```

`pipeline.py` drives the whole thing — `python3 pipeline.py all` (or individual stages
`scrape` / `calibrate` / `simulate` / `build` / `inject`).

- **Pipeline** (`draft_sheets/build_tool_data.py` + `scraping/scrape_league.py`): combine
  the checked-in projection baseline with your league's scraped settings + managers to
  export `tool_data.json`.
- **League-accurate valuation:** `build_tool_data.py` doesn't just read the workbook's
  pre-computed numbers — it **recomputes** each player's FPTS from your league's ESPN
  scoring, then VBD and auction-$ from your exact roster (starters, FLEX, teams, budget).
  Change any of those and the values move, so the board reflects *your* league, not the
  workbook author's defaults. (Falls back to the sheet's own values if the workbook has no
  raw stat sheets.)
- **App** (`draft_app/`): FastAPI serves the console and a `/api/advise` endpoint that
  calls Claude with a strategy briefing + the live draft state.

### Opponent tendencies
The per-manager bid model (`mult`/`conc`/`maxbuy`) is calibrated from auction history. **Seam:**
`config/tendencies.json` (`{"Manager Name": {"mult": {...}, "conc": N, "maxbuy": N}}`) —
`build_tool_data.py` merges it per manager; anyone unmatched stays neutral. Fill it either way:
- **Have years of ESPN auction history + the local `analysis/` pipeline?** `python3 pipeline.py
  calibrate` derives each manager's positional aggression, stars-and-scrubs tilt and max-buy
  from `scraping/scrape.py`'s history and writes `tendencies.json` (keyed to your scraped
  manager names). `pipeline.py all` runs it automatically when that history is present.
- **Fresh league / no history?** Leave it out and every opponent bids at projected value, or
  hand-write `config/tendencies.json` from what you know about your leaguemates.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r draft_app/requirements.txt

# 1) Configure your league
cp config/league.example.json config/league.json    # edit: league_id, season, your team name ("me")

# 2) Scrape your ESPN league's real settings + managers (recommended)
cp config/env.example config/.env                    # add ESPN_SWID + ESPN_S2 cookies
set -a && . config/.env && set +a
python3 pipeline.py scrape

# 3) Build the console data and inject it into the template — one command:
python3 pipeline.py build inject
#    No ESPN cookies yet? Skip step 2 — build falls back to the projection workbook's own
#    roster/budget defaults + generic opponents, so you still get a running console.
#    Have auction history + the local analysis pipeline? Use `all` to also calibrate opponents:
#        python3 pipeline.py all

# 4) (Optional) enable the advisor — see Configuration below
cp config/briefing.example.md config/briefing.md     # then customize it for your league
# ANTHROPIC_API_KEY goes in config/.env too (already loaded in step 2)

# 5) Run
cd draft_app && uvicorn server:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

`pipeline.py` stages compose in any order you list them (`scrape calibrate simulate build
inject`, or `all` for the local-refresh chain). It needs `openpyxl` from the requirements
above — run it with the venv active (or `.venv/bin/python pipeline.py …`).

Without `ANTHROPIC_API_KEY` the whole console still works client-side; only the Advisor
panel is disabled.

## Configuration

**All of your custom, league-specific setup lives in one un-pushed directory: `config/`.**
`.gitignore` keeps everything in it local except the `*.example` templates and its README.

```bash
cp config/league.example.json  config/league.json  # your league: id, season, your team ("me")
cp config/briefing.example.md   config/briefing.md  # advisor prompt: your opponents + plan
cp config/env.example           config/.env         # secrets: ANTHROPIC_API_KEY (+ ESPN cookies)
set -a && . config/.env && set +a                   # load secrets into your shell
```

See [`config/README.md`](config/README.md) for the full table. Generated league data
(`draft_sheets/tool_data.json`, `static/index.html`, `reports/`, `analysis/`, scraped
data) is also gitignored — it's built by the pipeline, not committed.

## The advisor

`POST /api/advise` sends `{question, state, model}` to Claude. The **system prompt** is
loaded from `config/briefing.md` (gitignored — put your league's opponent tendencies and
plan there; start from `config/briefing.example.md`). The **live state** — every team's
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

Drop the new season's Elboberto projection `.xlsm` into `draft_sheets/` (it's tracked as
the universal baseline), point `config/league.json`'s `projections_xlsm` + `season` at it,
then `python3 pipeline.py scrape build inject` (rosters/managers can change year to year, so
re-scrape). If you keep calibrated opponents, use `python3 pipeline.py all` to refresh
`tendencies.json` from the updated history too.

## What's ignored

`.gitignore` keeps league-specific and private files **local** (never pushed): scraped
data (`scraping/raw/`), your live-edited projection copies (`draft_sheets/*.xlsx/*.csv`;
the `*_elboberto.xlsm` baseline **is** tracked), generated payloads (`tool_data.json`,
`static/index.html`, `static/data.json`), the **deep analysis pipeline** (`analysis/` —
its scripts carry real manager names) and its outputs (`reports/`), private notes
(`league/`), your whole `config/` directory (`league.json`, advisor `briefing.md` +
secrets), and `scraping/.espn_auth.json`. The reusable app, scrapers, template, the
pipeline (`pipeline.py`, `build_tool_data.py`, `scrape_league.py`), and the projection
baseline are tracked.
