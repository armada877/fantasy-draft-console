#!/usr/bin/env python3
"""Edge OVER YOUR LEAGUE, not a generic replacement level.

Drafts YOU (under several budget strategies) against your 11 CALIBRATED opponents in the
a18 auction, then scores every team's best starting lineup in projected POINTS. Your edge =
your starter points − the field's average starter points (and how many of the 11 you outscore).
Because your opponents under-invest in QB/TE and pile into RB/WR, the strategy that maximizes
edge vs THIS field can differ from generic-VBD advice.

Run:  PYTHONPATH=analysis python3 analysis/research/edge_vs_field.py
"""
import statistics
from collections import defaultdict

import a18_agent_auction as a18

NSIM = 120
SEED0 = 700


def lineup_points(roster):
    """Best 1QB/2RB/2WR/1TE + 2FLEX by projected points from a drafted roster."""
    byp = defaultdict(list)
    for r in roster["players"]:
        byp[r["pos"]].append(r.get("fpts") or 0)
    for p in byp:
        byp[p].sort(reverse=True)
    used = defaultdict(int)
    tot = 0.0
    for pos, n in a18.START.items():
        for i in range(n):
            if i < len(byp[pos]):
                tot += byp[pos][i]; used[pos] += 1
    rem = sorted([pt for p in ("RB", "WR", "TE") for pt in byp[p][used[p]:]], reverse=True)
    return tot + sum(rem[:a18.NFLEX])


# candidate ME strategies (a18 bidding DNA): mult = positional aggressiveness vs projection
STRATS = {
    "RB anchor (pay up RB)":   {"mult": {"QB": 0.8, "RB": 1.55, "WR": 1.25, "TE": 0.8}, "conc": 76, "maxbuy": 120},
    "Balanced":                {"mult": {"QB": 1.0, "RB": 1.15, "WR": 1.15, "TE": 1.0}, "conc": 55, "maxbuy": 95},
    "QB/TE value (attack their weak spots)": {"mult": {"QB": 1.7, "RB": 1.0, "WR": 1.05, "TE": 1.9}, "conc": 55, "maxbuy": 90},
    "WR-heavy":                {"mult": {"QB": 0.9, "RB": 0.9,  "WR": 1.5,  "TE": 1.0}, "conc": 60, "maxbuy": 95},
    "Validated (a18)":         a18.VALIDATED_HARRY,
}


def main():
    opp = a18.build_agents()
    me = a18.lib.ME
    field = [m for m in {a18.lib.manager(2025, t) for t in a18.lib.team_owner(2025)} if m != me]
    field = [m for m in field if m in opp][:11]
    print("=" * 92)
    print(f"EDGE OVER YOUR LEAGUE — you vs your {len(field)} calibrated opponents, {NSIM} sims")
    print("  edge = your projected STARTER points − field average;  beat% = share of the 11 you outscore")
    print("=" * 92)
    print(f"   {'strategy':40}{'your pts':>9}{'field avg':>10}{'edge':>7}{'beat%':>7}")
    rows = []
    for name, dna in STRATS.items():
        yours, fields, beats = [], [], []
        for s in range(SEED0, SEED0 + NSIM):
            agents = {m: opp[m] for m in field}
            agents["Harry"] = dna
            teams = a18.run_auction(agents, s)
            yp = lineup_points(teams["Harry"]["roster"])
            fp = [lineup_points(teams[o]["roster"]) for o in field]
            yours.append(yp); fields.append(statistics.mean(fp))
            beats.append(100 * sum(1 for x in fp if yp > x) / len(fp))
        edge = statistics.mean(yours) - statistics.mean(fields)
        rows.append((name, statistics.mean(yours), statistics.mean(fields), edge, statistics.mean(beats)))
    for name, yp, fp, edge, beat in sorted(rows, key=lambda r: -r[3]):
        print(f"   {name:40}{yp:>9.0f}{fp:>10.0f}{edge:>+7.0f}{beat:>6.0f}%")
    best = max(rows, key=lambda r: r[3])
    print(f"\n  Best edge vs YOUR field: {best[0]!r}  (+{best[3]:.0f} starter pts, beats {best[4]:.0f}% of the field)")


if __name__ == "__main__":
    main()
