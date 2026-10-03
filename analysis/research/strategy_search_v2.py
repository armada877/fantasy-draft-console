#!/usr/bin/env python3
"""Budget-shape backtest, REGIME-CORRECTED — supersedes strategy_search.py's headline.

Two defects in the v1 harness made its verdict unsafe to trust:

  1. BUDGET MISMATCH. v1's `lineup()` hardcodes a $200 wallet but shops at each season's
     RAW prices, so five of eight seasons let a $200 wallet loose in a market where the
     top player cost $87-110. Fixed by scaling every cap to that season's EFFECTIVE WALLET
     (lib.effective_wallet) — non-keeper dollars actually spent per team, ~$170-188 in the
     keeper years. Note the nominal $300 cap is the wrong scale too: it existed only to
     carry the keeper cost+$100 encoding, never as spending power.
  2. KEEPER SUPPLY. 2020-2024 had 12 keepers, which both consumed budget and removed the
     elite players a top-heavy shape depends on. A shape that wants the #1 RB cannot be
     graded in a year where the #1 RB was never auctioned. Reported separately rather than
     averaged in.

Everything else is v1's method, deliberately: replay the shape at real prices, buy the
priciest AFFORDABLE player per slot (no hindsight), score the best lineup on ACTUAL season
points, and report BEAT-RATE = share of the real field that lineup outscores. Same
leave-one-year-out promotion gate.

This does NOT promise to rescue a winner. If the corrected numbers still fail the gate,
the answer is still "no static shape has an edge" — the point is that the verdict now
rests on a fair comparison.

Run:  PYTHONPATH=analysis:analysis/research python3 analysis/research/strategy_search_v2.py
"""
import statistics
import sys
from collections import defaultdict

import backtest_budget as bb
import strategy_search as v1

SKILL = ("QB", "RB", "WR", "TE")
ROSTER = 14
BASE_BUDGET = 200          # the budget every shape below is denominated in


def season_budget(yr):
    """The EFFECTIVE wallet (lib.effective_wallet), not the nominal cap.

    Using the nominal cap here was the original defect AND my first attempt at fixing it:
    2020-2024 report $300, but teams only ever had ~$170-188 to bid once the keeper's
    cost+$100 encoding was deducted. Shopping a $300 wallet in that market would overbuy
    just as badly as v1's $200 wallet underbought.
    """
    try:
        return bb.lib.effective_wallet(yr) or BASE_BUDGET
    except Exception:
        return BASE_BUDGET


def keepers(yr):
    return sum(1 for p in bb.lib.draft_picks(yr) if p["is_keeper"])


def lineup(caps, yr, pts):
    """v1.lineup, but the wallet AND the caps are expressed in that season's dollars."""
    b = season_budget(yr)
    scale = b / BASE_BUDGET
    pool = []
    for p in bb.lib.draft_picks(yr):
        pos, pp = pts.get(p["playerId"], (p["pos"], 0.0))
        # keepers are not purchasable at their recorded price — exclude them from supply
        if pos in SKILL and p["cost"] >= 1 and not p["is_keeper"]:
            pool.append({"pos": pos, "cost": p["cost"], "pts": pp, "taken": False})
    budget = b
    bought = []
    for cap_pos, cap in sorted(caps, key=lambda x: -x[1]):
        cap_s = cap * scale
        cand = [x for x in pool if not x["taken"] and v1.eligible(cap_pos, x["pos"])
                and x["cost"] <= min(cap_s, budget)]
        if cand:
            pk = max(cand, key=lambda x: x["cost"])
            pk["taken"] = True
            budget -= pk["cost"]
            bought.append(pk)
    while len(bought) < ROSTER and budget >= 1:
        cand = [x for x in pool if not x["taken"] and x["cost"] <= budget]
        if not cand:
            break
        pk = min(cand, key=lambda x: x["cost"])
        pk["taken"] = True
        budget -= pk["cost"]
        bought.append(pk)
    return bb.best_lineup_points([(x["pos"], x["pts"]) for x in bought])


# v1's shapes, plus candidates the price curve and the QB regime shift argue for.
# QB1 tier cost 6-9% of budget in 2022-24 but 16% in 2025 ($32) — v1 has no shape that
# pays for an elite QB at the price the league now charges, so it could never test one.
SHAPES = dict(v1.SHAPES)
SHAPES.update({
    "curve: RB1+WR1, elite QB":   [("RB", 72), ("WR", 57), ("QB", 32), ("RB", 12),
                                   ("TE", 8), ("WR", 8), ("FLEX", 4), ("FLEX", 2)],
    "curve: RB1+WR1+WR2, cheap QB": [("RB", 72), ("WR", 57), ("WR", 50), ("QB", 6),
                                     ("RB", 5), ("TE", 4), ("FLEX", 2), ("FLEX", 1)],
    "curve: three top-tier RB/WR": [("RB", 70), ("WR", 68), ("RB", 50), ("QB", 4),
                                    ("WR", 3), ("TE", 3), ("FLEX", 1), ("FLEX", 1)],
    "curve: elite QB + TE1 + RB1": [("RB", 72), ("QB", 32), ("TE", 33), ("WR", 26),
                                    ("WR", 10), ("RB", 12), ("FLEX", 4), ("FLEX", 2)],
})


def loyo(beat, seasons, shapes):
    """Leave-one-year-out: pick the best shape on the other years, grade on the held-out."""
    picks, oos = {}, []
    for held in seasons:
        rest = [y for y in seasons if y != held]
        best = max(shapes, key=lambda s: statistics.mean(beat[s][y] for y in rest))
        picks[held] = (best, beat[best][held])
        oos.append(beat[best][held])
    return picks, statistics.mean(oos) if oos else 0.0


def report(title, seasons, beat, shapes):
    print("\n" + "=" * 100)
    print(f"{title}   seasons={list(seasons)}")
    print("=" * 100)
    ranked = sorted(shapes, key=lambda s: -statistics.mean(beat[s][y] for y in seasons))
    print(f"   {'shape':32}" + "".join(f"{str(y)[2:]:>5}" for y in seasons) + f"{'MEAN':>7}{'WORST':>7}")
    for s in ranked:
        vals = [beat[s][y] for y in seasons]
        print(f"   {s:32}" + "".join(f"{v:>5.0f}" for v in vals)
              + f"{statistics.mean(vals):>7.0f}{min(vals):>7.0f}")
    picks, oos = loyo(beat, list(seasons), shapes)
    print(f"\n   OVERFIT GUARD (leave-one-year-out):")
    for y, (s, v) in picks.items():
        print(f"      {y}: would pick {s:34} -> beat {v:.0f}% held out")
    verdict = "✓ VALIDATED" if oos > 55 else "✗ NOT VALIDATED"
    print(f"   out-of-sample mean beat-rate: {oos:.0f}%   {verdict} (gate: >55%)")
    return oos


def main():
    seasons = list(bb.a5.AUCTION)
    allms = bb.manager_seasons()
    field = {yr: [r["starter_pts"] for r in allms if r["yr"] == yr] for yr in seasons}
    ptsc = {yr: bb.a5.all_player_points(yr) for yr in seasons}

    print("REGIME MAP  (wallet = non-keeper $ actually spent per team)")
    for yr in seasons:
        print(f"   {yr}: nominal ${bb.lib.auction_budget(yr):<4} keepers {keepers(yr):<3}"
              f" -> wallet ${season_budget(yr):.0f}  [{bb.lib.regime(yr)}]")

    beat = defaultdict(dict)
    for yr in seasons:
        fld = field[yr]
        for name, caps in SHAPES.items():
            p = lineup(caps, yr, ptsc[yr])
            beat[name][yr] = 100 * sum(1 for f in fld if p > f) / len(fld)

    report("ALL SEASONS, budget-normalized", seasons, beat, SHAPES)
    # regime label, not a float wallet comparison (the wallet is $198.8-$200.0, never ==200)
    clean = [y for y in seasons if bb.lib.regime(y) == "full-supply"]
    if len(clean) >= 3:
        report("SAME REGIME ONLY ($200, no keepers — what 2026 is)", clean, beat, SHAPES)
    else:
        print(f"\n(only {len(clean)} same-regime seasons: {clean} — too few for a separate gate)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
