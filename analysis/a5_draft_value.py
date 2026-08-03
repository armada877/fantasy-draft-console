#!/usr/bin/env python3
"""Analysis 5: draft VALUE — where the auction over/under-pays, via VORP.

For every drafted player (auction era) we have real $ cost and actual season
fantasy points. We compute Value Over Replacement (VORP) per position, then the
PRICE of marginal points ($/VORP) by position and price tier. That exposes where
the league overpays (RB) vs where value hides (WR/TE), and how often expensive
buys bust.

Starting lineup: 1QB / 2RB / 2WR / 1TE / 2FLEX(RB/WR/TE) / 1DST, 12 teams, half-PPR.
"""
import lib
from collections import defaultdict
import statistics

AUCTION = range(2018, 2026)

# startable counts across a 12-team league (replacement level = Nth player at pos)
# 2 flex slots/team (24 total) split ~55/40/5 RB/WR/TE per observed usage.
STARTABLE = {"QB": 12, "RB": 24 + 13, "WR": 24 + 10, "TE": 12 + 1, "DST": 12, "K": 0}


def all_player_points(season):
    """{pid: (pos, season_pts)} for every player in the universe that season."""
    out = {}
    for e in lib.load_players_raw(season):
        p = e["player"]
        pid = p["id"]
        pos = lib.POS.get(p.get("defaultPositionId"), "?")
        pts = 0.0
        for s in p.get("stats", []):
            if s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 0 and s.get("scoringPeriodId") == 0:
                pts = s.get("appliedTotal", 0.0)
        out[pid] = (pos, pts)
    return out


def replacement_levels(season):
    """{pos: replacement_points} = points of the last startable player."""
    pts_by_pos = defaultdict(list)
    for pid, (pos, pts) in all_player_points(season).items():
        if pos in STARTABLE:
            pts_by_pos[pos].append(pts)
    rep = {}
    for pos, n in STARTABLE.items():
        vals = sorted(pts_by_pos.get(pos, []), reverse=True)
        rep[pos] = vals[n - 1] if len(vals) >= n and n > 0 else 0
    return rep


def drafted_with_vorp():
    """List of drafted-player records with cost, points, VORP (auction era)."""
    recs = []
    for yr in AUCTION:
        pts = all_player_points(yr)
        rep = replacement_levels(yr)
        for p in lib.draft_picks(yr):
            pid = p["playerId"]
            pos, pp = pts.get(pid, (p["pos"], 0.0))
            vorp = pp - rep.get(pos, 0)
            recs.append({"year": yr, "pos": pos, "cost": p["cost"], "pts": pp,
                         "vorp": vorp, "is_keeper": p["is_keeper"],
                         "name": p["name"], "mgr": p["manager"]})
    return recs


def line(n=92):
    print("-" * n)


def report():
    recs = drafted_with_vorp()
    skill = [r for r in recs if r["pos"] in ("QB", "RB", "WR", "TE") and r["cost"] > 0]

    print("=" * 92)
    print("ANALYSIS 5 — DRAFT VALUE: WHERE THE AUCTION OVER/UNDER-PAYS (2018-2025)")
    print("=" * 92)

    # ---- $ per marginal point (VORP) by position, non-keeper buys only ----
    print("\n### A. Price of production by position — $ per VORP point (non-keeper buys $5+)")
    print("     (lower = cheaper marginal points = better value to target)")
    line()
    print(f"{'Pos':5}{'buys':>6}{'avg $':>8}{'avg VORP':>10}{'$/VORP pt':>12}{'% positive VORP':>17}")
    line()
    for pos in ["RB", "WR", "TE", "QB"]:
        g = [r for r in skill if r["pos"] == pos and not r["is_keeper"] and r["cost"] >= 5]
        if not g:
            continue
        tot_cost = sum(r["cost"] for r in g)
        tot_vorp = sum(max(r["vorp"], 0) for r in g)
        ppv = tot_cost / tot_vorp if tot_vorp else 0
        posrate = 100 * sum(1 for r in g if r["vorp"] > 0) / len(g)
        print(f"{pos:5}{len(g):>6}{statistics.mean(r['cost'] for r in g):>8.0f}"
              f"{statistics.mean(r['vorp'] for r in g):>10.0f}{ppv:>12.2f}{posrate:>16.0f}%")

    # ---- ROI by price tier ----
    print("\n### B. Return by price tier — do expensive buys pay off? (non-keeper)")
    line()
    tiers = [("$1-5", 1, 5), ("$6-15", 6, 15), ("$16-30", 16, 30), ("$31-50", 31, 50), ("$51+", 51, 999)]
    print(f"{'tier':9}{'n':>5}{'avg $':>8}{'avg pts':>9}{'avg VORP':>10}{'bust% (VORP<0)':>16}{'pts/$':>8}")
    line()
    for label, lo, hi in tiers:
        g = [r for r in skill if not r["is_keeper"] and lo <= r["cost"] <= hi]
        if not g:
            continue
        bust = 100 * sum(1 for r in g if r["vorp"] < 0) / len(g)
        print(f"{label:9}{len(g):>5}{statistics.mean(r['cost'] for r in g):>8.0f}"
              f"{statistics.mean(r['pts'] for r in g):>9.0f}{statistics.mean(r['vorp'] for r in g):>10.0f}"
              f"{bust:>15.0f}%{statistics.mean(r['pts']/r['cost'] for r in g):>8.1f}")

    # ---- RB vs WR at the top: is elite RB worth the premium? ----
    print("\n### C. The RB-premium test — elite RB vs elite WR (top-12 priced each, per year)")
    line()
    for pos in ["RB", "WR"]:
        top = []
        for yr in AUCTION:
            g = sorted([r for r in skill if r["pos"] == pos and r["year"] == yr],
                       key=lambda r: -r["cost"])[:12]
            top += g
        cost = statistics.mean(r["cost"] for r in top)
        vorp = statistics.mean(r["vorp"] for r in top)
        bust = 100 * sum(1 for r in top if r["vorp"] < 0) / len(top)
        print(f"   top-12 {pos}: avg cost ${cost:.0f}  avg VORP {vorp:.0f}  "
              f"$/VORP {cost/vorp if vorp else 0:.2f}  bust% {bust:.0f}")

    # ---- keeper surplus ----
    print("\n### D. Keeper surplus — value over what an equal player cost at auction")
    line()
    keepers = [r for r in recs if r["is_keeper"] and r["pos"] in ("QB", "RB", "WR", "TE")]
    # market $/VORP per year/pos for surplus estimate
    print(f"   keepers analyzed: {len(keepers)}   "
          f"avg keeper cost ${statistics.mean(r['cost'] for r in keepers):.0f}   "
          f"avg keeper VORP {statistics.mean(r['vorp'] for r in keepers):.0f}")
    best = sorted(keepers, key=lambda r: -(r["vorp"]))[:8]
    print("   highest-VORP keepers (elite production kept cheap):")
    for r in best:
        print(f"      {r['year']} {r['name']:22} {r['pos']:3} ${r['cost']:<3} "
              f"VORP {r['vorp']:.0f}  (kept by {r['mgr']})")


if __name__ == "__main__":
    report()
