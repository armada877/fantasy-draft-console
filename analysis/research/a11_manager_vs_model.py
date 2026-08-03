#!/usr/bin/env python3
"""Analysis 11: manager vs the model — who over/underpays the Elboberto baseline,
by position, and does discipline predict winning?

Feeds framework component #2 ("what they'll go for"): league + per-manager price
premium over model value, so we can PREDICT the room's bid on any 2026 player.
"""
import json
import os
import re
import statistics
from collections import defaultdict
import lib
import a4_success as a4

PROJ = json.load(open(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                                   "draft_sheets", "elboberto_projections.json")))
YEARS = [2022, 2023, 2024, 2025]
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(n):
    n = re.sub(r"\([^)]*\)", "", str(n))
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def proj_lookup(year):
    return {norm(p["name"]): p for p in PROJ[str(year)] if p.get("proj_value") is not None}


def build():
    rows = []
    for yr in YEARS:
        pl = proj_lookup(yr)
        for pk in lib.draft_picks(yr):
            if pk["is_keeper"] or pk["cost"] < 1:
                continue
            e = pl.get(norm(pk["name"]))
            if not e:
                continue
            rows.append({"yr": yr, "mgr": pk["manager"], "pos": pk["pos"],
                         "paid": pk["cost"], "proj": e["proj_value"],
                         "over": pk["cost"] - e["proj_value"]})
    return rows


def report():
    rows = build()
    print("=" * 94)
    print("ANALYSIS 11 — MANAGER vs THE MODEL (paid − Elboberto projected $, 2022-2025)")
    print("=" * 94)

    # league positional premium (base for predicting price)
    print("\n### League price premium over model, by position (predict-the-room base)")
    print("-" * 94)
    for pos in ["RB", "WR", "TE", "QB"]:
        g = [r for r in rows if r["pos"] == pos]
        prem = statistics.mean(r["over"] for r in g)
        ratio = statistics.mean(r["paid"] for r in g) / statistics.mean(r["proj"] for r in g)
        print(f"   {pos:4} n={len(g):>3}  avg paid−proj {prem:>+6.1f}   paid/proj ratio {ratio:.2f}")

    # per-manager over/underpay
    bym = defaultdict(list)
    for r in rows:
        bym[r["mgr"]].append(r)
    active = defaultdict(set)
    for yr in YEARS:
        for tid in lib.team_owner(yr):
            active[lib.manager(yr, tid)].add(yr)
    mgrs = [m for m in bym if len(active[m]) >= 3 and len(bym[m]) >= 15]

    print("\n### Per-manager discipline vs model (avg paid − proj; + = overpays)")
    print("-" * 94)
    print(f"{'Manager':17}{'picks':>6}{'overall':>9}{'RB':>7}{'WR':>7}{'TE':>7}{'QB':>7}   read")
    print("-" * 94)
    def posmean(m, pos):
        g = [r for r in bym[m] if r["pos"] == pos]
        return statistics.mean(r["over"] for r in g) if g else 0
    for m in sorted(mgrs, key=lambda m: statistics.mean(r["over"] for r in bym[m])):
        ov = statistics.mean(r["over"] for r in bym[m])
        rb, wr, te, qb = (posmean(m, p) for p in ["RB", "WR", "TE", "QB"])
        read = "disciplined (value)" if ov < -1 else "overpays" if ov > 2 else "at model"
        hot = max([("RB", rb), ("WR", wr), ("TE", te), ("QB", qb)], key=lambda x: x[1])
        if hot[1] > 4:
            read += f", inflates {hot[0]}"
        print(f"{m:17}{len(bym[m]):>6}{ov:>+9.1f}{rb:>+7.0f}{wr:>+7.0f}{te:>+7.0f}{qb:>+7.0f}   {read}")

    # discipline vs success
    print("\n### Does discipline predict winning?")
    print("-" * 94)
    srows = a4.build()
    succ = defaultdict(list)
    for s in srows:
        succ[s["mgr"]].append(s)
    pairs = []
    for m in mgrs:
        disc = statistics.mean(r["over"] for r in bym[m])   # + = overpays
        pf = statistics.mean(s["pf_z"] for s in succ[m]) if succ.get(m) else 0
        pairs.append((disc, pf, m))
    xs = [p[0] for p in pairs]; ys = [p[1] for p in pairs]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    den = (sum((x-mx)**2 for x in xs)**0.5) * (sum((y-my)**2 for y in ys)**0.5)
    r = num/den if den else 0
    print(f"   corr(overpay-vs-model, career PF-z) = {r:+.2f}")
    print("   (negative => paying UNDER model / discipline associates with more points)")
    for disc, pf, m in sorted(pairs):
        print(f"   {m:17} overpay {disc:>+5.1f}  PF-z {pf:>+5.2f}")


if __name__ == "__main__":
    report()
