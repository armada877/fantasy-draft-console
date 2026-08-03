#!/usr/bin/env python3
"""Analysis 4: what separates success from failure in the league.

Links each manager-season's BEHAVIOR (draft style, draft value, waiver work,
trade activity) to OUTCOMES (points-for, win%, playoffs, titles). Window
2018-2025 (draft+transactions); trades 2019+. PF is standardized within each
season (z-score) so seasons with different game/scoring totals are comparable.
"""
import lib
from collections import defaultdict
import statistics

SEASONS = range(2018, 2026)


def season_value(season):
    out = {}
    for e in lib.load_players_raw(season):
        pid = e["player"]["id"]
        for s in e["player"].get("stats", []):
            if s.get("statSourceId") == 0 and s.get("statSplitTypeId") == 0 and s.get("scoringPeriodId") == 0:
                out[pid] = s.get("appliedTotal", 0.0)
    return out


def build():
    """One row per manager-season with behaviors + outcomes."""
    rows = []
    for yr in SEASONS:
        d = lib.load(yr)
        val = season_value(yr)
        picks = lib.draft_picks(yr)
        by_team_picks = defaultdict(list)
        for p in picks:
            by_team_picks[p["teamId"]].append(p)
        # transactions
        adds = defaultdict(int); claims = defaultdict(int); faab = defaultdict(int); drops = defaultdict(int)
        for t in lib.load_transactions(yr):
            tid = t.get("teamId")
            if tid is None:
                continue
            if t.get("type") == "WAIVER":
                claims[tid] += 1
                if t.get("status") == "EXECUTED":
                    adds[tid] += 1
                    faab[tid] += t.get("bidAmount", 0) or 0
            elif t.get("type") == "ROSTER" and t.get("status") == "EXECUTED":
                if any(i.get("type") == "DROP" for i in t.get("items", [])):
                    drops[tid] += 1
        trades = defaultdict(int)
        for tr in lib.executed_trades(yr):
            for tm in tr["sides"]:
                trades[tm] += 1  # keyed by teamId (matches row tid below)
        # PF for z-score
        pfs = {t["id"]: t.get("record", {}).get("overall", {}).get("pointsFor", 0) for t in d["teams"]}
        pf_vals = list(pfs.values())
        pf_mean, pf_sd = statistics.mean(pf_vals), (statistics.pstdev(pf_vals) or 1)
        for t in d["teams"]:
            tid = t["id"]
            mgr = lib.manager(yr, tid)
            ov = t.get("record", {}).get("overall", {})
            tp = by_team_picks[tid]
            spend = sum(p["cost"] for p in tp) or 1
            pos = defaultdict(float)
            for p in tp:
                pos[p["pos"]] += p["cost"]
            costs = sorted((p["cost"] for p in tp), reverse=True)
            draft_pts = sum(val.get(p["playerId"], 0) for p in tp)
            rank = t.get("rankCalculatedFinal") or 99
            seed = t.get("playoffSeed") or 99
            rows.append({
                "year": yr, "mgr": mgr, "tid": tid,
                # behaviors
                "rb_pct": 100 * pos["RB"] / spend,
                "wr_pct": 100 * pos["WR"] / spend,
                "top3": 100 * sum(costs[:3]) / spend,
                "draft_pts": draft_pts,           # season pts of drafted players
                "draft_ppd": draft_pts / 200.0,   # points per $ (draft value)
                "adds": adds[tid], "claims": claims[tid],
                "hit": adds[tid] / claims[tid] if claims[tid] else 0,
                "faab": faab[tid], "drops": drops[tid],
                "trades": trades.get(tid, 0),
                "moves": adds[tid] + trades.get(tid, 0),
                # outcomes
                "wins": ov.get("wins", 0), "losses": ov.get("losses", 0),
                "winpct": ov.get("percentage", 0),
                "pf": ov.get("pointsFor", 0),
                "pf_z": (pfs[tid] - pf_mean) / pf_sd,
                "playoff": 1 if 1 <= seed <= 6 else 0,
                "champ": 1 if rank == 1 else 0,
                "final": rank,
            })
    return rows


def pearson(xs, ys):
    n = len(xs)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = (sum((x - mx) ** 2 for x in xs) ** 0.5)
    dy = (sum((y - my) ** 2 for y in ys) ** 0.5)
    return num / (dx * dy) if dx and dy else 0


def report():
    rows = build()
    print("=" * 96)
    print("ANALYSIS 4 — WHAT SEPARATES SUCCESS FROM FAILURE (2018-2025)")
    print("=" * 96)

    # ---- career leaderboard ----
    bym = defaultdict(list)
    for r in rows:
        bym[r["mgr"]].append(r)
    print("\n### Career success (managers with >=3 auction seasons)")
    print("-" * 96)
    print(f"{'Manager':17}{'seas':>5}{'avg PF-z':>9}{'win%':>7}{'PO rate':>9}{'titles':>7}{'avg finish':>11}")
    print("-" * 96)
    def career_key(m):
        rs = bym[m]
        return -statistics.mean(r["pf_z"] for r in rs)
    mgrs = [m for m in bym if len(bym[m]) >= 3]
    for m in sorted(mgrs, key=career_key):
        rs = bym[m]
        print(f"{m:17}{len(rs):>5}{statistics.mean(r['pf_z'] for r in rs):>+9.2f}"
              f"{100*statistics.mean(r['winpct'] for r in rs):>6.0f}%"
              f"{100*statistics.mean(r['playoff'] for r in rs):>8.0f}%"
              f"{sum(r['champ'] for r in rs):>7}{statistics.mean(r['final'] for r in rs):>11.1f}")

    # ---- correlations behavior -> success ----
    print("\n### What correlates with winning?  (Pearson r vs standardized Points-For, n=%d team-seasons)" % len(rows))
    print("-" * 96)
    metrics = [
        ("draft value (season pts of drafted roster)", "draft_pts"),
        ("draft $ concentration (top-3 %)", "top3"),
        ("RB% of draft budget", "rb_pct"),
        ("WR% of draft budget", "wr_pct"),
        ("waiver adds (volume)", "adds"),
        ("waiver hit rate", "hit"),
        ("FAAB spent", "faab"),
        ("trades made", "trades"),
        ("total in-season moves (adds+trades)", "moves"),
    ]
    for label, key in sorted(metrics, key=lambda kv: -abs(pearson([r[kv[1]] for r in rows], [r["pf_z"] for r in rows]))):
        r = pearson([x[key] for x in rows], [x["pf_z"] for x in rows])
        rw = pearson([x[key] for x in rows], [x["winpct"] for x in rows])
        bar = "+" * int(abs(r) * 40)
        sign = "" if r >= 0 else "-"
        print(f"   r={r:>+5.2f} (vs win% {rw:>+5.2f})  {label:44} {sign}{bar}")
    print("   (PF is a cleaner skill signal than win%, which carries more schedule luck.)")

    # ---- winners vs losers behavior contrast ----
    print("\n### Playoff teams vs non-playoff teams — average behavior")
    print("-" * 96)
    po = [r for r in rows if r["playoff"]]
    npo = [r for r in rows if not r["playoff"]]
    def avg(g, k):
        return statistics.mean(x[k] for x in g)
    print(f"{'metric':40}{'playoff':>10}{'missed':>10}{'edge':>10}")
    for label, key in [("draft value (pts)", "draft_pts"), ("top-3 concentration %", "top3"),
                       ("RB% budget", "rb_pct"), ("WR% budget", "wr_pct"),
                       ("waiver adds", "adds"), ("waiver hit%", "hit"),
                       ("FAAB spent", "faab"), ("trades made", "trades")]:
        a, b = avg(po, key), avg(npo, key)
        mult = 100 if key == "hit" else 1
        print(f"{label:40}{a*mult:>10.1f}{b*mult:>10.1f}{(a-b)*mult:>+10.1f}")


if __name__ == "__main__":
    report()
