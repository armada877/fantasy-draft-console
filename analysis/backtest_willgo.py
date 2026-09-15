#!/usr/bin/env python3
"""Backtest the console's OWN will-go estimator out-of-sample, and size its top-end bias.

Earlier attempts to correct will-go were sized against a PROXY (proj_value x flat
positional multiplier). That is not the estimator the console ships: the console takes a
SECOND price across every rival that still has budget and a need, and caps each rival's bid
by their calibrated max-buy. Those caps bind hardest exactly at the top of the board, so a
bias measured on the proxy does not transfer. That mistake produced a bad deploy once; this
file exists so the correction is measured on the real thing.

Method (strictly out-of-sample):
  1. Calibrate opponent profiles on seasons <= CUTOFF only, replicating
     a18.build_agents()'s logic here rather than importing it, so the cutoff is honest and
     a18 stays untouched.
  2. Reconstruct the held-out season's board: `worth` = that year's elboberto proj_value
     (the workbook's own auction value, the closest available stand-in for the console's
     recomputed worth), managers = the teams that actually played that season.
  3. Run the console's predictPrice at draft-open state for every player.
  4. Compare to the REAL price paid, bucketed by value, and report bias.

Run:  PYTHONPATH=analysis python3 analysis/backtest_willgo.py
"""
import json
import os
import re
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import lib  # noqa: E402

PROJ = json.load(open(os.path.join(ROOT, "draft_sheets", "elboberto_projections.json")))
POS = ("QB", "RB", "WR", "TE")
LEAGUE_MULT = {"QB": 0.41, "RB": 1.31, "WR": 1.47, "TE": 0.74}
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(n):
    n = re.sub(r"[^a-z ]", "", str(n).lower())
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def agents_through(cutoff):
    """a18.build_agents() logic, restricted to seasons <= cutoff (no leakage)."""
    paid = defaultdict(lambda: defaultdict(float))
    projs = defaultdict(lambda: defaultdict(float))
    cnt = defaultdict(lambda: defaultdict(int))
    for yr in [y for y in (2022, 2023, 2024, 2025) if y <= cutoff]:
        pl = {norm(p["name"]): p for p in PROJ[str(yr)] if p.get("proj_value") is not None}
        for p in lib.draft_picks(yr):
            if p["is_keeper"] or p["cost"] < 1:
                continue
            e = pl.get(norm(p["name"]))
            if e and (e["proj_value"] or 0) >= 3:
                paid[p["manager"]][p["pos"]] += p["cost"]
                projs[p["manager"]][p["pos"]] += e["proj_value"]
                cnt[p["manager"]][p["pos"]] += 1
    top3, maxbuy = defaultdict(list), defaultdict(list)
    for yr in [y for y in lib.AUCTION_SEASONS if y <= cutoff]:
        byteam = defaultdict(list)
        for p in lib.draft_picks(yr):
            byteam[p["teamId"]].append(p)
        for tid, ps in byteam.items():
            m = lib.manager(yr, tid)
            costs = sorted((x["cost"] for x in ps), reverse=True)
            sp = sum(costs) or 1
            top3[m].append(100 * sum(costs[:3]) / sp)
            maxbuy[m].append(costs[0] if costs else 0)
    out = {}
    for m in top3:
        mult = {}
        for pos in POS:
            if cnt[m].get(pos, 0) >= 3 and projs[m].get(pos, 0) > 0:
                mult[pos] = paid[m][pos] / projs[m][pos]
            else:
                mult[pos] = LEAGUE_MULT[pos]
        out[m] = {"mult": mult, "conc": statistics.mean(top3[m]),
                  "maxbuy": max(maxbuy[m]) * 1.15 if maxbuy[m] else 100}
    return out


def predict_prices(season, agents, budget=200, slots=14):
    """The console's predictPrice at draft-open, for every projected player that season."""
    afford = budget - (slots - 1)
    teams = []
    for tid in {p["teamId"] for p in lib.draft_picks(season)}:
        m = lib.manager(season, tid)
        a = agents.get(m) or {"mult": dict(LEAGUE_MULT), "conc": 50, "maxbuy": budget}
        teams.append({"name": m, **a})

    def team_bid(t, worth, pos):
        base = max(0.5, worth)
        b = base * (t["mult"].get(pos, 1))
        if t["conc"] > 72:
            if base >= 25:
                b *= 1.12
            elif base >= 8:
                b *= 0.82
        return max(0.0, min(b, afford, t["maxbuy"]))

    out = {}
    for e in PROJ[str(season)]:
        v = e.get("proj_value") or 0
        if v < 1 or e["pos"] not in POS:
            continue
        bids = sorted((b for b in (team_bid(t, v, e["pos"]) for t in teams) if b >= 1),
                      reverse=True)
        est = 1 if not bids else min(bids[0], (bids[1] if len(bids) > 1 else 1) + 1)
        out[norm(e["name"])] = (max(1.0, est), v, e["pos"])
    return out


def main():
    HELD = 2025
    agents = agents_through(HELD - 1)
    print(f"Opponent profiles calibrated on seasons <= {HELD-1} only "
          f"({len(agents)} managers). Predicting {HELD}.\n")
    pred = predict_prices(HELD, agents)
    actual = {}
    for p in lib.draft_picks(HELD):
        if p["is_keeper"] or p["cost"] < 1:
            continue
        actual[norm(p["name"])] = p["cost"]

    rows = []
    for k, (est, v, pos) in pred.items():
        if k in actual:
            rows.append((pos, v, est, actual[k]))
    print(f"matched {len(rows)} players that were actually bought\n")

    BANDS = [(50, 999, "worth $50+"), (35, 50, "$35-50"), (25, 35, "$25-35"),
             (15, 25, "$15-25"), (8, 15, "$8-15"), (0, 8, "under $8")]
    print(f"{'band':13}{'n':>5}{'pred':>8}{'actual':>8}{'bias':>8}{'mean|err|':>11}")
    corrections = {}
    for lo, hi, lab in BANDS:
        g = [(e, a) for (pos, v, e, a) in rows if lo <= v < hi]
        if not g:
            continue
        bias = statistics.mean(e - a for e, a in g)
        corrections[lab] = bias
        print(f"{lab:13}{len(g):>5}{statistics.mean(e for e,_ in g):>8.1f}"
              f"{statistics.mean(a for _,a in g):>8.1f}{bias:>+8.1f}"
              f"{statistics.mean(abs(e-a) for e,a in g):>11.1f}")

    print(f"\nby position, worth >= $25 (where the money goes):")
    print(f"{'pos':5}{'n':>5}{'pred':>8}{'actual':>8}{'bias':>8}")
    for pos in POS:
        g = [(e, a) for (po, v, e, a) in rows if po == pos and v >= 25]
        if not g:
            continue
        print(f"{pos:5}{len(g):>5}{statistics.mean(e for e,_ in g):>8.1f}"
              f"{statistics.mean(a for _,a in g):>8.1f}"
              f"{statistics.mean(e-a for e,a in g):>+8.1f}")

    top = sorted(rows, key=lambda r: -r[1])[:12]
    print(f"\ntop 12 by projected value — the numbers you actually stare at:")
    print(f"   {'pos':5}{'worth':>7}{'predicted':>11}{'actual':>8}{'err':>7}")
    for pos, v, e, a in top:
        print(f"   {pos:5}{v:>7.0f}{e:>11.0f}{a:>8}{e-a:>+7.0f}")
    print(f"\n   top-12 mean bias: {statistics.mean(e-a for _,_,e,a in top):+.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
