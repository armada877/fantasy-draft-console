#!/usr/bin/env python3
"""Per-week ESPN projections for every remaining week — the real weekly forecast.

WHY THIS EXISTS. The season builder derived one flat rate per player (rest-of-season
points / games remaining), so the cockpit showed the same number every week and the
lineup planner could not tell a good matchup from a bad one. Bo Nix ranges 15.8-20.7
across weeks 2-14; a flat 17.9 hides all of it.

An earlier probe concluded future-week projections were unavailable (HTTP 400). That
was WRONG: the 400 came from the `filterStatsForTopScoringPeriodIds` filter, not from
the endpoint. A plain `?scoringPeriodId=N` with an ordinary player filter returns
week-N projections for any remaining week.

    GET .../leagues/{id}?view=kona_player_info&scoringPeriodId={W}
      -> stats[] entry with statSourceId=1, statSplitTypeId=1, scoringPeriodId=W

Byes come out as 0.0 for free, so they no longer need separate handling.

Output: ctx.raw("weekly_proj.json")
    {"season":2026,"fetched":"...","weeks":[2,...,17],
     "proj":{"<week>":{"<playerId>": points}}}

    python3 scraping/scrape_weekly_proj.py [--league KEY] [--refresh]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import leagues                                    # noqa: E402
from cache import Cache, IMMUTABLE, LIVE                     # noqa: E402
from scrape_league import load_auth               # noqa: E402

BASE = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
        "{season}/segments/0/leagues/{league_id}")
# Ordinary filter only. Adding filterStatsForTopScoringPeriodIds makes ESPN 400.
PLAYER_FILTER = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                             "limit": 1500,
                             "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}}


def weekly_for(ctx, week, cache, cookie, current_week=None):
    url = BASE.format(season=ctx.season, league_id=ctx.league_id) + \
        f"?view=kona_player_info&scoringPeriodId={week}"
    data, meta = cache.get_json(
        url, headers={"cookie": cookie, "x-fantasy-platform": "kona",
                      "x-fantasy-source": "kona", "accept": "application/json",
                      "x-fantasy-filter": json.dumps(PLAYER_FILTER)},
        # A week that has already been played is frozen — its projection is now a
        # historical record, useful only for backtesting proj-vs-actual. Only the
        # current and future weeks can still move.
        key=f"weekly_proj/{ctx.season}_wk{week:02d}",
        tier=(IMMUTABLE if (current_week and week < int(current_week)) else LIVE),
        ttl=(None if (current_week and week < int(current_week)) else 6 * 3600),
        unwrap_list=True)
    out = {}
    for entry in (data.get("players") or []):
        pl = entry.get("player") or {}
        pid = pl.get("id")
        if pid is None:
            continue
        for s in pl.get("stats") or []:
            if (s.get("statSourceId") == 1 and s.get("statSplitTypeId") == 1
                    and s.get("scoringPeriodId") == week
                    and s.get("seasonId") == ctx.season):
                out[str(pid)] = round(float(s.get("appliedTotal") or 0.0), 2)
                break
    return out, meta.get("cache")


def run(ctx, refresh=False, quiet=False):
    prof = ctx.profile()
    current = 1
    try:
        payload = json.load(open(ctx.raw("league_full.json")))
        if isinstance(payload, list):
            payload = payload[0]
        current = int(payload.get("scoringPeriodId") or 1)
    except Exception:
        pass
    last = max(prof.playoff_weeks) if prof.playoff_weeks else prof.regular_weeks
    weeks = list(range(current, last + 1))
    cookie = "SWID={}; espn_s2={}".format(*load_auth())
    cache = Cache(os.path.join(ctx.raw_dir, "cache"), refresh=refresh)

    proj, hits = {}, {"hit": 0, "fetched": 0}
    for w in weeks:
        vals, how = weekly_for(ctx, w, cache, cookie, current_week=current)
        proj[str(w)] = vals
        hits["hit" if how == "hit" else "fetched"] += 1
        if how != "hit":
            time.sleep(0.4)                       # be a polite client
    ctx.ensure_dirs()
    out = {"season": ctx.season, "fetched": datetime.now(timezone.utc).isoformat(),
           "weeks": weeks, "current_week": current, "proj": proj}
    path = ctx.raw("weekly_proj.json")
    with open(path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    if not quiet:
        n = len(proj.get(str(weeks[0]), {})) if weeks else 0
        spread = 0
        for pid in list(proj.get(str(weeks[0]), {}))[:400]:
            vals = [proj[str(w)].get(pid) for w in weeks if proj[str(w)].get(pid)]
            if len(vals) > 3:
                spread = max(spread, max(vals) - min(vals))
        print(f"  {ctx.key:<18} weeks {weeks[0]}-{weeks[-1]} · {n} players/week · "
              f"{hits['fetched']} fetched / {hits['hit']} cached · "
              f"max wk-to-wk spread {spread:.1f} pts · {os.path.getsize(path)/1e3:.0f} KB")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--league")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args(argv)
    ctxs = [leagues.resolve(a.league)] if a.league else leagues.all()
    for ctx in ctxs:
        try:
            run(ctx, refresh=a.refresh)
        except Exception as e:
            print(f"  {ctx.key:<18} FAILED: {str(e)[:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
