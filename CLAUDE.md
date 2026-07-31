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
| `draft_sheets/build_tool_data.py` | **Bring-your-own pipeline** — projections + scrape → `tool_data.json` | yes |
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
| `analysis/lib.py`, `a1..a20*.py` | Optional deep analysis (real manager names) | **no (local)** |
| `scraping/raw/`, `reports/`, `league/` | League data / analysis outputs | no (local) |

## The golden rule: edit the template, then re-inject

The served console is **generated**. Never hand-edit `draft_app/static/index.html`.
Edit `draft_sheets/draft_tool_template.html` (it has a `/*DATA*/` marker), then:

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

**Bring-your-own path (tracked, default):**
`scraping/scrape_league.py` (ESPN → `raw/{season}/league_full.json`; auto-discovers
league_id via the fan API and `me` via SWID↔members) →
`draft_sheets/build_tool_data.py` (raw stat sheets + scraped league → `tool_data.json`) →
re-inject (above). Config in `config/league.json`.

**Valuation is league-accurate:** build_tool_data recomputes FPTS from the scraped ESPN
scoring (statId→points), then VBD (replacement = teams×starters + FLEX pooled over
RB/WR/TE) and auction-$ (VBD share of the discretionary pool). It does NOT trust the
workbook's baked-in CheatSheet values (those assume the author's settings). Falls back to
CheatSheet if raw sheets are absent, and to workbook roster defaults + generic opponents
when no scrape exists, so the console always runs. Schema is in build_tool_data's docstring.

**Opponent tendencies are a KNOWN GAP:** every manager starts neutral (mult 1.0, conc 50,
maxbuy=budget). The calibration/simulation pipeline (`analysis/`) that derived these from
auction history is NOT in the repo (local, real names; user will push later). Seam:
`config/tendencies.json` (`{name: {mult, conc, maxbuy}}`), if present, overrides per manager.

**Original deep-analysis path (local, gitignored `analysis/`):** calibrated opponent
tendencies from years of ESPN auction history (`a20_export_tool_data.py` →
`a18_agent_auction.py` → `lib.py`, reads `scraping/raw/` from the full `scrape.py`).
`extract_elboberto_master.py` expects per-year master files with computed per-position
sheets — a **different layout** than the current-year CheatSheet workbook that
`build_tool_data.py` reads. Use `build_tool_data.py` for a fresh league.

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
