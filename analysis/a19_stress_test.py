#!/usr/bin/env python3
"""Analysis 19: stress-test the RB-anchor strategy — does it GENERALIZE?

(1) Strategy tournament: 6 Harry archetypes vs the calibrated field.
(2) Field-condition sweep: baseline / RB-hot / WR-hot / disciplined room.
Metric per cell = Harry's median finish RANK among 12 teams by projected starter
VBD (1=best), plus median VBD, across N seeded sims. If RB-anchor stays top across
conditions it generalizes; if a WR-pivot wins when RB inflates, it's condition-dependent.
"""
import statistics
from collections import defaultdict
import a18_agent_auction as a18


# candidate Harry strategies: (mult, conc, maxbuy, env, elite_qb_steal)
def strat(rb, wr, te, qb, conc, env, steal=True):
    return {"mult": {"RB": rb, "WR": wr, "TE": te, "QB": qb}, "conc": conc,
            "maxbuy": 110, "env": env, "elite_qb_steal": steal}


STRATEGIES = {
    "RB-anchor (rec)":  strat(1.5, 1.2, 1.05, 0.9, 76, {"RB": 112, "WR": 58, "TE": 15, "QB": 14}),
    "Spread-RB":        strat(1.2, 1.3, 1.0, 0.5, 60, {"RB": 105, "WR": 60, "TE": 20, "QB": 13}),
    "Two-anchor RB+WR": strat(1.5, 1.5, 1.0, 0.9, 78, {"RB": 90, "WR": 82, "TE": 13, "QB": 14}),
    "Zero-RB / WR-hvy": strat(0.9, 1.6, 1.1, 0.9, 70, {"RB": 48, "WR": 112, "TE": 25, "QB": 14}),
    "Balanced":         strat(1.2, 1.2, 1.1, 0.8, 55, {"RB": 70, "WR": 70, "TE": 36, "QB": 23}),
    "Mega-RB hero":     strat(1.75, 1.1, 1.0, 0.6, 82, {"RB": 135, "WR": 45, "TE": 12, "QB": 7}),
}


def modify_field(opp, condition):
    out = {}
    for m, a in opp.items():
        mult = dict(a["mult"])
        if condition == "RB-hot":
            mult["RB"] *= 1.15
        elif condition == "WR-hot":
            mult["WR"] *= 1.15
        elif condition == "disciplined":
            mult = {p: 1 + (v - 1) * 0.5 for p, v in mult.items()}
        out[m] = {**a, "mult": mult}
    return out


def eval_strategy(field, hp, seeds):
    ranks, vbds = [], []
    for s in seeds:
        agents = {m: field[m] for m in field}
        agents["Harry"] = hp
        teams = a18.run_auction(agents, s)
        all_vbd = {m: a18.starter_vbd(teams[m]["roster"])[0] for m in agents}
        hv = all_vbd["Harry"]
        rank = 1 + sum(1 for m, v in all_vbd.items() if m != "Harry" and v > hv)
        ranks.append(rank); vbds.append(hv)
    return statistics.median(ranks), statistics.mean(ranks), statistics.median(vbds)


def main():
    opp = a18.build_agents()
    field_names = sorted(m for m in {a18.lib.manager(2025, t) for t in a18.lib.team_owner(2025)}
                         if m != lib.ME and m in opp)[:11]
    base_field = {m: opp[m] for m in field_names}
    seeds = list(range(300, 360))   # 60 sims/cell
    conditions = ["baseline", "RB-hot", "WR-hot", "disciplined"]

    print("=" * 90)
    print("ANALYSIS 19 — STRATEGY GENERALIZATION STRESS TEST (60 sims/cell, 12-team field)")
    print("=" * 90)
    print("Cell = Harry's MEDIAN finish rank among 12 (1=best) under each field condition.\n")
    header = f"{'Harry strategy':20}" + "".join(f"{c:>13}" for c in conditions)
    print(header); print("-" * 90)
    results = {}
    for name, hp in STRATEGIES.items():
        row = f"{name:20}"
        for cond in conditions:
            field = base_field if cond == "baseline" else modify_field(base_field, cond)
            medrank, meanrank, medvbd = eval_strategy(field, hp, seeds)
            results[(name, cond)] = (medrank, meanrank, medvbd)
            row += f"{f'{medrank:.0f} ({meanrank:.1f})':>13}"
        print(row)
    print("-" * 90)
    print("(shown: median rank (mean rank). lower is better.)\n")

    # who wins each condition
    print("### Best strategy per condition (by mean rank)")
    for cond in conditions:
        best = min(STRATEGIES, key=lambda n: results[(n, cond)][1])
        ranked = sorted(STRATEGIES, key=lambda n: results[(n, cond)][1])
        print(f"   {cond:12}: {best:20} (mean rank {results[(best,cond)][1]:.1f})   "
              f"then {ranked[1]}, {ranked[2]}")

    print("\n### Generalization: avg mean-rank across all 4 conditions (robustness)")
    for name in sorted(STRATEGIES, key=lambda n: statistics.mean(results[(n, c)][1] for c in conditions)):
        avg = statistics.mean(results[(name, c)][1] for c in conditions)
        spread = max(results[(name, c)][1] for c in conditions) - min(results[(name, c)][1] for c in conditions)
        print(f"   {name:20} avg rank {avg:.2f}   (swing across conditions {spread:.1f})")


if __name__ == "__main__":
    main()
