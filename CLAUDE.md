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
| `draft_sheets/CSG*auction.xlsm` | CSG sheet — **complementary market view** (third-party) | no (local) |
| `draft_sheets/extract_csg.py` | CSG `Overall` tab → `csg_consensus.json` | yes |
| `draft_sheets/check_csg_settings.py` | Diff CSG's league settings vs your scrape | yes |
| `draft_sheets/csg_consensus.json` | Generated CSG consensus data, by season | no (generated) |
| `config/league.json` | Your league: id, season, `me`, projections path, `my_mult` | no (local) |
| `config/league.example.json` | League config template | yes |
| `config/briefing.md` | Advisor system prompt (league-specific) | no (local) |
| `config/briefing.example.md` | Generic briefing template | yes |
| `config/` | All local, league-specific config + secrets | no (except `*.example*`, `README.md`) |
| `draft_app/eval_advisor.py` | Advisor eval (mock draft → probe → check) | yes |
| `draft_sheets/tool_data.json` | Generated console data (players + profiles) | no (generated) |
| `scraping/scrape.py`, `scrape_playercards.py`, `extract_har.py` | Full historical scrapers | yes |
| `analysis/calibrate.py` | Opponent history → `config/tendencies.json` (reuses `a18.build_agents`) | yes |
| `analysis/backtest_*.py` | WS-8 validation: projection accuracy, lineup replay, source value | yes |
| `scraping/backfill_weeks.py` | WS-8a: historical weekly boxscores → paired (projection, actual) | yes |
| `reports/` | Generated validation artifacts (`projection_accuracy.json`, `lineup_replay.json`, `source_value.json`) | no (generated) |
| `analysis/lib.py` | **Canonical** `effective_wallet` / `regime` / `norm_cost` helpers | **no (local)** |
| `analysis/price_curve.py` | Full-supply price + tier curve → `config/price_curve.json` | **no (local)** |
| `analysis/plan_tiers.py` | Grades a budget plan → the tiers it actually buys | **no (local)** |
| `config/price_curve.json` | What each board rank / tier really costs | no (local) |
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
| `csg` | CSG sheet → `csg_consensus.json` (market view; self-skips if absent) | `draft_sheets/extract_csg.py` |
| `simulate` | agent-auction strategy test (stdout; `--stress` adds `a19`) | `analysis/a18_agent_auction.py` |
| `build` | projections × league → `tool_data.json` | `draft_sheets/build_tool_data.py` |
| `inject` | template + data → `static/index.html` + `data.json` | (the golden-rule step) |
| `all` | local refresh: `calibrate` (if history) → `csg` (if sheet) → `build` → `inject` | — |

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
`config/tendencies.json` (`{name: {mult, conc, maxbuy}}` plus a reserved `_league_default`
entry). `build_tool_data.py` merges it per manager by name; a manager with **no** auction
history (a substitution, an expansion team) gets `_league_default` — the **league average**,
not a flat `1.0`. This matters: `1.0` models a newcomer as paying full projected value at
every position (~2.4× the league norm at QB here) with no max-buy, which inflates the
console's predicted competition. `config/league.json` `manager_labels` optionally renames a
manager on the board (ESPN reports the owner's real name; you may know a team by its team
name) — tendencies merge on the **relabelled** name. `calibrate.py` keys tendencies by the current
league's **scraped** manager names (bridging ESPN member GUIDs) so a returning manager whose
scraped display name drifted from their calibration identity (e.g. "Jon" vs "Jonathan") still
matches. The projection baseline
`elboberto_projections.json` is regenerated from the tracked `*_elboberto.xlsm` by
`extract_elboberto_master.py` (its fields are named `proj_value`/`start_vbd` — what the analysis
code reads); it feeds calibration/research **only**, never the console valuation.

For a brand-new league with no history, `all` skips `calibrate` and builds a neutral-opponent
console. The archived `analysis/research/a1..a17` (the scripts behind `reports/league_analysis.md`)
run with `PYTHONPATH=analysis python3 analysis/research/<script>.py`.

## In-season projections: the baseline is validated, and nothing else ships

**Default state: the baseline, unmodified.** `ros_points` is ESPN's projection recomputed
on the league's own scraped scoring. Every deviation from it routes through
`engine/projection_policy.py`, which REFUSES anything that has not been paid for. As of
2026-09-15 five corrections have been measured and all five are refused, so the console
runs the untouched vendor projection — by evidence, not by default.

**The evidence base (WS-8, `docs/backtest_charter.md`):**

| artifact | what it is |
|---|---|
| `scraping/backfill_weeks.py` | 39,020 paired (projection, actual) player-weeks — 2kdome 2018-25, chi-phi 2022-25, inlaws 2025. `pipeline.py backfill` |
| `analysis/backtest_projections.py` | accuracy, leave-one-season-out, per league/position/tier/week |
| `analysis/backtest_lineups.py` | 2,362 team-weeks replayed — what the projection is worth IN POINTS |
| `analysis/backtest_sources.py` | forward test of external sources as rankings; "insufficient data" until weeks accumulate |
| `analysis/registered_adjustments.py` | loads the measured results into the policy registry so the data console shows every refusal |

Run the lot with `python3 pipeline.py validate`. `pipeline.py week` re-runs `backfill`
every week, so the evidence stays current instead of frozen at the last manual run.

**The two-part bar — do not weaken it back to MAE.** An `Adjustment` needs a `Validation`
carrying BOTH `results` (accuracy: >= 2 leagues at n >= 500 held-out player-weeks, whole
seasons held out, beats baseline in every powered league, harms no underpowered league by
> 2%) AND `decision` (realized points from the lineup replay, positive in every league,
t >= 2 in at least two). The second half exists because four corrections beat the
baseline's MAE in **all three leagues** and then lost points: `week_decay` "improved" MAE
0.4% while moving **0% of lineups** (a within-week multiplier cannot reorder a week);
`positional_bias` cost chi-phi-american 0.12 pts/team-week at t = -2.31. Conformance test
[5] enforces both halves on every run.

**What is actually true about the baseline** (all three leagues, out of sample): MAE
5.3-5.9 points per player-week, bias ≈ 0 overall, but D/ST is under-projected by 0.9-1.4
and the 20+ tier over-projected by 0.4-0.8. Ranking by it beats what the manager really
started by +3.88 (2kdome) / +2.11 (chi-phi) / -0.31 (inlaws) points per team-week, against
a hindsight ceiling ~17-20 points higher. Correcting the known biases does not convert
into points — that has been tried and measured, not assumed.

**FantasyPros cannot be the baseline, on availability.** Its full-coverage product is a
RANKING (no points, so no `marginal`, no FAAB ceiling, no trade delta), and its projections
pages serve **ten players per position** (measured: `rb.php` 10 rows, `&max=200` changes
nothing, `ros-{pos}.php` is not a real page — it silently serves the QB table).
`scraping/sources/fantasypros_proj.py` keeps that finding and the top-10 cross-check; it is
registered but deliberately NOT in the default fetch order. The comparison that IS possible
is FantasyPros-as-a-ranking against ESPN's ordering, which `backtest_sources.py` runs
forward from the first archived snapshot — external sources publish no history, so
`scraping/sources/common.archive()` freezes one snapshot per source-week, FIRST WRITE WINS
(a snapshot taken later in the week has seen the results).

## Cross-season price normalization (read before touching any historical $)

**The nominal `auctionBudget` is not spending power.** 2020–2024 report $300, but that extra
$100 existed only to carry the keeper encoding (a keeper is recorded as a bid of `cost+$100`,
so the cap had to rise to fit it). Proof: total league spend is **~$2,400 in every season**,
$200-cap and $300-cap alike. `lib.effective_wallet(season)` — non-keeper dollars actually
spent per team — is the comparable denominator (~$199 full-supply, ~$170–188 keeper era).

A **second, independent** effect must not be conflated with it: keeper seasons removed 12
elite players from supply, pushing top-of-board prices **up** and mid-board **down**
(measured full-supply/keeper share ratio: 0.77–0.86 at ranks 1–8, 0.99–1.18 at ranks 11–15,
0.97 whole-distribution). So:

| quantity | treatment |
|---|---|
| top-of-board (max-buy, price curve, plan ceilings) | **`lib.FULL_SUPPLY_SEASONS` only** — rescaling keeper-era top prices is not enough, they encode absent scarcity |
| whole-distribution traits (positional `mult`, `conc`) | may pool all seasons **after** `lib.norm_cost` (~3% distortion) |

Canonical helpers live in `analysis/lib.py`: `regime`, `effective_wallet`, `norm_cost`,
`FULL_SUPPLY_SEASONS`, `KEEPER_SEASONS`. **Use them; do not re-derive.** Normalized:
`a18.build_agents` (mult wallet-normalized, `conc` keeper-excluded, `maxbuy` full-supply-only
and no longer +15%), `a5_draft_value`, `research/backtest_budget`, `research/strategy_search_v2`,
`price_curve`, `plan_tiers`.

**Two bugs this audit fixed, worth not reintroducing:** `build_agents`' `conc`/`maxbuy` loop
counted keeper picks (its docstring claimed otherwise), and every shape-replay `lineup()`
hardcoded a $200 wallet while shopping at raw prices.

## What the league actually pays (`analysis/price_curve.py` → `config/price_curve.json`)

Derived from `FULL_SUPPLY_SEASONS` (2017/18/19/25 — the regime 2026 repeats, `keeperCount=0`).
Board-rank curve: **#1 $77 · #2 $73 · #3 $71 · #5 $69 · #10 $61**. Tier ladder: RB1 $72 /
RB2 $60 / RB3 $59 ‖ RB4 $17 / RB5 $24 ‖ RB6 $6; WR1 $68 / WR2 $57 / WR3 $50 ‖ WR4 $26 /
WR5 $10; TE1 $33; **QB1 $32** (QB has re-priced — top QB went $19 in 2019 → $39 in 2025, so
the "elite QB is a steal" thesis is retired). Tiers 4–5 are the worst points-per-dollar on
the board: spend up or down, never in between. `analysis/plan_tiers.py` grades a plan against
this and flags trough money.

**Still honest about the limit:** *which* budget shape wins is **not** validated
out-of-sample — `research/strategy_search_v2.py` (regime-corrected) lands at **41%**, i.e.
worse than the coin-flip gate and below v1's 45%. An earlier 56% "validated" reading was an
artifact of normalizing by the nominal $300. No static shape is promoted; `config/plan.json`
remains a disciplined default.

## The CSG sheet: a complementary market view (never the valuation)

`extract_csg.py` reads the CSG workbook's `Overall` tab (header row 11, players from row
12; layout stable across v11–v14) into `csg_consensus.json`, keyed by season.
`build_tool_data.py` merges the current season onto each player as `p["mkt"]`
(`price`, `espn`, `ecr`, `boris`, `gold`, `advbd`, `bs`, `status`, plus a derived `edge` =
our `worth` − market price). Joined on a punctuation/suffix-insensitive name key
(`extract_csg.norm_name` — the single definition, imported by the builder; don't fork it).
Currently 238/264 players = **99% of the $ pool**; every miss is a $1 player.

**It is advisory and must stay that way.** `worth`/`vbd` remain recomputed from the scraped
ESPN scoring — CSG's own VBD/price columns are computed for *its* settings, so treating them
as valuation would silently regress league accuracy. The value is the *disagreement*: the
console shows `market $X (±N us)` under Worth and a ▲/▼ on the board when divergence is
material (≥$5 **and** ≥25%), and the advisor state carries `market_price`/`ecr` so it can
reason about consensus. As of the 2026 build our model runs ~12% above market on the top 16.

**Coverage is uneven per year — check `pipeline.py csg` output, never assume a column.**
BeerSheets (`bs_val`/`beer_tier`) is **0/385 in the 2026 sheet** (needs a manual paste into
the hidden `Beersheet Paste` tab) though populated for 2023–25; `PosScarcity` is empty in all
four; `AdjVBD` exists only from v14.1; Gold Score is top-of-board only (~45); NFL ranks are
empty in 2026.

**Settings:** run `python3 draft_sheets/check_csg_settings.py` — it diffs the sheet's
`League Info`/`Overall` settings against your scrape and prints the exact cells to change.
**Do not write these workbooks with openpyxl:** it cannot recalculate formulas and drops
this file's conditional-formatting and data-validation extensions, so the sheet would look
updated while every downstream VBD/price stayed cached at the old settings. Edit in Excel,
save, re-run the checker, then `python3 pipeline.py csg build inject`.

## Deploying (Railway)

`./deploy_railway.sh` (`--dry-run` to inspect first). The repo's GitHub remote is **public**
and the console payload (`draft_app/static/*`) + `config/briefing.md` are gitignored, so a
git-based deploy would ship an empty console with a generic advisor. The script instead
stages just the runtime surface — `server.py`, `requirements.txt`, `Procfile`,
`railway.json`, `static/`, `config/briefing.md` — into a temp dir and runs `railway up`
from there, so ESPN cookies, raw scrapes, the `.xlsm` and `analysis/` never leave the box.
It refuses to deploy if anything secret-shaped is staged.

Service env vars: `ANTHROPIC_API_KEY`, `CONSOLE_PASSWORD` (HTTP Basic on everything but
`/healthz`; **unset = the URL is public and `/api/advise` spends your credit**), and
`STRATEGY_BRIEFING_PATH=/app/config/briefing.md`.

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
