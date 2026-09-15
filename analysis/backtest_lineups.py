#!/usr/bin/env python3
"""WS-8c — replay every historical team-week and ask what the projection was WORTH.

MAE is not the product. Nobody starts a mean absolute error. The console's claim is
that ranking players by projection produces better STARTING LINEUPS, so the honest
test replays the decision: take each team's actual roster in each actual week, pick
a legal lineup using nothing but the information available at the time, and score it
with what really happened.

Four lineups per team-week, and the gaps between them are the whole report:

    manager      what the manager really started (`lineupSlotId` from the boxscore)
    baseline     what ESPN's projection would have started
    candidate    what a corrected projection would have started (one per candidate)
    hindsight    the best legal lineup in hindsight — the ceiling nobody reaches

`hindsight - manager` is **manager regret**: points left on the bench, per week, per
manager. `baseline - manager` is what the console is worth if you follow it.
`candidate - baseline` is the only number that can justify deviating from the
vendor baseline, and it is measured in POINTS, which is the unit decisions are
actually made in — a correction that improves MAE by 0.5% and moves no lineup at all
has bought nothing.

Two things are derived from the league's own history rather than assumed, because
both changed across the seasons being replayed:

  * **The lineup shape** comes from the week itself — the slots that team actually
    filled that week. Replaying 2018 against 2026's roster settings would score a
    lineup the league did not play.
  * **Slot eligibility** is learned per league: a position may fill a slot if some
    manager in this league's history ever started that position there. No position
    table is written down, so FLEX, OP and 2QB fall out of the data.

Out-of-sample discipline is identical to WS-8b: whole seasons held out, corrections
fitted only on other seasons (or, for a single-season league, on the other leagues).

Run:
    python3 analysis/backtest_lineups.py
    python3 analysis/backtest_lineups.py --league 2kdome --by-season
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import leagues                                                    # noqa: E402
from analysis.backtest_projections import (ACT, CANDIDATES, PID,  # noqa: E402
                                           POS, PROJ, SEASON, SLOT, TEAM, WEEK,
                                           load_rows)
from engine import projection_policy as pp                        # noqa: E402
from engine.lineup import Player, optimal_lineup                  # noqa: E402
from engine.profile import (Acquisition, BENCH_SLOTS, FAAB,       # noqa: E402
                            LeagueProfile, position_name, slot_name)

MIN_STARTERS = 5          # a team-week with fewer filled seats is a data artifact


def shim_profile(slot_counts: dict) -> LeagueProfile:
    """A LeagueProfile carrying ONE real fact: the slots this team-week actually
    filled. Everything else is inert — `optimal_lineup` reads `starting_slots` and
    nothing more, and inventing settings we do not have would be a lie the solver
    cannot use anyway."""
    return LeagueProfile(
        league_id=0, season=0, name="replay", size=0, scoring={},
        lineup_slots=dict(slot_counts), bench=0, ir=0,
        acquisition=Acquisition(FAAB, 100, 0, True, 24, True, -1),
        draft_type="", auction_budget=None, keeper_count=0, regular_weeks=0,
        playoff_teams=0, trade_deadline_ms=None, veto_votes=0)


def learn_eligibility(rows) -> dict:
    """{position_id: frozenset(startable slot ids)} from this league's own lineups.

    A position may fill a slot because somebody in this league started it there —
    that is how FLEX, OP and superflex get discovered without a table. The player's
    own `eligibleSlots` would be better, but a historical boxscore does not carry it.
    """
    seen = defaultdict(set)
    for r in rows:
        slot = int(r[SLOT])
        if slot in BENCH_SLOTS or slot < 0:
            continue
        seen[int(r[POS])].add(slot)
    return {p: frozenset(s) for p, s in seen.items()}


def team_weeks(rows):
    """Group into (season, week, team_id) -> [row, ...]."""
    out = defaultdict(list)
    for r in rows:
        out[(int(r[SEASON]), int(r[WEEK]), int(r[TEAM]))].append(r)
    return out


def _players(rows, elig, score_of):
    return [Player(id=int(r[PID]), name=str(r[PID]),
                   pos=position_name(r[POS]),
                   eligible_slots=elig.get(int(r[POS]), frozenset()),
                   points_per_game=float(score_of(r)))
            for r in rows]


def realized(rows, elig, slot_counts, score_of, actual_by_id):
    """Pick a lineup by `score_of`, return what it REALLY scored."""
    prof = shim_profile(slot_counts)
    _, by_slot, _ = optimal_lineup(_players(rows, elig, score_of), prof)
    return sum(actual_by_id.get(p.id, 0.0)
               for pls in by_slot.values() for p in pls)


def replay(rows, elig, candidates_applied):
    """Every team-week -> {lineup name: realized points}.

    `candidates_applied` is {name: fn(row) -> adjusted projection}, already fitted
    on data that excludes this row's season.
    """
    out = []
    for (season, week, team), group in sorted(team_weeks(rows).items()):
        started = [r for r in group if int(r[SLOT]) not in BENCH_SLOTS and int(r[SLOT]) >= 0]
        if len(started) < MIN_STARTERS:
            continue
        slot_counts = defaultdict(int)
        for r in started:
            slot_counts[int(r[SLOT])] += 1
        actual_by_id = {int(r[PID]): float(r[ACT]) for r in group}
        rec = {
            "season": season, "week": week, "team": team,
            "manager": sum(float(r[ACT]) for r in started),
            "baseline": realized(group, elig, slot_counts,
                                 lambda r: float(r[PROJ]), actual_by_id),
            "hindsight": realized(group, elig, slot_counts,
                                  lambda r: float(r[ACT]), actual_by_id),
        }
        for name, fn in candidates_applied.items():
            rec[name] = realized(group, elig, slot_counts, fn, actual_by_id)
        out.append(rec)
    return out


def fit_candidates(train_rows, train_form, form):
    """{name: fn(row) -> adjusted projection}, each fitted on `train_rows` only."""
    applied = {}
    for name, fit, apply_fn, _ in CANDIDATES:
        params = fit(train_rows, train_form)
        applied[name] = (lambda r, _a=apply_fn, _p=params: _a(r, _p, form))
    return applied


def league_replay(key, rows, form, per_league):
    """Leave-one-season-out replay for one league."""
    seasons = sorted({int(r[SEASON]) for r in rows})
    elig = learn_eligibility(rows)
    recs = []
    for s in seasons:
        test = [r for r in rows if int(r[SEASON]) == s]
        train = [r for r in rows if int(r[SEASON]) != s]
        train_form = form
        if len(train) < 300:                      # single-season league: borrow the others
            train, train_form = [], {}
            for k2, (r2, f2, _) in per_league.items():
                if k2 == key:
                    continue
                train.extend(r2)
                train_form.update(f2)
        if not train:
            continue
        recs.extend(replay(test, elig, fit_candidates(train, train_form, form)))
    return recs


def summarise(recs, names):
    """Mean realized points per team-week for each lineup, plus the gaps."""
    if not recs:
        return {}
    out = {"team_weeks": len(recs)}
    for nm in ["manager", "baseline", "hindsight"] + names:
        out[nm] = round(statistics.fmean(r[nm] for r in recs), 3)
    out["manager_regret"] = round(out["hindsight"] - out["manager"], 3)
    out["baseline_vs_manager"] = round(out["baseline"] - out["manager"], 3)
    out["baseline_capture"] = (round((out["baseline"] - out["manager"]) /
                                     (out["hindsight"] - out["manager"]), 3)
                               if out["hindsight"] > out["manager"] else None)
    for nm in names:
        deltas = [r[nm] - r["baseline"] for r in recs]
        mean = statistics.fmean(deltas)
        # paired t on the per-team-week difference: with ~2,000 pairs a 0.05-point
        # "gain" is noise, and the standard error is the only thing that says so.
        sd = statistics.pstdev(deltas) if len(deltas) > 1 else 0.0
        se = sd / (len(deltas) ** 0.5) if deltas else 0.0
        out[f"{nm}_vs_baseline"] = round(mean, 4)
        out[f"{nm}_se"] = round(se, 4)
        out[f"{nm}_t"] = round(mean / se, 2) if se else None
        out[f"{nm}_moved_lineups_pct"] = round(
            100 * sum(1 for d in deltas if abs(d) > 1e-9) / len(deltas), 1)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 analysis/backtest_lineups.py",
        description="WS-8c — replay historical lineups; measure what the projection is worth.")
    ap.add_argument("--league", "-l", action="append", metavar="KEY")
    ap.add_argument("--by-season", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "reports", "lineup_replay.json"))
    args = ap.parse_args(argv)

    ctxs = ([leagues.resolve(k) for k in args.league] if args.league else leagues.all())
    per_league = {}
    for ctx in ctxs:
        rows, form, err = load_rows(ctx)
        if rows:
            per_league[ctx.key] = (rows, form, err)
        else:
            print(f"  ! {ctx.key}: {err or 'no gradeable rows'}")
    if not per_league:
        print("✗ no backfilled player-weeks. Run: python3 scraping/backfill_weeks.py")
        return 1

    names = [n for n, _, _, _ in CANDIDATES]
    art = {"generated": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
           "method": ("replay of every historical team-week; lineup shape and slot "
                      "eligibility learned from the league's own boxscores; whole "
                      "seasons held out"),
           "leagues": {}}
    print("WS-8c — lineup replay: what is the projection actually worth?\n")
    for key, (rows, form, _) in per_league.items():
        recs = league_replay(key, rows, form, per_league)
        s = summarise(recs, names)
        art["leagues"][key] = {"summary": s}
        if args.by_season:
            by = defaultdict(list)
            for r in recs:
                by[r["season"]].append(r)
            art["leagues"][key]["by_season"] = {
                str(k): summarise(v, names) for k, v in sorted(by.items())}
        print(f"[{key}] {s.get('team_weeks', 0):,} team-weeks replayed")
        print(f"  manager started      {s['manager']:7.2f} pts/team-week")
        print(f"  ESPN projection      {s['baseline']:7.2f}   "
              f"({s['baseline_vs_manager']:+.2f} vs the manager)")
        print(f"  hindsight ceiling    {s['hindsight']:7.2f}   "
              f"(manager regret {s['manager_regret']:.2f} pts/week)")
        cap = s.get("baseline_capture")
        if cap is not None:
            print(f"  -> following the projection captures {cap:.0%} of the gap "
                  f"between the manager and perfect hindsight")
        print(f"  {'candidate':18}{'Δ vs baseline':>15}{'se':>8}{'t':>7}{'lineups moved':>15}")
        for nm in names:
            print(f"  {nm:18}{s[f'{nm}_vs_baseline']:>+15.4f}{s[f'{nm}_se']:>8.4f}"
                  f"{(s[f'{nm}_t'] if s[f'{nm}_t'] is not None else 0):>7.2f}"
                  f"{s[f'{nm}_moved_lineups_pct']:>14.1f}%")
        print()

    # The cross-league verdict, in the same shape WS-8b uses: a candidate has to
    # add POINTS, in every powered league, or it has bought nothing.
    print("verdict — a correction must add realized points, not reduce MAE:")
    art["verdict"] = {}
    for nm in names:
        gains = {k: v["summary"][f"{nm}_vs_baseline"] for k, v in art["leagues"].items()}
        ts = {k: v["summary"].get(f"{nm}_t") for k, v in art["leagues"].items()}
        wins = [k for k, g in gains.items() if g > 0 and (ts.get(k) or 0) >= 2]
        ok = len(wins) >= pp.MIN_POWERED_LEAGUES and all(g > 0 for g in gains.values())
        art["verdict"][nm] = {"per_league": gains, "t": ts, "ships": bool(ok),
                              "reason": ("adds points in every league, significant in "
                                         + ", ".join(wins)) if ok else
                                        ("no significant, consistent points gain "
                                         f"(Δ {', '.join(f'{k} {g:+.3f}' for k, g in gains.items())})")}
        print(f"  {nm:18} {'SHIPS' if ok else 'REFUSED'} — {art['verdict'][nm]['reason']}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(art, f, indent=1)
    print(f"\n-> {os.path.relpath(args.out, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
