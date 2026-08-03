# CLAUDE.md

Guidance for Claude working in this repo. See `README.md` for the user-facing overview.

## What this is

A live fantasy-football **auction draft console** (`draft_app/`) plus the **data pipeline**
that feeds it (`scraping/`, `analysis/`, `draft_sheets/`). The console re-prices players
from calibrated opponent tendencies and includes a thin LLM advisor (`/api/advise`).

## Layout

| Path | Role | Tracked? |
|------|------|----------|
| `draft_sheets/draft_tool_template.html` | **Frontend source** — edit this | yes |
| `draft_app/static/index.html` | Generated: template + injected data | no (generated) |
| `draft_app/server.py` | FastAPI: serves console + `/api/advise` | yes |
| `pipeline.py` | **Single entry point** — `scrape/calibrate/simulate/build/inject/all` | yes |
| `draft_sheets/build_tool_data.py` | Console builder — projections + scrape → `tool_data.json` | yes |
| `scraping/scrape_league.py` | Fresh-setup ESPN scraper (settings + managers, config-driven) | yes |
| `draft_sheets/*_elboberto.xlsm` | Universal projection baseline (checked in) | yes |
| `config/league.json` | Your league: id, season, `me`, projections path, `my_mult` | no (local) |
| `config/league.example.json` | League config template | yes |
| `config/briefing.md` | Advisor system prompt (league-specific) | no (local) |
| `config/briefing.example.md` | Generic briefing template | yes |
| `config/` | All local, league-specific config + secrets | no (except `*.example*`, `README.md`) |
| `draft_app/eval_advisor.py` | Advisor eval (mock draft → probe → check) | yes |
| `draft_sheets/tool_data.json` | Generated console data (players + profiles) | no (generated) |
| `scraping/scrape.py`, `scrape_playercards.py`, `extract_har.py` | Full historical scrapers | yes |
| `analysis/calibrate.py` | Opponent history → `config/tendencies.json` (reuses `a18.build_agents`) | **no (local)** |
| `analysis/lib.py`, `a5`, `a18`, `a19` | Calibration + auction-sim engine (real manager names) | **no (local)** |
| `analysis/research/a1..a17` | Archived one-off research that produced `reports/league_analysis.md` | **no (local)** |
| `config/tendencies.json` | Calibrated opponent profiles (produced by `calibrate.py`) | no (local) |
| `scraping/raw/`, `reports/`, `league/` | League data / analysis outputs | no (local) |

## The golden rule: edit the template, then re-inject

The served console is **generated**. Never hand-edit `draft_app/static/index.html`.
Edit `draft_sheets/draft_tool_template.html` (it has a `/*DATA*/` marker), then re-inject —
`python3 pipeline.py inject` does exactly this (template + `tool_data.json` →
`static/index.html` + `static/data.json`). The equivalent by hand:

```bash
python3 -c "tpl=open('draft_sheets/draft_tool_template.html').read(); \
data=open('draft_sheets/tool_data.json').read(); \
open('draft_app/static/index.html','w').write(tpl.replace('/*DATA*/', data))"
cp draft_sheets/tool_data.json draft_app/static/data.json
```

## Run / verify

```bash
# server (advisor needs the key; briefing.md is loaded if present)
cd draft_app && ANTHROPIC_API_KEY=sk-ant-... uvicorn server:app --host 127.0.0.1 --port 8000
curl -s localhost:8000/healthz        # {"ok":true,"advisor":true}
python3 draft_app/eval_advisor.py     # eval the advisor against a mock draft
```

The server has **no --reload**; restart it after editing `server.py` or `config/briefing.md`.

## Data pipeline (regenerate console data)

**One entry point — `pipeline.py`.** Stages run in the order you list them:

| Stage | Does | Wraps |
|-------|------|-------|
| `scrape` | ESPN settings + managers → `raw/{season}/league_full.json` (`--deep` also pulls history) | `scraping/scrape_league.py` (+ `scrape.py`) |
| `calibrate` | opponent auction history → `config/tendencies.json` | `extract_elboberto_master.py`, `analysis/calibrate.py` |
| `simulate` | agent-auction strategy test (stdout; `--stress` adds `a19`) | `analysis/a18_agent_auction.py` |
| `build` | projections × league → `tool_data.json` | `draft_sheets/build_tool_data.py` |
| `inject` | template + data → `static/index.html` + `data.json` | (the golden-rule step) |
| `all` | local refresh: `calibrate` (if history present) → `build` → `inject` | — |

```bash
python3 pipeline.py all                # refresh console from already-scraped history
python3 pipeline.py scrape calibrate build inject   # full refresh from ESPN
python3 pipeline.py build inject       # rebuild after editing the template only
```
Needs `openpyxl` (in `draft_app/requirements.txt`) — use a venv: `.venv/bin/python pipeline.py …`.

**Valuation is league-accurate (do not regress this):** `build_tool_data.py` recomputes FPTS
from the scraped ESPN scoring (statId→points), then VBD (replacement = teams×starters + FLEX
pooled over RB/WR/TE) and auction-$ (VBD share of the discretionary pool). It does NOT trust
the workbook's baked-in CheatSheet values. Falls back to CheatSheet if raw sheets are absent,
and to workbook roster defaults + generic opponents when no scrape exists, so the console
always runs. Schema is in build_tool_data's docstring.

**Opponent calibration (local, gitignored `analysis/`):** `calibrate.py` reuses
`a18_agent_auction.build_agents()` — per-manager positional aggression ($-weighted paid/proj),
stars-and-scrubs concentration, and max-buy ceiling from 2017–2025 auction history — and writes
`config/tendencies.json` (`{name: {mult, conc, maxbuy}}`). `build_tool_data.py` merges it per
manager by name; unmatched managers stay neutral. `calibrate.py` keys tendencies by the current
league's **scraped** manager names (bridging ESPN member GUIDs) so a returning manager whose
scraped display name drifted from their calibration identity (e.g. "Jon" vs "Jonathan") still
matches. The projection baseline
`elboberto_projections.json` is regenerated from the tracked `*_elboberto.xlsm` by
`extract_elboberto_master.py` (its fields are named `proj_value`/`start_vbd` — what the analysis
code reads); it feeds calibration/research **only**, never the console valuation.

For a brand-new league with no history, `all` skips `calibrate` and builds a neutral-opponent
console. The archived `analysis/research/a1..a17` (the scripts behind `reports/league_analysis.md`)
run with `PYTHONPATH=analysis python3 analysis/research/<script>.py`.

## Conventions & guardrails

- **Secrets stay out of git.** `ANTHROPIC_API_KEY` via env (the user keeps it in 1Password:
  `op read 'op://HMD LOCAL/Claude - API Key/credential'`). ESPN cookies live in
  `scraping/.espn_auth.json` (gitignored). Never print or commit these.
- **League-specific content stays local** (see `.gitignore`): scraped data, generated
  payloads, `reports/`, `league/`, `config/league.json`, and `config/briefing.md`. The
  universal `*_elboberto.xlsm` projection baseline **is** tracked; live-edited `.xlsx`/`.csv` copies are not.
- **Models:** default `claude-haiku-4-5` for live latency; the dropdown also allows
  `claude-sonnet-5` and `claude-opus-4-8` (allow-listed in `server.py`).
- **The advisor's grounding** is the live `state` posted each call (every team's budget,
  needs, roster; best-available; inflation; on-the-block). Keep `draftStateForAdvisor()`
  in the template and the eval's state-builder in sync when changing the shape.
- Don't commit or push unless the user asks.
