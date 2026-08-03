#!/usr/bin/env python3
"""Analysis 3b: REAL executed-trade history (from playercards, 2019-2025).

2018 trade items were not recorded by ESPN's playercards (0 usable); 2019+ are
complete. Each trade is deduped across the redundant per-player card copies.
"""
import lib
from collections import defaultdict, Counter
import statistics

SEASONS = range(2019, 2026)


def season_value(season):
    out = {}
    for e in lib.load_players_raw(season):
        pid = e["player"]["id"]
        for s in e["player"].get("stats", []):
            if s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 0 and s.get("scoringPeriodId") == 0:
                out[pid] = s.get("appliedTotal", 0.0)
    return out


def report():
    print("=" * 92)
    print("ANALYSIS 3b — EXECUTED TRADE HISTORY (2019-2025, from playercards)")
    print("=" * 92)

    # volume by year
    print("\n### Trade volume by season")
    per_year = {}
    for yr in SEASONS:
        per_year[yr] = len(lib.executed_trades(yr))
    for yr in SEASONS:
        print(f"   {yr}: {per_year[yr]:>2} trades  {'#'*per_year[yr]}")
    print(f"   total: {sum(per_year.values())} trades over {len(SEASONS)} seasons "
          f"(avg {statistics.mean(per_year.values()):.1f}/yr)")

    # per-manager involvement
    involved = Counter()
    seasons_active = defaultdict(set)
    for yr in SEASONS:
        for tr in lib.executed_trades(yr):
            for tm, mgr in tr["managers"].items():
                involved[mgr] += 1
        for tid in lib.team_owner(yr):
            seasons_active[lib.manager(yr, tid)].add(yr)
    print("\n### Trades made per manager (a trade counts for both sides)")
    print("-" * 92)
    print(f"{'Manager':18}{'trades':>8}{'seasons':>9}{'trades/yr':>11}   engagement")
    print("-" * 92)
    for mgr in sorted(involved, key=lambda m: -involved[m] / max(len(seasons_active[m]), 1)):
        n = involved[mgr]
        s = len(seasons_active[mgr]) or 1
        rate = n / s
        eng = ("hyperactive" if rate > 4 else "active" if rate > 2.5 else
               "moderate" if rate > 1.2 else "reluctant" if rate > 0.4 else "avoids trading")
        print(f"{mgr:18}{n:>8}{s:>9}{rate:>11.1f}   {eng}")

    # partner network
    print("\n### Who trades with whom (executed-trade pairs, all seasons)")
    pairs = Counter()
    for yr in SEASONS:
        for tr in lib.executed_trades(yr):
            mgrs = sorted(set(tr["managers"].values()))
            if len(mgrs) == 2:
                pairs[(mgrs[0], mgrs[1])] += 1
    for (a, b), n in pairs.most_common(12):
        print(f"   {a:18} <-> {b:18} {n}")

    # positions traded
    print("\n### What gets traded (by position, all seasons)")
    pos = Counter()
    for yr in SEASONS:
        for tr in lib.executed_trades(yr):
            for pid in tr["pids"]:
                pos[lib.ppos(pid)] += 1
    tot = sum(pos.values())
    for p, n in pos.most_common():
        print(f"   {p:4} {n:>4} ({100*n/tot:.0f}%)")

    # biggest trades by combined season fantasy value
    print("\n### Notable trades (by combined season fantasy value of players moved)")
    allt = []
    for yr in SEASONS:
        val = season_value(yr)
        for tr in lib.executed_trades(yr):
            tv = sum(val.get(p, 0) for p in tr["pids"])
            allt.append((tv, yr, tr))
    for tv, yr, tr in sorted(allt, key=lambda x: -x[0])[:10]:
        parts = " <=> ".join(
            f"{tr['managers'][tm]} gets {', '.join(lib.pname(p) for p in pl)}"
            for tm, pl in tr["sides"].items())
        print(f"   {yr} wk{tr['sp']}: {parts}")


if __name__ == "__main__":
    report()
