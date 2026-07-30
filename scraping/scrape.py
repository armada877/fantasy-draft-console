#!/usr/bin/env python3
"""Scrape all seasons of ESPN fantasy league 2KDOME (id 44252).

Auth: needs the two ESPN session cookies, SWID and espn_s2. Provide them via
either:
  - env vars ESPN_SWID and ESPN_S2, or
  - a JSON file scraping/.espn_auth.json  ->  {"SWID": "{...}", "espn_s2": "..."}

Endpoints:
  - seasons >= 2018 : /apis/v3/games/ffl/seasons/{year}/segments/0/leagues/{id}
  - seasons <= 2017 : /apis/v3/games/ffl/leagueHistory/{id}?seasonId={year}
    (older seasons are served from the historical endpoint as a single-element list)

Everything is saved as raw JSON under scraping/raw/{season}/ so downstream
analysis never has to re-hit the network.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

LEAGUE_ID = 44252
HERE = os.path.dirname(__file__)
RAW = os.path.join(HERE, "raw")

# League has run since 2013; current upcoming season is 2026.
SEASONS = list(range(2013, 2026))  # 2013..2025 completed

# Views we want per league request. ESPN accepts repeated view params.
LEAGUE_VIEWS = [
    "mDraftDetail",     # draft board: auction bids, keepers ($100 hack lives here)
    "mSettings",        # league rules / scoring / roster config
    "mTeam",            # teams, owners, records, budget
    "mRoster",          # current rosters
    "mMatchup",         # matchup pairings + team scores
    "mMatchupScore",    # detailed matchup scoring
    "mStandings",       # standings
    "mTransactions2",   # adds/drops/trades/waivers/offers
    "mStatus",          # season status
    "mSchedule",        # full schedule
    "mBoxscore",        # per-week boxscores
    "modular",
    "mNav",
]

BASE_HEADERS = {
    "accept": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "x-fantasy-platform": "kona",
    "x-fantasy-source": "kona",
}


def load_auth():
    swid = os.environ.get("ESPN_SWID")
    s2 = os.environ.get("ESPN_S2")
    if not (swid and s2):
        p = os.path.join(HERE, ".espn_auth.json")
        if os.path.exists(p):
            with open(p) as f:
                a = json.load(f)
            swid = swid or a.get("SWID")
            s2 = s2 or a.get("espn_s2")
    if not (swid and s2):
        sys.exit(
            "Missing auth. Set ESPN_SWID and ESPN_S2 env vars, or create "
            "scraping/.espn_auth.json with {\"SWID\":..., \"espn_s2\":...}"
        )
    if not swid.startswith("{"):
        swid = "{" + swid.strip("{}") + "}"
    return swid, s2


def fetch(url, cookie, extra_headers=None):
    headers = dict(BASE_HEADERS)
    headers["cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            body = e.read().decode("utf-8", "ignore")[:200]
            raise RuntimeError(f"HTTP {e.code} for {url}\n  {body}")
        except (urllib.error.URLError, TimeoutError):
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise


def league_url(season):
    if season >= 2018:
        return (
            f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
            f"seasons/{season}/segments/0/leagues/{LEAGUE_ID}"
        )
    return (
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
        f"leagueHistory/{LEAGUE_ID}?seasonId={season}"
    )


def with_views(url, views):
    sep = "&" if "?" in url else "?"
    return url + sep + "&".join(f"view={v}" for v in views)


def players_url(season):
    return (
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
        f"seasons/{season}/segments/0/leagues/{LEAGUE_ID}?view=kona_player_info"
    )


# x-fantasy-filter to pull the whole player universe with season stats
PLAYER_FILTER = json.dumps({
    "players": {
        "limit": 2000,
        "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
    }
})

# Transactions are only returned per scoringPeriodId, so we iterate weeks and
# dedupe by transaction id. These are the filterType values ESPN accepts.
# NOTE: executed trades are type TRADE_ACCEPT (TRADE_PROPOSAL is just offers).
TXN_TYPES = ["WAIVER", "WAIVER_ERROR", "FREEAGENT", "TRADE_PROPOSAL",
             "TRADE_ACCEPT", "ROSTER", "DRAFT", "FUTURE_ROSTER"]
TXN_FILTER = json.dumps({"transactions": {"filterType": {"value": TXN_TYPES}}})


def fetch_transactions(season, cookie):
    """Pull all transactions for a season by iterating scoring periods."""
    seen = {}
    base = league_url(season)
    sep = "&" if "?" in base else "?"
    for sp in range(1, 19):
        url = f"{base}{sep}scoringPeriodId={sp}&view=mTransactions2"
        try:
            data = fetch(url, cookie, {"x-fantasy-filter": TXN_FILTER})
        except RuntimeError:
            continue
        # leagueHistory endpoint returns a list wrapper
        if isinstance(data, list):
            data = data[0] if data else {}
        for t in (data.get("transactions") or []):
            tid = t.get("id")
            if tid is not None:
                seen[tid] = t
    return list(seen.values())


def save(season, name, data):
    d = os.path.join(RAW, str(season))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, name + ".json")
    with open(path, "w") as f:
        json.dump(data, f)
    n = len(data) if isinstance(data, list) else 1
    print(f"  saved {season}/{name}.json  ({os.path.getsize(path):,} bytes)")


def main():
    swid, s2 = load_auth()
    cookie = f"SWID={swid}; espn_s2={s2}"
    only = None
    if len(sys.argv) > 1:
        only = set(int(x) for x in sys.argv[1:])

    for season in SEASONS:
        if only and season not in only:
            continue
        print(f"== {season} ==")
        try:
            data = fetch(with_views(league_url(season), LEAGUE_VIEWS), cookie)
            save(season, "league_full", data)
        except RuntimeError as e:
            print(f"  league fetch failed: {e}")
            # retry with a smaller view set (older seasons reject some views)
            try:
                data = fetch(with_views(league_url(season),
                             ["mDraftDetail", "mSettings", "mTeam",
                              "mMatchupScore", "mStandings", "mTransactions2"]),
                             cookie)
                save(season, "league_full", data)
            except RuntimeError as e2:
                print(f"  reduced league fetch also failed: {e2}")
                continue
        try:
            players = fetch(players_url(season), cookie,
                            {"x-fantasy-filter": PLAYER_FILTER})
            save(season, "players", players)
        except RuntimeError as e:
            print(f"  players fetch failed: {e}")
        try:
            txns = fetch_transactions(season, cookie)
            save(season, "transactions", txns)
            print(f"    ({len(txns)} unique transactions)")
        except RuntimeError as e:
            print(f"  transactions fetch failed: {e}")
        time.sleep(1.0)

    print("\nDone.")


if __name__ == "__main__":
    main()
