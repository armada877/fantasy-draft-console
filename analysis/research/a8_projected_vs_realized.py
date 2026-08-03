#!/usr/bin/env python3
"""Analysis 8: VALUE BOTH WAYS for 2025 — projected vs realized.

The 2025 draft tool gives projected POINTS + projected auction $. We compute:
  - PROJECTED VORP  (projected points - projected replacement)  -> draft-time value
  - REALIZED  VORP  (actual points    - actual replacement)     -> did it convert
and line both up against actual price paid. This is the template for the 2026 board:
draft on projected value, but weight it by how well projections have converted.
"""
import json
import os
import re
import statistics
from collections import defaultdict
import lib
import a5_draft_value as a5

PROJ = json.load(open(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                                   "draft_sheets", "elboberto_projections.json")))["2025"]
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
STARTABLE = a5.STARTABLE


def norm(n):
    n = re.sub(r"\([^)]*\)", "", str(n))          # drop "(CIN - WR)"
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def proj_replacement():
    """Replacement level from PROJECTED points (rank Nth projected at each pos)."""
    by = defaultdict(list)
    for p in PROJ:
        if p["proj_points"] is not None and p["pos"] in STARTABLE:
            by[p["pos"]].append(p["proj_points"])
    rep = {}
    for pos, n in STARTABLE.items():
        v = sorted(by.get(pos, []), reverse=True)
        rep[pos] = v[n-1] if len(v) >= n and n > 0 else 0
    return rep


def corr(xs, ys):
    if len(xs) < 3:
        return 0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    dx = sum((x-mx)**2 for x in xs)**0.5
    dy = sum((y-my)**2 for y in ys)**0.5
    return num/(dx*dy) if dx and dy else 0


def build():
    prep = proj_replacement()
    proj_by_name = {}
    for p in PROJ:
        if p["proj_points"] is not None:
            p = dict(p)
            p["proj_vorp"] = p["proj_points"] - prep.get(p["pos"], 0)
            proj_by_name[norm(p["name"])] = p
    # ESPN 2025 actuals
    pts_map = a5.all_player_points(2025)
    rep_act = a5.replacement_levels(2025)
    rows = []
    for pk in lib.draft_picks(2025):
        e = proj_by_name.get(norm(pk["name"]))
        pos, ap = pts_map.get(pk["playerId"], (pk["pos"], 0.0))
        rows.append({
            "name": pk["name"], "pos": pk["pos"], "paid": pk["cost"], "mgr": pk["manager"],
            "proj_val": e["proj_value"] if e else None,
            "proj_pts": e["proj_points"] if e else None,
            "proj_vorp": e["proj_vorp"] if e else None,
            "act_pts": ap, "act_vorp": ap - rep_act.get(pos, 0),
        })
    return rows


def report():
    rows = build()
    matched = [r for r in rows if r["proj_vorp"] is not None]
    print("=" * 94)
    print("ANALYSIS 8 — VALUE BOTH WAYS, 2025 (projected vs realized)")
    print("=" * 94)
    print(f"\nMatched {len(matched)}/{len(rows)} 2025 draft picks to a projected-points row.")

    # A. projection accuracy
    prod = [r for r in matched if r["paid"] >= 1]
    print("\n### A. Did the 2025 projections convert? (accuracy)")
    print(f"   corr(projected pts,  actual pts)  = {corr([r['proj_pts'] for r in prod], [r['act_pts'] for r in prod]):.2f}")
    print(f"   corr(projected VORP, actual VORP) = {corr([r['proj_vorp'] for r in prod], [r['act_vorp'] for r in prod]):.2f}")
    print(f"   corr(price paid,     actual VORP) = {corr([r['paid'] for r in prod], [r['act_vorp'] for r in prod]):.2f}")

    # B. best DRAFT-TIME value (projected) and whether it hit
    print("\n### B. Best projected value at draft time — proj VORP per projected $  (top 12)")
    print("     (this is what the board would have told you to target)")
    print("-" * 94)
    cand = [r for r in matched if r["proj_val"] and r["proj_val"] >= 3 and r["proj_vorp"] > 0]
    for r in sorted(cand, key=lambda r: -r["proj_vorp"]/r["proj_val"])[:12]:
        hit = "HIT" if r["act_vorp"] > 0 else "miss"
        print(f"   {r['name']:22} {r['pos']:3} proj${r['proj_val']:.0f} projVORP{r['proj_vorp']:>4.0f} "
              f"-> paid${r['paid']:<3.0f} actVORP{r['act_vorp']:>5.0f}  [{hit}]")

    # C. position-level projected vs realized
    print("\n### C. By position — projected vs realized (matched, paid $1+)")
    print("-" * 94)
    print(f"{'Pos':5}{'n':>4}{'avg proj$':>10}{'avg paid$':>10}{'avg projVORP':>14}{'avg actVORP':>13}")
    for pos in ["RB", "WR", "TE", "QB"]:
        g = [r for r in prod if r["pos"] == pos]
        if not g:
            continue
        print(f"{pos:5}{len(g):>4}{statistics.mean(r['proj_val'] for r in g):>10.1f}"
              f"{statistics.mean(r['paid'] for r in g):>10.1f}"
              f"{statistics.mean(r['proj_vorp'] for r in g):>14.0f}"
              f"{statistics.mean(r['act_vorp'] for r in g):>13.0f}")

    # D. biggest projection misses (bought high on projection, flopped) & hidden gems
    print("\n### D. Projection busts (high projected value, negative realized)")
    print("-" * 94)
    busts = [r for r in prod if r["proj_vorp"] > 40 and r["act_vorp"] < 0]
    for r in sorted(busts, key=lambda r: r["act_vorp"])[:8]:
        print(f"   {r['name']:22} {r['pos']:3} projVORP{r['proj_vorp']:>4.0f} paid${r['paid']:<3.0f} "
              f"-> actVORP{r['act_vorp']:>5.0f}")
    print("\n### D2. Hidden gems (low/zero projected value, big realized) — waiver-tier upside")
    gems = [r for r in prod if (r["proj_vorp"] or 0) < 20 and r["act_vorp"] > 60]
    for r in sorted(gems, key=lambda r: -r["act_vorp"])[:8]:
        print(f"   {r['name']:22} {r['pos']:3} projVORP{(r['proj_vorp'] or 0):>4.0f} paid${r['paid']:<3.0f} "
              f"-> actVORP{r['act_vorp']:>5.0f}")


if __name__ == "__main__":
    report()
