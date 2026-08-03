#!/usr/bin/env python3
"""Analysis 6: auction NOMINATION strategy (2017-2025).

Each draft pick carries `nominatingTeamId` (who put the player up) and `teamId`
(who won them). That lets us characterize nomination style:
  - nominate-to-BUY (win your own nominations) vs nominate-to-DRAIN (throw out
    players you don't want to make others spend)
  - do you nominate studs (price-enforcer) or $1 scrubs (budget-preserver)?
  - do you buy your OWN nominations or POUNCE on players others put up?
  - early-draft aggression
Keepers excluded (nominatingTeamId = 0).
"""
import lib
from collections import defaultdict, Counter
import statistics

AUCTION = range(2017, 2026)


def records():
    recs = []
    for yr in AUCTION:
        to = lib.team_owner(yr)
        for p in lib.draft_picks(yr):
            nom_tid = p.get("nominatingTeamId") or 0
            if nom_tid == 0 or p["is_keeper"]:
                continue
            recs.append({
                "year": yr,
                "nominator": lib.MANAGER_CANON.get(to.get(nom_tid, {}).get("owner"),
                                                   to.get(nom_tid, {}).get("ownerName", "?")),
                "winner": p["manager"],
                "cost": p["cost"], "pos": p["pos"], "overall": p["overall"],
                "name": p["name"],
            })
    return recs


def main():
    recs = records()
    # per-season pick counts for timing thirds
    max_overall = defaultdict(int)
    for r in recs:
        max_overall[r["year"]] = max(max_overall[r["year"]], r["overall"] or 0)

    nom = defaultdict(list)      # nominator -> list of recs they nominated
    buys = defaultdict(list)     # winner -> recs they won
    for r in recs:
        nom[r["nominator"]].append(r)
        buys[r["winner"]].append(r)

    seasons_active = defaultdict(set)
    for yr in AUCTION:
        for tid in lib.team_owner(yr):
            seasons_active[lib.manager(yr, tid)].add(yr)
    mgrs = [m for m in nom if len(seasons_active[m]) >= 3]

    # league baselines
    lg_winrate = statistics.mean(
        [sum(1 for x in nom[m] if x["nominator"] == x["winner"]) / len(nom[m]) for m in mgrs])

    print("=" * 100)
    print("ANALYSIS 6 — AUCTION NOMINATION STRATEGY (2017-2025, non-keeper picks)")
    print("=" * 100)
    print(f"\nLeague avg 'win your own nomination' rate: {100*lg_winrate:.0f}%  "
          f"(random baseline ~8%; higher = nominate players you actually want)")

    print("\n### Nomination profile per manager")
    print("-" * 100)
    print(f"{'Manager':17}{'noms/yr':>8}{'win-own%':>10}{'avg nom $':>11}"
          f"{'early nom $':>12}{'top pos nom':>13}   style")
    print("-" * 100)
    for m in sorted(mgrs, key=lambda m: -statistics.mean(x["cost"] for x in nom[m])):
        rs = nom[m]
        yrs = len(seasons_active[m])
        winown = sum(1 for x in rs if x["nominator"] == x["winner"]) / len(rs)
        avg_cost = statistics.mean(x["cost"] for x in rs)
        # early = first third of draft by overall pick number
        early = [x for x in rs if x["overall"] and x["overall"] <= max_overall[x["year"]] / 3]
        early_cost = statistics.mean(x["cost"] for x in early) if early else 0
        toppos = Counter(x["pos"] for x in rs).most_common(1)[0][0]
        if avg_cost > 22:
            style = "throws out studs (price-enforcer)"
        elif avg_cost < 12:
            style = "nominates cheap (budget-preserver)"
        else:
            style = "mixed"
        if winown > 0.30:
            style += ", nominates own targets"
        elif winown < 0.15:
            style += ", pure drain"
        print(f"{m:17}{len(rs)/yrs:>8.0f}{100*winown:>9.0f}%{avg_cost:>11.0f}"
              f"{early_cost:>12.0f}{toppos:>13}   {style}")

    print("\n### Do managers BUY their own nominations, or POUNCE on others'?")
    print("-" * 100)
    print(f"{'Manager':17}{'buys/yr':>8}{'self-nominated%':>17}{'others-nom%':>13}   read")
    print("-" * 100)
    for m in sorted(mgrs, key=lambda m: sum(1 for x in buys[m] if x['nominator'] == m) / max(len(buys[m]), 1)):
        bs = buys[m]
        if not bs:
            continue
        selfp = sum(1 for x in bs if x["nominator"] == m) / len(bs)
        yrs = len(seasons_active[m])
        read = ("pouncer (lets others throw out, then bids)" if selfp < 0.25 else
                "proactive (nominates what he wants)" if selfp > 0.45 else "balanced")
        print(f"{m:17}{len(bs)/yrs:>8.0f}{100*selfp:>16.0f}%{100*(1-selfp):>12.0f}%   {read}")

    # league timing: does the expensive stuff go early?
    print("\n### League nomination timing — avg winning price by draft third")
    thirds = {1: [], 2: [], 3: []}
    for r in recs:
        mo = max_overall[r["year"]] or 1
        frac = (r["overall"] or 1) / mo
        t = 1 if frac <= 1/3 else 2 if frac <= 2/3 else 3
        thirds[t].append(r["cost"])
    for t in (1, 2, 3):
        seg = ["first", "middle", "last"][t-1]
        print(f"   {seg:6} third: avg winning price ${statistics.mean(thirds[t]):.1f} "
              f"(n={len(thirds[t])})")


if __name__ == "__main__":
    main()
