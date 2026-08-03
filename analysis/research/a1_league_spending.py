#!/usr/bin/env python3
"""Analysis 1: league-wide auction spending tendencies (2017-2025).

All costs are REAL dollars (keeper bids de-inflated by $100). Effective budget
is $200/team every season. Reports positional allocation, star concentration,
positional price curves, and keeper economics — the aggregate market, not
individual managers (that's analysis 2).
"""
import lib
from collections import defaultdict, Counter
import statistics

SKILL = ["QB", "RB", "WR", "TE", "DST", "K"]
AUCTION = list(range(2017, 2026))


def line(c="-", n=88):
    print(c * n)


def positional_allocation():
    print("\n### A. Positional budget allocation — % of league auction $ by position")
    line()
    hdr = f"{'Season':7}" + "".join(f"{p:>7}" for p in SKILL) + f"{'$/team':>9}{'keepers':>9}"
    print(hdr)
    line()
    agg = defaultdict(float)
    n_years = 0
    for yr in AUCTION:
        picks = lib.draft_picks(yr)
        total = sum(p["cost"] for p in picks)
        by = defaultdict(float)
        for p in picks:
            by[p["pos"]] += p["cost"]
        row = f"{yr:<7}"
        for p in SKILL:
            pct = 100 * by[p] / total if total else 0
            row += f"{pct:>6.1f}%"
            agg[p] += pct
        kn = sum(1 for p in picks if p["is_keeper"])
        row += f"{total/12:>9.0f}{kn:>9}"
        print(row)
        n_years += 1
    line()
    avg = f"{'AVG':<7}" + "".join(f"{agg[p]/n_years:>6.1f}%" for p in SKILL)
    print(avg)
    print("(FLEX-eligible RB+WR+TE typically absorb ~90%+; QB cheap in 1-QB, no K spend recently)")


def star_concentration():
    print("\n### B. Stars-and-scrubs: how top-heavy is roster construction?")
    line()
    print(f"{'Season':7}{'top buy':>9}{'top3 %bud':>11}{'$1-2 buys/tm':>14}{'>=$40 buys':>12}{'median buy':>12}")
    line()
    for yr in AUCTION:
        picks = [p for p in lib.draft_picks(yr) if p["cost"] > 0 or not p["is_keeper"]]
        by_team = defaultdict(list)
        for p in picks:
            by_team[p["teamId"]].append(p["cost"])
        top_buy = max(p["cost"] for p in picks)
        # avg share of each team's spend in its top 3 buys
        shares, cheap = [], []
        for t, costs in by_team.items():
            s = sorted(costs, reverse=True)
            tot = sum(s) or 1
            shares.append(100 * sum(s[:3]) / tot)
            cheap.append(sum(1 for c in costs if 1 <= c <= 2))
        big = sum(1 for p in picks if p["cost"] >= 40)
        allc = [p["cost"] for p in picks if p["cost"] > 0]
        print(f"{yr:<7}{top_buy:>8}${statistics.mean(shares):>10.1f}%"
              f"{statistics.mean(cheap):>14.1f}{big:>12}{statistics.median(allc):>12.0f}")
    line()
    print("top3 %bud = avg share of a team's budget concentrated in its 3 priciest buys")


def positional_price_curve():
    print("\n### C. Positional value curve — avg REAL $ of the Nth-most-expensive at each position")
    print("     (across the league each season, then averaged over 2020-2025 keeper era)")
    line()
    ERA = range(2020, 2026)
    # rank -> pos -> list of prices
    curve = defaultdict(lambda: defaultdict(list))
    for yr in ERA:
        picks = lib.draft_picks(yr)
        bypos = defaultdict(list)
        for p in picks:
            if p["pos"] in ("RB", "WR", "TE", "QB"):
                bypos[p["pos"]].append(p["cost"])
        for pos, costs in bypos.items():
            for rank, c in enumerate(sorted(costs, reverse=True)[:12], 1):
                curve[pos][rank].append(c)
    print(f"{'rank':5}" + "".join(f"{p:>8}" for p in ["QB", "RB", "WR", "TE"]))
    line()
    for rank in range(1, 13):
        row = f"{rank:<5}"
        for pos in ["QB", "RB", "WR", "TE"]:
            vals = curve[pos].get(rank)
            row += f"{statistics.mean(vals):>8.0f}" if vals else f"{'-':>8}"
        print(row)
    line()
    print("Read: 'what did the #N-priced RB/WR/etc cost on average' — the going market rate by tier")


def keeper_economics():
    print("\n### D. Keeper economics — value captured (keeper era 2020-2025)")
    line()
    print(f"{'Season':7}{'#keepers':>9}{'avg keep $':>12}{'median':>9}{'max':>6}  most expensive keepers")
    line()
    for yr in range(2020, 2026):
        keepers = [p for p in lib.draft_picks(yr) if p["is_keeper"]]
        if not keepers:
            print(f"{yr:<7}{0:>9}  (no keepers recorded)")
            continue
        costs = [p["cost"] for p in keepers]
        top = sorted(keepers, key=lambda x: -x["cost"])[:3]
        tops = ", ".join(f"{k['name']} ${k['cost']}" for k in top)
        print(f"{yr:<7}{len(keepers):>9}{statistics.mean(costs):>12.1f}"
              f"{statistics.median(costs):>9.0f}{max(costs):>6}  {tops}")
    line()


def top_buys_history():
    print("\n### E. Biggest auction purchases ever (real $, non-keeper)")
    line()
    allbuys = []
    for yr in AUCTION:
        for p in lib.draft_picks(yr):
            if not p["is_keeper"]:
                allbuys.append((p["cost"], yr, p["name"], p["pos"], p["ownerName"]))
    for cost, yr, name, pos, owner in sorted(allbuys, reverse=True)[:15]:
        print(f"  ${cost:<4} {yr}  {name:24} {pos:3}  by {owner}")


if __name__ == "__main__":
    print("=" * 88)
    print("ANALYSIS 1 — LEAGUE AUCTION SPENDING TENDENCIES  (real $, $200 effective budget)")
    print("Format: 1QB / 2RB / 2WR / 1TE / 2FLEX / 1DST, no K, half-PPR")
    print("=" * 88)
    positional_allocation()
    star_concentration()
    positional_price_curve()
    keeper_economics()
    top_buys_history()
