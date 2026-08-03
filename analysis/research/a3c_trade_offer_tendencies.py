#!/usr/bin/env python3
"""Analysis 3c: TRADE & OFFER tendency characterization — league + individuals.

Offers = TRADE_PROPOSAL rows (transactions.json). Executed trades = parsed from
playercards via lib.executed_trades(). Window 2019-2025 (2018 offers are a data
artifact: 386 vs ~30 typical; 2018 trade items unavailable).
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


def offers():
    recs = []
    for yr in SEASONS:
        for t in lib.load_transactions(yr):
            if t.get("type") != "TRADE_PROPOSAL":
                continue
            ptid = t.get("teamId")
            if ptid is None:
                continue
            other = None
            for i in t.get("items", []):
                if i.get("type") == "TRADE" and i.get("fromTeamId") == ptid:
                    other = i.get("toTeamId") or other
                elif i.get("type") == "TRADE" and i.get("toTeamId") == ptid:
                    other = i.get("fromTeamId") or other
            recs.append({"year": yr, "proposer": lib.manager(yr, ptid),
                         "recipient": lib.manager(yr, other) if other else "?"})
    return recs


def manager_trade_metrics():
    """Per manager: executed-trade behavior over 2019-25."""
    m = defaultdict(lambda: {"trades": 0, "recv": 0, "given": 0,
                             "recv_val": 0.0, "given_val": 0.0,
                             "partners": Counter(), "acq_pos": Counter()})
    active = defaultdict(set)
    for yr in SEASONS:
        val = season_value(yr)
        for tid in lib.team_owner(yr):
            active[lib.manager(yr, tid)].add(yr)
        for tr in lib.executed_trades(yr):
            names = list(tr["managers"].values())
            for tm, mgr in tr["managers"].items():
                rec = m[mgr]
                rec["trades"] += 1
                got = tr["sides"][tm]
                gave = [p for t2, pl in tr["sides"].items() if t2 != tm for p in pl]
                rec["recv"] += len(got)
                rec["given"] += len(gave)
                rec["recv_val"] += sum(val.get(p, 0) for p in got)
                rec["given_val"] += sum(val.get(p, 0) for p in gave)
                for p in got:
                    rec["acq_pos"][lib.ppos(p)] += 1
                for other in names:
                    if other != mgr:
                        rec["partners"][other] += 1
    return m, active


def report():
    print("=" * 96)
    print("ANALYSIS 3c — TRADE & OFFER TENDENCIES (2019-2025)")
    print("=" * 96)

    # ---------------- LEAGUE ----------------
    trades_by_year = {yr: len(lib.executed_trades(yr)) for yr in SEASONS}
    offs = offers()
    offers_by_year = Counter(o["year"] for o in offs)
    print("\n### LEAGUE — volume & 'talk-to-action' conversion")
    print("-" * 96)
    print(f"{'Season':8}{'offers':>8}{'trades':>8}{'offers/trade':>14}")
    for yr in SEASONS:
        o, t = offers_by_year[yr], trades_by_year[yr]
        print(f"{yr:<8}{o:>8}{t:>8}{(o/t if t else 0):>14.1f}")
    to, tt = sum(offers_by_year.values()), sum(trades_by_year.values())
    print(f"{'ALL':<8}{to:>8}{tt:>8}{to/tt:>14.1f}")
    print("Fewer formal offers than executed trades most years => deals are struck via chat/verbally,")
    print("not the ESPN offer button. The offer feed undercounts real negotiation.")

    # timing
    sp = Counter()
    for yr in SEASONS:
        for tr in lib.executed_trades(yr):
            sp[tr["sp"]] += 1
    pre = sp.get(0, 0)
    early = sum(v for k, v in sp.items() if 1 <= k <= 5)
    mid = sum(v for k, v in sp.items() if 6 <= k <= 9)
    late = sum(v for k, v in sp.items() if k >= 10)
    print("\n### LEAGUE — timing")
    print(f"   preseason/draft (wk0): {pre}   early (wk1-5): {early}   "
          f"mid (wk6-9): {mid}   late/deadline (wk10+): {late}")
    print("   Trade market peaks mid-season (wk6-9) then cools; deadline ~wk13.")

    # structure
    sizes = Counter()
    consolidation = 0
    total = 0
    for yr in SEASONS:
        for tr in lib.executed_trades(yr):
            sizes[len(tr["pids"])] += 1
            total += 1
            counts = sorted(len(pl) for pl in tr["sides"].values())
            if len(counts) == 2 and counts[0] != counts[1]:
                consolidation += 1
    print("\n### LEAGUE — trade structure")
    print(f"   sizes (players in deal): {dict(sorted(sizes.items()))}")
    print(f"   {100*sizes.get(2,0)/total:.0f}% are 1-for-1; "
          f"{100*consolidation/total:.0f}% are unbalanced (consolidation: many-for-few).")
    pos = Counter()
    for yr in SEASONS:
        for tr in lib.executed_trades(yr):
            for p in tr["pids"]:
                pos[lib.ppos(p)] += 1
    tp = sum(pos.values())
    print("   positions moved: " + "  ".join(f"{p} {100*n/tp:.0f}%" for p, n in pos.most_common()))

    # ---------------- INDIVIDUAL ----------------
    m, active = manager_trade_metrics()
    sent = Counter(o["proposer"] for o in offs)
    recv = Counter(o["recipient"] for o in offs)
    print("\n### INDIVIDUAL — trade & offer profile (per season averages)")
    print("-" * 96)
    print(f"{'Manager':17}{'trd/yr':>7}{'off_sent/yr':>12}{'off_rcvd/yr':>12}"
          f"{'net players':>12}{'net value':>11}   archetype")
    print("-" * 96)
    mgrs = [mm for mm in m if len(active[mm]) >= 3]
    for mgr in sorted(mgrs, key=lambda x: -m[x]["trades"] / max(len(active[x]), 1)):
        rec = m[mgr]
        yrs = max(len(active[mgr]), 1)
        tpy = rec["trades"] / yrs
        spy = sent[mgr] / yrs
        rpy = recv[mgr] / yrs
        net_players = (rec["recv"] - rec["given"]) / max(rec["trades"], 1)
        net_val = (rec["recv_val"] - rec["given_val"]) / max(rec["trades"], 1)
        # archetype
        if tpy > 5:
            arch = "wheeler-dealer"
        elif spy > 6 and tpy < 4:
            arch = "tire-kicker (spams offers)"
        elif tpy < 1.3:
            arch = "avoids trading"
        else:
            arch = "selective trader"
        if net_players < -0.4:
            arch += ", consolidator"
        elif net_players > 0.4:
            arch += ", accumulates depth"
        print(f"{mgr:17}{tpy:>7.1f}{spy:>12.1f}{rpy:>12.1f}{net_players:>+12.2f}"
              f"{net_val:>+11.0f}   {arch}")
    print("-" * 96)
    print("net players = avg (received - given) per trade  (- = trades many for few / consolidates)")
    print("net value  = avg season-pts (received - given) per trade  (hindsight; + = acquired more prod.)")

    print("\n### INDIVIDUAL — preferred trade partners & position targeted")
    for mgr in sorted(mgrs, key=lambda x: -m[x]["trades"]):
        rec = m[mgr]
        if not rec["trades"]:
            continue
        top = rec["partners"].most_common(2)
        tp2 = ", ".join(f"{p}({n})" for p, n in top)
        acq = rec["acq_pos"].most_common(1)
        acqs = f"{acq[0][0]}" if acq else "-"
        print(f"   {mgr:17} partners: {tp2:38} mostly acquires: {acqs}")


if __name__ == "__main__":
    report()
