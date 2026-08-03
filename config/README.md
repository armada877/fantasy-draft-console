# config/ — your local, un-pushed configuration

Everything in this directory that is **specific to you or your league stays local** —
`.gitignore` ignores all of `config/` except the `*.example` files and this README. Copy
each example to its real name and fill it in.

| You create | From | What it is |
|---|---|---|
| `config/league.json` | `league.example.json` | Your league: `league_id`, `season`, your team name (`me`), the projections `.xlsm` path, and your positional tilt (`my_mult`). Read by `scraping/scrape_league.py` and `draft_sheets/build_tool_data.py`. |
| `config/briefing.md` | `briefing.example.md` | The advisor's system prompt — your league's opponent tendencies, roster rules, and draft plan. The richer this is, the sharper the advisor. Loaded by `draft_app/server.py` at startup. |
| `config/.env` | `env.example` | Secrets: `ANTHROPIC_API_KEY` (advisor) and, if you scrape, `ESPN_SWID` / `ESPN_S2`. |
| `config/tendencies.json` *(optional)* | `tendencies.example.json` | Calibrated per-manager bid tendencies (`mult`/`conc`/`maxbuy`). Overrides the neutral default. Produced by `analysis/calibrate.py` (`pipeline.py calibrate`). Omit to keep all opponents neutral. |
| `config/manager_canon.json` *(optional)* | `manager_canon.example.json` | Maps ESPN owner GUID → canonical manager name for the local `analysis/` pipeline — real leaguemate names, so it stays local. Only needed to merge a manager's multiple ESPN accounts or fix inconsistent scraped names; absent → analysis uses scraped owner names. |

## Setup

```bash
cp config/league.example.json  config/league.json  # then edit for your league
cp config/briefing.example.md   config/briefing.md  # then edit for your league
cp config/env.example           config/.env         # then paste your key(s)
```

Load the secrets into your shell before running the server or scrapers:

```bash
set -a && . config/.env && set +a
```

The server auto-loads the briefing from `config/briefing.md` (override with the
`STRATEGY_BRIEFING_PATH` env var). If no briefing is present it falls back to a generic
one, so the app still runs.

## What else is local (elsewhere, also gitignored)

Generated/large league data lives next to the pipeline that produces it, not here:
`draft_sheets/tool_data.json`, `draft_sheets/*.xlsm`, `draft_app/static/index.html`,
`reports/`, `analysis/`, `scraping/raw/`. See the root `README.md` for how those are built.
