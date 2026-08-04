#!/usr/bin/env python3
"""Backtest the sim-optimal 'edge' budget shape on REAL past outcomes.

For each auction season, replay a budget SHAPE at that year's real auction prices (buy the
priciest affordable player per slot = the market's best signal, no hindsight), score the
resulting lineup on ACTUAL season points, and rank it against what the real managers actually
scored (their best lineup from their drafted players). Answers: would this strategy have
finished above the field, and does it beat the RB-anchor / balanced shapes historically?

Run:  PYTHONPATH=analysis:analysis/research python3 analysis/research/backtest_edge_strategy.py
"""
import statistics
from collections import defaultdict

import backtest_budget as bb   # reuse best_lineup_points, replay, manager_seasons, a5, lib

# budget SHAPES as per-slot (position, $) caps. FLEX = best affordable RB/WR/TE.
SHAPES = {
    "EDGE (sim-optimal: RB depth + TE/QB, punt WR)":
        [("QB", 29), ("RB", 46), ("RB", 36), ("WR", 18), ("WR", 1), ("TE", 23), ("FLEX", 25), ("FLEX", 15)],
    "RB ANCHOR (one elite RB)":
        [("QB", 8), ("RB", 90), ("RB", 20), ("WR", 40), ("WR", 12), ("TE", 14), ("FLEX", 10), ("FLEX", 5)],
    "EVEN (spread flat)":
        [("QB", 25), ("RB", 30), ("RB", 28), ("WR", 30), ("WR", 25), ("TE", 22), ("FLEX", 20), ("FLEX", 15)],
    "WR-HEAVY":
        [("QB", 12), ("RB", 25), ("RB", 18), ("WR", 55), ("WR", 40), ("TE", 15), ("FLEX", 20), ("FLEX", 12)],
}


def field_points(yr):
    """Each real manager's actual best-lineup points that season (the field distribution)."""
    return sorted((r["starter_pts"] for r in bb.manager_seasons() if r["yr"] == yr), reverse=True)


def main():
    seasons = list(bb.a5.AUCTION)  # 2018-2025
    # precompute the field once (manager_seasons scans all years)
    allms = bb.manager_seasons()
    field_by_yr = {yr: sorted((r["starter_pts"] for r in allms if r["yr"] == yr), reverse=True) for yr in seasons}
    ptsc = {yr: bb.a5.all_player_points(yr) for yr in seasons}

    print("=" * 100)
    print("BACKTEST: budget SHAPE at real prices -> ACTUAL points, vs the real field each season")
    print("  pct = share of the real field the strategy would have outscored that year")
    print("=" * 100)
    hdr = "   " + "season".ljust(8) + "".join(s[:14].ljust(15) for s in SHAPES)
    print(hdr + "field avg / best")
    agg = defaultdict(list)
    for yr in seasons:
        fld = field_by_yr[yr]
        cells = []
        for name, caps in SHAPES.items():
            pts = bb.replay(caps, yr, ptsc[yr])
            beat = 100 * sum(1 for f in fld if pts > f) / len(fld)
            agg[name].append(beat)
            cells.append(f"{pts:.0f} ({beat:.0f}%)".ljust(15))
        print("   " + str(yr).ljust(8) + "".join(cells) + f"{statistics.mean(fld):.0f} / {max(fld):.0f}")
    print("-" * 100)
    print("   " + "AVG pct".ljust(8) + "".join(f"{statistics.mean(agg[s]):.0f}%".ljust(15) for s in SHAPES))
    print("\n  (replay buys priciest-affordable per slot at real prices; real managers' rosters include")
    print("   keeper-discounted studs the replay must pay market for, so treat the RELATIVE ranking of")
    print("   the shapes as the signal — same field, same handicap for each.)")


if __name__ == "__main__":
    main()
