#!/usr/bin/env python3
"""Analysis 3: individual manager transaction behavior (2018-2025).

Three layers:
  A. Roster churn & FAAB aggression (waivers, adds, drops, bids)
  B. Offer behavior — proposal volume, near-zero execution, target network
  C. Offer VALUE context — are offers stud-hunts, lowballs, or bust-dumps,
     and do they target positional need? (season fantasy value + draft cost)

Data note: executed trades are not in the mTransactions2 feed (they move to a
separate activity feed). All trade rows here are OFFERS/proposals. Injury-TIMING
correlation needs weekly player data not in the current scrape.
"""
import lib
from collections import defaultdict, Counter
import statistics

SEASONS = range(2018, 2026)


# ---------- player value maps ----------
def season_value(season):
    """{playerId: fantasy_points_season} actual production (statSourceId 0)."""
    out = {}
    for e in lib.load_players_raw(season):
        pid = e["player"]["id"]
        for s in e["player"].get("stats", []):
            if s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 0 and s.get("scoringPeriodId") == 0:
                out[pid] = s.get("appliedTotal", 0.0)
    return out


def season_cost(season):
    """{playerId: real draft $} for players drafted this season."""
    return {p["playerId"]: p["cost"] for p in lib.draft_picks(season)}


# ---------- A. churn & FAAB ----------
def churn_and_faab():
    """{manager: [per-season dict]}"""
    prof = defaultdict(list)
    for yr in SEASONS:
        txns = lib.load_transactions(yr)
        adds = defaultdict(int)       # executed waiver adds
        claims = defaultdict(int)     # all waiver claims attempted
        faab = defaultdict(int)       # FAAB spent (executed)
        maxbid = defaultdict(int)
        drops = defaultdict(int)
        for t in txns:
            typ, status, tid = t.get("type"), t.get("status"), t.get("teamId")
            if tid is None:
                continue
            mgr = lib.manager(yr, tid)
            if typ == "WAIVER":
                claims[mgr] += 1
                if status == "EXECUTED":
                    adds[mgr] += 1
                    faab[mgr] += t.get("bidAmount", 0) or 0
                    maxbid[mgr] = max(maxbid[mgr], t.get("bidAmount", 0) or 0)
            elif typ == "ROSTER" and status == "EXECUTED":
                if any(i.get("type") == "DROP" for i in t.get("items", [])):
                    drops[mgr] += 1
        for mgr in set(list(adds) + list(claims) + list(drops)):
            prof[mgr].append({
                "year": yr, "adds": adds[mgr], "claims": claims[mgr],
                "faab": faab[mgr], "maxbid": maxbid[mgr], "drops": drops[mgr],
                "hitrate": adds[mgr] / claims[mgr] if claims[mgr] else 0,
            })
    return prof


# ---------- B & C. offers ----------
def offer_records():
    """Flatten all offers into records with proposer, recipient, players, values."""
    recs = []
    for yr in SEASONS:
        val = season_value(yr)
        cost = season_cost(yr)
        for t in lib.load_transactions(yr):
            if t.get("type") != "TRADE_PROPOSAL":
                continue
            ptid = t.get("teamId")
            if ptid is None:
                continue
            proposer = lib.manager(yr, ptid)
            gives, gets = [], []          # players proposer gives / receives
            other_team = None
            for i in t.get("items", []):
                if i.get("type") != "TRADE":
                    continue
                pid = i.get("playerId")
                if i.get("fromTeamId") == ptid:
                    gives.append(pid)
                    other_team = i.get("toTeamId") or other_team
                elif i.get("toTeamId") == ptid:
                    gets.append(pid)
                    other_team = i.get("fromTeamId") or other_team
            recipient = lib.manager(yr, other_team) if other_team else "?"
            recs.append({
                "year": yr, "sp": t.get("scoringPeriodId"), "status": t.get("status"),
                "proposer": proposer, "recipient": recipient,
                "gives": gives, "gets": gets,
                "give_val": sum(val.get(p, 0) for p in gives),
                "get_val": sum(val.get(p, 0) for p in gets),
                "give_cost": sum(cost.get(p, 0) for p in gives),
                "get_cost": sum(cost.get(p, 0) for p in gets),
            })
    return recs


def report():
    print("=" * 100)
    print("ANALYSIS 3 — MANAGER TRANSACTION BEHAVIOR (2018-2025)")
    print("=" * 100)

    # ---- A ----
    prof = churn_and_faab()
    def a(m, k):
        return statistics.mean(x[k] for x in prof[m]) if prof[m] else 0
    mgrs = [m for m in prof if len(prof[m]) >= 3]
    print("\n### A. Roster churn & FAAB aggression (avg per season)")
    print("-" * 100)
    print(f"{'Manager':18}{'adds/yr':>8}{'claims':>8}{'hit%':>7}{'FAAB$':>7}{'maxbid':>8}{'drops':>7}   style")
    print("-" * 100)
    for m in sorted(mgrs, key=lambda m: -a(m, "adds")):
        adds, cl, hr, fa, mb, dr = (a(m, k) for k in ["adds", "claims", "hitrate", "faab", "maxbid", "drops"])
        style = ("waiver hawk" if adds > 26 else "active" if adds > 18 else
                 "moderate" if adds > 11 else "set-and-forget")
        print(f"{m:18}{adds:>8.0f}{cl:>8.0f}{100*hr:>6.0f}%{fa:>7.0f}{mb:>8.0f}{dr:>7.0f}   {style}")
    print("-" * 100)
    print("adds=won waiver claims; claims=attempts; hit%=win rate; FAAB=$ spent of ~$100 budget")

    # ---- B ----
    recs = offer_records()
    print("\n### B. Offer behavior — proposals sent, and how few ever close")
    print("-" * 100)
    tot = len(recs)
    executed = sum(1 for r in recs if r["status"] == "EXECUTED")
    print(f"League totals 2018-2025: {tot} offers proposed, {executed} executed in-feed "
          f"({100*executed/tot:.0f}%). Offers are mostly CANCELED/PENDING — a high-noise, "
          f"low-consummation trade culture.")
    sent = Counter(r["proposer"] for r in recs)
    recv = Counter(r["recipient"] for r in recs)
    print(f"\n{'Manager':18}{'offers sent':>12}{'received':>10}{'sent/yr':>9}   role")
    print("-" * 100)
    yrs_active = {m: len(prof[m]) for m in prof}
    for m in sorted(sent, key=lambda m: -sent[m]):
        if m not in mgrs:
            continue
        n = sent[m]
        peryr = n / max(yrs_active.get(m, 1), 1)
        role = ("dealmaker (initiator)" if peryr > 6 else "active proposer" if peryr > 3 else
                "occasional" if peryr > 1 else "rarely proposes")
        print(f"{m:18}{n:>12}{recv[m]:>10}{peryr:>9.1f}   {role}")

    # target network
    print("\n### B2. Who targets whom (top proposer->recipient pairs)")
    pairs = Counter((r["proposer"], r["recipient"]) for r in recs if r["recipient"] != "?")
    for (p, rc), n in pairs.most_common(10):
        print(f"   {p:18} -> {rc:18} {n} offers")

    # ---- C ----
    print("\n### C. Offer VALUE context — stud-hunt vs lowball vs bust-dump")
    print("-" * 100)
    print("Per manager: avg season fantasy value of players they'd GIVE vs GET in offers.")
    print("get>give => buying up (targeting studs); give<get w/ low give-cost => lowballing.")
    print("-" * 100)
    by_mgr = defaultdict(list)
    for r in recs:
        by_mgr[r["proposer"]].append(r)
    print(f"{'Manager':18}{'avg GIVE val':>13}{'avg GET val':>12}{'net(get-give)':>14}   read")
    print("-" * 100)
    for m in sorted(mgrs, key=lambda m: -(statistics.mean([r['get_val']-r['give_val'] for r in by_mgr[m]]) if by_mgr[m] else 0)):
        rs = by_mgr[m]
        if not rs:
            continue
        gv = statistics.mean(r["give_val"] for r in rs)
        gt = statistics.mean(r["get_val"] for r in rs)
        net = gt - gv
        read = ("buying up (wants studs)" if net > 20 else "lowballing" if net > 5 else
                "even swaps" if net > -5 else "selling high / offloading")
        print(f"{m:18}{gv:>13.0f}{gt:>12.0f}{net:>+14.0f}   {read}")
    print("-" * 100)
    print("(fantasy value = actual season points of the players in the offer)")


if __name__ == "__main__":
    report()
