#!/usr/bin/env python3
"""Map a budget plan to the TIERS it actually buys, priced from this league's history.

This is the "crystal clear" half. A plan of bare dollar figures is unreadable — $12 at TE
sounds like a decision until you learn it buys the TE4 tier. So every slot's dollars are
resolved against config/price_curve.json (what each tier really cost in this league) and
the plan is checked for the two ways a budget lies to you:

  OVERPRICED SLOT — the dollars can't win the tier you imagine (plan QB $8 buys QB3, not
                    an elite QB, because QB1 now clears at ~$32 in this league).
  DEAD MONEY      — the dollars land in the mid-tier trough. RB/WR tiers 1-3 cost $50-72
                    and tier 6+ costs $1-6, so $17-26 buys the tier with the worst
                    points-per-dollar. Spend up or spend down, not in between.

Honest limits, stated because the plan will be tempted to overclaim:
  - WHICH shape to run is NOT validated out of sample. See strategy_search_v2.py: the best
    shape reaches 56% on all 8 budget-normalized seasons but only 53% on the 3 same-regime
    ones. Treat any shape as a disciplined default, never as an edge.
  - WHAT a shape costs and buys IS stable, and that is all this file claims.

Run:  PYTHONPATH=analysis python3 analysis/plan_tiers.py            # grade config/plan.json
      PYTHONPATH=analysis python3 analysis/plan_tiers.py --propose  # + candidate plans
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CURVE = os.path.join(ROOT, "config", "price_curve.json")
PLAN = os.path.join(ROOT, "config", "plan.json")

# plan slot key -> which position's tier ladder prices it
SLOT_POS = {"QB": "QB", "RB1": "RB", "RB2": "RB", "WR1": "WR", "WR2": "WR", "TE": "TE",
            "FLX1": "FLEX", "FLX2": "FLEX", "BN": "BENCH", "DST": "DST"}
FLEX_POS = ("RB", "WR", "TE")


def tier_num(t):
    m = re.sub(r"\D", "", t or "")
    return int(m) if m else 99


def ladder(curve, pos):
    """[(tier, price)] cheapest-last, only tiers priced in the CURRENT regime."""
    d = curve["by_tier"].get(pos, {})
    out = [(t, v["price"], v["regime"], v["n"]) for t, v in d.items()]
    return sorted(out, key=lambda x: tier_num(x[0]))


def tier_for(curve, pos, dollars):
    """Best tier winnable at this price: the priciest tier whose typical price fits."""
    if pos == "BENCH":
        return ("bench dart", 1, "-")
    if pos == "DST":
        return ("any DST", 1, "-")
    cands = []
    if pos == "FLEX":
        for p in FLEX_POS:
            cands += [(t, pr, p) for (t, pr, reg, n) in ladder(curve, p) if pr <= dollars]
    else:
        cands = [(t, pr, pos) for (t, pr, reg, n) in ladder(curve, pos) if pr <= dollars]
    if not cands:
        return ("nothing — under the cheapest tier", 0, pos)
    best = min(cands, key=lambda x: tier_num(x[0]))     # lowest tier number = best players
    return best


TROUGH_TIERS = (4, 5)   # the mid-tier band: priced well above the $1-8 darts, but far
                        # short of the top-3 band that actually wins weeks


def trough_band(curve, pos):
    """The $ range that lands you in tiers 4-5, for the warning text. None if unpriced."""
    prs = [pr for (t, pr, reg, n) in ladder(curve, pos) if tier_num(t) in TROUGH_TIERS]
    return (min(prs), max(prs)) if prs else None


def grade(curve, plan, label):
    budget = curve["budget"]
    slots = [(k, v) for k, v in plan.items() if not k.startswith("_")]
    total = 0
    print(f"\n── {label} ──")
    print(f"   {'slot':6}{'$':>5}   {'buys tier':<26}{'note'}")
    warn = []
    for key, dollars in sorted(slots, key=lambda x: -x[1]):
        pos = SLOT_POS.get(key, "FLEX")
        total += dollars      # every plan key is ONE line item, BN included (pooled bench)
        t, pr, actual = tier_for(curve, pos, dollars)
        tag = f"{t}" + (f" ({actual})" if pos == "FLEX" and actual != pos else "")
        note = ""
        if pos in ("RB", "WR", "TE", "QB"):
            tr = trough_band(curve, pos)
            if tier_num(t) in TROUGH_TIERS:
                rng = f" (${tr[0]}-{tr[1]})" if tr else ""
                note = f"⚠ buys the {pos} mid-tier{rng} — spend up or down"
                warn.append(f"{key} ${dollars} buys {t}, the mid-tier trough")
            elif pr and dollars >= pr * 1.6:
                note = f"overpays: {t} clears ~${pr}"
                warn.append(f"{key} ${dollars} overpays for {t} (~${pr})")
        lbl = f"{key}{'*' if key == 'BN' else ''}"
        print(f"   {lbl:6}{dollars:>5}   {tag:<26}{note}")
    print(f"   {'TOTAL':6}{total:>5}   of ${budget}" + ("" if total <= budget else "  ⚠ OVER BUDGET"))
    if total < budget:
        print(f"   {'':6}{'':>5}   ${budget - total} unallocated")
    return total, warn


def main():
    curve = json.load(open(CURVE))
    print(f"Prices from config/price_curve.json — {curve['regime']} regime, "
          f"${curve['budget']} wallet, seasons {curve['comparable_seasons']}")
    for pos in ("RB", "WR", "TE", "QB"):
        lad = ladder(curve, pos)
        shown = "  ".join(f"{t} ${pr}" for (t, pr, reg, n) in lad if tier_num(t) <= 8)
        tr = trough_band(curve, pos)
        print(f"   {pos}: {shown}" + (f"   [mid-tier trough = {pos}4-5, ${tr[0]}-{tr[1]}]" if tr else ""))

    total, warn = grade(curve, json.load(open(PLAN)), "config/plan.json (current)")
    if warn:
        print("\n   problems:")
        for w in warn:
            print(f"     • {w}")

    if "--propose" in sys.argv:
        # Candidates built FROM the curve: take 2-3 players out of the expensive top-3
        # band, then buy only from the $1-8 band. Nothing lands in the trough.
        # Each sums to exactly $200. BN is pooled over DST + 6 bench (7 slots, $7 floor).
        # Built FROM the curve: take players out of the expensive top-3 band, fill the rest
        # from the $1-8 dart band, and keep the mid-tier trough empty.
        for label, plan in [
            ("A. bellcow RB + alpha WR + elite QB",
             {"RB1": 72, "WR1": 57, "QB": 32, "TE": 8, "RB2": 6,
              "WR2": 2, "FLX1": 2, "FLX2": 2, "BN": 19}),
            ("B. three from the top band, punt QB",
             {"RB1": 72, "WR1": 57, "WR2": 50, "QB": 6, "TE": 4,
              "RB2": 2, "FLX1": 1, "FLX2": 1, "BN": 7}),
            ("C. hero RB + 3 WR (best shape in the v2 backtest)",
             {"RB1": 72, "WR1": 50, "WR2": 26, "FLX1": 10, "QB": 6,
              "TE": 8, "RB2": 6, "FLX2": 2, "BN": 20}),
        ]:
            grade(curve, plan, label)
        print("\nNone of these is a validated edge — see the module docstring. They are "
              "\n disciplined defaults that (a) fit $200 and (b) avoid the trough.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
