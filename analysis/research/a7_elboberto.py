#!/usr/bin/env python3
"""Analysis 7: cross-source — Elboberto BASELINE PROJECTIONS vs ESPN ACTUALS.

Sources per player-year (2022-2024):
  - Elboberto projected auction $  (the pre-draft model the user drafts from)
  - ESPN actual $ paid             (authoritative; the room's real price)
  - ESPN actual production         (season fantasy pts -> VORP)

Questions:
  A. Model vs market: where does the room pay above/below the model?
  B. Model accuracy: does projected $ predict actual production?
  C. Actionable edges: value the room leaves that the model would catch.
"""
import json
import os
import re
import statistics
from collections import defaultdict
import lib
import a5_draft_value as a5

ELBO = json.load(open(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                                   "draft_sheets", "elboberto_projections.json")))
# 2022 projections tab is on a different/mis-scaled basis (sum ~$2960 over 65
# players, values to $138 — inconsistent with the $200 auction), so we use the
# clean, same-scale 2023 & 2024 baselines only.
YEARS = [2023, 2024]
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(n):
    n = str(n).lower()
    n = re.sub(r"[.'`]", "", n)
    n = re.sub(r"[-/]", " ", n)
    toks = [t for t in re.split(r"\s+", n) if t and t not in SUFFIX]
    # drop trailing team code if present (results tabs); projections are clean
    if toks and len(toks[-1]) <= 3 and toks[-1].isalpha() and len(toks) > 2:
        pass  # keep — risk of dropping real name; projections have no suffix
    return " ".join(toks)


def elbo_lookup(year):
    d = {}
    for p in ELBO[str(year)]:
        if p["proj_value"] is not None:
            d[norm(p["name"])] = p
    return d


def py():
    print()


def build_matched():
    """[{year, name, pos, proj, paid, pts, vorp, matched}]"""
    rows = []
    for yr in YEARS:
        el = elbo_lookup(yr)
        pts_map = a5.all_player_points(yr)
        rep = a5.replacement_levels(yr)
        used = set()
        for p in lib.draft_picks(yr):
            if p["is_keeper"]:
                continue  # keepers aren't auction-priced normally
            nm = norm(p["name"])
            e = el.get(nm)
            pos, pp = pts_map.get(p["playerId"], (p["pos"], 0.0))
            vorp = pp - rep.get(pos, 0)
            if e:
                used.add(nm)
            rows.append({
                "year": yr, "name": p["name"], "pos": p["pos"],
                "proj": e["proj_value"] if e else None,
                "paid": p["cost"], "pts": pp, "vorp": vorp,
                "mgr": p["manager"],
            })
    return rows


def corr(xs, ys):
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x-mx)*(y-my) for x, y in zip(xs, ys))
    dx = sum((x-mx)**2 for x in xs)**0.5
    dy = sum((y-my)**2 for y in ys)**0.5
    return num/(dx*dy) if dx and dy else 0


def report():
    rows = build_matched()
    m = [r for r in rows if r["proj"] is not None]
    print("=" * 92)
    print("ANALYSIS 7 — ELBOBERTO PROJECTIONS vs ESPN ACTUALS (2022-2024)")
    print("=" * 92)
    print(f"\nMatched {len(m)}/{len(rows)} non-keeper draft picks to an Elboberto projection "
          f"({100*len(m)//len(rows)}%).")
    for yr in YEARS:
        yrows = [r for r in rows if r["year"] == yr]
        ym = [r for r in yrows if r["proj"] is not None]
        print(f"   {yr}: {len(ym)}/{len(yrows)} matched ({100*len(ym)//max(len(yrows),1)}%)")

    # A. model vs market
    print("\n### A. Model vs market — did the room pay above/below Elboberto's projection?")
    print("-" * 92)
    print(f"{'Pos':5}{'n':>5}{'avg proj $':>12}{'avg paid $':>12}{'paid-proj':>12}{'room tendency':>18}")
    print("-" * 92)
    for pos in ["RB", "WR", "TE", "QB"]:
        g = [r for r in m if r["pos"] == pos and r["paid"] >= 1]
        if not g:
            continue
        ap = statistics.mean(r["proj"] for r in g)
        apd = statistics.mean(r["paid"] for r in g)
        diff = apd - ap
        tend = "overpays vs model" if diff > 2 else "underpays vs model" if diff < -2 else "≈ model"
        print(f"{pos:5}{len(g):>5}{ap:>12.1f}{apd:>12.1f}{diff:>+12.1f}{tend:>18}")

    # B. model accuracy: proj vs actual production
    print("\n### B. Model accuracy — does projected $ predict actual production?")
    print("-" * 92)
    allp = [r for r in m if r["paid"] >= 1]
    print(f"   corr(projected $, actual points) = {corr([r['proj'] for r in allp], [r['pts'] for r in allp]):.2f}")
    print(f"   corr(projected $, actual VORP)   = {corr([r['proj'] for r in allp], [r['vorp'] for r in allp]):.2f}")
    print(f"   corr(actual PAID, actual points) = {corr([r['paid'] for r in allp], [r['pts'] for r in allp]):.2f}")
    print("   (if proj-vs-points >= paid-vs-points, the model predicts production at least as well")
    print("    as the room's own prices — i.e. trust the baseline over the room's bidding.)")

    # C. edges: biggest gaps between model and room price
    print("\n### C. Actionable edges — where room price diverged most from the model")
    print("-" * 92)
    for r in m:
        r["gap"] = r["paid"] - r["proj"]
    over = sorted([r for r in m if r["paid"] >= 5], key=lambda r: -r["gap"])[:10]
    under = sorted([r for r in m if r["proj"] >= 5], key=lambda r: r["gap"])[:10]
    print("Room OVERPAID vs model (fade these types — paid >> projected):")
    for r in over:
        print(f"   {r['year']} {r['name']:22} {r['pos']:3} proj ${r['proj']:.0f} paid ${r['paid']:.0f} "
              f"(+{r['gap']:.0f})  actual {r['pts']:.0f}pts VORP {r['vorp']:.0f}  [{r['mgr']}]")
    print("\nRoom got BARGAINS vs model (paid << projected — pounce targets):")
    for r in under:
        print(f"   {r['year']} {r['name']:22} {r['pos']:3} proj ${r['proj']:.0f} paid ${r['paid']:.0f} "
              f"({r['gap']:.0f})  actual {r['pts']:.0f}pts VORP {r['vorp']:.0f}  [{r['mgr']}]")


if __name__ == "__main__":
    report()
