#!/usr/bin/env python3
"""Analysis 2: individual manager DRAFT tendencies (auction era 2017-2025).

For each manager, average their per-season auction behavior: positional budget
split, RB-vs-WR lean, and how stars-and-scrubs vs balanced they build.
"""
import lib
from collections import defaultdict
import statistics

AUCTION = range(2017, 2026)
CURRENT_2025 = None  # filled below


def per_season_profiles():
    """{manager: [per-season dict]}"""
    profiles = defaultdict(list)
    for yr in AUCTION:
        picks = lib.draft_picks(yr)
        by_team = defaultdict(list)
        for p in picks:
            by_team[p["teamId"]].append(p)
        for tid, ps in by_team.items():
            mgr = lib.manager(yr, tid)
            spend = sum(p["cost"] for p in ps) or 1
            pos = defaultdict(float)
            for p in ps:
                pos[p["pos"]] += p["cost"]
            costs = sorted((p["cost"] for p in ps), reverse=True)
            profiles[mgr].append({
                "year": yr,
                "spend": sum(p["cost"] for p in ps),
                "rb": 100 * pos["RB"] / spend,
                "wr": 100 * pos["WR"] / spend,
                "qb": 100 * pos["QB"] / spend,
                "te": 100 * pos["TE"] / spend,
                "top3": 100 * sum(costs[:3]) / spend,
                "maxbuy": costs[0] if costs else 0,
                "darts": sum(1 for c in costs if 1 <= c <= 2),
                "studs": sum(1 for c in costs if c >= 40),
                "keeper_cost": sum(p["cost"] for p in ps if p["is_keeper"]),
                "keepers": [(p["name"], p["pos"], p["cost"]) for p in ps if p["is_keeper"]],
            })
    return profiles


def avg(profiles, mgr, key):
    return statistics.mean(p[key] for p in profiles[mgr])


def main():
    profiles = per_season_profiles()
    # league baselines
    def league_mean(key):
        return statistics.mean(p[key] for ps in profiles.values() for p in ps)
    lg = {k: league_mean(k) for k in ["rb", "wr", "qb", "te", "top3", "maxbuy", "darts", "studs"]}

    mgrs = sorted(profiles, key=lambda m: -(avg(profiles, m, "rb") - avg(profiles, m, "wr")))
    # only managers with >= 3 auction seasons for stable profiles
    mgrs = [m for m in mgrs if len(profiles[m]) >= 3]

    print("=" * 100)
    print("ANALYSIS 2 — INDIVIDUAL MANAGER DRAFT TENDENCIES (auction era, real $, avg per season)")
    print("=" * 100)
    print("\n### Positional budget split + RB-WR lean  (sorted most RB-leaning -> most WR-leaning)")
    print("-" * 100)
    print(f"{'Manager':18}{'seas':>5}{'RB%':>6}{'WR%':>6}{'QB%':>6}{'TE%':>6}{'RB-WR':>7}   archetype")
    print("-" * 100)
    for m in mgrs:
        n = len(profiles[m])
        rb, wr, qb, te = (avg(profiles, m, k) for k in ["rb", "wr", "qb", "te"])
        lean = rb - wr
        tag = ("RB-anchor" if lean > 15 else "RB-lean" if lean > 5 else
               "WR-lean" if lean < -5 else "balanced")
        if qb > lg["qb"] + 3:
            tag += ", pays QB"
        if te > lg["te"] + 4:
            tag += ", pays TE"
        print(f"{m:18}{n:>5}{rb:>6.0f}{wr:>6.0f}{qb:>6.0f}{te:>6.0f}{lean:>+7.0f}   {tag}")
    print("-" * 100)
    print(f"{'LEAGUE AVG':18}{'':>5}{lg['rb']:>6.0f}{lg['wr']:>6.0f}{lg['qb']:>6.0f}{lg['te']:>6.0f}{lg['rb']-lg['wr']:>+7.0f}")

    print("\n### Draft STYLE — stars-and-scrubs vs balanced")
    print("-" * 100)
    print(f"{'Manager':18}{'top3%':>7}{'maxBuy':>8}{'studs>=40':>10}{'$1-2 darts':>12}   style")
    print("-" * 100)
    for m in sorted(mgrs, key=lambda m: -avg(profiles, m, "top3")):
        t3, mx, st, dr = (avg(profiles, m, k) for k in ["top3", "maxbuy", "studs", "darts"])
        style = ("extreme stars&scrubs" if t3 > 72 else "stars&scrubs" if t3 > 67 else
                 "moderate" if t3 > 62 else "balanced")
        print(f"{m:18}{t3:>7.0f}{mx:>8.0f}{st:>10.1f}{dr:>12.1f}   {style}")
    print("-" * 100)
    print(f"{'LEAGUE AVG':18}{lg['top3']:>7.0f}{lg['maxbuy']:>8.0f}{lg['studs']:>10.1f}{lg['darts']:>12.1f}")

    print("\n### Consistency — does the manager stick to a style? (stdev of RB% across seasons)")
    print("-" * 100)
    for m in sorted(mgrs, key=lambda m: statistics.pstdev([p['rb'] for p in profiles[m]])):
        rbs = [p["rb"] for p in profiles[m]]
        sd = statistics.pstdev(rbs)
        print(f"{m:18} RB% range {min(rbs):>3.0f}-{max(rbs):<3.0f}  stdev {sd:>4.1f}  "
              f"{'very consistent' if sd<10 else 'consistent' if sd<16 else 'variable' if sd<22 else 'erratic'}")


if __name__ == "__main__":
    main()
