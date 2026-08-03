#!/usr/bin/env python3
"""Analysis 13: framework v2 backtest — do the ALLOCATION RULES beat naive
projection-buying, out-of-sample (test 2024-25)?

Two strategies buy from the SAME real player pool at ACTUAL prices, filling a
legal lineup (1QB/2RB/2WR/1TE/2FLEX/1DST + bench) under $200:
  BASELINE : buy best available by raw Elboberto projected $ (budget-aware).
  RULES    : same, but skip the fade zones (WR $8-24, non-elite QB $8-40) and
             prioritize RB + elite + value QB/TE.
Compare each roster's realized starter VORP to the actual field that year.
(Caveat: not a full re-auction — prices are held at actual; indicative, not exact.)
"""
import json
import os
import re
import statistics
from collections import defaultdict
import lib
import a5_draft_value as a5

PROJ = json.load(open(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                                   "draft_sheets", "elboberto_projections.json")))
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
TEST = [2024, 2025]
START = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
FLEX_POS = ("RB", "WR", "TE")
NFLEX, NBENCH, BUDGET, ROSTER = 2, 6, 200, 15


def norm(n):
    n = re.sub(r"\([^)]*\)", "", str(n))
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def year_pool(yr):
    pl = {norm(p["name"]): p for p in PROJ[str(yr)] if p.get("proj_value") is not None}
    pts = a5.all_player_points(yr)
    rep = a5.replacement_levels(yr)
    pool = []
    for pk in lib.draft_picks(yr):
        if pk["is_keeper"] or pk["cost"] < 1:
            continue
        pos = pk["pos"]
        if pos not in ("QB", "RB", "WR", "TE", "DST"):
            continue
        e = pl.get(norm(pk["name"]))
        proj = e["proj_value"] if e else 0
        _, ap = pts.get(pk["playerId"], (pos, 0.0))
        pool.append({"name": pk["name"], "pos": pos, "paid": pk["cost"],
                     "proj": proj, "vorp": ap - rep.get(pos, 0)})
    return pool


def legal_starter_vorp(roster):
    ps = sorted(roster, key=lambda r: -r["vorp"])
    need = dict(START); flex = NFLEX; tot = 0
    for r in ps:
        if need.get(r["pos"], 0) > 0:
            need[r["pos"]] -= 1; tot += r["vorp"]
        elif r["pos"] in FLEX_POS and flex > 0:
            flex -= 1; tot += r["vorp"]
    return tot


def buy(pool, rules):
    """Greedy budget-aware buyer. Reserve $1 per remaining slot."""
    got, spent, need, flex, bench = [], 0, dict(START), NFLEX, NBENCH
    def slot_for(pos):
        if need.get(pos, 0) > 0: return "start"
        if pos in FLEX_POS and flex > 0: return "flex"
        if bench > 0: return "bench"
        return None
    # value = projection; rules exclude fade zones & tilt to RB/elite
    def val(r):
        v = r["proj"]
        if rules:
            if r["pos"] == "WR" and 8 <= r["paid"] <= 24:   # WR dead zone
                return -1
            if r["pos"] == "QB" and 8 <= r["paid"] <= 40 and r["proj"] < 40:  # QB dead zone
                return -1
            if r["pos"] == "RB":
                v *= 1.3           # prioritize RB (converts deepest)
            if r["proj"] >= 25:
                v *= 1.15          # prioritize elite (converts)
        return v
    cand = sorted(pool, key=lambda r: -val(r))
    while len(got) < ROSTER:
        slots_left = ROSTER - len(got)
        bought = False
        for r in cand:
            if r in got or val(r) < 0:
                continue
            if slot_for(r["pos"]) is None:
                continue
            if spent + r["paid"] > BUDGET - (slots_left - 1):  # reserve $1/slot
                continue
            s = slot_for(r["pos"])
            if s == "start": need[r["pos"]] -= 1
            elif s == "flex": flex -= 1
            else: bench -= 1
            got.append(r); spent += r["paid"]; bought = True
            break
        if not bought:
            # fill remaining with cheapest $1-2 that fit a slot
            fillers = sorted([r for r in pool if r not in got and 1 <= r["paid"] <= 2
                              and slot_for(r["pos"])], key=lambda r: r["paid"])
            if not fillers:
                break
            r = fillers[0]; s = slot_for(r["pos"])
            if s == "start": need[r["pos"]] -= 1
            elif s == "flex": flex -= 1
            else: bench -= 1
            got.append(r); spent += r["paid"]
    return got, spent


def field_vorp(yr):
    pts = a5.all_player_points(yr); rep = a5.replacement_levels(yr)
    byteam = defaultdict(list)
    for pk in lib.draft_picks(yr):
        pos = pk["pos"]
        if pos not in ("QB", "RB", "WR", "TE", "DST"):
            continue
        _, ap = pts.get(pk["playerId"], (pos, 0.0))
        byteam[pk["teamId"]].append({"pos": pos, "vorp": ap - rep.get(pos, 0)})
    return sorted((legal_starter_vorp(v) for v in byteam.values()), reverse=True)


def main():
    print("=" * 88)
    print("ANALYSIS 13 — FRAMEWORK v2 BACKTEST: do allocation RULES beat naive projection?")
    print("=" * 88)
    print("(buy from real pool at actual prices; realized starter VORP vs field. Indicative.)\n")
    for yr in TEST:
        pool = year_pool(yr)
        fld = field_vorp(yr)
        for label, rules in [("BASELINE (buy by projection)", False), ("RULES v2", True)]:
            roster, spent = buy(pool, rules)
            sv = legal_starter_vorp(roster)
            rank = 1 + sum(1 for f in fld if f > sv)
            rb = sum(r["paid"] for r in roster if r["pos"] == "RB")
            print(f"   {yr} {label:30} starterVORP {sv:>4.0f}  spent ${spent:<3} "
                  f"RB${rb:<3} -> ~#{rank}/{len(fld)+1}")
        print(f"        field: best {fld[0]:.0f}, median {statistics.median(fld):.0f}\n")


if __name__ == "__main__":
    main()
