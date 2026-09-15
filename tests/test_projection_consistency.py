#!/usr/bin/env python3
"""One projection, everywhere.

Waivers, trades and the lineup planner must never disagree about what a player is
projected to score in a given week. They cannot, structurally — all three reduce to
engine.lineup.points_in() — but "cannot by design" is worth pinning down, because the
frontend re-implements the same solver in JS and could drift from the Python one.

    python3 tests/test_projection_consistency.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import leagues                                                   # noqa: E402
from engine.lineup import Player, lineup_points, marginal_add, trade_delta   # noqa: E402
from engine.state import SeasonState                                        # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAIL.append(name)


def mk(rec, proj_weeks):
    wk = rec.get("wk") or []
    wp = {w: wk[i] for i, w in enumerate(proj_weeks) if i < len(wk)}
    return Player(id=rec["id"], name=rec["name"], pos=rec["pos"],
                  eligible_slots=frozenset(rec["eligible_slots"]),
                  points_per_game=rec.get("ros_ppg") or 0.0,
                  week_points=wp, bye_week=rec.get("bye"))


def main():
    print("projection consistency across waivers / trades / lineup")
    for ctx in leagues.all():
        p = ctx.out("season_data.json")
        if not os.path.exists(p):
            continue
        d = json.load(open(p))
        P, LG = d["players"], d["league"]
        pw = [int(w) for w in (LG.get("proj_weeks") or [])]
        print(f"\n[{ctx.key}]")
        check("payload carries league.proj_weeks", bool(pw), f"{len(pw)} weeks")
        withwk = [v for v in P.values() if v.get("wk")]
        check("players carry a weekly series", bool(withwk),
              f"{len(withwk)}/{len(P)}")
        if not (pw and withwk):
            continue

        # the series must actually vary — a flat series is the bug this test exists for
        varying = [v for v in withwk
                   if len({x for x in v["wk"] if x}) > 1]
        check("weekly projections VARY week to week", len(varying) > len(withwk) * 0.5,
              f"{len(varying)}/{len(withwk)} players have a non-flat series")

        prof = ctx.profile()
        me = next(t for t in d["teams"] if t["team_id"] == d["me"]["team_id"])
        roster = [mk(P[str(i)], pw) for i in me["roster"] if str(i) in P]
        sample = max(withwk, key=lambda v: len({x for x in v["wk"] if x}))
        cand = mk(sample, pw)
        wk_now = d["week"]
        weeks = [w for w in pw if w > wk_now][:6]
        if not weeks:
            continue

        # 1. the lineup path and the raw series agree
        direct = [round(cand.points_in(w), 2) for w in weeks]
        series = [round(sample["wk"][pw.index(w)], 2) for w in weeks]
        check("lineup points_in == the payload's weekly series", direct == series,
              f"{sample['name']}: {direct} vs {series}")

        # 2. Waivers and trades must agree on the SAME swap, measured from the SAME
        #    baseline. marginal_add(must_drop=True) picks the cheapest player to cut
        #    and returns "swap him for the candidate, vs my roster as it stands" —
        #    which is exactly trade_delta(send=[that drop], receive=[candidate]).
        #    (Comparing against a roster that already lost the drop is a different
        #    question and will not match; that was a bug in an earlier version here.)
        if roster:
            gain_w, chosen = marginal_add(roster, cand, prof, weeks, must_drop=True)
            if chosen is not None:
                gain_t = trade_delta(roster, [chosen], [cand], prof, weeks)
                check("waiver marginal == trade delta for the identical swap",
                      abs(gain_w - gain_t) < 1e-6,
                      f"drop {chosen.name}: {gain_w:.4f} vs {gain_t:.4f}")
            else:
                check("waiver marginal == trade delta for the identical swap", True,
                      "roster has room; no drop required")
            # Give the check teeth. A roster under capacity needs no drop (that is
            # correct, not a bug), so assert the always-exercisable identity instead:
            # adding a player with no drop is a trade that sends nothing.
            star = mk(dict(sample, id=-1, name="Test Star",
                           ros_ppg=max(r.points_per_game for r in roster) + 10), pw)
            star.week_points = {w: star.points_per_game for w in pw}
            g_add, _ = marginal_add(roster, star, prof, weeks, must_drop=False)
            g_recv = trade_delta(roster, [], [star], prof, weeks)
            check("adding a star == a trade that sends nothing, and the gain is real",
                  g_add > 0 and abs(g_add - g_recv) < 1e-6,
                  f"add {g_add:.2f} vs trade {g_recv:.2f} pts over {len(weeks)} wks")

        # 3. THE PATH THE ENGINES ACTUALLY TAKE. The waiver board and trade finder do
        #    not build Players themselves — they go through SeasonState.from_season_data.
        #    An earlier version of this test constructed Players by hand and so missed
        #    that from_season_data ignored the weekly series entirely, leaving waivers
        #    and trades on flat rates while the lineup tab used real per-week numbers.
        state = SeasonState.from_season_data(d, profile=prof)
        sp = state.players.get(int(sample["id"]))
        check("SeasonState carries the weekly series", bool(sp and sp.week_points),
              f"{sample['name']}")
        if sp and sp.week_points:
            via_state = [round(sp.points_in(w), 2) for w in weeks]
            check("SeasonState points_in == the payload's weekly series",
                  via_state == series, f"{via_state} vs {series}")
            flat = round(float(sample.get("ros_ppg") or 0), 2)
            check("...and it is NOT just the flat season rate",
                  len({v for v in via_state}) > 1 or via_state[0] != flat,
                  f"flat would be {flat}")

        # 4. a bye really zeroes out
        byes = [v for v in withwk if v.get("bye") and v["bye"] in pw
                and v["wk"][pw.index(v["bye"])] == 0.0]
        check("bye weeks project 0 in the series", bool(byes),
              f"{len(byes)} players with a 0.0 at their bye")

    print("\n" + ("ALL PASS" if not FAIL else f"{len(FAIL)} FAILURE(S): " + ", ".join(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
