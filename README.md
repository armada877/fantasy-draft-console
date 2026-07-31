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
ESPN league (scrape)┘   (settings + managers)                                 │
                                                                inject into template ▼
draft_sheets/draft_tool_template.html  ──────────────────►  draft_app/static/index.html
                                                                                    │
                              draft_app/server.py  (FastAPI: serves console + /api/advise)
```

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

### Opponent tendencies — a known gap
The per-manager bid model (`mult`/`conc`/`maxbuy`) starts **neutral** for every team. The
modeling/simulation pipeline that calibrates these from years of ESPN auction history
(`analysis/`, fed by the full `scraping/scrape.py`) is **not yet in this repo** — it carries
real manager names and lives locally. Until it lands, opponents bid at projected value with
no personality. **Seam:** if `config/tendencies.json` exists (`{"Manager Name": {"mult":
{...}, "conc": N, "maxbuy": N}}`), `build_tool_data.py` uses it per manager — that's where
calibrated output plugs in.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r draft_app/requirements.txt

# 1) Configure your league
cp config/league.example.json config/league.json    # edit: league_id, season, your team name ("me")

# 2) Build the data payload (tool_data.json).
#    a) Recommended — scrape your ESPN league's real settings + managers first:
cp config/env.example config/.env                    # add ESPN_SWID + ESPN_S2 cookies
set -a && . config/.env && set +a
python3 scraping/scrape_league.py
#    b) Combine the checked-in projections with your league into tool_data.json:
python3 draft_sheets/build_tool_data.py
#    (No ESPN cookies yet? Skip 2a — build_tool_data.py falls back to the projection
#     workbook's own roster/budget defaults + generic opponents, so you still get a
#     running console. Re-run 2a + 2b once you have cookies.)

# 3) Inject the data into the console template:
python3 -c "tpl=open('draft_sheets/draft_tool_template.html').read(); \
data=open('draft_sheets/tool_data.json').read(); \
open('draft_app/static/index.html','w').write(tpl.replace('/*DATA*/', data))"
cp draft_sheets/tool_data.json draft_app/static/data.json

# 4) (Optional) enable the advisor — see Configuration below
cp config/briefing.example.md config/briefing.md     # then customize it for your league
# ANTHROPIC_API_KEY goes in config/.env too (already loaded in step 2a)

# 5) Run
cd draft_app && uvicorn server:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000
```

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
re-run `scraping/scrape_league.py` (rosters/managers can change year to year), then
`python3 draft_sheets/build_tool_data.py` and the inject step above.

## What's ignored

`.gitignore` keeps league-specific and private files **local** (never pushed): scraped
data (`scraping/raw/`), your live-edited projection copies (`draft_sheets/*.xlsx/*.csv`;
the `*_elboberto.xlsm` baseline **is** tracked), generated payloads (`tool_data.json`,
`static/index.html`, `static/data.json`), the **deep analysis pipeline** (`analysis/` —
its scripts carry real manager names) and its outputs (`reports/`), private notes
(`league/`), your whole `config/` directory (`league.json`, advisor `briefing.md` +
secrets), and `scraping/.espn_auth.json`. The reusable app, scrapers, template, the
bring-your-own pipeline (`build_tool_data.py`, `scrape_league.py`), and the projection
baseline are tracked.
