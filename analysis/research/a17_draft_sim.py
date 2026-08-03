#!/usr/bin/env python3
"""Analysis 17: Monte-Carlo the 2026 auction to get a target roster shape + budget.

Field prices each player by the room's validated behavior: proj $ x positional
multiplier (§11: RB 1.31, WR 1.47, TE 0.74, QB 0.41) x lognormal noise. "Harry"
bids the validated strategy: pay up for RB (converts deepest), grab elite QB/TE
cheap (room underpays), don't chase mid-WR, buy the value that falls late. 10
independent runs -> what roster (tier/price per slot) and budget split to aim for.
"""
import json
import os
import random
import statistics
from collections import defaultdict

PROJ = json.load(open(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
              "draft_sheets", "elboberto_projections.json")))["2026"]

ROOM_MULT = {"RB": 1.31, "WR": 1.47, "TE": 0.74, "QB": 0.41}
BUDGET = 200
# starting slots to fill by spending (DST streamed for $1). 1QB/2RB/2WR/1TE/2FLEX
START = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
FLEX = 2
BENCH = 5
POS = ("QB", "RB", "WR", "TE")


def harry_max_bid(p):
    """Harry's worth-based ceiling (his edge encoded)."""
    pos, v = p["pos"], (p["proj_value"] or 0)
    if v <= 0:
        return 1
    if pos == "RB":
        return v * 1.40                      # pay up; RB converts deepest
    if pos == "QB":
        return v * 0.95 if v >= 25 else v * 0.45   # steal elite QB, else stream
    if pos == "TE":
        return v * 1.05 if v >= 12 else v * 0.8    # grab value/elite TE
    if pos == "WR":
        return v * 1.35 if v >= 25 else v * 0.85   # 1 elite WR ok; fade mid-WR
    return v


def field_price(p, rng):
    v = p["proj_value"] or 0
    if v <= 0:
        return 1
    noise = rng.lognormvariate(0, 0.28)
    return max(1, v * ROOM_MULT.get(p["pos"], 1.0) * noise)


# positional budget envelopes (validated RB-tilt, still balanced) + how many bodies to
# actively buy per position (starters+flex+key bench). Rest of roster = $1 darts.
ENVELOPE = {"RB": 112, "WR": 58, "TE": 15, "QB": 12}   # sums 197; ~$3 to DST+darts
TARGET_N = {"RB": 3, "WR": 3, "TE": 1, "QB": 1}         # bodies to compete for


def sim(seed):
    rng = random.Random(seed)
    pool = [p for p in PROJ if p["pos"] in POS and (p["proj_value"] or 0) > 0]
    priced = [(p, field_price(p, rng)) for p in pool]
    priced.sort(key=lambda x: -x[1])       # board order: pricey go first (§14)

    got = []
    spent_pos = defaultdict(int)
    n_pos = defaultdict(int)

    for p, price in priced:
        pos = p["pos"]
        if n_pos[pos] >= TARGET_N[pos]:
            continue
        mb = harry_max_bid(p)
        if price > mb:                     # field outbids Harry -> misses
            continue
        # spend within this position's envelope, reserving $1 for each still-needed body
        bodies_left_pos = TARGET_N[pos] - n_pos[pos]
        env_left = ENVELOPE[pos] - spent_pos[pos]
        if price > env_left - (bodies_left_pos - 1):
            continue
        pay = max(1, min(int(round(price)) + 1, int(mb), env_left - (bodies_left_pos - 1)))
        got.append({"pos": pos, "tier": p["tier"], "name": p["name"],
                    "proj": p["proj_value"], "pay": pay})
        spent_pos[pos] += pay
        n_pos[pos] += 1

    # any unfilled target bodies -> $1 darts (cheap fills that fell through)
    for pos in POS:
        while n_pos[pos] < TARGET_N[pos]:
            got.append({"pos": pos, "tier": pos + "-dart", "name": "$1 dart",
                        "proj": 0, "pay": 1})
            n_pos[pos] += 1; spent_pos[pos] += 1
    return got, sum(r["pay"] for r in got)


def main():
    print("=" * 84)
    print("ANALYSIS 17 — 2026 DRAFT SIMULATIONS (10 runs): target roster shape & budget")
    print("=" * 84)
    runs = [sim(s) for s in range(101, 111)]

    # per-run summary
    for i, (roster, spent) in enumerate(runs, 1):
        bypos = defaultdict(lambda: [0, 0])
        for r in roster:
            bypos[r["pos"]][0] += 1; bypos[r["pos"]][1] += r["pay"]
        top = sorted(roster, key=lambda r: -r["pay"])[:3]
        tag = ", ".join(f"{r['tier']} ${r['pay']}" for r in top)
        split = " ".join(f"{p}{bypos[p][0]}=${bypos[p][1]}" for p in POS if bypos[p][0])
        print(f" run{i:>2}: ${spent:<3} | {split} | top: {tag}")

    # aggregate target template
    print("\n### Target roster template (median across 10 runs; 8 spendable starters + $1 DST)")
    print("-" * 84)
    slot_prices = defaultdict(list)
    slot_tiers = defaultdict(list)
    posspend = defaultdict(list)
    for roster, spent in runs:
        byp = defaultdict(list)
        for r in roster:
            byp[r["pos"]].append(r)
        for pos, rs in byp.items():
            rs.sort(key=lambda r: -r["pay"])
            for rank, r in enumerate(rs, 1):
                slot_prices[(pos, rank)].append(r["pay"])
                slot_tiers[(pos, rank)].append(r["tier"])
            posspend[pos].append(sum(r["pay"] for r in rs))
    def mode(xs):
        return statistics.mode(xs) if xs else "-"
    print(f"   {'slot':10}{'freq':>6}{'med $':>7}{'range $':>10}{'typical tier':>14}")
    for pos in POS:
        rank = 1
        while (pos, rank) in slot_prices:
            pr = slot_prices[(pos, rank)]
            freq = f"{len(pr)}/10"
            lo, hi = min(pr), max(pr)
            print(f"   {pos+' #'+str(rank):10}{freq:>6}{int(statistics.median(pr)):>7}"
                  f"{f'{lo}-{hi}':>10}{mode(slot_tiers[(pos,rank)]):>14}")
            rank += 1
    print("\n### Budget split (median $ on starters by position)")
    for pos in POS:
        if posspend[pos]:
            print(f"   {pos}: ${int(statistics.median(posspend[pos]))} "
                  f"(range ${min(posspend[pos])}-{max(posspend[pos])})")
    tot_start = [sum(r['pay'] for r in roster) for roster, _ in runs]
    print(f"   8-starter total: ${int(statistics.median(tot_start))}  "
          f"(+ ~$1 DST + $1 bench darts to reach $200)")


if __name__ == "__main__":
    main()
