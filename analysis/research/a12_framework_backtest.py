#!/usr/bin/env python3
"""Analysis 12: the draft-navigation FRAMEWORK, backtested out-of-sample.

Framework (all from the Elboberto baseline + historical recalibration):
  WORTH(VORP)   = projected VBD x realization_ratio(pos, tier)
                  (how that position/tier historically converts projection->reality)
  EXPECTED PRICE = projected $ x room_multiplier(pos)   (what the room pays vs model)
  MAX BID / target logic follows from WORTH vs EXPECTED PRICE.

Backtest: TRAIN ratios+multipliers on 2022-2023, TEST on 2024-2025 (unseen). Validate:
  A. Does WORTH predict actual VORP better than raw projection AND better than price?
  B. Do framework "targets" out-return "fades" in realized VORP per dollar?
  C. Roster sim: a lineup filled by framework value vs the actual field.
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
TRAIN = [2022, 2023]
TEST = [2024, 2025]


def norm(n):
    n = re.sub(r"\([^)]*\)", "", str(n))
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def tier(v):
    return "elite" if v >= 25 else "mid" if v >= 8 else "cheap"


def rows_for(years):
    out = []
    for yr in years:
        pl = {norm(p["name"]): p for p in PROJ[str(yr)] if p.get("proj_value") is not None}
        pts = a5.all_player_points(yr)
        rep = a5.replacement_levels(yr)
        for pk in lib.draft_picks(yr):
            if pk["is_keeper"] or pk["cost"] < 1:
                continue
            e = pl.get(norm(pk["name"]))
            if not e or e.get("start_vbd") is None:
                continue
            pos, ap = pts.get(pk["playerId"], (pk["pos"], 0.0))
            if pos not in ("QB", "RB", "WR", "TE"):
                continue
            out.append({"yr": yr, "name": pk["name"], "pos": pos, "tid": pk["teamId"],
                        "proj_val": e["proj_value"], "proj_vbd": e["start_vbd"],
                        "paid": pk["cost"], "act_vorp": ap - rep.get(pos, 0)})
    return out


def corr(xs, ys):
    if len(xs) < 3:
        return 0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    n = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    d = (sum((x-mx)**2 for x in xs)**0.5)*(sum((y-my)**2 for y in ys)**0.5)
    return n/d if d else 0


def train_params(tr):
    # room multiplier per position
    mult = {}
    for pos in ["QB", "RB", "WR", "TE"]:
        g = [r for r in tr if r["pos"] == pos]
        mult[pos] = statistics.mean(r["paid"] for r in g)/statistics.mean(r["proj_val"] for r in g)
    # realization ratio per (pos,tier): actual VORP vs projected VBD
    ratio = {}
    for pos in ["QB", "RB", "WR", "TE"]:
        for t in ["elite", "mid", "cheap"]:
            g = [r for r in tr if r["pos"] == pos and tier(r["proj_val"]) == t]
            if len(g) >= 4:
                pv = statistics.mean(r["proj_vbd"] for r in g)
                av = statistics.mean(r["act_vorp"] for r in g)
                ratio[(pos, t)] = av/pv if pv else 0
            else:
                ratio[(pos, t)] = None
    return mult, ratio


def apply_framework(r, mult, ratio):
    t = tier(r["proj_val"])
    rt = ratio.get((r["pos"], t))
    if rt is None:
        rt = ratio.get((r["pos"], "mid")) or 0
    r["worth_vorp"] = r["proj_vbd"] * rt            # recalibrated worth (in VORP)
    r["exp_price"] = r["proj_val"] * mult[r["pos"]]  # predicted room price
    return r


def main():
    tr, te = rows_for(TRAIN), rows_for(TEST)
    mult, ratio = train_params(tr)
    for r in te:
        apply_framework(r, mult, ratio)

    print("=" * 92)
    print("ANALYSIS 12 — DRAFT FRAMEWORK, BACKTEST (train 2022-23, test 2024-25 unseen)")
    print("=" * 92)
    print(f"train picks {len(tr)}, test picks {len(te)}")
    print("\nTrained room multipliers:", {k: round(v, 2) for k, v in mult.items()})

    # A. predictive power out-of-sample
    print("\n### A. Predicting actual VORP on UNSEEN 2024-25 (correlation)")
    print("-" * 92)
    print(f"   framework WORTH  vs actual VORP : {corr([r['worth_vorp'] for r in te], [r['act_vorp'] for r in te]):.3f}")
    print(f"   raw Elboberto VBD vs actual VORP : {corr([r['proj_vbd'] for r in te], [r['act_vorp'] for r in te]):.3f}")
    print(f"   raw projected $   vs actual VORP : {corr([r['proj_val'] for r in te], [r['act_vorp'] for r in te]):.3f}")
    print(f"   room price PAID   vs actual VORP : {corr([r['paid'] for r in te], [r['act_vorp'] for r in te]):.3f}")

    # B. targets vs fades — value gap = worth (in $, via k) - expected price
    #    convert worth_vorp to $ with train $/VORP so gap is in dollars
    knum = sum(r["paid"] for r in tr)
    kden = sum(max(r["act_vorp"], 0) for r in tr)
    k = knum/kden if kden else 0.5
    for r in te:
        r["worth_$"] = r["worth_vorp"] * k
        r["gap"] = r["worth_$"] - r["exp_price"]
    tgt = [r for r in te if r["gap"] > 3]
    fad = [r for r in te if r["gap"] < -3]
    def vpd(g):  # realized VORP per actual dollar
        return sum(r["act_vorp"] for r in g)/sum(r["paid"] for r in g)
    print("\n### B. Framework TARGETS vs FADES on unseen years (k=$%.2f/VORP)" % k)
    print("-" * 92)
    print(f"   TARGETS (worth>price, n={len(tgt)}): avg actual VORP {statistics.mean(r['act_vorp'] for r in tgt):+.0f}, "
          f"VORP/$ {vpd(tgt):+.2f}")
    print(f"   FADES   (price>worth, n={len(fad)}): avg actual VORP {statistics.mean(r['act_vorp'] for r in fad):+.0f}, "
          f"VORP/$ {vpd(fad):+.2f}")

    # C. roster sim per test year: greedily buy by gap at ACTUAL price, respect slots+budget
    print("\n### C. Roster sim — build a $200 team by framework value at actual prices")
    print("-" * 92)
    NEED = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}
    FLEX = 2; BENCH = 7; BUDGET = 200
    for yr in TEST:
        cand = sorted([r for r in te if r["yr"] == yr and r["worth_$"] > 0],
                      key=lambda r: -r["gap"])
        got, spent = [], 0
        need = dict(NEED); flex = FLEX; bench = BENCH
        for r in cand:
            # only buy at or below our max bid (=worth), and if we'd beat expected price
            maxbid = r["worth_$"] * 0.9
            if r["paid"] > maxbid:
                continue
            if spent + r["paid"] > BUDGET:
                continue
            slot = None
            if need.get(r["pos"], 0) > 0:
                slot = "start"; need[r["pos"]] -= 1
            elif r["pos"] in ("RB", "WR", "TE") and flex > 0:
                slot = "flex"; flex -= 1
            elif bench > 0:
                slot = "bench"; bench -= 1
            else:
                continue
            got.append((r, slot)); spent += r["paid"]
        starters = [r for r, s in got if s in ("start", "flex")]
        star_vorp = sum(r["act_vorp"] for r in starters)
        # actual teams' starter VORP that year (best lineup by act_vorp)
        allpicks = [r for r in rows_for([yr])]
        byteam = defaultdict(list)
        for r in allpicks:
            byteam[r["tid"]].append(r)
        field = []
        for tid, ps in byteam.items():
            ps = sorted(ps, key=lambda r: -r["act_vorp"])
            # best legal lineup
            n = {"QB": 1, "RB": 2, "WR": 2, "TE": 1}; fl = 2; s = 0
            for r in ps:
                if n.get(r["pos"], 0) > 0:
                    n[r["pos"]] -= 1; s += r["act_vorp"]
                elif r["pos"] in ("RB", "WR", "TE") and fl > 0:
                    fl -= 1; s += r["act_vorp"]
            field.append(s)
        field.sort(reverse=True)
        rank = 1 + sum(1 for f in field if f > star_vorp)
        print(f"   {yr}: framework team starter VORP {star_vorp:.0f} (spent ${spent}, {len(starters)} starters) "
              f"-> would rank ~#{rank}/{len(field)+1}  (field best {field[0]:.0f}, median {statistics.median(field):.0f})")


if __name__ == "__main__":
    main()
