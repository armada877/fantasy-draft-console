#!/usr/bin/env python3
"""Analysis 15: per-opponent draft-day briefs. Fuses every per-manager signal
(draft lean/style §2, bid-vs-model §11, nomination style §6, waivers/trades §3,
success §4) into one actionable card per current (2025) manager.
"""
import json
import os
import re
import statistics
from collections import defaultdict, Counter
import lib
import a4_success as a4

PROJ = json.load(open(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
                                   "draft_sheets", "elboberto_projections.json")))
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
AUCTION = range(2017, 2026)
MODEL_YEARS = [2022, 2023, 2024, 2025]


def norm(n):
    n = re.sub(r"\([^)]*\)", "", str(n))
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def current_managers():
    return sorted({lib.manager(2025, tid) for tid in lib.team_owner(2025)})


def compute():
    # per-manager accumulators
    draft = defaultdict(list)          # per-season draft profile dicts
    over = defaultdict(lambda: defaultdict(list))   # bid-vs-model by pos
    nom_buys = defaultdict(lambda: [0, 0])          # [others_nominated, total]
    nom_val = defaultdict(list)
    adds = defaultdict(list); hit = defaultdict(list)
    trades = defaultdict(int); partners = defaultdict(Counter); seasons = defaultdict(set)

    for yr in AUCTION:
        to = lib.team_owner(yr)
        for tid in to:
            seasons[lib.manager(yr, tid)].add(yr)
        picks = lib.draft_picks(yr)
        byteam = defaultdict(list)
        for p in picks:
            byteam[p["teamId"]].append(p)
        for tid, ps in byteam.items():
            m = lib.manager(yr, tid)
            spend = sum(p["cost"] for p in ps) or 1
            pos = defaultdict(float)
            for p in ps:
                pos[p["pos"]] += p["cost"]
            costs = sorted((p["cost"] for p in ps), reverse=True)
            draft[m].append({"rb": 100*pos["RB"]/spend, "wr": 100*pos["WR"]/spend,
                             "te": 100*pos["TE"]/spend, "top3": 100*sum(costs[:3])/spend,
                             "maxbuy": costs[0] if costs else 0,
                             "darts": sum(1 for c in costs if 1 <= c <= 2)})
        # nominations
        for p in picks:
            nt = p.get("nominatingTeamId") or 0
            if nt == 0 or p["is_keeper"]:
                continue
            winner = p["manager"]
            nom_val[lib.MANAGER_CANON.get(to.get(nt, {}).get("owner"),
                    to.get(nt, {}).get("ownerName"))].append(p["cost"])
            nb = nom_buys[winner]
            nb[1] += 1
            if nt != p["teamId"]:
                nb[0] += 1

    # bid vs model
    for yr in MODEL_YEARS:
        pl = {norm(p["name"]): p for p in PROJ[str(yr)] if p.get("proj_value") is not None}
        for p in lib.draft_picks(yr):
            if p["is_keeper"] or p["cost"] < 1:
                continue
            e = pl.get(norm(p["name"]))
            if e:
                over[p["manager"]][p["pos"]].append(p["cost"] - e["proj_value"])
    # transactions
    for yr in range(2018, 2026):
        for t in lib.load_transactions(yr):
            tid = t.get("teamId")
            if tid is None:
                continue
            m = lib.manager(yr, tid)
            if t.get("type") == "WAIVER":
                hit[(m, yr)] = hit.get((m, yr), [0, 0])
                hit[(m, yr)][1] += 1
                if t.get("status") == "EXECUTED":
                    hit[(m, yr)][0] += 1
        for tr in lib.executed_trades(yr) if yr >= 2019 else []:
            names = list(tr["managers"].values())
            for m in names:
                trades[m] += 1
                for o in names:
                    if o != m:
                        partners[m][o] += 1
    # adds/yr aggregate
    adds_year = defaultdict(list); hitrate = defaultdict(list)
    for (m, yr), (a, c) in hit.items():
        adds_year[m].append(a)
        if c:
            hitrate[m].append(a/c)

    # success
    succ = defaultdict(list)
    for s in a4.build():
        succ[s["mgr"]].append(s)

    return dict(draft=draft, over=over, nom_buys=nom_buys, nom_val=nom_val,
                adds=adds_year, hitrate=hitrate, trades=trades, partners=partners,
                seasons=seasons, succ=succ)


def brief(m, D):
    d = D["draft"][m]
    rb = statistics.mean(x["rb"] for x in d); wr = statistics.mean(x["wr"] for x in d)
    te = statistics.mean(x["te"] for x in d); top3 = statistics.mean(x["top3"] for x in d)
    maxbuy = statistics.mean(x["maxbuy"] for x in d)
    rbsd = statistics.pstdev([x["rb"] for x in d])
    lean = rb - wr
    lean_s = ("hard RB-anchor" if lean > 20 else "RB-lean" if lean > 6 else
              "WR-lean" if lean < -6 else "balanced")
    style = ("extreme stars&scrubs" if top3 > 74 else "stars&scrubs" if top3 > 66 else
             "moderate" if top3 > 60 else "balanced/spreads")
    pred = "very predictable" if rbsd < 10 else "consistent" if rbsd < 16 else "opportunistic"
    # bid vs model
    ov = D["over"][m]
    def om(pos):
        return statistics.mean(ov[pos]) if ov.get(pos) else 0
    infl = max([("RB", om("RB")), ("WR", om("WR")), ("TE", om("TE"))], key=lambda x: x[1])
    # nomination
    nb = D["nom_buys"][m]; pounce = 100*nb[0]/nb[1] if nb[1] else 0
    # transactions
    addsyr = statistics.mean(D["adds"][m]) if D["adds"].get(m) else 0
    hr = 100*statistics.mean(D["hitrate"][m]) if D["hitrate"].get(m) else 0
    nseas = len([y for y in D["seasons"][m] if y >= 2019]) or 1
    tpy = D["trades"].get(m, 0)/nseas
    topp = D["partners"][m].most_common(1)
    # success
    sc = D["succ"].get(m, [])
    pfz = statistics.mean(s["pf_z"] for s in sc) if sc else 0
    po = 100*statistics.mean(s["playoff"] for s in sc) if sc else 0
    titles = sum(s["champ"] for s in sc)

    print(f"── {m} " + "─"*(58-len(m)))
    print(f"   DRAFT : {lean_s}, {style} (top3 {top3:.0f}%, max buy ${maxbuy:.0f}); {pred} (RB% sd {rbsd:.0f})")
    print(f"           avg RB {rb:.0f}% / WR {wr:.0f}% / TE {te:.0f}%")
    bidline = f"   BIDS  : overpays {infl[0]} (+${infl[1]:.0f} vs model)" if infl[1] > 2 else "   BIDS  : disciplined vs model"
    print(bidline + f"; QB {om('QB'):+.0f} TE {om('TE'):+.0f} vs model")
    print(f"   NOMS  : {'pouncer (waits, bids others)' if pounce>72 else 'proactive (nominates own targets)' if pounce<66 else 'balanced'} ({pounce:.0f}% of buys off others' noms)")
    print(f"   MOVES : {addsyr:.0f} waiver adds/yr ({hr:.0f}% hit); {tpy:.1f} trades/yr"
          + (f", trades most w/ {topp[0][0]}" if topp else ""))
    print(f"   RESULT: PF-z {pfz:+.2f}, playoffs {po:.0f}%, titles {titles}")
    return dict(lean=lean, infl=infl, style=style, top3=top3, maxbuy=maxbuy, pounce=pounce)


def main():
    D = compute()
    cur = [m for m in current_managers() if m != lib.ME and len(D["draft"].get(m, [])) >= 3]
    print("=" * 66)
    print("ANALYSIS 15 — PER-OPPONENT DRAFT BRIEFS (current league, ex-Harry)")
    print("=" * 66)
    for m in sorted(cur, key=lambda m: -statistics.mean(x["rb"] for x in D["draft"][m])):
        brief(m, D)
    small = [m for m in current_managers() if m != lib.ME and len(D["draft"].get(m, [])) < 3]
    if small:
        print("\n(small sample, <3 auctions — limited read:", ", ".join(small), ")")


if __name__ == "__main__":
    main()
