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
| `config/briefing.md` | Advisor system prompt (league-specific) | no (local) |
| `config/briefing.example.md` | Generic briefing template | yes |
| `config/` | All local, league-specific config + secrets | no (except `*.example`, `README.md`) |
| `draft_app/eval_advisor.py` | Advisor eval (mock draft → probe → check) | yes |
| `analysis/lib.py`, `a1..a20*.py` | Analysis + `tool_data.json` exporter | yes |
| `draft_sheets/tool_data.json` | Generated console data (players + profiles) | no (generated) |
| `scraping/*.py` | ESPN scrapers | yes |
| `scraping/raw/`, `draft_sheets/*.xlsm`, `reports/`, `league/` | League data / analysis | no (local) |

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

`a20_export_tool_data.py` → `a18_agent_auction.py` → `lib.py` (reads `scraping/raw/`).
`draft_sheets/extract_elboberto_master.py` builds `elboberto_projections.json` from the
`.xlsm` sheets. Output: `draft_sheets/tool_data.json` → re-inject (above).

## Conventions & guardrails

- **Secrets stay out of git.** `ANTHROPIC_API_KEY` via env (the user keeps it in 1Password:
  `op read 'op://HMD LOCAL/Claude - API Key/credential'`). ESPN cookies live in
  `scraping/.espn_auth.json` (gitignored). Never print or commit these.
- **League-specific content stays local** (see `.gitignore`): scraped data, projection
  sheets, generated payloads, `reports/`, `league/`, and `draft_app/briefing.md`.
- **Models:** default `claude-haiku-4-5` for live latency; the dropdown also allows
  `claude-sonnet-5` and `claude-opus-4-8` (allow-listed in `server.py`).
- **The advisor's grounding** is the live `state` posted each call (every team's budget,
  needs, roster; best-available; inflation; on-the-block). Keep `draftStateForAdvisor()`
  in the template and the eval's state-builder in sync when changing the shape.
- Don't commit or push unless the user asks.
