# WS-8 — Backtesting & tuning charter

Launches when WS-1..5,7 land. Verify **across all three leagues**, and report per league —
never pool them into one number.

## STATUS — 8a, 8b, 8c, 8e executed 2026-09-15

| task | state | result |
|---|---|---|
| 8a backfill | **DONE** | 39,020 paired player-weeks. 2kdome 2018-25, chi-phi 2022-25, inlaws 2025, plus 2026 wk1 in all three — exactly the availability table below. `scraping/backfill_weeks.py`, cached IMMUTABLE so re-runs are free. |
| 8b projection accuracy | **DONE** | Baseline MAE 5.31 / 5.87 / 5.70 pts per player-week; bias ≈ 0 overall. Five candidate corrections tested leave-one-season-out; four beat the baseline's MAE. `analysis/backtest_projections.py` |
| 8c lineup replay | **DONE** | 2,362 team-weeks replayed. Following the projection beats the manager by **+3.88 / +2.11 / −0.31** pts per team-week; manager regret is 16.8 / 20.4 / 15.2. **Every MAE-winning correction lost points.** `analysis/backtest_lineups.py` |
| 8e source value | **HARNESS READY, NO DATA** | Archive now freezes every source weekly (`common.archive`). First gradeable week arrives after 2026 wk2 plays. Reports "insufficient data" until then. `analysis/backtest_sources.py` |
| 8d acquisition tuning | not started | the FAAB curve already carries its own leave-one-season-out verdict |
| 8f trade model | not started | expect underpowered |

**The headline finding, and the reason the bar changed.** `positional_bias`,
`linear_recal`, `week_decay` and `form_blend` each beat the baseline's MAE in **all
three leagues** out of sample (+0.1% to +0.9%) — and each *lost* realized points when
the lineups they imply were actually started. `week_decay` "improved" MAE by 0.4%
while moving **0% of lineups**, because scaling a whole week uniformly cannot reorder
it. `positional_bias` cost chi-phi-american 0.12 pts/team-week at t = −2.31.

So `engine/projection_policy.py` now requires BOTH halves — accuracy (`results`) and
realized points (`decision`) — and an accuracy-only `Validation` is refused by
construction. A bar that reads MAE alone would have promoted four losers.

**Net result: no correction ships. The baseline is unmodified, on evidence.**

## The unlock (verified 2026-09-14)

The deep-history scrape on disk has **no** weekly data — `players.json` holds season totals
only, and `schedule` has team `pointsByScoringPeriod` but no rosters. That made the core
model look unbacktestable. It is not:

```
GET /apis/v3/games/ffl/seasons/{YEAR}/segments/0/leagues/{ID}
    ?view=mBoxscore&scoringPeriodId={WEEK}
```
returns, per matchup side, `rosterForCurrentScoringPeriod.entries[]` with
`lineupSlotId` (**what the manager actually started**) and per-player `stats` carrying BOTH
`src0 split1` (**actual** week points) and `src1 split1` (**projected** week points).
Example: Chase Brown, slot RB, projected 12.19, actual 8.3.

Use the `seasons/{year}` endpoint, **NOT** `leagueHistory` — leagueHistory returns no roster
detail (verified). Availability probed per league:

| league | weekly lineups+projections available |
|---|---|
| 2kdome | **2018-2025** (8 seasons) |
| chi-phi-american | **2022-2025** (4 seasons) |
| inlaws-outlaws | **2025 only** (1 season) |

**WS-8a must backfill this first** — it is the prerequisite for everything else.
~220 requests total (seasons x weeks x 3 leagues). Be a polite client; cache to
`ctx.raw("weeks/{season}_wk{N}.json")`; never re-fetch what is on disk.

## What is backtestable, and what is not

| model | ground truth | verdict |
|---|---|---|
| ESPN projection accuracy | actual vs projected per player-week | **strong** (~290k player-weeks) |
| `lineup.optimal_lineup` / `marginal_add` | actual lineups + actual points | **strong** — replayable |
| FAAB bid + `p_contested` | bids incl. losing bids, in transactions | **good** (2kdome 2019-25; chi-phi 2024-25) |
| priority claim/wait | claim outcomes | **fair** (2kdome 2018; chi-phi 2022-23; inlaws 2025) |
| tendency stability / shrinkage | seasons 1..N-1 predicting season N | **good** in 2kdome, **thin** elsewhere |
| trade evaluation | post-trade production | **weak** — 16-45 trades/season, one league only |
| **external sources** (FantasyPros, Boris Chen, FantasyCalc, Sleeper) | — | **NOT retroactively backtestable** — no historical archive is published. See 8e. |

## Tasks

### 8a — Historical weekly backfill (BLOCKING; run first, alone) — **DONE**
`scraping/backfill_weeks.py`; `python3 pipeline.py backfill`. Distils to
`ctx.raw("player_weeks.json")` so backtests never re-parse a megabyte of boxscore and
never need the network. Two traps it now encodes: `leagueHistory` returns no rosters
(use `seasons/{year}`), and a FUTURE week answers with projections and no actuals —
grading those scores every projection against zero. The first run did exactly that;
`completed_weeks()` and `tests/validation_smoke.py` exist so it cannot recur.

**Population caveat, carried in every artifact:** a boxscore holds ROSTERED players
only. This grades the projections managers were deciding on — the right population for
lineup and waiver questions, the wrong one for "ESPN's accuracy over all NFL players".

### 8b — Projection accuracy & calibration
ESPN projection vs actual, per position, week, and projection tier, per league.
Deliver: bias and MAE curves; is ESPN systematically high on some positions? does accuracy
decay in later weeks? is it worse in the 8-team PPR league than the 12-team half-PPR one?
**If a systematic bias exists, fit a correction and validate it out-of-sample (hold out a
whole season).** This directly improves `ros_points`, which everything else is built on.

**Done. Systematic biases DO exist and none of them is worth acting on.** Measured
across all three leagues: D/ST is under-projected by 0.9-1.4 pts/week; the 20+ point
tier is over-projected by 0.4-0.8; QB correlation is only 0.22-0.29 against RB's
0.45-0.55. Corrections fitted to those biases improve MAE and lose points (8c). The
biases are real; the vendor is not leaving points on the table in a way this data can
reach.

### 8c — Lineup / marginal-value model (the core claim) — **DONE**
`analysis/backtest_lineups.py`. The lineup shape comes from the week being replayed
(the slots that team actually filled) and slot eligibility is LEARNED from the
league's own lineups, so 2QB and superflex replay correctly without a position table.
**The central claim holds in the two leagues with real history**: ranking by the
projection captures 23% (2kdome) and 10% (chi-phi) of the gap between what the manager
started and perfect hindsight. In inlaws-outlaws (one season) it is −2% — not
distinguishable from noise at that sample, and reported as such.

Original task text:
Replay history: for each team-week, compare `optimal_lineup` against what the manager
actually started. Quantify **manager regret** (points left on the bench) — that is both a
validation of the solver and a genuinely useful per-manager tendency.
Then the real test: does `marginal_add` predict **realized** lineup improvement from
historical waiver adds? Regress predicted marginal gain against actual subsequent
contribution. If it does not predict, the product's central claim is wrong — say so.

### 8d — Acquisition model tuning
Fit and validate the FAAB curve and `p_contested` against held-out seasons. Tune the
shrinkage parameter in `calibrate_season.py` by cross-season predictive accuracy rather
than by taste. Validate the priority claim/wait model on the priority-regime seasons.
**Never pool across an acquisition-regime switch** (2kdome 2018->19, chi-phi 2023->24).

### 8e — Source value: build the forward-test harness — **HARNESS READY**
Both deliverables exist. Archiving is automatic inside `common.run_adapter` and
**freezes on first write** — a snapshot overwritten later in the week has seen the
results, which would turn a forecast into a postdiction. The harness grades sources as
RANKINGS (order the roster, start the lineup it implies, count the real points),
because that is the only like-for-like comparison available: FantasyPros publishes a
full ranking and no public full projection set.

**Checked, as the task asked: FantasyPros does NOT expose prior weeks, and its
projections pages serve ten players per position** (`rb.php` 10 rows, `wr.php` 10,
`flex.php` 10; `&max=200` changes nothing; `ros-{pos}.php` is not a real page and
silently serves the QB table). So FantasyPros cannot be a projection baseline at all —
not on accuracy grounds, on availability grounds. See
`scraping/sources/fantasypros_proj.py`, which keeps the top-10 cross-check and the
record that this avenue was measured and closed.

Original task text:
External rankings publish only CURRENT values — there is no free historical archive, so
their incremental value **cannot** be measured retroactively. Two deliverables:
1. **Start archiving now**: snapshot every source weekly to
   `ctx.raw("sources/archive/{name}_{season}_wk{N}.json")`. Without this the question stays
   permanently unanswerable.
2. A ready harness that, once weeks accumulate, answers: **does any source add predictive
   power over ESPN's projection alone?** (nested model comparison, out-of-sample). Until it
   has data it must report "insufficient data", never a number.
   Check whether FantasyPros exposes prior weeks via a `week=` parameter; if it does,
   backfill the current season only, and say plainly that earlier seasons are unavailable.

### 8f — Trade model (expect a null result; report it honestly)
Historical executed trades: did `trade_delta` predict which side gained? Sample is small
(16-45/season, 2kdome only). **The correct output here is very likely "underpowered".**
Report the confidence interval, not a point estimate dressed as a finding.

## Reporting rules — non-negotiable

These follow the precedent in CLAUDE.md, where a "validated 56%" reading turned out to be an
artifact of normalising by a nominal budget:

1. **Per league, always.** Never a single pooled number across leagues that differ in size,
   scoring, lineup and waiver type.
2. **Out-of-sample or it does not count.** Hold out whole seasons, never random rows —
   adjacent weeks leak.
3. **Beat a stated baseline** (ESPN projection as-is; league-mean tendency; "bid $0"), and
   print the baseline's score next to the model's.
4. **A null result is a deliverable.** If a model does not beat baseline, ship that finding
   and leave the model unpromoted. Do not tune until something passes.
5. **State the power.** Inlaws-outlaws has ONE season — most questions are simply not
   answerable there. Say "not validatable", never a number with false precision.
6. Write `validation` blocks into the JSON artifacts so the data console can surface
   whether each model beat baseline.

## The projection bar — enforced in code, not by policy

`engine/projection_policy.py` makes "no unvalidated deviation from baseline" structural.
Baseline = ESPN's projection recomputed on the league's own scoring. Any deviation is a
claim that we out-model a vendor with a large modelling team, and must be paid for.

An `Adjustment` is REFUSED at apply time unless its attached `Validation` clears all of:

1. **Validated in >= 2 leagues** with `n >= 500` held-out player-weeks (`MIN_POWERED_LEAGUES`,
   `MIN_N`). One league is never enough — that is how overfitting looks.
2. **Beats baseline in EVERY powered league.** An effect that helps 2kdome and hurts Chi Phi
   is not an effect; it is noise with a favourable slice.
3. **Does not harm an underpowered league** by more than `HARM_TOLERANCE` (2%). Inlaws has one
   season — it cannot confirm anything, but it can still veto.
4. **Whole SEASONS held out**, never random rows. Adjacent weeks share injuries, roles and
   defensive matchups, so row-level splits leak and will manufacture a false pass.

With an empty registry — or a registry full of unvalidated ideas — the system returns the
baseline unchanged. That is the intended default, and `engine/conformance.py` test [5]
enforces it on every run.

**Every WS-8 task must express its tuning as a registered `Adjustment` with a `Validation`.**
Do not hand-apply a correction anywhere in the pipeline. `projection_policy.refused()` lists
rejected tunings with reasons; the data console must surface them — **a rejected tuning is a
finding worth showing, not a failure to hide.**

Applies equally beyond projections: FAAB curve, `p_contested`, shrinkage, and any source
blend must clear the same bar before they influence a recommendation.

## Finding to validate rigorously: the run-timing effect (measured 2026-09-14)

The single strongest waiver signal found so far is **when the claim processes**, not who is
claimed. All three leagues process waivers SIX days a week (Tuesday is the only skip), so
"weekly waivers" is the wrong mental model — each run is a separate, differently-contested
market.

2kdome, 9 seasons, 2,591 runs (run = one player in one `processDate` bucket; contested =
more than one DISTINCT claiming team):

| day | runs | share | contested | mean winning bid |
|---|---|---|---|---|
| Wed | 1234 | 47.6% | **38.5%** | **$5.75** |
| Thu | 555 | 21.4% | 18.6% | $1.99 |
| Sun | 328 | 12.7% | 9.5% | $0.97 |
| Fri | 253 | 9.8% | 13.0% | $1.30 |
| Sat | 198 | 7.6% | 12.1% | $1.66 |
| Mon | 23 | 0.9% | 8.7% | $0.00 |

Wednesday 38.5% contested vs 14.2% all other days = **2.71x**. Cross-league, same direction:
chi-phi-american 92% of runs on Wed, 9.8% vs 0.0%; inlaws-outlaws 88% of runs, 29.2% vs 6.7%
(4.38x). Overall contested rate 25.8%, which independently cross-checks WS-3's 28%.

This MATTERS because WS-3 showed the player-quality contest model has AUC 0.564 — no skill.
Timing carries the signal that player features do not.

**WS-8 must test this properly**, because the descriptive numbers above are not a validated
model:
1. Does day-of-week + week-of-season beat the league base rate out-of-sample (season holdout)?
   Report Brier/AUC against the 0.258 base rate, per league.
2. Is the effect separable from week-of-season, or is Wednesday simply where volume lands?
3. chi-phi (122 runs) and inlaws (128 runs) are thin — well under `MIN_N`. Report power
   honestly rather than promoting on three consistent directions.
4. Test the actionable claim directly: **does waiting for a later run actually save money
   without losing the player?** Ground truth exists — the same player, claimed on different
   days, at different prices. This is the decision the tool will make, so it is the one to
   validate.

Until validated, the engine uses the EMPIRICAL per-league, per-day rate (a descriptive
statistic with its n, not a tuning) — that does not require the projection-policy bar, but it
must be labelled as measured, not modelled.
