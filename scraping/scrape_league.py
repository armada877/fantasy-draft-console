#!/usr/bin/env python3
"""Scrape ONE season of an ESPN fantasy league — just what a draft console needs.

Unlike scrape.py (which pulls 13 years of history for deep analysis), this fetches
the current season's roster/scoring settings and the team+owner list, so
build_tool_data.py can wire your real league config and managers into the console.

Zero-config discovery: if config/league.json has no league_id, this uses your cookies
to look up every fantasy-football league you belong to (ESPN "fan" API) and auto-picks
the one for your season (or lists them if there's more than one). It also auto-detects
which team is YOU by matching your SWID to the league's members, and writes both back
into config/league.json.

Auth: needs your two ESPN session cookies, SWID and espn_s2. Provide them via either
  - env vars ESPN_SWID and ESPN_S2, or
  - a JSON file scraping/.espn_auth.json  ->  {"SWID": "{...}", "espn_s2": "..."}
Grab both from a logged-in fantasy.espn.com session (DevTools -> Application -> Cookies).

Output: scraping/raw/{season}/league_full.json  (mSettings + mTeam views)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(HERE, "raw")
CONFIG_PATH = os.path.join(ROOT, "config", "league.json")

# Only the views a fresh draft setup needs: league rules + teams/owners.
LEAGUE_VIEWS = ["mSettings", "mTeam"]
FAN_API = "https://fan.api.espn.com/apis/v2/fans/{swid}"

BASE_HEADERS = {
    "accept": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "x-fantasy-platform": "kona",
    "x-fantasy-source": "kona",
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            "Missing config/league.json. Copy config/league.example.json to "
            "config/league.json (league_id may be left as 0 to auto-discover)."
        )
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    """Persist discovered values back into config/league.json, keeping other keys."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


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
            'scraping/.espn_auth.json with {"SWID":..., "espn_s2":...}'
        )
    if not swid.startswith("{"):
        swid = "{" + swid.strip("{}") + "}"
    return swid, s2


def fetch(url, cookie):
    headers = dict(BASE_HEADERS)
    headers["cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code in (401, 403):
                body += "\n  -> auth rejected: your SWID/espn_s2 cookies are wrong or expired."
            raise RuntimeError(f"HTTP {e.code} for {url}\n  {body}")
        except (urllib.error.URLError, TimeoutError):
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise


# ───────────────────────────── league discovery (fan API) ─────────────────────────────
def discover_leagues(swid, s2):
    """Return every fantasy-football league tied to these cookies:
    [{season, league_id, league_name, team_name}] (tolerant of ESPN's shifting shape)."""
    url = (FAN_API.format(swid=urllib.parse.quote(swid, safe="")) +
           "?context=fantasy&displayHiddenPrefs=true&featureFlags=fanApiSecurityDisabled"
           "&source=espncom-fantasy&lang=en&region=us")
    cookie = f"SWID={swid}; espn_s2={s2}"
    data = fetch(url, cookie)

    found, seen = [], set()
    for pref in (data.get("preferences") or []):
        entry = (pref.get("metaData") or {}).get("entry") or {}
        # football only; ESPN marks it via abbrev "FFL" or gameId "ffl"
        if entry.get("abbrev") != "FFL" and entry.get("gameId") != "ffl":
            continue
        groups = entry.get("groups") or [{}]
        g = groups[0]
        lid = g.get("groupId") or entry.get("groupId")
        if not lid:
            continue
        key = (entry.get("seasonId"), lid)
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "season": int(entry.get("seasonId") or 0),
            "league_id": int(lid),
            "league_name": g.get("groupName") or entry.get("name") or f"League {lid}",
            "team_name": entry.get("name") or "",
        })
    return sorted(found, key=lambda x: -x["season"])


def resolve_league(cfg, swid, s2):
    """Return (league_id, season), discovering via the fan API when league_id is unset."""
    league_id = int(cfg.get("league_id") or 0)
    season = int(cfg.get("season") or 0)
    if league_id > 0:
        return league_id, (season or 2026)

    print("No league_id in config — discovering your leagues from ESPN...")
    leagues = discover_leagues(swid, s2)
    if not leagues:
        sys.exit("  No fantasy-football leagues found for these cookies. "
                 "Check the cookies, or set league_id manually in config/league.json.")
    print(f"  Found {len(leagues)} football league(s):")
    for lg in leagues:
        print(f"    - {lg['season']}  id={lg['league_id']}  "
              f"{lg['league_name']!r}  (your team: {lg['team_name']!r})")

    pick = [lg for lg in leagues if not season or lg["season"] == season] or leagues
    if len(pick) > 1:
        sys.exit("  More than one league — set league_id (and season) in config/league.json "
                 "to the one you want, then re-run.")
    chosen = pick[0]
    cfg["league_id"] = chosen["league_id"]
    cfg["season"] = chosen["season"]
    save_config(cfg)
    print(f"  Using league {chosen['league_id']} ({chosen['league_name']}), "
          f"season {chosen['season']} — written to config/league.json.")
    return chosen["league_id"], chosen["season"]


def league_url(league_id, season):
    if season >= 2018:
        return (
            f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
            f"seasons/{season}/segments/0/leagues/{league_id}"
        )
    return (
        f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/"
        f"leagueHistory/{league_id}?seasonId={season}"
    )


def with_views(url, views):
    sep = "&" if "?" in url else "?"
    return url + sep + "&".join(f"view={v}" for v in views)


def owner_name(member):
    """Manager display name — MUST match build_tool_data.py's mapping."""
    if member and (member.get("firstName") or member.get("lastName")):
        return " ".join(f"{member.get('firstName','')} {member.get('lastName','')}".split())
    if member and member.get("displayName"):
        return member["displayName"]
    return ""


def detect_me(data, swid, cfg):
    """If config 'me' is unset/placeholder, match the SWID to a league member and persist."""
    me = (cfg.get("me") or "").strip()
    if me and me.lower() != "your name" and me != "Me":
        return me
    member = next((m for m in (data.get("members") or []) if m.get("id") == swid), None)
    name = owner_name(member)
    if name:
        cfg["me"] = name
        save_config(cfg)
        print(f"  Detected your team via SWID: me = {name!r} — written to config/league.json.")
        return name
    print("  Could not auto-detect your team from the SWID; set 'me' in config/league.json.")
    return me


def main():
    cfg = load_config()
    swid, s2 = load_auth()
    league_id, season = resolve_league(cfg, swid, s2)
    cookie = f"SWID={swid}; espn_s2={s2}"

    print(f"Scraping league {league_id}, season {season} (settings + teams)...")
    data = fetch(with_views(league_url(league_id, season), LEAGUE_VIEWS), cookie)
    if isinstance(data, list):  # historical endpoint wraps in a list
        data = data[0] if data else {}

    d = os.path.join(RAW, str(season))
    os.makedirs(d, exist_ok=True)
    out = os.path.join(d, "league_full.json")
    with open(out, "w") as f:
        json.dump(data, f)
    teams = data.get("teams") or []
    members = data.get("members") or []
    print(f"  saved {out} ({os.path.getsize(out):,} bytes)")
    print(f"  found {len(teams)} teams, {len(members)} members")
    detect_me(data, swid, cfg)
    print("\nNext: python3 draft_sheets/build_tool_data.py")


if __name__ == "__main__":
    main()
