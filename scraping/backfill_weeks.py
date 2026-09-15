#!/usr/bin/env python3
"""WS-8a — historical weekly backfill: the ground truth every backtest needs.

Until this ran, the repo could not score its own projections. The deep-history
scrape holds SEASON totals only (`players.json`), so "was ESPN's week-6 number any
good?" had no answer on disk. It does now:

    GET /apis/v3/games/ffl/seasons/{season}/segments/0/leagues/{id}
        ?view=mBoxscore&scoringPeriodId={week}

returns, per matchup side, `rosterForCurrentScoringPeriod.entries[]` carrying
`lineupSlotId` — **what the manager actually started** — and, per player, BOTH

    src0 split1 sp=W   what he ACTUALLY scored that week
    src1 split1 sp=W   what he was PROJECTED to score that week

which is a paired (projection, outcome) observation, the thing a projection can be
graded against. Verified on 2kdome 2024 wk5: Chase Brown projected 8.03, actual 14.90.

Two traps, both load-bearing:

* **Use `seasons/{season}`, NEVER `leagueHistory`.** The history endpoint answers
  with matchup scores and no roster detail at all (`entries: []`) — re-verified here,
  2024 wk5: 103 matchups, 0 rosters. Same league, same week, `seasons/2024` returns
  the full 16-man rosters.
* **The population is ROSTERED PLAYERS ONLY.** A boxscore cannot show you the free
  agent nobody started. So this set grades "the projections a manager was actually
  deciding on", which is the right population for lineup/waiver questions and the
  wrong one for "ESPN's accuracy over all NFL players". Every artifact says so.

Everything is fetched through `cache.Cache` at tier IMMUTABLE — a completed week can
never change, so the whole backfill costs one fetch per league-season-week, ever.
Re-running is free and offline-safe.

Outputs, per league:

    ctx.raw("cache/weeks/{season}_wk{NN}.json.gz")   the raw payloads (via Cache)
    ctx.raw("player_weeks.json")                     the distilled paired rows
    ctx.raw("player_weeks_index.json")               coverage: what exists, what doesn't

The distilled file is what backtests read, so a backtest never re-parses a megabyte
of boxscore and never needs the network:

    {"league","season_current","built","population","rows":[[season,week,team_id,
      player_id,slot_id,pos_id,proj,actual], ...],"names":{player_id: name},
      "seasons":{season:{"weeks":[...],"rows":N}}}

Run:
    python3 scraping/backfill_weeks.py                    # every league, every season
    python3 scraping/backfill_weeks.py --league 2kdome
    python3 scraping/backfill_weeks.py --season 2025 --refresh
    python3 scraping/backfill_weeks.py --distill-only     # rebuild from cache, no network
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import leagues                                             # noqa: E402
from cache import Cache, CacheMiss, IMMUTABLE, LIVE        # noqa: E402
from espn_client import (auth_cookie, available_seasons,   # noqa: E402
                         league_url, with_params, with_views)

# The NFL regular season plus every playoff scoring period any of these leagues
# plays. Probed, not assumed: a week with no rostered player anywhere did not
# happen, and two of those in a row ends the season.
MAX_WEEK = 18
EMPTY_RUN_ENDS_SEASON = 2

# Stat coordinates. `statSplitTypeId=1` is one scoring period; the source id is
# what separates "what he did" from "what he was expected to do".
SRC_ACTUAL, SRC_PROJECTED, SPLIT_WEEK = 0, 1, 1

POPULATION = ("rostered players only — a boxscore cannot contain the free agent "
              "nobody started, so this grades the projections managers actually "
              "decided on, not ESPN's accuracy across all NFL players")


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def week_url(league_id, season, week):
    return with_params(with_views(league_url(league_id, season), ["mBoxscore"]),
                       scoringPeriodId=week)


def fetch_week(cache: Cache, cookie, league_id, season, week, live_season=None):
    """One league-season-week boxscore. IMMUTABLE unless it is the live season's
    current-or-future week, which can still move."""
    tier = IMMUTABLE
    if live_season and int(season) == int(live_season):
        tier = LIVE
    data, meta = cache.get_json(
        week_url(league_id, season, week),
        headers={"cookie": cookie, "x-fantasy-platform": "kona",
                 "x-fantasy-source": "kona", "accept": "application/json"},
        key=f"weeks/{season}_wk{week:02d}", tier=tier,
        ttl=(None if tier == IMMUTABLE else 6 * 3600))
    return data, meta


def week_rows(payload, season, week):
    """(rows, names) for one COMPLETED week. A row is one rostered player-week:

        [season, week, team_id, player_id, lineup_slot_id, position_id, proj, actual]

    Both numbers are ESPN's own `appliedTotal` — that season's scoring as the
    manager faced it. Recomputing history against the league's CURRENT scoring
    would grade old projections under rules that did not exist yet.

    CALLER MUST HAVE CHECKED THE WEEK IS PLAYED (`completed_weeks`). A future week
    answers with projections and no actuals, and the absent actual is indisting-
    uishable here from "rostered, did not play" — which is a real 0. Grading those
    rows scores every projection against zero and reports a catastrophic, entirely
    fictional bias. The first run of this backfiller did exactly that.
    """
    rows, names = [], {}
    for m in payload.get("schedule") or []:
        for side in ("home", "away"):
            s = m.get(side) or {}
            tid = s.get("teamId")
            for e in ((s.get("rosterForCurrentScoringPeriod") or {}).get("entries") or []):
                pl = ((e.get("playerPoolEntry") or {}).get("player") or {})
                pid = pl.get("id")
                if pid is None or tid is None:
                    continue
                proj = actual = None
                for st in pl.get("stats") or []:
                    if int(st.get("statSplitTypeId", -1)) != SPLIT_WEEK:
                        continue
                    if int(st.get("scoringPeriodId", -1)) != int(week):
                        continue
                    src = int(st.get("statSourceId", -1))
                    if src == SRC_PROJECTED:
                        proj = st.get("appliedTotal")
                    elif src == SRC_ACTUAL:
                        actual = st.get("appliedTotal")
                # A player with no projection cannot grade a projection. A player
                # with no actual entry did not play — that is a real 0, and dropping
                # it would grade only the weeks the projection got right.
                if proj is None:
                    continue
                rows.append([int(season), int(week), int(tid), int(pid),
                             int(e.get("lineupSlotId", -1)),
                             int(pl.get("defaultPositionId") or 0),
                             round(float(proj), 2),
                             round(float(actual or 0.0), 2)])
                names[str(int(pid))] = pl.get("fullName") or ""
    return rows, names


def live_current_week(ctx) -> int:
    """The scoring period in progress, off the league's own scraped status. Weeks
    strictly below it are played; this one and everything after are not."""
    try:
        with open(ctx.raw("league_full.json")) as f:
            payload = json.load(f)
        payload = payload[0] if isinstance(payload, list) and payload else payload
        wk = int(((payload or {}).get("status") or {}).get("latestScoringPeriod") or 0)
        cur = int((payload or {}).get("scoringPeriodId") or 0)
        return max(wk, cur) or 1
    except Exception:
        return 1


def completed_weeks(ctx, season, live_season):
    """Which weeks of this season are safe to grade. A past season is entirely
    played; the live season is played only up to the week before the current one."""
    if int(season) < int(live_season):
        return range(1, MAX_WEEK + 1)
    return range(1, max(1, live_current_week(ctx)))


def backfill_league(ctx, cookie, seasons=None, refresh=False, distill_only=False,
                    quiet=False):
    """Every season-week this league can answer for. Returns the index dict.

    The live season is INCLUDED (its completed weeks are the freshest ground truth
    there is) and is unioned with the historical list rather than replacing it, so
    `--season` narrows what is fetched without amputating the artifact.
    """
    ctx.ensure_dirs()
    cache = Cache(ctx.raw("cache"), offline=distill_only, refresh=refresh)
    try:
        avail = seasons or (list(available_seasons(ctx.league_id, cookie)) + [ctx.season])
    except Exception as e:
        avail = seasons or [ctx.season]
        if not quiet:
            print(f"  ! {ctx.key}: season list unavailable ({e}); trying {avail}")
    avail = sorted({int(s) for s in avail})

    all_rows, names, per_season, t0 = [], {}, {}, time.time()
    for season in avail:
        weeks, empties, srows = [], 0, 0
        playable = completed_weeks(ctx, season, ctx.season)
        if not len(playable):
            if not quiet:
                print(f"  {ctx.key} {season}: no completed weeks yet")
            continue
        for week in playable:
            try:
                payload, meta = fetch_week(cache, cookie, ctx.league_id, season, week,
                                           live_season=ctx.season)
            except CacheMiss:
                empties += 1
                if empties >= EMPTY_RUN_ENDS_SEASON and week > 1:
                    break
                continue
            except Exception as e:
                if not quiet:
                    print(f"  ! {ctx.key} {season} wk{week}: {e}")
                empties += 1
                if empties >= EMPTY_RUN_ENDS_SEASON and week > 1:
                    break
                continue
            rows, nm = week_rows(payload, season, week)
            if not rows:
                empties += 1
                if empties >= EMPTY_RUN_ENDS_SEASON and week > 1:
                    break
                continue
            empties = 0
            weeks.append(week)
            srows += len(rows)
            all_rows.extend(rows)
            names.update(nm)
        if weeks:
            per_season[str(season)] = {"weeks": weeks, "rows": srows}
            if not quiet:
                print(f"  {ctx.key} {season}: weeks {weeks[0]}-{weeks[-1]} "
                      f"({len(weeks)}) · {srows:,} player-weeks")
        elif not quiet:
            print(f"  {ctx.key} {season}: no weekly roster detail available")

    # `--season` narrows what we FETCH, never what the artifact CONTAINS: seasons we
    # did not visit this run are carried over from the file rather than dropped.
    path = ctx.raw("player_weeks.json")
    if seasons and os.path.exists(path):
        try:
            with open(path) as f:
                prev = json.load(f)
            visited = {int(s) for s in avail}
            kept = [r for r in (prev.get("rows") or []) if int(r[0]) not in visited]
            if kept:
                all_rows.extend(kept)
                names = {**(prev.get("names") or {}), **names}
                for s, meta in (prev.get("seasons") or {}).items():
                    if int(s) not in visited:
                        per_season[s] = meta
                if not quiet:
                    print(f"  {ctx.key}: carried over {len(kept):,} rows from "
                          f"{len(per_season) - len(visited & set(map(int, per_season)))} "
                          f"unvisited season(s)")
        except Exception as e:                       # a corrupt artifact must not be silent
            print(f"  ! {ctx.key}: could not merge previous artifact ({e}) — rewriting")
    all_rows.sort(key=lambda r: (r[0], r[1], r[2], r[3]))

    art = {
        "league": ctx.key, "league_id": ctx.league_id, "season_current": ctx.season,
        "built": _now(), "population": POPULATION,
        "source": "ESPN mBoxscore view, seasons/{season} endpoint (NOT leagueHistory)",
        "columns": ["season", "week", "team_id", "player_id", "lineup_slot_id",
                    "position_id", "projected", "actual"],
        "seasons": per_season, "n_rows": len(all_rows),
        "rows": all_rows, "names": names,
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(art, f, separators=(",", ":"))
    index = {k: v for k, v in art.items() if k not in ("rows", "names")}
    index["path"] = os.path.relpath(path, ROOT)
    index["bytes"] = os.path.getsize(path)
    index["elapsed_s"] = round(time.time() - t0, 1)
    index["cache"] = dict(cache.stats)
    with open(ctx.raw("player_weeks_index.json"), "w") as f:
        json.dump(index, f, indent=1)
    if not quiet:
        print(f"  -> {index['path']}  {len(all_rows):,} rows, "
              f"{index['bytes'] / 1e6:.1f} MB · {cache.summary()}")
    return index


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 scraping/backfill_weeks.py",
        description="WS-8a — backfill weekly boxscores (projection vs actual) per league.")
    ap.add_argument("--league", "-l", action="append", metavar="KEY")
    ap.add_argument("--season", "-s", action="append", type=int, metavar="YEAR")
    ap.add_argument("--refresh", action="store_true", help="re-fetch even cached weeks")
    ap.add_argument("--distill-only", action="store_true",
                    help="rebuild the distilled artifact from cache; never touch the network")
    args = ap.parse_args(argv)

    ctxs = ([leagues.resolve(k) for k in args.league] if args.league else leagues.all())
    if not ctxs:
        print("• backfill: no leagues registered (run `python3 leagues.py`).")
        return 0
    cookie = "" if args.distill_only else auth_cookie()
    print(f"WS-8a weekly backfill · {len(ctxs)} league(s)"
          + (" · distill-only (offline)" if args.distill_only else ""))
    print(f"population: {POPULATION}\n")
    total = 0
    for ctx in ctxs:
        print(f"[{ctx.key}]")
        idx = backfill_league(ctx, cookie, seasons=args.season, refresh=args.refresh,
                              distill_only=args.distill_only)
        total += idx["n_rows"]
        print()
    print(f"✓ backfill done · {total:,} paired player-weeks across {len(ctxs)} league(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
