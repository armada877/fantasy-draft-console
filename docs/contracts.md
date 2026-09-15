# Frozen contracts — read this before writing any in-season code

Every workstream codes against these. **Do not change a contract unilaterally** — if one
is wrong, say so and stop, don't "fix" it locally. Owner of this file: the coordinating
session.

## Hard guardrails (apply to every workstream)

1. **Do not modify** any of: `draft_sheets/draft_tool_template.html`,
   `draft_sheets/build_tool_data.py`, `draft_sheets/extract_csg.py`, `analysis/lib.py`,
   `analysis/calibrate.py`, `draft_app/static/index.html`, or the existing stages in
   `pipeline.py`. The draft console is shipped and working; the in-season tool is additive.
   *Exception*: you may ADD new stages / new functions, never alter existing behaviour.
2. **Before you finish, both must pass:**
   ```
   python3 tests/draft_regression.py      # draft console byte-identical
   python3 -m engine.conformance          # engine still generic + solver optimal
   ```
3. **No hardcoded league facts.** No `12`, `$200`, `$100`, `["QB","RB","WR","TE"]`,
   14-week seasons, PPR assumptions. Everything comes off `LeagueProfile`. Three real
   leagues differ on team count (8/12/14), scoring (PPR/half), lineup (1QB vs 2QB, K vs
   no-K), waivers (FAAB vs priority order) and draft type (auction vs snake).
4. **Each league is a separate context.** Never let one league's data, tendencies, or
   briefing influence another. Paths come from `LeagueContext`, never string-built.
5. **External sources self-skip.** A third-party outage must degrade the output, never
   fail the pipeline. Print coverage like the CSG builder does
   (`matched 238/264 = 99% of the $ pool`).
6. **Advisory vs valuation.** `ros_points` / `marginal` are computed from the league's own
   scraped scoring. External rankings are advisory context only — surface the
   *disagreement*, never substitute them for the valuation.
7. League-specific outputs are gitignored; reusable CODE is tracked.

## Already built and stable — import, don't reimplement

### `engine/profile.py`
```python
LeagueProfile.from_espn(payload) -> LeagueProfile    # payload = league_full.json
  .size .scoring{statId:pts} .lineup_slots{slotId:count} .bench .ir
  .starting_slots  .starters_per_team  .roster_size
  .scoring_label            # "PPR" | "half-PPR" | "standard"  (DERIVED — use to pick
                            #  the right FantasyPros / Boris Chen file)
  .acquisition              # Acquisition(model=FAAB|PRIORITY, budget, min_bid,
                            #   continuous, waiver_hours, order_resets, limit)
  .draft_type .auction_budget .keeper_count
  .regular_weeks .playoff_teams .playoff_weeks .trade_deadline_ms .veto_votes
  .summary() -> str
slot_name(id) position_name(id) BENCH_SLOTS FAAB PRIORITY
```

### `engine/lineup.py` — the primitive everything reduces to
```python
Player(id, name, pos, eligible_slots: frozenset, points_per_game, bye_week,
       out_weeks, team, injury, owner)
  .points_in(week)                     # 0 on bye / out week

optimal_lineup(players, profile, week=None) -> (total, {slot_id:[Player]}, bench)
lineup_points(players, profile, week=None) -> float
lineup_points_over(players, profile, weeks) -> float        # bye-aware
marginal_add(roster, candidate, profile, weeks, must_drop=True) -> (gain, dropped)
trade_delta(roster, send, receive, profile, weeks) -> float
starter_replacement_level(pool, profile) -> {slot_id: points}
```
Solver is a max-weight bipartite assignment, brute-force verified across 6 league shapes.
**Use it for every value question.** A waiver add is worth its `marginal_add`, not its points.

### `leagues.py`
```python
leagues.all()            -> [LeagueContext]      # auto-discovered from ESPN cookies
leagues.resolve("2kdome")-> LeagueContext
leagues.default()        -> LeagueContext        # the legacy single-league context
ctx.key .name .league_id .season .team_id .team_name
ctx.raw("x.json") ctx.config("x.json") ctx.out("x.json")   # ALWAYS use these
ctx.raw_dir ctx.config_dir ctx.out_dir ctx.static_dir ctx.ensure_dirs()
ctx.profile() -> LeagueProfile
```
Current keys: `2kdome` (legacy layout: `raw/{season}/`, `config/`),
`chi-phi-american`, `inlaws-outlaws` (namespaced: `raw/{key}/{season}/`,
`config/leagues/{key}/`).

## Artifacts — the interfaces between workstreams

### WS-1 produces: `ctx.raw("league_full.json")`
ESPN league payload, views `mSettings,mTeam,mRoster,mMatchup,mStatus,mDraftDetail`.
Must be **fresh** (the checked-in 2kdome copy is a stale pre-draft snapshot).

### WS-1 produces: `ctx.raw("players_wk{N}.json")`
`kona_player_info` for free agents + waivers + rostered, with `ownership`,
`injuryStatus`, and `stats`. Stat decoding (verified live; id is
`{statSourceId}{statSplitTypeId}{season}`):
| entry | meaning |
|---|---|
| `src0 split0` | season actual to date |
| `src1 split0` | **full-season projection** (live, updates weekly) |
| `src1 split1 sp=W` | week-W projection (current week only; future weeks 400) |
| `src1 split2` | **NOT rest-of-season — do not use** (unexplained; Gibbs 347 > his 338 season) |
`ros_points = proj(src1,split0) - actual(src0,split0)`. `appliedTotal` is already scored
for the league (verified exact), but recompute from raw `stats` x `profile.scoring` anyway
so a settings change propagates.

### WS-1 produces: `ctx.raw("transactions.json")` — all seasons, per league.

### WS-2 produces: `ctx.raw("sources/{name}.json")`
```json
{"source":"fantasypros","fetched":"<iso8601>","season":2026,"week":2,
 "scoring":"half-PPR","ok":true,"error":null,
 "coverage":{"matched":238,"total":264,"pct_of_pool":0.99},
 "records":[{"name":"Jahmyr Gibbs","norm":"jahmyrgibbs","pos":"RB","team":"DET",
             "espn_id":4430807,
             "fields":{"ros_ecr":1,"rank_min":1,"tier":1,"bye":6}}]}
```
`norm` MUST come from `from extract_csg import norm_name` — the single shared join key,
imported never forked. Prefer `espn_id` (nflverse `players.csv`/`depth_charts_*.csv` carry
it); fall back to `norm`. `ok:false` + `error` when a source is down — never raise.

### WS-3 produces: `ctx.config("season_tendencies.json")`
```json
{"_meta":{"seasons":[2019,2025],"acquisition_regimes":{"2018":"priority","2019":"faab"},
          "method":"shrinkage-to-league-mean"},
 "_league_default":{...same shape as a manager...},
 "Manager Name":{
   "n_seasons":3,"shrink":0.42,"borrowed":false,
   "waiver":{"adds_per_season":48,"aggression":0.9,"contested_win_rate":0.55,
             "zero_bid_share":0.64,"spend_pace":[...by week...],"max_bid":30},
   "trade":{"rate_per_season":2.1,"accept_rate":0.6,"partners":{"X":3},
            "pos_flow":{"RB":0.3,"WR":-0.2},"responds":true},
   "drop":{"latency_weeks":1.8}}}
```
`borrowed:true` when a manager has no history and got the league mean — the UI must show
calibrated and borrowed numbers differently.

### WS-3 produces: `ctx.config("faab_curve.json")` (FAAB leagues) /
`ctx.config("priority_curve.json")` (priority leagues)
```json
{"model":"faab","seasons":[2019,2025],"n_claims":2100,
 "by_quality":[{"pct":0.95,"median_win":22,"p80_win":34,"p_contested":0.62,"n":41}],
 "by_week":[{"week":2,"median_win":14,"share_of_budget_spent":0.18}],
 "validation":{"holdout_mae":6.1,"baseline_mae":9.4,"n_holdout":180}}
```
Priority variant: `p_survives_to_free_agency` by quality instead of bid levels.
**Report the out-of-sample validation honestly** — `price_curve.py` /
`strategy_search_v2.py` set the precedent; do not promote an unvalidated rule.
Never pool across an acquisition-regime switch (2kdome 2018->19, Chi Phi 2023->24).

### WS-4 produces: `ctx.out("season_data.json")` — what the cockpit runs on
```json
{"generated":"<iso8601>","week":2,
 "league":{"key","name","season","size","scoring_label","regular_weeks","playoff_teams",
           "playoff_weeks":[15,16,17],          // REQUIRED — the POST horizon; do not assume 3
           "trade_deadline","veto_votes",
           "draft_type":"AUCTION|SNAKE","auction_budget":200|null,  // launcher card shows these
           "bench":5,"ir":2,                    // so the cockpit can show bench utilisation
           "acquisition":{"model","budget","min_bid"},
           "lineup_slots":{"0":1},"slot_names":{"0":"QB"}},
 "me":{"team_id":11,"name":"...","manager":"..."},
 "teams":[{"team_id","name","manager","record":{"w","l","t"},"points_for",
           "faab_left","waiver_priority","roster":[<player_id>],
           "needs":{"<slot_id>":<count_short>},"playoff_odds":0.42,   // REQUIRED — a cockpit vital
           "tendencies":{...},"borrowed":false}],
 "players":{"<id>":{"id","name","pos","team","eligible_slots":[2,23],
            "owner_team_id":null,"injury","bye","ros_points","ros_ppg","actual_ppg",
            "last3_ppg","pct_owned","pct_owned_change",
            "mkt":{"ros_ecr","tier","trend_adds","trade_value","snap_share","target_share"}}},
 "waivers":[{"player_id","marginal_reg","marginal_post",
             "drop_player_id",   // relative to `me` ONLY. The cockpit's team switcher
                                 // recomputes it per-team; producers need not.
             "bid":{"suggested","p80","p_contested"},
             "claim":{"recommend":true,"p_survives":0.2},"why":["..."]}],
 "trades":{"finder":[{"partner_team_id","send":[id],"receive":[id],
                      "my_delta","their_delta","accept_odds","veto_risk","why":["..."]}],
           "buy_low":[{"player_id","owner_team_id","why":["..."]}],
           "sell_high":[{"player_id","why":["..."]}]}}
```
`bid` present only in FAAB leagues, `claim` only in priority leagues — driven by
`profile.acquisition.model`, never by league name.

### WS-5 consumes `season_data.json` only
Frontend source is `draft_sheets/season_tool_template.html` + `home_template.html`; the
served files are GENERATED. The launcher renders to **`draft_app/static/home.html`**, served
at `/` — NOT to `static/index.html`, which `pipeline.py inject` owns for the draft console and
which `tests/draft_regression.py` hashes. Writing the launcher there would make the gate
clobber it. **The golden rule holds: edit the template, then re-inject.**
Never hand-edit anything under `draft_app/static/`.

---

## WS-7 artifacts — the data/pipeline console

### `ctx.out("pipeline_status.json")` — produced by the status collector, consumed by the UI
```json
{"league":"2kdome","season":2026,"week":2,"generated":"<iso8601>",
 "stages":[{"name":"season-scrape","ok":true,"last_run":"<iso8601>","age_hours":2.1,
            "stale":false,"summary":"12 teams, 168 draft picks, 14-man rosters",
            "outputs":[{"path":"scraping/raw/2026/league_full.json","bytes":812344,
                        "mtime":"<iso8601>","rows":null}],
            "error":null,"duration_s":18.3}],
 "sources":[{"name":"fantasypros","ok":true,"fetched":"<iso8601>","age_hours":1.2,
             "variant":"half-point-ppr","coverage":{"matched":271,"total":300,"pct":0.90},
             "error":null}],
 "calibration":{"managers_total":12,"managers_borrowed":3,"seasons_used":[2019,2025],
                "excluded_seasons":{"2018":"priority-order regime"},
                "curve":{"model":"faab","validated":true,"holdout_mae":6.1,
                         "baseline_mae":9.4,"beats_baseline":true}},
 "warnings":["inlaws-outlaws: 1 season of history; 4 of 14 managers borrowed"]}
```
`stale` is computed per stage against a declared max age (waivers move daily; calibration
is seasonal). Freshness is the console's primary job — a confident recommendation built on
week-old projections is the failure mode this UI exists to prevent.

### Run-trigger API — `POST /api/pipeline/run`
`{"league":"2kdome","stage":"season-scrape"}` -> `{"job_id":...}`, streamed/pollable log.

**Guardrail:** executing pipeline stages from a browser is a remote-code surface. It MUST be
gated behind an explicit env flag (`PIPELINE_RUN_ENABLED=1`), default OFF, so the Railway
deployment is read-only unless deliberately enabled. Stage names must be validated against a
fixed allow-list — never interpolate user input into a shell command. Reuse the existing
HTTP Basic gate; never weaken it.

### Provenance contract — how any number explains itself
Every recommendation surfaced anywhere in the app must be traceable to its inputs. The
console renders a `why` for any player or recommendation:
```json
{"player_id":4430807,"value":{"ros_points":261.4,"ros_ppg":17.4,"marginal_reg":31.2},
 "inputs":{"espn_season_proj":338.1,"espn_actual":31.1,"games_remaining":15,
           "recomputed_from_scoring":true,"bye":6},
 "market":{"fantasypros_ros_ecr":1,"borischen_tier":1,"fantasycalc_value":8800},
 "disagreement":{"vs_ecr_pct":+12.0},
 "league_context":{"replacement_at_slot":8.9,"teams_who_gain":[3,7,11]}}
```

### Research console — interpreting the sources

**Disagreement is NOT uniformly an edge in-season.** It monetizes only where a market sets a
price or a counterparty sets terms. Label every divergence by which regime it is in — a
single undifferentiated "disagreement" table would actively mislead on start/sit.

| context | is disagreement an edge? | why |
|---|---|---|
| **FAAB waivers** | **YES — strongest** | A blind-bid auction with a budget: structurally the same mechanism as the draft. Value a player above the room and you win him cheap. |
| **Trades** | **YES, but** | The disagreement that pays is with **that specific manager**, not national consensus. We have `season_tendencies.json` (calibrated positional bias) — prefer it. Consensus is a fallback proxy for a counterparty we cannot model, plus the negotiation anchor people actually cite. |
| **Priority waivers** | **Weak** | No price, so no surplus to capture — you burn your position or you don't. Consensus informs TIMING (will he clear?), not value. |
| **Start / sit** | **NO — inverted** | No market, no price, no counterparty. Pure prediction. ECR exists because averaging ~100 analysts cuts variance, so a sharp divergence means CHECK YOUR INPUTS (bad join, stale projection, missed injury), not "found alpha". Render it as a WARNING, never as an opportunity. |

**The sources' biggest in-season job is predicting RIVAL DEMAND, not re-pricing players.**
FantasyPros ECR, Sleeper trending adds, and ESPN `pct_owned_change` are the best available
estimators of `p_contested` — how many rivals want this player this week. That feeds the FAAB
bid and the claim/wait call directly, and it is a use with no draft-console analogue. Weight
it accordingly in the UI.

**Honest limit, state it in the console.** In-season we have no independent projection source
— ESPN's projection IS our backbone (docs/local/inseason_plan.md 8). So "our ros_ppg vs FantasyPros
ECR" is largely *ESPN vs FantasyPros*, not our model vs the market. It is a vendor
cross-check, and must be labelled as one rather than implying proprietary edge.

What that backbone is actually worth is now measured rather than assumed: replaying every
historical team-week, ranking by it beats what the manager really started by +3.88 / +2.11 /
−0.31 points per team-week, against a hindsight ceiling 16.8 / 20.4 / 15.2 points above the
manager. That is the product's central claim, and it holds in the two leagues with enough
history to test it.

**Where the real in-season edge lives** — surface this ABOVE any consensus comparison:
`marginal` value to a specific roster. A 14-ppg player is worth a lot to a team with a hole
and ~nothing to one stacked at that slot. No national consensus prices that, because it is
roster-specific — and neither can any rival's generic ranking. Bye-week coverage and
playoff-week (weeks 15-17) schedule are the same kind of edge.

The console must therefore make inspectable:

- **Source registry**: every adapter — live/failed, last fetch + age, coverage
  (matched/total), and WHICH VARIANT it pulled for this league (a half-PPR 12-team league and
  a PPR 8-team 2QB league must visibly fetch different FantasyPros / Boris Chen / FantasyCalc
  files; showing that is how the user confirms per-league correctness).
- **Rival-demand view (primary)**: consensus rank, trending adds, and `pct_owned_change` as
  inputs to `p_contested`, next to the resulting bid / claim recommendation.
- **Divergence table (secondary, regime-labelled)**: our `ros_ppg` / `marginal` vs
  FantasyPros `ros_ecr`, Boris Chen `tier`, FantasyCalc `trade_value`. Each row tagged
  opportunity (FAAB/trade) or warning (start/sit) per the table above.
- **Coverage gaps**: who we failed to join, and what share of the value pool they represent
  (the draft builder's convention: "238/264 = 99% of the $ pool" — a miss that is all $1
  players does not matter, a miss on a starter does).
- **Tier-B allow-list**: browse `config/research_sources.json` (WS-2; may be absent —
  degrade), showing what the advisor is permitted to fetch live, by category, with a
  test-fetch button.
- **Research trace**: when the advisor does live research, show what it fetched, what it
  cited, and the added latency — so live research is auditable rather than magic.

Every row links to the per-player provenance `why` object above. A number the user cannot
trace back to its inputs is a bug in this console.

## Projection policy — no unvalidated deviation from baseline

Baseline = ESPN's projection recomputed on the league's own scraped scoring. It ships
unmodified unless a deviation is validated out-of-sample ACROSS LEAGUES.

**Why ESPN and not FantasyPros (settled 2026-09-15, availability not preference).** A
baseline has to be in POINTS and has to cover the whole pool — every free agent a waiver
claim might touch. FantasyPros publishes its consensus as a RANKING (~408 players, no
points; an ordering cannot produce `marginal`, a FAAB ceiling or a trade delta), and its
projections pages — which do carry re-scorable component stats — serve **ten players per
position** behind the paywall. Measured, not assumed; `scraping/sources/fantasypros_proj.py`
carries the evidence. Where FantasyPros CAN be compared is as a ranking, and that is
`analysis/backtest_sources.py` (forward-only: no source publishes an archive).

**The bar has two halves and the second is not optional.** WS-8 scored five candidate
corrections on 39,020 held-out player-weeks. Four beat the baseline's MAE in all three
leagues — and lost realized points when `analysis/backtest_lineups.py` replayed 2,362
team-weeks and started the lineups they implied. `week_decay` "improved" MAE 0.4% while
moving 0% of lineups. So a `Validation` must carry BOTH `results` (accuracy) and
`decision` (realized points from the replay, with a paired t over team-weeks); an
accuracy-only Validation is REFUSED by construction, and conformance test [5] enforces it.

Use `engine/projection_policy.py`. Never hand-apply a correction factor anywhere in the
pipeline; express it as a registered `Adjustment` carrying a `Validation`, and let the module
decide whether it applies. The bar (enforced by `engine/conformance.py` test [5]):
>= 2 leagues with n >= 500 held-out player-weeks, beats baseline in EVERY powered league,
harms no underpowered league by > 2%, whole seasons held out rather than random rows —
AND adds realized points in the lineup replay, in every league, significant at t >= 2 in
at least 2 of them.

`projection_policy.refused()` returns rejected tunings with reasons — surface them in the
data console. The same bar applies to the FAAB curve, `p_contested`, shrinkage, and any
external-source blend.

## Corrections (found by workstreams; the contract was wrong)

1. **`norm` is SPACE-JOINED.** `extract_csg.norm_name("Jahmyr Gibbs")` -> `"jahmyr gibbs"`, not
   `"jahmyrgibbs"`. The earlier WS-2 example was wrong; the function is authoritative.
2. **D/ST needs a supplementary join key.** ESPN writes "Texans D/ST"; FantasyPros, Boris Chen
   and FantasyCalc write "Houston Texans". Use `scraping/sources/common.dst_key()`. Join order
   is `espn_id` -> `norm` -> `dst_key`. Without it each league drops ~16 defenses — a position
   all three leagues start and stream weekly.
3. **`LeagueProfile.scoring` is insufficient on its own.** ESPN scoring items also carry
   `pointsOverrides` keyed by position; all three leagues use them for D/ST. Flat scoring
   mis-scores ~32 players per league. Use `engine/valuation.py`'s `effective_scoring`.
4. **`ctx.raw("transactions.json")` has two shapes.** WS-1's bundle
   (`{league_id, seasons, counts, transactions:[{…, seasonId}]}`) is CANONICAL going forward;
   2kdome's archived per-season bare lists are legacy. Readers must handle both.
5. **`/leagueHistory` does not serve `mTransactions2`** — HTTP 200 with the key absent, which
   reads as a quiet season. Only `/seasons/{year}/…` serves transactions. Same endpoint caveat
   applies to weekly boxscores.
6. **Waiver loser-status is era-dependent, not regime-dependent.** 2kdome 2018 files losers as
   `FAILED_MATCHUPACQUISITIONLIMIT` (111 rows, no `FAILED_INVALIDPLAYERSOURCE` at all);
   later seasons use `FAILED_INVALIDPLAYERSOURCE`. Only a non-zero `bidAmount` reliably
   identifies FAAB.
7. **2018 EXECUTED waivers carry `teamId = -2147483648`** — attribute from the ADD item's
   `toTeamId` instead. Verified: all 134 rows.
8. **A waiver "run" is a `processDate` bucket, not a `scoringPeriodId`.** Bucketing by
   processDate yields exactly one winner across all 2,075 contests; scoring-period grouping
   breaks on 3.
9. **FAAB is not a hard $100.** `acquisitionBudget` is an end-of-season snapshot; 8 team-seasons
   in 2kdome exceed it. Use the measured effective wallet (~$94.2/team).
10. **Correction to an earlier brief: 8 of 12 teams spent exactly $100 in 2025, not 9.**
    Verified: [100,100,100,100,100,100,100,100,96,86,65,50].
