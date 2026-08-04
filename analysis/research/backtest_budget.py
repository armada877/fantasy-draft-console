#!/usr/bin/env python3
"""Substantiate the budget plan against REAL history: actual auction cost + actual
season fantasy points (2018-2025, auction era).

Three tests, all on realized outcomes (no projections, no sim):
  1. Manager-season allocation -> realized STARTER points (best 1QB/2RB/2WR/1TE/2FLEX
     lineup from that manager's DRAFTED players). Correlations + buckets by top-RB spend.
  2. $ per ACTUAL point by position & price tier (which tiers actually convert).
  3. Counterfactual replay: fill a roster under different budget SHAPES at that season's
     REAL prices (buy the priciest affordable player per slot = market's best signal, no
     hindsight), score by ACTUAL points. Average across seasons.

Run:  PYTHONPATH=analysis python3 analysis/research/backtest_budget.py
"""
import statistics
from collections import defaultdict

import lib
import a5_draft_value as a5

SKILL = ("QB", "RB", "WR", "TE")
LINEUP = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
NFLEX = 2


def best_lineup_points(pool):
    """pool: list of (pos, pts). Best 1QB/2RB/2WR/1TE + 2 FLEX(RB/WR/TE) by points."""
    byp = {p: sorted([pt for (ps, pt) in pool if ps == p], reverse=True) for p in SKILL}
    used = defaultdict(int)
    total = 0.0
    for pos, n in LINEUP.items():
        for i in range(n):
            if i < len(byp[pos]):
                total += byp[pos][i]
                used[pos] += 1
    rem = sorted([pt for p in ("RB", "WR", "TE") for pt in byp[p][used[p]:]], reverse=True)
    return total + sum(rem[:NFLEX])


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = (sum((x - mx) ** 2 for x in xs) ** 0.5)
    dy = (sum((y - my) ** 2 for y in ys) ** 0.5)
    return num / (dx * dy) if dx and dy else 0.0


def manager_seasons():
    recs = []
    for yr in a5.AUCTION:
        pts = a5.all_player_points(yr)
        by = defaultdict(list)
        for p in lib.draft_picks(yr):
            pos, pp = pts.get(p["playerId"], (p["pos"], 0.0))
            if pos in SKILL:
                by[p["manager"]].append({"pos": pos, "cost": p["cost"], "pts": pp})
        for mgr, pl in by.items():
            costs = sorted((x["cost"] for x in pl), reverse=True)
            tot = sum(costs) or 1
            rbs = sorted((x["cost"] for x in pl if x["pos"] == "RB"), reverse=True)
            sp = lambda P: sum(x["cost"] for x in pl if x["pos"] == P)
            recs.append({
                "yr": yr, "mgr": mgr,
                "rb1": rbs[0] if rbs else 0,
                "rb": sp("RB"), "wr": sp("WR"), "te": sp("TE"), "qb": sp("QB"),
                "top3pct": 100 * sum(costs[:3]) / tot,
                "starter_pts": best_lineup_points([(x["pos"], x["pts"]) for x in pl]),
            })
    return recs


def replay(caps, yr, pts):
    """Fill slots under $ caps at real prices; buy priciest affordable available per slot
    (market's value signal, no hindsight). Score by actual points. caps: list of (slotpos, $)."""
    pool = []
    for p in lib.draft_picks(yr):
        pos, pp = pts.get(p["playerId"], (p["pos"], 0.0))
        if pos in SKILL and p["cost"] >= 1:
            pool.append({"pos": pos, "cost": p["cost"], "pts": pp, "taken": False})
    budget = 200
    total = 0.0
    for slotpos, cap in caps:
        # candidates: right position (FLEX = RB/WR/TE), affordable under cap AND remaining budget
        cand = [x for x in pool if not x["taken"]
                and (x["pos"] == slotpos or (slotpos == "FLEX" and x["pos"] in ("RB", "WR", "TE")))
                and x["cost"] <= min(cap, budget)]
        if not cand:
            continue
        pick = max(cand, key=lambda x: x["cost"])   # priciest affordable = market's best
        pick["taken"] = True
        budget -= pick["cost"]
        total += pick["pts"]
    return total


def main():
    recs = manager_seasons()
    print("=" * 84)
    print(f"BUDGET BACKTEST on REAL cost + REAL points — {len(recs)} manager-seasons, 2018-2025")
    print("=" * 84)

    # 1. correlations of allocation features with realized starter points
    print("\n### 1. Allocation feature -> realized STARTER points  (Pearson r, n=%d)" % len(recs))
    y = [r["starter_pts"] for r in recs]
    for f, lab in [("rb1", "top-RB $ (anchor size)"), ("rb", "total RB $"), ("wr", "total WR $"),
                   ("te", "total TE $"), ("qb", "total QB $"), ("top3pct", "concentration (top-3 %)")]:
        print(f"   {lab:26} r = {pearson([r[f] for r in recs], y):+.2f}")

    # buckets by top-RB spend
    print("\n### by top-RB spend (anchor size) -> avg realized starter points")
    buckets = [("$0-40", 0, 40), ("$41-60", 41, 60), ("$61-80", 61, 80), ("$81+", 81, 999)]
    for lab, lo, hi in buckets:
        g = [r for r in recs if lo <= r["rb1"] <= hi]
        if g:
            print(f"   RB1 {lab:7} n={len(g):>2}  avg starter pts {statistics.mean(r['starter_pts'] for r in g):>7.0f}"
                  f"   (avg total RB ${statistics.mean(r['rb'] for r in g):.0f})")

    # 2. $ per ACTUAL point by position & tier
    print("\n### 2. $ per ACTUAL point by position (non-keeper $5+ picks)")
    recs2 = a5.drafted_with_vorp()
    skill = [r for r in recs2 if r["pos"] in SKILL and not r["is_keeper"] and r["cost"] >= 5]
    for pos in ("RB", "WR", "TE", "QB"):
        g = [r for r in skill if r["pos"] == pos]
        c = sum(r["cost"] for r in g); pt = sum(r["pts"] for r in g) or 1
        print(f"   {pos}: n={len(g):>3}  $/actual-point {c/pt:.3f}  (avg ${statistics.mean(r['cost'] for r in g):.0f} -> {statistics.mean(r['pts'] for r in g):.0f} pts)")
    print("   -- by price tier (all skill, non-keeper): does spend convert to points? --")
    for lab, lo, hi in [("$1-5", 1, 5), ("$6-15", 6, 15), ("$16-30", 16, 30), ("$31-50", 31, 50), ("$51+", 51, 999)]:
        g = [r for r in recs2 if r["pos"] in SKILL and not r["is_keeper"] and lo <= r["cost"] <= hi]
        if g:
            print(f"   {lab:7} n={len(g):>3}  avg ${statistics.mean(r['cost'] for r in g):>4.0f} -> {statistics.mean(r['pts'] for r in g):>5.0f} pts"
                  f"   pts/$ {statistics.mean(r['pts']/r['cost'] for r in g):.1f}   bust%(VORP<0) {100*sum(1 for r in g if r['vorp']<0)/len(g):.0f}")

    # 3. counterfactual replay of budget SHAPES at real prices, scored on real points
    print("\n### 3. Counterfactual: budget SHAPE at real prices -> realized points (avg over seasons)")
    shapes = {
        "Recommended (anchor)": [("RB", 90), ("RB", 25), ("WR", 39), ("WR", 11), ("TE", 12), ("QB", 15), ("FLEX", 4), ("FLEX", 2)],
        "Balanced spread":      [("RB", 45), ("RB", 38), ("WR", 40), ("WR", 30), ("TE", 15), ("QB", 15), ("FLEX", 10), ("FLEX", 5)],
        "Stars & scrubs (2)":   [("RB", 100), ("WR", 70), ("RB", 1), ("WR", 1), ("TE", 1), ("QB", 1), ("FLEX", 1), ("FLEX", 1)],
        "RB-punt / WR-heavy":   [("WR", 55), ("WR", 45), ("RB", 25), ("RB", 20), ("TE", 15), ("QB", 15), ("FLEX", 5), ("FLEX", 5)],
    }
    seasons = list(a5.AUCTION)
    ptsc = {yr: a5.all_player_points(yr) for yr in seasons}
    for name, caps in shapes.items():
        vals = [replay(caps, yr, ptsc[yr]) for yr in seasons]
        print(f"   {name:22} avg starter pts {statistics.mean(vals):>7.0f}   (per season: "
              + " ".join(f"{yr%100}:{v:.0f}" for yr, v in zip(seasons, vals)) + ")")
    print("\n   (replay is single-team, at market prices, buying the priciest affordable per slot —")
    print("    isolates ALLOCATION shape; ignores in-draft competition for the same player.)")


if __name__ == "__main__":
    main()
