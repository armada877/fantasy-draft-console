#!/usr/bin/env python3
"""Analysis 10: where does projected value CONVERT? Position x projected-price tier.

Refines the "only RB converts" finding. For pooled 2022-2025 non-keeper picks
matched to Elboberto projections, bucket by position and by projected auction $,
and compare avg projected VBD vs avg REALIZED VORP + hit rate. Shows whether elite
WR/TE convert even though those positions fail in aggregate.
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
YEARS = [2022, 2023, 2024, 2025]
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
TIERS = [("$40+", 40, 999), ("$25-39", 25, 39), ("$15-24", 15, 24),
         ("$8-14", 8, 14), ("$3-7", 3, 7), ("$1-2", 1, 2)]


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
        pts_map = a5.all_player_points(yr)
        rep = a5.replacement_levels(yr)
        for pk in lib.draft_picks(yr):
            if pk["is_keeper"]:
                continue
            e = pl.get(norm(pk["name"]))
            if not e:
                continue
            pos, ap = pts_map.get(pk["playerId"], (pk["pos"], 0.0))
            rows.append({"pos": pk["pos"], "proj_val": e["proj_value"],
                         "paid": pk["cost"], "act_vorp": ap - rep.get(pos, 0)})
    return rows


def report():
    rows = build()
    print("=" * 90)
    print("ANALYSIS 10 — WHERE PROJECTED VALUE CONVERTS  (position x projected-$ tier, 2022-25)")
    print("=" * 90)
    print("act VORP = realized value over replacement; hit% = share with VORP>0.\n")
    for pos in ["RB", "WR", "TE", "QB"]:
        print(f"### {pos}")
        print(f"   {'proj tier':10}{'n':>4}{'avg proj$':>10}{'avg paid$':>10}{'avg actVORP':>13}{'hit%':>7}{'  verdict'}")
        for label, lo, hi in TIERS:
            g = [r for r in rows if r["pos"] == pos and lo <= (r["proj_val"] or 0) <= hi]
            if len(g) < 3:
                continue
            av = statistics.mean(r["act_vorp"] for r in g)
            hit = 100 * sum(1 for r in g if r["act_vorp"] > 0) / len(g)
            verdict = "CONVERTS" if av > 20 and hit >= 55 else "ok" if av > 0 else "fades"
            print(f"   {label:10}{len(g):>4}{statistics.mean(r['proj_val'] for r in g):>10.0f}"
                  f"{statistics.mean(r['paid'] for r in g):>10.0f}{av:>13.0f}{hit:>6.0f}%  {verdict}")
        print()

    # summary: elite (proj $25+) vs mid ($8-24) vs cheap ($1-7) by position
    print("### Summary — elite / mid / cheap realized VORP by position")
    print(f"   {'pos':5}{'elite($25+)':>13}{'mid($8-24)':>12}{'cheap($1-7)':>13}")
    for pos in ["RB", "WR", "TE", "QB"]:
        def band(lo, hi):
            g = [r for r in rows if r["pos"] == pos and lo <= (r["proj_val"] or 0) <= hi]
            return statistics.mean(r["act_vorp"] for r in g) if g else float("nan")
        print(f"   {pos:5}{band(25,999):>13.0f}{band(8,24):>12.0f}{band(1,7):>13.0f}")


if __name__ == "__main__":
    report()
