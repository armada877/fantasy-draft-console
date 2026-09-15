#!/usr/bin/env python3
"""The best roster $200 can actually buy, and the per-slot budget it implies.

This answers "does my ideal budget represent optimal value" as an OPTIMIZATION rather than
a guess: given each player's expected clearing price and projected value-over-replacement,
solve for the starting lineup that maximizes total VBD subject to $200 and the real roster
shape. The winning roster's prices ARE the ideal budget.

Prices come from the console's own bid model (worth x manager positional multiplier,
second-price across rivals with budget + need, capped by budget/max-buy). That model is
used because it is the best-validated one available, not by default:

    leave-one-season-out over 2022-2025, predicting real auction prices
      flat positional mults (this model)      mean |err| $7.4   bias +0.3
      projection x fitted position mult                  $8.4        -3.5
      price = projection                                 $9.7        -2.5
      projection x fitted global k                      $10.5        -5.6

Two things this is NOT:
  - It is not a claim that a fixed budget SHAPE beats the field. It does not; see
    research/strategy_search.py's leave-one-year-out gate. This solves a different and
    well-posed question — what is affordable and best AT these prices — and says nothing
    about whether the shape repeats.
  - It is not a tier-median rule. Tier composition shifts year to year, so a tier's median
    price is not a price for a specific player (2025's "RB4" tier median was $17 while
    Ashton Jeanty, projected $42, went for $61). Every player is priced individually.

Only starters score, so the optimizer buys starters and reserves $1 per bench/DST slot.

Run:  PYTHONPATH=analysis python3 analysis/optimal_budget.py
      PYTHONPATH=analysis python3 analysis/optimal_budget.py --compare   # vs config/plan.json
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(ROOT, "draft_sheets", "tool_data.json")
PLAN = os.path.join(ROOT, "config", "plan.json")
FLEX_OK = ("RB", "WR", "TE")


def load():
    d = json.load(open(TOOL))
    return d


def expected_prices(d):
    """Mirror the console's predictPrice at draft open (all rivals full budget/needs)."""
    me = d["me"]
    slots = sum(d["starters"].values()) + d["flex"] + d["bench"]
    afford = d["budget"] - (slots - 1)

    def team_bid(t, p):
        base = max(0.5, p["worth"])
        b = base * (t["mult"].get(p["pos"], 1))
        if t["conc"] > 72:                       # stars-and-scrubs tilt
            if base >= 25:
                b *= 1.12
            elif base >= 8:
                b *= 0.82
        return max(0.0, min(b, afford, t["maxbuy"]))

    out = {}
    for p in d["players"]:
        bids = sorted((b for b in (team_bid(t, p) for t in d["managers"]
                                   if t["name"] != me) if b >= 1), reverse=True)
        if not bids:
            est = 1
        else:
            est = min(bids[0], (bids[1] if len(bids) > 1 else 1) + 1)
        out[p["name"]] = max(1, int(round(est)))
    return out


def solve(d, price):
    """Max total starter VBD within budget. DP over (nRB, nWR, nTE, $) after fixing QB.

    Exact, not greedy: value-per-dollar ordering is wrong here because the slot minimums
    (2RB/2WR/1TE) and the single QB slot interact — a cheap high-VBD RB can be worth taking
    even at poor $/VBD if it frees the flex for a better WR.
    """
    starters, flex, bench = d["starters"], d["flex"], d["bench"]
    n_skill = starters["RB"] + starters["WR"] + starters["TE"] + flex     # non-QB starters
    reserve = bench + 1                                                   # bench + 1 DST at $1
    cap = d["budget"] - reserve

    # QB is its own single slot: try every QB, keep the best total
    qbs = [p for p in d["players"] if p["pos"] == "QB"]
    skill = [p for p in d["players"] if p["pos"] in FLEX_OK and p["vbd"] > 0]
    # only sane candidates: best VBD per position-price point (keeps the DP small)
    skill.sort(key=lambda p: -p["vbd"])
    skill = skill[:90]

    best = None
    for qb in sorted(qbs, key=lambda p: -p["vbd"])[:12]:
        qb_cost = price[qb["name"]]
        if qb_cost > cap:
            continue
        room = cap - qb_cost
        # dp[(nrb,nwr,nte)][budget] = (vbd, picks)
        dp = {(0, 0, 0): {0: (0.0, ())}}
        for p in skill:
            c, v, pos = price[p["name"]], p["vbd"], p["pos"]
            nxt = {k: {b: val for b, val in row.items()} for k, row in dp.items()}
            for (nrb, nwr, nte), row in dp.items():
                if nrb + nwr + nte >= n_skill:
                    continue
                k2 = (nrb + (pos == "RB"), nwr + (pos == "WR"), nte + (pos == "TE"))
                dst = nxt.setdefault(k2, {})
                for b, (tv, picks) in row.items():
                    nb = b + c
                    if nb > room:
                        continue
                    cur = dst.get(nb)
                    if cur is None or tv + v > cur[0]:
                        dst[nb] = (tv + v, picks + (p["name"],))
            dp = nxt
        for (nrb, nwr, nte), row in dp.items():
            if nrb + nwr + nte != n_skill or nrb < starters["RB"] \
               or nwr < starters["WR"] or nte < starters["TE"]:
                continue
            for b, (tv, picks) in row.items():
                tot = tv + qb["vbd"]
                if best is None or tot > best[0]:
                    best = (tot, qb["name"], picks, qb_cost + b)
    return best


def main():
    d = load()
    price = expected_prices(d)
    byname = {p["name"]: p for p in d["players"]}
    best = solve(d, price)
    if not best:
        raise SystemExit("no feasible roster — check tool_data.json")
    total_vbd, qb, picks, spend = best
    reserve = d["bench"] + 1

    print(f"OPTIMAL-VALUE ROSTER at expected prices  (${d['budget']} budget, "
          f"${reserve} reserved for {d['bench']} bench + DST at $1)\n")
    print(f"   {'slot':6}{'player':24}{'tier':7}{'exp $':>7}{'VBD':>6}{'$/VBD':>8}")
    rows = [("QB", qb)] + [("", n) for n in picks]
    # label slots in the console's plan vocabulary
    seen = {"RB": 0, "WR": 0, "TE": 0}
    labeled = []
    for _, n in rows:
        p = byname[n]
        if p["pos"] == "QB":
            lab = "QB"
        else:
            seen[p["pos"]] += 1
            base = d["starters"].get(p["pos"], 0)
            lab = f"{p['pos']}{seen[p['pos']]}" if seen[p["pos"]] <= base else "FLX"
        labeled.append((lab, p))
    for lab, p in sorted(labeled, key=lambda x: -price[x[1]["name"]]):
        c = price[p["name"]]
        print(f"   {lab:6}{p['name']:24}{p['tier']:7}{c:>7}{p['vbd']:>6}"
              f"{(c / p['vbd'] if p['vbd'] else 0):>8.2f}")
    print(f"   {'':6}{'':24}{'TOTAL':>7}{spend:>7}{total_vbd:>6.0f}"
          f"{(spend / total_vbd if total_vbd else 0):>8.2f}")
    print(f"   {'':6}{'':24}{'+reserve':>7}{reserve:>7}   -> ${spend + reserve} of ${d['budget']}")

    if "--compare" in sys.argv and os.path.exists(PLAN):
        plan = {k: v for k, v in json.load(open(PLAN)).items() if not k.startswith("_")}
        print("\nvs config/plan.json — what each slot's budget buys at expected prices:")
        opt = {}
        for lab, p in labeled:
            opt.setdefault(lab, []).append(price[p["name"]])
        print(f"   {'slot':7}{'plan $':>8}{'optimal $':>11}{'delta':>8}")
        for k in ("QB", "RB1", "RB2", "WR1", "WR2", "TE", "FLX1", "FLX2"):
            pv = plan.get(k)
            ov = None
            base = k.rstrip("12")
            if k in opt:
                ov = opt[k][0]
            elif base == "FLX" and "FLX" in opt:
                idx = 0 if k.endswith("1") else 1
                ov = opt["FLX"][idx] if idx < len(opt["FLX"]) else None
            if pv is None:
                continue
            ds = f"{ov - pv:+d}" if ov is not None else "-"
            print(f"   {k:7}{pv:>8}{(ov if ov is not None else '-'):>11}{ds:>8}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
