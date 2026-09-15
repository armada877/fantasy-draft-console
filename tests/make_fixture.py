#!/usr/bin/env python3
"""Generate a season_data.json FIXTURE per league, to the frozen contract.

Lets the app-logic and UI workstreams build in parallel with data acquisition: the
league shape (sizes, lineup slots, scoring, FAAB vs priority) is REAL — pulled from
each league's live settings — while players/points are synthetic and deterministic.

    python3 tests/make_fixture.py        # -> tests/fixtures/season_data.<key>.json

Writes only under tests/fixtures/, so it never collides with the real pipeline.
"""
import json, os, random, sys, urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scraping"))
import leagues                                    # noqa: E402
from engine.profile import LeagueProfile, slot_name, FAAB   # noqa: E402

OUT = os.path.join(ROOT, "tests", "fixtures")
WEEK = 2
ELIG = {"QB": [0, 7, 20, 21], "RB": [2, 3, 23, 7, 20, 21], "WR": [4, 3, 5, 23, 7, 20, 21],
        "TE": [6, 5, 23, 7, 20, 21], "K": [17, 20, 21], "DST": [16, 20, 21]}
NFL = ["BUF","MIA","NE","NYJ","BAL","CIN","CLE","PIT","DET","GB","MIN","CHI","SF","SEA","LAR","ARI"]


def live_settings(ctx):
    from scrape_league import load_auth
    swid, s2 = load_auth()
    url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
           f"{ctx.season}/segments/0/leagues/{ctx.league_id}?view=mSettings&view=mTeam")
    h = {"accept": "application/json", "x-fantasy-platform": "kona", "x-fantasy-source": "kona",
         "cookie": f"SWID={swid}; espn_s2={s2}",
         "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")}
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=40) as r:
        return json.load(r)


def build(ctx):
    payload = live_settings(ctx)
    prof = LeagueProfile.from_espn(payload)
    rng = random.Random(hash(ctx.key) & 0xffff)
    teams_raw = payload.get("teams") or []

    # which positions this league actually needs, derived from its own slots
    need_pos = ["QB", "RB", "WR", "TE"]
    if 17 in prof.starting_slots:
        need_pos.append("K")
    if 16 in prof.starting_slots:
        need_pos.append("DST")

    players, pid = {}, 1000
    def mk(pos, ppg, owner):
        nonlocal pid
        pid += 1
        players[str(pid)] = {
            "id": pid, "name": f"{pos} Player {pid}", "pos": pos,
            "team": rng.choice(NFL), "eligible_slots": ELIG[pos],
            "owner_team_id": owner, "injury": rng.choice(["ACTIVE"] * 9 + ["QUESTIONABLE"]),
            "bye": rng.choice([5, 6, 7, 9, 10, 11, 12, 14]),
            "ros_points": round(ppg * 15, 1), "ros_ppg": round(ppg, 2),
            "actual_ppg": round(max(0, ppg + rng.uniform(-4, 4)), 2),
            "last3_ppg": round(max(0, ppg + rng.uniform(-6, 6)), 2),
            "pct_owned": round(rng.uniform(0, 100), 1),
            "pct_owned_change": round(rng.uniform(-8, 20), 2),
            "mkt": {"ros_ecr": rng.randint(1, 250), "tier": rng.randint(1, 10),
                    "trend_adds": rng.randint(0, 90000), "trade_value": rng.randint(1, 9000),
                    "snap_share": round(rng.uniform(0.1, 0.95), 2),
                    "target_share": round(rng.uniform(0.0, 0.32), 2)},
        }
        return pid

    teams = []
    for t in teams_raw:
        roster = []
        for pos in need_pos:
            n = {"QB": 2, "RB": 4, "WR": 5, "TE": 2, "K": 1, "DST": 1}[pos]
            for _ in range(n):
                roster.append(mk(pos, rng.uniform(3, 22), t["id"]))
        tc = t.get("transactionCounter") or {}
        rec = (t.get("record") or {}).get("overall") or {}
        entry = {
            "team_id": t["id"], "name": t.get("name") or f"Team {t['id']}",
            "manager": f"Manager {t['id']}",
            "record": {"w": rec.get("wins", 0), "l": rec.get("losses", 0), "t": rec.get("ties", 0)},
            "points_for": rec.get("pointsFor", 0.0),
            "roster": roster,
            "needs": {}, "playoff_odds": round(rng.uniform(0.05, 0.9), 2),
            "tendencies": {"waiver": {"aggression": round(rng.uniform(0.4, 1.6), 2)},
                           "trade": {"rate_per_season": round(rng.uniform(0, 4), 1),
                                     "responds": rng.random() > 0.2}},
            "borrowed": rng.random() < 0.25,
        }
        if prof.acquisition.is_faab:
            entry["faab_left"] = max(0, (prof.acquisition.budget or 100)
                                     - int(tc.get("acquisitionBudgetSpent") or rng.randint(0, 40)))
            entry["waiver_priority"] = None
        else:
            entry["faab_left"] = None
            entry["waiver_priority"] = t.get("waiverRank") or rng.randint(1, prof.size)
        teams.append(entry)

    # free-agent pool
    fa = [mk(rng.choice(need_pos), rng.uniform(0, 14), None) for _ in range(60)]

    me = next((t for t in teams if t["team_id"] == ctx.team_id), teams[0])
    waivers = []
    for p in sorted(fa, key=lambda i: -players[str(i)]["ros_ppg"])[:15]:
        w = {"player_id": p, "marginal_reg": round(rng.uniform(0, 40), 1),
             "marginal_post": round(rng.uniform(0, 12), 1),
             "drop_player_id": rng.choice(me["roster"]), "why": ["fixture data"]}
        if prof.acquisition.is_faab:
            w["bid"] = {"suggested": rng.randint(0, 30), "p80": rng.randint(0, 45),
                        "p_contested": round(rng.uniform(0, 1), 2)}
        else:
            w["claim"] = {"recommend": rng.random() > 0.5, "p_survives": round(rng.uniform(0, 1), 2)}
        waivers.append(w)

    others = [t for t in teams if t["team_id"] != me["team_id"]]
    finder = []
    for t in others[:5]:
        finder.append({"partner_team_id": t["team_id"],
                       "send": [rng.choice(me["roster"])], "receive": [rng.choice(t["roster"])],
                       "my_delta": round(rng.uniform(-5, 18), 1),
                       "their_delta": round(rng.uniform(0, 12), 1),
                       "accept_odds": round(rng.uniform(0, 1), 2),
                       "veto_risk": round(rng.uniform(0, 1), 2) if prof.veto_votes else 0.0,
                       "why": ["fixture data"]})

    return {
        "generated": datetime.now(timezone.utc).isoformat(), "week": WEEK, "fixture": True,
        "league": {
            "key": ctx.key, "name": prof.name, "season": prof.season, "size": prof.size,
            "scoring_label": prof.scoring_label, "regular_weeks": prof.regular_weeks,
            "playoff_teams": prof.playoff_teams, "playoff_weeks": prof.playoff_weeks,
            "trade_deadline": prof.trade_deadline_ms, "veto_votes": prof.veto_votes,
            "draft_type": prof.draft_type, "auction_budget": prof.auction_budget,
            "bench": prof.bench, "ir": prof.ir,
            "acquisition": {"model": prof.acquisition.model, "budget": prof.acquisition.budget,
                            "min_bid": prof.acquisition.min_bid,
                            "continuous": prof.acquisition.continuous},
            "lineup_slots": {str(k): v for k, v in prof.starting_slots.items()},
            "slot_names": {str(k): slot_name(k) for k in prof.starting_slots},
        },
        "me": {"team_id": me["team_id"], "name": me["name"], "manager": me["manager"]},
        "teams": teams, "players": players, "waivers": waivers,
        "trades": {"finder": finder,
                   "buy_low": [{"player_id": rng.choice(others[0]["roster"]),
                                "owner_team_id": others[0]["team_id"], "why": ["fixture"]}],
                   "sell_high": [{"player_id": rng.choice(me["roster"]), "why": ["fixture"]}]},
    }


def status_fixture(ctx, data):
    """pipeline_status.json fixture — the WS-7 contract."""
    rng = random.Random(hash(ctx.key) & 0xff)
    now = datetime.now(timezone.utc).isoformat()
    stages = []
    for nm, summ, age in [("season-scrape", f"{data['league']['size']} teams", 2.1),
                          ("sources", "5 adapters", 1.2),
                          ("season-calibrate", "tendencies + curve", 300.0),
                          ("season-build", f"{len(data['players'])} players", 0.4),
                          ("season-inject", "cockpit written", 0.3)]:
        stages.append({"name": nm, "ok": True, "last_run": now, "age_hours": age,
                       "stale": age > 24 and nm in ("season-scrape", "sources"),
                       "summary": summ, "outputs": [], "error": None,
                       "duration_s": round(rng.uniform(1, 40), 1)})
    srcs = []
    for nm, var in [("fantasypros", data["league"]["scoring_label"]), ("borischen", "weekly"),
                    ("sleeper", "trending"), ("fantasycalc", "redraft"), ("nflverse", "usage")]:
        ok = not (nm == "nflverse" and ctx.key == "inlaws-outlaws")
        tot = len(data["players"])
        srcs.append({"name": nm, "ok": ok, "fetched": now, "age_hours": round(rng.uniform(0.2, 6), 1),
                     "variant": var,
                     "coverage": {"matched": int(tot * rng.uniform(0.75, 0.99)), "total": tot,
                                  "pct": round(rng.uniform(0.75, 0.99), 2)} if ok else None,
                     "error": None if ok else "HTTP 503 (fixture: simulated outage)"})
    borrowed = sum(1 for t in data["teams"] if t.get("borrowed"))
    return {"league": ctx.key, "season": ctx.season, "week": data["week"], "generated": now,
            "fixture": True, "stages": stages, "sources": srcs,
            "calibration": {"managers_total": len(data["teams"]), "managers_borrowed": borrowed,
                            "seasons_used": [2019, 2025],
                            "excluded_seasons": {"2018": "priority-order regime"},
                            "curve": {"model": data["league"]["acquisition"]["model"],
                                      "validated": True, "holdout_mae": 6.1,
                                      "baseline_mae": 9.4, "beats_baseline": True}},
            "warnings": ([f"{ctx.key}: {borrowed} of {len(data['teams'])} managers borrowed"]
                         if borrowed else [])}


def main():
    os.makedirs(OUT, exist_ok=True)
    for ctx in leagues.all():
        data = build(ctx)
        p = os.path.join(OUT, f"season_data.{ctx.key}.json")
        with open(p, "w") as f:
            json.dump(data, f, indent=1)
        sp = os.path.join(OUT, f"pipeline_status.{ctx.key}.json")
        with open(sp, "w") as f:
            json.dump(status_fixture(ctx, data), f, indent=1)
        lg = data["league"]
        slots = "/".join(f"{lg['slot_names'][k]}x{v}" for k, v in lg["lineup_slots"].items())
        print(f"{ctx.key:18s} {lg['size']:>2}tm {lg['scoring_label']:9s} "
              f"{lg['acquisition']['model']:8s} "
              f"slots={slots} "
              f"-> {len(data['players'])} players, {os.path.getsize(p):,}b")


if __name__ == "__main__":
    main()
