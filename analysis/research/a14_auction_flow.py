#!/usr/bin/env python3
"""Analysis 14: auction FLOW for live navigation — how the room bids relative to
Elboberto projection as the draft progresses, and when each position dries up.

Answers: when to pounce (value appears) vs when to pay (room overbids), and which
positions clear early vs stay cheap late. Uses overallPickNumber as the draft-time
axis (order players came off the board). 2022-2025, matched to projections.
"""
import json
import os
import re
import statistics
from collections import defaultdict
import lib

PROJ = json.load(open(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                                   "draft_sheets", "elboberto_projections.json")))
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
YEARS = [2022, 2023, 2024, 2025]


def norm(n):
    n = re.sub(r"\([^)]*\)", "", str(n))
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def rows():
    out = []
    for yr in YEARS:
        pl = {norm(p["name"]): p for p in PROJ[str(yr)] if p.get("proj_value") is not None}
        picks = [p for p in lib.draft_picks(yr) if not p["is_keeper"] and p["cost"] >= 1]
        mx = max((p["overall"] or 0) for p in picks) or 1
        for p in picks:
            e = pl.get(norm(p["name"]))
            if not e:
                continue
            out.append({"yr": yr, "pos": p["pos"], "paid": p["cost"], "proj": e["proj_value"],
                        "frac": (p["overall"] or 1)/mx, "overall": p["overall"]})
    return out


def main():
    R = rows()
    print("=" * 90)
    print("ANALYSIS 14 — AUCTION FLOW: room bids vs projection over the draft (2022-2025)")
    print("=" * 90)

    # A. inflation over draft progress (quintiles), proj>=5 to avoid $1 noise
    print("\n### A. Do bids run hot early, cheap late?  (players with proj $5+)")
    print("-" * 90)
    print(f"{'draft phase':16}{'n':>5}{'avg proj$':>10}{'avg paid$':>10}{'paid/proj':>11}{'% bargains':>12}")
    print("-" * 90)
    big = [r for r in R if r["proj"] >= 5]
    bins = [("first 20%", 0, .2), ("20-40%", .2, .4), ("40-60%", .4, .6),
            ("60-80%", .6, .8), ("last 20%", .8, 1.01)]
    for label, lo, hi in bins:
        g = [r for r in big if lo <= r["frac"] < hi]
        if not g:
            continue
        ratio = statistics.mean(r["paid"] for r in g)/statistics.mean(r["proj"] for r in g)
        barg = 100*sum(1 for r in g if r["paid"] < r["proj"])/len(g)
        print(f"{label:16}{len(g):>5}{statistics.mean(r['proj'] for r in g):>10.1f}"
              f"{statistics.mean(r['paid'] for r in g):>10.1f}{ratio:>11.2f}{barg:>11.0f}%")

    # B. when does each position clear? median draft-fraction of elite (proj $20+) buys
    print("\n### B. When does each position's value clear? (draft-fraction of $20+ proj buys)")
    print("-" * 90)
    print(f"{'Pos':5}{'n($20+)':>9}{'median frac':>13}{'25th':>8}{'75th':>8}   read")
    print("-" * 90)
    for pos in ["RB", "WR", "TE", "QB"]:
        g = sorted([r["frac"] for r in R if r["pos"] == pos and r["proj"] >= 20])
        if len(g) < 4:
            continue
        med = statistics.median(g)
        q1 = g[len(g)//4]; q3 = g[3*len(g)//4]
        read = "clears early" if med < 0.35 else "spread" if med < 0.55 else "lingers"
        print(f"{pos:5}{len(g):>9}{med:>13.2f}{q1:>8.2f}{q3:>8.2f}   {read}")

    # C. paid/proj by position x draft half — where's the value window per position
    print("\n### C. paid/proj by position, early half vs late half (proj $5+)")
    print("-" * 90)
    print(f"{'Pos':5}{'early paid/proj':>18}{'late paid/proj':>17}   value window")
    print("-" * 90)
    for pos in ["RB", "WR", "TE", "QB"]:
        early = [r for r in big if r["pos"] == pos and r["frac"] < 0.5]
        late = [r for r in big if r["pos"] == pos and r["frac"] >= 0.5]
        if len(early) < 3 or len(late) < 3:
            continue
        er = statistics.mean(r["paid"] for r in early)/statistics.mean(r["proj"] for r in early)
        lr = statistics.mean(r["paid"] for r in late)/statistics.mean(r["proj"] for r in late)
        win = "late (wait)" if lr < er - 0.1 else "early (pounce)" if er < lr - 0.1 else "flat"
        print(f"{pos:5}{er:>18.2f}{lr:>17.2f}   {win}")


if __name__ == "__main__":
    main()
