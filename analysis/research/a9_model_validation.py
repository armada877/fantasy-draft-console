#!/usr/bin/env python3
"""Analysis 9: validate the Elboberto baseline vs ESPN actuals, 2022-2025.

Now using the AUTHORITATIVE master projections (projected FPTS + StartVBD +
projected $, per player, 2022-2026). Answers, across 4 years:
  - Does the model's projected $ / VBD predict production better than the room's
    own price? (how much to trust the baseline over live bidding)
  - Does projected value CONVERT by position? (which positions the model nails)
  - The elite-RB check (does the room's willingness to overpay top RB pay off?)
2026 has no actuals yet — it's the board input (built separately).
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


def norm(n):
    n = re.sub(r"\([^)]*\)", "", str(n))
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def proj_lookup(year):
    d = {}
    for p in PROJ[str(year)]:
        if p.get("proj_value") is not None:
            d[norm(p["name"])] = p
    return d


def corr(xs, ys):
    if len(xs) < 3:
        return 0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    dx = sum((x-mx)**2 for x in xs)**0.5
    dy = sum((y-my)**2 for y in ys)**0.5
    return num/(dx*dy) if dx and dy else 0


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
            pos, ap = pts_map.get(pk["playerId"], (pk["pos"], 0.0))
            rows.append({
                "year": yr, "name": pk["name"], "pos": pk["pos"], "paid": pk["cost"],
                "proj_val": e["proj_value"] if e else None,
                "proj_vbd": (e.get("start_vbd") if e else None),
                "act_pts": ap, "act_vorp": ap - rep.get(pos, 0),
            })
    return rows


def report():
    rows = build()
    m = [r for r in rows if r["proj_val"] is not None and r["paid"] >= 1]
    print("=" * 92)
    print("ANALYSIS 9 — ELBOBERTO MODEL vs ACTUALS (authoritative projections, 2022-2025)")
    print("=" * 92)
    tot = [r for r in rows if r["paid"] >= 1]
    print(f"\nMatched {len(m)}/{len(tot)} non-keeper picks to a projection "
          f"({100*len(m)//len(tot)}%).")

    print("\n### A. Does the model beat the room? (correlation with actual production)")
    print("-" * 92)
    print(f"{'Year':6}{'n':>5}{'proj$ vs pts':>14}{'projVBD vs VORP':>17}{'PAID vs pts':>13}{'winner':>10}")
    print("-" * 92)
    for yr in YEARS:
        g = [r for r in m if r["year"] == yr]
        c_proj = corr([r["proj_val"] for r in g], [r["act_pts"] for r in g])
        c_vbd = corr([r["proj_vbd"] for r in g if r["proj_vbd"] is not None],
                     [r["act_vorp"] for r in g if r["proj_vbd"] is not None])
        c_paid = corr([r["paid"] for r in g], [r["act_pts"] for r in g])
        winner = "MODEL" if c_proj > c_paid else "room"
        print(f"{yr:<6}{len(g):>5}{c_proj:>14.2f}{c_vbd:>17.2f}{c_paid:>13.2f}{winner:>10}")
    cp = corr([r["proj_val"] for r in m], [r["act_pts"] for r in m])
    cpd = corr([r["paid"] for r in m], [r["act_pts"] for r in m])
    print("-" * 92)
    print(f"{'POOLED':<6}{len(m):>5}{cp:>14.2f}{'':>17}{cpd:>13.2f}"
          f"{('MODEL' if cp>cpd else 'room'):>10}")

    print("\n### B. Does projected value CONVERT by position? (2022-2025 pooled)")
    print("-" * 92)
    print(f"{'Pos':5}{'n':>5}{'avg proj$':>11}{'avg paid$':>11}{'avg projVBD':>13}{'avg actVORP':>13}{'convert?':>10}")
    print("-" * 92)
    for pos in ["RB", "WR", "TE", "QB"]:
        g = [r for r in m if r["pos"] == pos]
        gv = [r for r in g if r["proj_vbd"] is not None]
        if not g:
            continue
        pv = statistics.mean(r["proj_vbd"] for r in gv) if gv else 0
        av = statistics.mean(r["act_vorp"] for r in g)
        conv = "yes" if av > 0.5 * pv and av > 0 else "weak" if av > 0 else "NO"
        print(f"{pos:5}{len(g):>5}{statistics.mean(r['proj_val'] for r in g):>11.1f}"
              f"{statistics.mean(r['paid'] for r in g):>11.1f}{pv:>13.0f}{av:>13.0f}{conv:>10}")

    print("\n### C. Elite-RB check — top-10 projected RB each year: did paying up work?")
    print("-" * 92)
    elite = []
    for yr in YEARS:
        g = sorted([r for r in m if r["pos"] == "RB"], key=lambda r: -(r["proj_val"] or 0))
        elite += [r for r in [r for r in m if r["year"] == yr and r["pos"] == "RB"]
                  if r["proj_val"] and r["proj_val"] >= 30]
    if elite:
        print(f"   n={len(elite)}  avg proj$ {statistics.mean(r['proj_val'] for r in elite):.0f}  "
              f"avg paid$ {statistics.mean(r['paid'] for r in elite):.0f}  "
              f"avg actVORP {statistics.mean(r['act_vorp'] for r in elite):.0f}  "
              f"hit% {100*sum(1 for r in elite if r['act_vorp']>0)//len(elite)}")


if __name__ == "__main__":
    report()
