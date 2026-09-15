#!/usr/bin/env python3
"""What each position/tier ACTUALLY costs in this league -> config/price_curve.json.

This is the honest half of "optimal budget". Two different questions live here, and only
one of them is answerable from 9 seasons:

  "Which budget SHAPE wins?"   -> NOT answerable. strategy_search.py's leave-one-year-out
                                  guard puts the best-looking shape at 45% out-of-sample.
                                  No static allocation is promoted, and none should be.
  "What does $X BUY?"          -> Answerable and stable. That is a MARKET fact, and it is
                                  what makes a budget concrete: every slot's dollar figure
                                  maps to a tier you can actually win at that price.

WHY THE NOMINAL BUDGET IS THE WRONG DENOMINATOR. The $300 budget of 2020-2024 was NOT
extra spending power — it existed to carry the keeper accounting hack (a keeper is recorded
as a bid of cost+$100, so the cap had to rise by $100 to fit it). The evidence: total league
spend is ~$2,400 in EVERY season, $200 and $300 alike (2373/2386/2399/2382/2397/2376/2397/
2411/2398), i.e. ~$200 per team throughout. So we normalize by the EFFECTIVE AUCTION WALLET
— non-keeper dollars actually spent per team — which is ~$199 in the $200 years and ~$170-188
in the keeper years (the keeper's inflated bid ate the rest).

  2017-2019, 2025 : ~$199 wallet, FULL player supply   <- the regime 2026 repeats
  2020-2024       : ~$170-188 wallet, 12 elites KEPT   <- supply-constrained

Against the effective wallet, keeper-era top prices are HIGHER (44-65%) than no-keeper years
(36-40%), which is the correct economics: similar money chasing 12 fewer elite players. That
is why 2020's $110 Elliott is not evidence about 2026 — with full supply, losing the #1 RB
just means buying the #2, so the top clears lower. Tier labels come from the tracked
elboberto projections (2022+ only), so tier prices lean on 2025 for the current regime.

Emits config/price_curve.json:
  {"season":2026,"budget":200,"regime":"no-keeper",
   "by_tier":  {"RB":{"RB1":{"p50":72,...}}},
   "by_rank":  [{"rank":1,"p50":76},...],       # Nth-priciest buy, any position
   "sources":  {...}}

Run:  PYTHONPATH=analysis python3 analysis/price_curve.py
"""
import json
import os
import re
import statistics
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import lib  # noqa: E402

OUT = os.path.join(ROOT, "config", "price_curve.json")
PROJ = os.path.join(ROOT, "draft_sheets", "elboberto_projections.json")
POS = ("QB", "RB", "WR", "TE")
TIER_YEARS = (2022, 2023, 2024, 2025)     # elboberto tier labels only exist from 2022
RANK_DEPTH = 12


def norm(s):
    return re.sub(r"[^a-z]", "", str(s).lower())


def tier_num(t):
    m = re.sub(r"\D", "", t or "")
    return int(m) if m else 99


def regime(yr):
    """(label, nominal_budget, n_keepers) — label from lib, the single definition."""
    keepers = sum(1 for p in lib.draft_picks(yr) if p["is_keeper"])
    return lib.regime(yr), lib.auction_budget(yr), keepers


def wallet(yr):
    """Effective auction wallet per team. Canonical version lives in lib."""
    return lib.effective_wallet(yr)


def buys(yr):
    """Real competitive buys for a season: keepers excluded (a keeper price is not a clear)."""
    return [p for p in lib.draft_picks(yr) if not p["is_keeper"] and p["cost"] >= 1]


def by_rank(seasons, budget):
    """The Nth-most-expensive buy of the draft, as share of budget -> this season's $.

    Position-blind on purpose: it answers "what does the top of the board cost", which is
    the number the console's will-go estimate has to stay honest against.
    """
    out = []
    for n in range(RANK_DEPTH):
        shares = []
        for yr in seasons:
            costs = sorted((p["cost"] for p in buys(yr)), reverse=True)
            if n < len(costs):
                shares.append(costs[n] / wallet(yr))
        if shares:
            out.append({"rank": n + 1, "share": round(statistics.median(shares), 4),
                        "p50": round(statistics.median(shares) * budget),
                        "lo": round(min(shares) * budget), "hi": round(max(shares) * budget),
                        "n": len(shares)})
    return out


def by_tier(budget):
    """position -> tier -> price stats, per regime, in this season's dollars."""
    proj = json.load(open(PROJ))
    acc = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))  # pos->tier->regime->[share]
    for yr in TIER_YEARS:
        reg, _b, _ = regime(yr)
        pj = {norm(p["name"]): p for p in proj.get(str(yr), [])}
        for p in buys(yr):
            r = pj.get(norm(p["name"]))
            if not r or r["pos"] != p["pos"] or p["pos"] not in POS:
                continue
            acc[p["pos"]][r["tier"]][reg].append(p["cost"] / wallet(yr))
    out = {}
    for pos in POS:
        tiers = sorted(acc[pos], key=tier_num)
        out[pos] = {}
        for t in tiers:
            rec = {}
            for reg, shares in acc[pos][t].items():
                rec[reg] = {"p50": round(statistics.median(shares) * budget),
                            "lo": round(min(shares) * budget),
                            "hi": round(max(shares) * budget),
                            "n": len(shares)}
            # headline = the current regime when we have it, else the keeper-era shape
            head = rec.get("full-supply") or rec.get("keeper")
            out[pos][t] = {"price": head["p50"], "lo": head["lo"], "hi": head["hi"],
                           "n": head["n"],
                           "regime": "full-supply" if "full-supply" in rec else "keeper",
                           "detail": rec}
    return out


def main():
    cfg = json.load(open(os.path.join(ROOT, "config", "league.json")))
    season, budget = cfg["season"], 200
    try:
        budget = lib.auction_budget(season) or 200
    except Exception:
        pass

    seasons = [y for y in range(2017, season) if regime(y)[0] == "full-supply"]
    print(f"season {season}: ${budget} cap, full player supply (keeperCount=0)")
    print("effective auction wallet per team, by season:")
    for y in range(2017, season):
        r = regime(y)
        print(f"   {y}: nominal ${r[1]:<4} keepers {r[2]:<3} -> wallet ${wallet(y):.0f}  [{r[0]}]")
    print(f"\ncomparable seasons (full supply): {seasons}")
    others = [y for y in range(2017, season) if y not in seasons]
    print(f"excluded as different regime: {others}\n")

    ranks = by_rank(seasons, budget)
    print("TOP OF THE BOARD — Nth-priciest buy of the draft")
    print(f"   {'rank':>5}{'median $':>10}{'range':>12}{'seasons':>9}")
    for r in ranks:
        rng = f"${r['lo']}-{r['hi']}"
        print(f"   {r['rank']:>5}{r['p50']:>10}{rng:>12}{r['n']:>9}")

    tiers = by_tier(budget)
    print("\nWHAT EACH TIER COSTS (this season's dollars; * = from keeper era, less reliable)")
    for pos in POS:
        line = []
        for t, v in tiers[pos].items():
            if v["n"] < 1:
                continue
            star = "*" if v["regime"] == "keeper" else ""
            line.append(f"{t} ${v['price']}{star}")
        print(f"   {pos}: " + "  ".join(line))

    payload = {"season": season, "budget": budget, "regime": "full-supply",
               "comparable_seasons": seasons, "excluded_seasons": others,
               "by_rank": ranks, "by_tier": tiers,
               "wallets": {str(y): round(wallet(y)) for y in range(2017, season)},
               "note": "Prices are what THIS league actually paid, normalized to share of the "
                       "EFFECTIVE AUCTION WALLET (not the nominal cap, which was inflated $100 "
                       "in keeper years purely to encode keepers). Which budget SHAPE to choose is "
                       "NOT validated out-of-sample (see analysis/research/strategy_search.py) "
                       "— this file only says what a given dollar figure buys."}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(payload, open(OUT, "w"), indent=1)
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
