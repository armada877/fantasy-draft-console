#!/usr/bin/env python3
"""Smoke test for the WS-8 validation chain — offline, no network, no league data.

Every check here is a bug that actually happened while this chain was being built,
or a guarantee the whole "nothing ships unvalidated" claim rests on:

1. **A future week must never be graded.** The first run of `backfill_weeks.py`
   distilled unplayed weeks, whose projections have no actual to pair with, and
   scored every one of them against zero. That is a catastrophic, entirely
   fictional bias, and it looked like a finding.
2. **The archive freezes on first write.** A snapshot overwritten later in the week
   has seen the results; grading it would turn a forecast into a postdiction and
   manufacture a prescient source.
3. **FantasyPros' projection table parses into ESPN statIds**, so the projection can
   be re-scored in the league's own scoring instead of importing FantasyPros'.
4. **The policy bar refuses what MAE alone would promote** — the finding that forced
   the second half of the bar (conformance test [5] covers the rest).
5. **The eligibility learner reads slots off real lineups** rather than a hardcoded
   position table, which is what makes 2QB and superflex replays correct.

    python3 tests/validation_smoke.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scraping"))

from analysis.backtest_lineups import learn_eligibility               # noqa: E402
from engine import projection_policy as pp                            # noqa: E402
from scraping import backfill_weeks as bw                             # noqa: E402
from scraping.sources import common, fantasypros_proj as fpp          # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


def _boxscore(week, with_actuals=True):
    """A minimal mBoxscore payload: one team, two players."""
    def stats(proj, actual):
        out = [{"statSourceId": 1, "statSplitTypeId": 1, "scoringPeriodId": week,
                "seasonId": 2025, "appliedTotal": proj}]
        if with_actuals:
            out.append({"statSourceId": 0, "statSplitTypeId": 1, "scoringPeriodId": week,
                        "seasonId": 2025, "appliedTotal": actual})
        return out
    return {"schedule": [{"home": {"teamId": 3, "rosterForCurrentScoringPeriod": {"entries": [
        {"lineupSlotId": 2, "playerPoolEntry": {"player": {
            "id": 11, "fullName": "A Back", "defaultPositionId": 2,
            "stats": stats(12.0, 8.5)}}},
        {"lineupSlotId": 20, "playerPoolEntry": {"player": {
            "id": 22, "fullName": "B Receiver", "defaultPositionId": 3,
            "stats": stats(9.0, 14.0)}}}]}}}]}


class _Ctx:
    """The two LeagueContext bits the code under test touches."""
    def __init__(self, root, season=2026):
        self.root, self.season, self.key = root, season, "smoke"

    def raw(self, name):
        return os.path.join(self.root, name)


def test_future_weeks_are_not_graded():
    print("\n[1] a week that has not been played is never distilled")
    rows, names = bw.week_rows(_boxscore(4), 2025, 4)
    check("a completed week yields paired rows", len(rows) == 2,
          f"{len(rows)} rows, actuals {[r[bw_ACT] for r in rows]}")
    check("the pairing is (projected, actual), not (projected, 0)",
          rows[0][6] == 12.0 and rows[0][7] == 8.5, str(rows[0]))

    # The bug: a future week answers with projections and no actuals at all.
    future, _ = bw.week_rows(_boxscore(9, with_actuals=False), 2025, 9)
    check("a future week's rows would score every projection against 0 "
          "(which is why completed_weeks gates them)",
          all(r[7] == 0.0 for r in future), str(future[:1]))

    with tempfile.TemporaryDirectory() as d:
        ctx = _Ctx(d, season=2026)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "league_full.json"), "w") as f:
            json.dump({"scoringPeriodId": 3, "status": {"latestScoringPeriod": 3}}, f)
        live = list(bw.completed_weeks(ctx, 2026, 2026))
        past = list(bw.completed_weeks(ctx, 2024, 2026))
        check("the live season stops before the week in progress",
              live == [1, 2], f"current week 3 -> {live}")
        check("a past season is entirely playable", past[-1] == bw.MAX_WEEK,
              f"1-{past[-1]}")


def test_archive_freezes():
    print("\n[2] the source archive freezes on first write")
    with tempfile.TemporaryDirectory() as d:
        ctx = _Ctx(d)
        env = {"source": "fantasypros", "season": 2026, "week": 2, "ok": True,
               "records": [{"name": "first"}]}
        p1 = common.archive(ctx, "fantasypros", env)
        p2 = common.archive(ctx, "fantasypros", {**env, "records": [{"name": "second"}]})
        check("the first snapshot is written", bool(p1) and os.path.exists(p1))
        check("a later snapshot of the SAME week does not overwrite it", p2 is None)
        with open(p1) as f:
            kept = json.load(f)
        check("the frozen file still holds the first snapshot",
              kept["records"][0]["name"] == "first")
        check("it records when it was taken, so contamination is checkable",
              bool(kept.get("_archived")))
        check("a failed source is not archived",
              common.archive(ctx, "borischen", {**env, "source": "borischen",
                                                "ok": False, "week": 2}) is None)


def test_fantasypros_projection_parse():
    print("\n[3] FantasyPros component stats parse into ESPN statIds")
    html = """<table id="data"><thead>
      <tr><td>&nbsp;</td><td colspan="3">RUSHING</td><td colspan="3">RECEIVING</td>
          <td colspan="2">MISC</td></tr>
      <tr><th>Player</th><th>ATT</th><th>YDS</th><th>TDS</th><th>REC</th><th>YDS</th>
          <th>TDS</th><th>FL</th><th>FPTS</th></tr></thead><tbody>
      <tr><td><a class="player-name" fp-player-name="Jahmyr Gibbs">Jahmyr Gibbs</a> DET</td>
      <td>16.4</td><td>87.9</td><td>0.9</td><td>4.3</td><td>32.4</td><td>0.2</td>
      <td>0.1</td><td data-sort-value="20.455">20.5</td></tr></tbody></table>"""
    rows = fpp.parse_table(html, "rb")
    check("one player row parses", len(rows) == 1)
    name, team, stats, fpts = rows[0]
    check("name and team come off the row", name == "Jahmyr Gibbs" and team == "DET",
          f"{name} / {team}")
    check("RUSHING YDS -> statId 24, RECEIVING YDS -> statId 42 (the colspan expansion)",
          stats.get(24) == 87.9 and stats.get(42) == 32.4, str(stats))
    check("receptions -> statId 53", stats.get(53) == 4.3)
    check("FantasyPros' own FPTS is kept separate from the stats", fpts == 20.5)

    # Re-scoring is the whole point: the same stat line is worth different points in
    # a PPR league and a half-PPR one, and neither is FantasyPros' number.
    fields = {"wk_stats": {str(k): v for k, v in stats.items()}}
    ppr = fpp.rescore(fields, {24: 0.1, 42: 0.1, 43: 6.0, 25: 6.0, 53: 1.0}, "wk_")
    half = fpp.rescore(fields, {24: 0.1, 42: 0.1, 43: 6.0, 25: 6.0, 53: 0.5}, "wk_")
    check("PPR and half-PPR re-score to different points", ppr > half,
          f"PPR {ppr} vs half {half}")
    check("a record with no stats re-scores to None, not 0",
          fpp.rescore({"wk_fpts_fp": 20.5}, {53: 1.0}, "wk_") is None)


def test_bar_needs_points():
    print("\n[4] the policy bar cannot be cleared on accuracy alone")
    mae_ok = (pp.LeagueResult("a", 5.0, 4.5, 5000), pp.LeagueResult("b", 5.0, 4.6, 5000))
    v = pp.Validation("m", (2024,), mae_ok)
    ships, why = v.verdict()
    check("MAE-only evidence is refused", not ships, why[:70])

    v = pp.Validation("m", (2024,), mae_ok, decision=(
        pp.DecisionResult("a", 1000, 0.0, None, 0.0),
        pp.DecisionResult("b", 900, 0.0, None, 0.0)))
    ships, why = v.verdict()
    check("a correction that moves no lineup is refused as 'changes nothing'",
          not ships and "changes no lineup" in why, why[:70])

    v = pp.Validation("m", (2024,), mae_ok, decision=(
        pp.DecisionResult("a", 1000, 0.8, 3.1, 14.0),
        pp.DecisionResult("b", 900, 0.6, 2.4, 12.0)))
    ships, why = v.verdict()
    check("MAE gain AND a significant points gain ships", ships, why[:80])


def test_eligibility_is_learned():
    print("\n[5] slot eligibility is learned from real lineups, not hardcoded")
    # [season, week, team, player, slot, pos, proj, actual]; QB started in a
    # superflex (OP=7) seat, RB in FLEX — neither is written down anywhere.
    rows = [[2025, 1, 1, 10, 0, 1, 20.0, 18.0], [2025, 1, 1, 11, 7, 1, 17.0, 21.0],
            [2025, 1, 1, 12, 2, 2, 12.0, 9.0], [2025, 1, 1, 13, 23, 2, 10.0, 14.0],
            [2025, 1, 1, 14, 20, 3, 8.0, 3.0]]
    elig = learn_eligibility(rows)
    check("QB is eligible for both QB and the superflex seat", elig[1] == frozenset({0, 7}),
          str(sorted(elig[1])))
    check("RB is eligible for RB and FLEX", elig[2] == frozenset({2, 23}),
          str(sorted(elig[2])))
    check("a position only ever benched claims no starting slot", 3 not in elig)


bw_ACT = 7


def main():
    print("WS-8 validation chain smoke test (offline)")
    test_future_weeks_are_not_graded()
    test_archive_freezes()
    test_fantasypros_projection_parse()
    test_bar_needs_points()
    test_eligibility_is_learned()
    print(f"\n{'ALL PASS' if not FAILURES else 'FAILURES: ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
