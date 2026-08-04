#!/usr/bin/env python3
"""Search for a budget/draft strategy VALIDATED to beat your field — with an overfit guard.

Each strategy is a budget shape (per-slot $ caps). For each past season we replay it at that
year's REAL auction prices (buy the priciest AFFORDABLE player per slot = the best the market
offers at that price; then fill bench with cheap darts), score the best starting lineup on
ACTUAL season points, and compute BEAT-RATE = the share of your real managers that lineup would
have outscored. That directly answers "chance of outperforming each other team," and it bakes
in what others did (real prices + the real field).

Overfit guard: leave-one-year-out. For each held-out season, pick the shape that looked best on
the OTHER years, then score it on the held-out year. If that out-of-sample beat-rate isn't clearly
> 50%, no shape has a validated edge and none gets promoted.

Run:  PYTHONPATH=analysis:analysis/research python3 analysis/research/strategy_search.py
"""
import statistics
from collections import defaultdict

import backtest_budget as bb

SKILL = ("QB", "RB", "WR", "TE")
ROSTER = 14


def eligible(cap_pos, ppos):
    return ppos in ("RB", "WR", "TE") if cap_pos == "FLEX" else ppos == cap_pos


def lineup(caps, yr, pts):
    """Replay the shape at real prices; return best-lineup ACTUAL points from what it buys."""
    pool = []
    for p in bb.lib.draft_picks(yr):
        pos, pp = pts.get(p["playerId"], (p["pos"], 0.0))
        if pos in SKILL and p["cost"] >= 1:
            pool.append({"pos": pos, "cost": p["cost"], "pts": pp, "taken": False})
    budget = 200
    bought = []
    for cap_pos, cap in sorted(caps, key=lambda x: -x[1]):        # claim priciest slots first
        cand = [x for x in pool if not x["taken"] and eligible(cap_pos, x["pos"]) and x["cost"] <= min(cap, budget)]
        if cand:
            pk = max(cand, key=lambda x: x["cost"]); pk["taken"] = True
            budget -= pk["cost"]; bought.append(pk)
    while len(bought) < ROSTER and budget >= 1:                    # bench darts (breakout lottery)
        cand = [x for x in pool if not x["taken"] and x["cost"] <= budget]
        if not cand:
            break
        pk = min(cand, key=lambda x: x["cost"]); pk["taken"] = True
        budget -= pk["cost"]; bought.append(pk)
    return bb.best_lineup_points([(x["pos"], x["pts"]) for x in bought])


# budget shapes: (position, $) per slot — QB, 2 RB, 2 WR, TE, 2 FLEX (~$192 + bench darts)
SHAPES = {
    "bellcow RB + elite WR":      [("RB", 85), ("WR", 60), ("QB", 8), ("RB", 15), ("TE", 12), ("WR", 6), ("FLEX", 4), ("FLEX", 2)],
    "two elite RB":               [("RB", 80), ("RB", 55), ("WR", 22), ("QB", 8), ("WR", 10), ("TE", 10), ("FLEX", 5), ("FLEX", 2)],
    "two elite WR (zero-RB)":     [("WR", 75), ("WR", 55), ("RB", 20), ("QB", 8), ("RB", 12), ("TE", 10), ("FLEX", 8), ("FLEX", 4)],
    "studs 3-4 (pay up RB+WR)":   [("RB", 70), ("WR", 62), ("QB", 8), ("RB", 18), ("WR", 12), ("TE", 12), ("FLEX", 6), ("FLEX", 2)],
    "RB anchor + spread":         [("RB", 90), ("WR", 30), ("RB", 25), ("WR", 15), ("TE", 14), ("QB", 8), ("FLEX", 8), ("FLEX", 2)],
    "WR-heavy":                   [("WR", 55), ("WR", 42), ("QB", 12), ("RB", 25), ("RB", 18), ("TE", 15), ("FLEX", 15), ("FLEX", 10)],
    "even/balanced":              [("RB", 35), ("WR", 35), ("RB", 30), ("WR", 30), ("TE", 22), ("QB", 25), ("FLEX", 10), ("FLEX", 5)],
    "RB depth (sim-edge, punt WR)": [("RB", 46), ("RB", 36), ("QB", 29), ("FLEX", 25), ("TE", 23), ("WR", 18), ("FLEX", 15), ("WR", 1)],
    "hero RB + 3 WR":             [("RB", 78), ("WR", 45), ("WR", 30), ("FLEX", 15), ("QB", 8), ("RB", 6), ("TE", 10), ("FLEX", 2)],
    "punt QB, elite RB+WR":       [("RB", 78), ("WR", 65), ("RB", 15), ("WR", 15), ("TE", 12), ("FLEX", 6), ("QB", 2), ("FLEX", 2)],
    "invest TE + elite WR":       [("WR", 60), ("TE", 38), ("RB", 25), ("WR", 15), ("RB", 18), ("FLEX", 18), ("QB", 8), ("FLEX", 10)],
    "mid everything (spread)":    [("RB", 30), ("RB", 28), ("WR", 30), ("WR", 26), ("TE", 22), ("QB", 18), ("FLEX", 20), ("FLEX", 16)],
}


def main():
    seasons = list(bb.a5.AUCTION)
    allms = bb.manager_seasons()
    field = {yr: [r["starter_pts"] for r in allms if r["yr"] == yr] for yr in seasons}
    ptsc = {yr: bb.a5.all_player_points(yr) for yr in seasons}

    # beat-rate per shape per season
    beat = defaultdict(dict)
    for yr in seasons:
        fld = field[yr]
        for name, caps in SHAPES.items():
            p = lineup(caps, yr, ptsc[yr])
            beat[name][yr] = 100 * sum(1 for f in fld if p > f) / len(fld)

    print("=" * 104)
    print("VALIDATED STRATEGY SEARCH — beat-rate (% of your real field outscored) at real prices/points")
    print("=" * 104)
    print(f"   {'shape':30}" + "".join(str(y)[2:].rjust(5) for y in seasons) + f"{'MEAN':>7}{'WORST':>7}")
    ranked = sorted(SHAPES, key=lambda n: -statistics.mean(beat[n].values()))
    for name in ranked:
        yrs = [beat[name][y] for y in seasons]
        print(f"   {name:30}" + "".join(f"{v:4.0f}" + " " for v in yrs)
              + f"{statistics.mean(yrs):>7.0f}{min(yrs):>7.0f}")

    # ---- overfit guard: leave-one-year-out selection ----
    loyo = {}
    for held in seasons:
        train = [y for y in seasons if y != held]
        pick = max(SHAPES, key=lambda n: statistics.mean(beat[n][y] for y in train))
        loyo[held] = (pick, beat[pick][held])
    loyo_mean = statistics.mean(v for _, v in loyo.values())

    print("\n" + "-" * 104)
    print("OVERFIT GUARD — leave-one-year-out (pick best on the other 7, grade on the held-out year):")
    for held in seasons:
        pick, sc = loyo[held]
        print(f"   {held}: would have picked {pick!r:34} → beat {sc:.0f}% of the field that year")
    print(f"\n   Out-of-sample mean beat-rate of 'pick the historically-best shape': {loyo_mean:.0f}%")
    bar = 55
    if loyo_mean >= bar:
        winner = statistics.mode([p for p, _ in loyo.values()])
        print(f"   ✓ VALIDATED (>= {bar}% out-of-sample). Promotable shape: {winner!r}")
    else:
        print(f"   ✗ NOT VALIDATED (< {bar}% out-of-sample = no reliable edge over the field).")
        print("     => Per the promotion rule, NO static budget shape is promoted. The edge is in-draft")
        print("        value-buying, not a fixed allocation.")


if __name__ == "__main__":
    main()
