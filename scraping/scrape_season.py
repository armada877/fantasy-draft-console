#!/usr/bin/env python3
"""In-season ESPN pull, for every league on the account — WS-1's data acquisition.

    python3 scraping/scrape_season.py                 # all leagues in the registry
    python3 scraping/scrape_season.py --league 2kdome # just one
    python3 scraping/scrape_season.py --skip-transactions

Each league is a separate context: every path comes from `LeagueContext`, never
string-built, so nothing one league produces can land in another's directory.

Per league it writes three artifacts (the WS-1 half of docs/contracts.md), plus a
small fourth that the contract does not name but the valuation needs:

  ctx.raw("league_full.json")   views mSettings,mTeam,mRoster,mMatchup,mStatus,
                                mDraftDetail. OVERWRITES whatever is there: the
                                checked-in 2026 copy is a pre-draft snapshot
                                (drafted:false, 0 picks) and the real league has
                                a completed draft.
  ctx.raw("players_wk{N}.json") kona_player_info for FREEAGENT + WAIVERS + ONTEAM
                                with ownership, injuryStatus and stats. Two passes
                                are merged: the default one (season actual, live
                                full-season projection, current-week projection)
                                and a recent-scoring-periods one (per-week actuals,
                                which the default drops). Stat entries dedupe on id.
  ctx.raw("transactions.json")  every transaction in every season ESPN will serve
                                one for. It only answers per scoringPeriodId, so we
                                iterate the periods each season actually had and
                                dedupe on transaction id. Seasons with no log at all
                                are listed in `unavailable_seasons`.
  ctx.raw("pro_teams.json")     proTeamId -> {abbrev, bye}. Bye weeks are NOT on the
                                player object; without this the ROS per-game rate
                                would silently ignore byes.

Nothing here knows a league fact. Team counts, scoring, roster shape, season
length and the set of historical seasons are all read back off the platform.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (HERE, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import leagues  # noqa: E402
from cache import Cache, IMMUTABLE, LIVE  # noqa: E402
from espn_client import (  # noqa: E402
    EspnError, auth_cookie, fantasy_filter, fetch,
    history_index_url, league_url, pro_teams, unwrap, with_params, with_views,
)

# The views the in-season tool consumes. mRoster gives every team's players with
# lineupSlotId + acquisitionType; mTeam carries FAAB spent and waiverRank;
# mDraftDetail is the sunk-cost/priors context; mMatchup is the remaining schedule.
SEASON_VIEWS = ["mSettings", "mTeam", "mRoster", "mMatchup", "mStatus", "mDraftDetail"]

# A generous ceiling on the player universe — ESPN returns what exists, this only
# has to be larger than any league's pool. It is not a league property.
PLAYER_LIMIT = 2000

# Every status we care about: the add pool AND the rostered players, because the
# trade tools need both sides valued with the same numbers.
PLAYER_STATUSES = ["FREEAGENT", "WAIVERS", "ONTEAM"]

# How many recent scoring periods of per-week actuals to ask for in the second
# pass (feeds "last N weeks" form signals downstream).
RECENT_PERIODS = 5

# Scoring periods to sweep for the LIVE season, which /leagueHistory cannot tell us
# the length of yet. Covers the NFL regular season plus the playoff periods.
LIVE_SEASON_PERIODS = 18

# Executed trades are TRADE_ACCEPT; TRADE_PROPOSAL is only an offer.
TXN_TYPES = ["WAIVER", "WAIVER_ERROR", "FREEAGENT", "TRADE_PROPOSAL",
             "TRADE_ACCEPT", "ROSTER", "DRAFT", "FUTURE_ROSTER"]


def _now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _save(path, data) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)
    return os.path.getsize(path)


def _human(n) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0


# ── 1. the league payload ────────────────────────────────────────────────────
def fetch_league(ctx, cookie):
    url = with_views(league_url(ctx.league_id, ctx.season), SEASON_VIEWS)
    return fetch(url, cookie, unwrap=True)


def current_scoring_period(payload) -> int:
    st = (payload or {}).get("status") or {}
    for key in ("latestScoringPeriod", "currentMatchupPeriod"):
        if st.get(key):
            return int(st[key])
    return int((payload or {}).get("scoringPeriodId") or 1)


# ── 2. the player pool ───────────────────────────────────────────────────────
def _player_filter(extra=None):
    spec = {
        "players": {
            "filterStatus": {"value": list(PLAYER_STATUSES)},
            "limit": PLAYER_LIMIT,
            "offset": 0,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
        }
    }
    if extra:
        spec["players"].update(extra)
    return fantasy_filter(spec)


def _merge_stats(base_entries, extra_entries):
    """Fold `extra`'s stat rows into `base`, deduped on the ESPN stat entry id.

    The two passes return overlapping but not identical stat sets — the recent-
    periods pass adds per-week actuals but drops the current-week projection —
    so the union is strictly more useful than either alone.
    """
    extra_by_id = {}
    for e in extra_entries:
        p = e.get("player") or {}
        if p.get("id") is not None:
            extra_by_id[p["id"]] = p.get("stats") or []
    added = 0
    for e in base_entries:
        p = e.get("player") or {}
        more = extra_by_id.get(p.get("id"))
        if not more:
            continue
        seen = {s.get("id") for s in (p.get("stats") or [])}
        for s in more:
            if s.get("id") not in seen:
                p.setdefault("stats", []).append(s)
                seen.add(s.get("id"))
                added += 1
    return added


def fetch_players(ctx, cookie):
    """The player universe with ownership, injury status and stats."""
    url = with_views(league_url(ctx.league_id, ctx.season), ["kona_player_info"])
    data = fetch(url, cookie, _player_filter(), unwrap=True)
    entries = data.get("players") or []

    # Second pass: per-week actuals. Advisory enrichment — a failure here must
    # degrade the artifact, never fail the scrape.
    added, note = 0, ""
    try:
        recent = fetch(url, cookie, _player_filter(
            {"filterStatsForTopScoringPeriodIds": {"value": RECENT_PERIODS}}), unwrap=True)
        added = _merge_stats(entries, recent.get("players") or [])
    except EspnError as e:
        note = f" (recent-week pass unavailable: {str(e).splitlines()[0]})"
    return data, len(entries), added, note


# ── 3. transactions, every season ────────────────────────────────────────────
def season_index(league_id, cookie):
    """{season: last_scoring_period} for every season the league has existed.

    Read off /leagueHistory rather than probed year by year: ESPN returns one
    element per season, so the answer is exact and the season count is a fact
    about THIS league (they range from 1 to 13 across the account).
    """
    data = fetch(with_views(history_index_url(league_id), ["mStatus"]), cookie)
    if not isinstance(data, list):
        data = [data]
    out = {}
    for d in data:
        if not d or not d.get("seasonId"):
            continue
        st = d.get("status") or {}
        last = max(int(st.get("finalScoringPeriod") or 0),
                   int(st.get("latestScoringPeriod") or 0)) or 18
        out[int(d["seasonId"])] = last
    return out


def _transactions_page(league_id, season, sp, cookie, cache=None, live_season=None):
    """One (season, scoringPeriod) page of transactions, or None if unavailable.

    VERIFIED: /leagueHistory silently DROPS the `transactions` key — it answers 200
    with no error and no data, which would look like "this season had no activity".
    The /seasons/ endpoint is the only one that serves mTransactions2, and it 404s
    for seasons older than the v3 transaction log. So we ask the seasons endpoint
    and distinguish the three outcomes: rows / empty / not served (None).
    """
    url = with_params(
        with_views(league_url(league_id, season, historical=False), ["mTransactions2"]),
        scoringPeriodId=sp)
    filt = fantasy_filter({"transactions": {"filterType": {"value": TXN_TYPES}}})
    # A finished season's transaction log is FROZEN — week 5 of 2019 will never gain
    # a row. Re-pulling eight completed seasons every run was ~144 pointless requests
    # per league and the bulk of this stage's runtime.
    if cache is not None:
        tier = LIVE if (live_season is not None and int(season) >= int(live_season)) else IMMUTABLE
        try:
            data, _meta = cache.get_json(
                url, headers={**filt, "cookie": cookie, "accept": "application/json",
                              "x-fantasy-platform": "kona", "x-fantasy-source": "kona"},
                key=f"txn/{season}_sp{int(sp):02d}", tier=tier,
                ttl=900 if tier == LIVE else None, unwrap_list=True)
        except Exception:
            return None
        return data.get("transactions")
    try:
        data = unwrap(fetch(url, cookie, filt, retries=3))
    except EspnError:
        return None
    return data.get("transactions")          # None = the view was not served


def fetch_season_transactions(league_id, season, last_period, cookie, cache=None, live_season=None):
    """All transactions in one season, deduped on id.

    Returns (rows, served). `served=False` means ESPN has no transaction log for
    that season at all — a fact about how far back the API goes, discovered rather
    than hardcoded to a cutoff year.
    """
    seen, served = {}, False
    for sp in range(1, int(last_period) + 1):
        rows = _transactions_page(league_id, season, sp, cookie, cache, live_season)
        if rows is None:
            continue
        served = True
        for t in rows:
            tid = t.get("id")
            if tid is not None and tid not in seen:
                t.setdefault("seasonId", season)
                seen[tid] = t
    return list(seen.values()), served


def fetch_transactions(ctx, cookie):
    """Every transaction, every season, one flat list tagged with `seasonId`.

    The set of seasons is this league's own (they run 1 to 13 deep across the
    account); seasons ESPN will not serve are recorded in `unavailable` so a thin
    league degrades visibly instead of looking quiet.
    """
    index = season_index(ctx.league_id, cookie)
    index.setdefault(ctx.season, LIVE_SEASON_PERIODS)
    cache = Cache(os.path.join(ctx.raw_dir, "cache"))
    rows, counts, missing = [], {}, []
    for season in sorted(index):
        got, served = fetch_season_transactions(
            ctx.league_id, season, index[season], cookie,
            cache=cache, live_season=ctx.season)
        if not served:
            missing.append(season)
            continue
        counts[str(season)] = len(got)
        rows.extend(got)
        if int(season) >= int(ctx.season):
            time.sleep(0.2)          # only the live season re-hits the network
    return {"league_id": ctx.league_id, "fetched": _now(),
            "seasons": sorted(int(s) for s in counts), "counts": counts,
            "unavailable_seasons": missing, "transactions": rows}, counts, missing


# ── per-league driver ────────────────────────────────────────────────────────
def scrape_league(ctx, cookie, *, skip_transactions=False, week=None):
    from engine.profile import LeagueProfile

    ctx.ensure_dirs()
    print(f"\n\033[1m== {ctx.key} — {ctx.name} ({ctx.league_id}, {ctx.season})\033[0m")
    sizes = {}

    payload = fetch_league(ctx, cookie)
    wk = int(week) if week else current_scoring_period(payload)
    sizes["league_full.json"] = _save(ctx.raw("league_full.json"), payload)
    prof = LeagueProfile.from_espn(payload)
    draft = payload.get("draftDetail") or {}
    print("   " + prof.summary().replace("\n", "\n   "))
    print(f"   week {wk} | draft {'complete' if draft.get('drafted') else 'PENDING'}, "
          f"{len(draft.get('picks') or [])} picks | "
          f"{len(payload.get('teams') or [])} teams, "
          f"{sum(len((t.get('roster') or {}).get('entries') or []) for t in payload.get('teams') or [])} "
          f"rostered players")

    teams = pro_teams(ctx.season, cookie)
    sizes["pro_teams.json"] = _save(ctx.raw("pro_teams.json"), teams)

    players, n_players, merged, note = fetch_players(ctx, cookie)
    name = f"players_wk{wk}.json"
    sizes[name] = _save(ctx.raw(name), players)
    owned = sum(1 for e in players.get("players") or [] if e.get("onTeamId"))
    print(f"   players: {n_players} ({owned} rostered, {n_players - owned} available), "
          f"+{merged} merged weekly stat rows{note}")

    if skip_transactions:
        print("   transactions: skipped (--skip-transactions)")
    else:
        txns, counts, missing = fetch_transactions(ctx, cookie)
        sizes["transactions.json"] = _save(ctx.raw("transactions.json"), txns)
        live = ", ".join(f"{s}:{n}" for s, n in sorted(counts.items()) if n)
        print(f"   transactions: {len(txns['transactions'])} across "
              f"{len([n for n in counts.values() if n])} season(s)  [{live}]")
        if missing:
            print(f"                 no transaction log served for "
                  f"{', '.join(str(s) for s in missing)} (predates ESPN's v3 log)")

    print(f"   -> {os.path.relpath(ctx.raw_dir, ROOT)}/")
    for fn, n in sizes.items():
        print(f"        {_human(n):>10}  {fn}")
    return sizes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--league", default=None,
                    help="league key (default: every league in the registry)")
    ap.add_argument("--week", type=int, default=None,
                    help="override the scoring period (default: read from ESPN)")
    ap.add_argument("--skip-transactions", action="store_true",
                    help="skip the multi-season transaction pull (much faster)")
    args = ap.parse_args(argv)

    ctxs = [leagues.resolve(args.league)] if args.league else leagues.all()
    if not ctxs:
        print("no leagues discovered — check your ESPN cookies "
              "(scraping/.espn_auth.json or ESPN_SWID/ESPN_S2).")
        return 1
    cookie = auth_cookie()

    failed = []
    for ctx in ctxs:
        try:
            scrape_league(ctx, cookie, skip_transactions=args.skip_transactions,
                          week=args.week)
        except EspnError as e:
            failed.append(ctx.key)
            print(f"   \033[31mFAILED\033[0m {ctx.key}: {str(e).splitlines()[0]}")
    print(f"\n{len(ctxs) - len(failed)}/{len(ctxs)} league(s) scraped."
          + (f"  failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
