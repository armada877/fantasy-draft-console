#!/usr/bin/env python3
"""Bulk-pull kona_playercard for every league-involved player, every season.

The playercard is the ONLY complete source of TRADE details: the mTransactions2
feed's TRADE_ACCEPT rows have empty `items` (they say a trade happened but not
which players moved). Each playercard carries a full `transactions` array with
the complete provenance chain (draft -> trades -> adds -> drops) including both
sides of every trade.

Candidate players per season = union of everyone in players.json (the universe) +
every playerId appearing in draft picks or transactions. Batched ~120 ids/request
via the x-fantasy-filter header. Saved to raw/{season}/playercards.json.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(os.path.abspath(HERE))
RAW = os.path.join(HERE, "raw")


def _league_config():
    p = os.path.join(ROOT, "config", "league.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}


_CFG = _league_config()
LEAGUE_ID = _CFG.get("league_id") or 0          # your ESPN league id (config/league.json)
# transaction era: playercards/trades exist from ~2018 on
SEASONS = list(range(max(2018, int(_CFG.get("first_season", 2013))), int(_CFG.get("season", 2026))))
BATCH = 120

BASE_HEADERS = {
    "accept": "application/json",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
    "x-fantasy-platform": "kona",
}


def load_auth():
    a = json.load(open(os.path.join(HERE, ".espn_auth.json")))
    swid = a["SWID"]
    if not swid.startswith("{"):
        swid = "{" + swid.strip("{}") + "}"
    return f"SWID={swid}; espn_s2={a['espn_s2']}"


def fetch(url, cookie, filt):
    headers = dict(BASE_HEADERS)
    headers["cookie"] = cookie
    headers["x-fantasy-filter"] = json.dumps(filt)
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError):
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise


def candidate_pids(season):
    pids = set()
    # players.json universe
    pj = os.path.join(RAW, str(season), "players.json")
    if os.path.exists(pj):
        d = json.load(open(pj))
        for e in (d.get("players", []) if isinstance(d, dict) else d):
            pids.add(e["player"]["id"])
    # league_full: draft picks + rosters
    lf = json.load(open(os.path.join(RAW, str(season), "league_full.json")))
    if isinstance(lf, list):
        lf = lf[0]
    for p in lf.get("draftDetail", {}).get("picks", []) or []:
        if p.get("playerId"):
            pids.add(p["playerId"])
    # transactions items
    tp = os.path.join(RAW, str(season), "transactions.json")
    if os.path.exists(tp):
        for t in json.load(open(tp)):
            for i in t.get("items", []):
                if i.get("playerId"):
                    pids.add(i["playerId"])
    return sorted(pids)


def playercard_url(season):
    return (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
            f"seasons/{season}/segments/0/leagues/{LEAGUE_ID}?view=kona_playercard")


def main():
    if not LEAGUE_ID:
        sys.exit("Set your ESPN league_id in config/league.json (copy config/league.example.json).")
    cookie = load_auth()
    only = set(int(x) for x in sys.argv[1:]) if len(sys.argv) > 1 else None
    for season in SEASONS:
        if only and season not in only:
            continue
        pids = candidate_pids(season)
        url = playercard_url(season)
        cards, trade_rows = [], 0
        for i in range(0, len(pids), BATCH):
            batch = pids[i:i + BATCH]
            filt = {"players": {"filterIds": {"value": batch}}}
            try:
                d = fetch(url, cookie, filt)
            except RuntimeError as e:
                print(f"  {season} batch {i}: {e}")
                continue
            for p in d.get("players", []):
                cards.append(p)
                trade_rows += sum(1 for t in p.get("transactions", [])
                                  if t.get("type") == "TRADE_ACCEPT" and t.get("items"))
            time.sleep(0.4)
        out = os.path.join(RAW, str(season), "playercards.json")
        json.dump(cards, open(out, "w"))
        print(f"{season}: {len(pids)} candidates -> {len(cards)} cards saved "
              f"({trade_rows} trade rows w/ items, {os.path.getsize(out):,} bytes)")


if __name__ == "__main__":
    main()
