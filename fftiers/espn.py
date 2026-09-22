"""Live ESPN fantasy data for the manage board.

The HTTP mechanics (auth cookie, kona headers, retry/unwrap, the fan API for
league discovery, and the kona_player_info projection view) were ported from
this repo's original in-season scraping layer (espn_client / scrape_league /
scrape_weekly_proj — see the in-season-console branch history), including the
slot/position id tables. The draft-side scraping/scrape_league.py remains a
separate, stdlib-only client; this module serves the manage half only.

What this module produces for this repo:
  * discover_leagues()        -> every FFL league on your ESPN account
  * league_settings()         -> teams / roster slots / point values, ready to
                                 write as a leagues/<key>.yaml
  * player_pool()             -> one week's league-scored projections with
                                 name/pos/ownership -> engine-ready CSV rows
  * my_roster()               -> your roster w/ weekly + ROS projections

Auth: env ESPN_SWID / ESPN_S2, else scraping/.espn_auth.json (shared with the
draft-side scrapers). Format: {"SWID": "{...}", "espn_s2": "..."}. Nothing here
prints or stores the cookie.
"""
from __future__ import annotations

import json
import time
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
FAN_API = "https://fan.api.espn.com/apis/v2/fans/{swid}"
BASE_HEADERS = {
    "accept": "application/json",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "x-fantasy-platform": "kona",
    "x-fantasy-source": "kona",
}
RETRY_CODES = (429, 500, 502, 503, 504)

# Platform constants (fantasy/engine/profile.py). A player's own eligibleSlots
# is authoritative for eligibility; these are display/canonical names.
SLOT_NAMES = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S",
    14: "DB", 15: "DP", 16: "DST", 17: "K", 18: "P", 19: "HC",
    20: "BE", 21: "IR", 23: "FLEX", 24: "ER",
}
POSITION_NAMES = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}

# ESPN scoring statIds -> this repo's scoring keys (the flat, non-override
# subset our engines consume; ESPN's per-position pointsOverrides are rare in
# these leagues and are ignored with a warning).
STAT_KEYS = {
    0: "passing_attempts", 1: "completions", 3: "passing_yards", 4: "passing_td",
    19: "two_point_conversions", 20: "interceptions",
    23: "rushing_attempts", 24: "rushing_yards", 25: "rushing_td",
    42: "receiving_yards", 43: "receiving_td", 53: "receptions",
    72: "fumbles_lost",
}


class EspnError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ── auth ─────────────────────────────────────────────────────────────────────
def load_auth() -> tuple[str, str]:
    swid = os.environ.get("ESPN_SWID")
    s2 = os.environ.get("ESPN_S2")
    if not (swid and s2):
        p = Path(__file__).resolve().parents[1] / "scraping" / ".espn_auth.json"
        if p.exists():
            a = json.loads(p.read_text())
            swid = swid or a.get("SWID")
            s2 = s2 or a.get("espn_s2")
    if not (swid and s2):
        raise EspnError(
            "Missing ESPN auth. Set ESPN_SWID and ESPN_S2, or create scraping/.espn_auth.json "
            'with {"SWID": "{...}", "espn_s2": "..."}')
    if not swid.startswith("{"):
        swid = "{" + swid.strip("{}") + "}"
    return swid, s2


def auth_cookie() -> str:
    swid, s2 = load_auth()
    return f"SWID={swid}; espn_s2={s2}"


# ── fetch ────────────────────────────────────────────────────────────────────
def fetch(url: str, cookie: str, extra_headers: dict | None = None, *,
          timeout: int = 45, retries: int = 4):
    headers = dict(BASE_HEADERS)
    headers["cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data[0] if isinstance(data, list) and data else data
        except urllib.error.HTTPError as e:
            if e.code in RETRY_CODES and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code in (401, 403):
                body += "\n  -> auth rejected: SWID/espn_s2 wrong or expired."
            raise EspnError(f"HTTP {e.code} for {url}\n  {body}", status=e.code)
        # OSError covers URLError plus what escapes it: ConnectionResetError/SSL errors
        # raised mid-body-read (seen from datacenter IPs, e.g. a deployed refresh run).
        except (OSError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise EspnError(f"{type(e).__name__} for {url}: {e}")
    raise EspnError(f"exhausted retries for {url}: {last}")


def league_url(league_id: int, season: int, views: list[str] | None = None, **params) -> str:
    url = f"{BASE}/seasons/{season}/segments/0/leagues/{league_id}"
    q = [f"view={v}" for v in (views or [])]
    q += [f"{k}={v}" for k, v in params.items() if v is not None]
    return url + ("?" + "&".join(q) if q else "")


# ── discovery (fan API) ──────────────────────────────────────────────────────
def discover_leagues(cookie: str | None = None) -> list[dict]:
    """Every FFL league tied to these cookies: [{season, league_id, league_name, team_name}]."""
    swid, _ = load_auth()
    cookie = cookie or auth_cookie()
    url = (FAN_API.format(swid=urllib.parse.quote(swid, safe="")) +
           "?context=fantasy&displayHiddenPrefs=true&featureFlags=fanApiSecurityDisabled"
           "&source=espncom-fantasy&lang=en&region=us")
    data = fetch(url, cookie)
    found, seen = [], set()
    for pref in (data.get("preferences") or []):
        entry = (pref.get("metaData") or {}).get("entry") or {}
        if entry.get("abbrev") != "FFL" and entry.get("gameId") != "ffl":
            continue
        g = (entry.get("groups") or [{}])[0]
        lid = g.get("groupId") or entry.get("groupId")
        key = (entry.get("seasonId"), lid)
        if not lid or key in seen:
            continue
        seen.add(key)
        found.append({"season": int(entry.get("seasonId") or 0), "league_id": int(lid),
                      "league_name": g.get("groupName") or entry.get("name") or f"League {lid}",
                      "team_name": entry.get("name") or ""})
    return sorted(found, key=lambda x: -x["season"])


# ── league settings -> config-shaped dict ────────────────────────────────────
def league_settings(league_id: int, season: int, cookie: str | None = None) -> dict:
    cookie = cookie or auth_cookie()
    data = fetch(league_url(league_id, season, ["mSettings", "mTeam"]), cookie)
    s = data.get("settings") or {}
    roster_s = s.get("rosterSettings") or {}
    slots = {}
    for sid, n in (roster_s.get("lineupSlotCounts") or {}).items():
        if int(n) > 0:
            slots[SLOT_NAMES.get(int(sid), f"SLOT{sid}")] = int(n)
    scoring, overrides = {}, 0
    for item in (s.get("scoringSettings") or {}).get("scoringItems") or []:
        sid = item.get("statId")
        if sid is None:
            continue
        if item.get("pointsOverrides"):
            overrides += 1
        key = STAT_KEYS.get(int(sid))
        if key is not None:
            scoring[key] = float(item.get("points", 0) or 0)
    st = data.get("status") or {}
    return {
        "league_id": league_id,
        "name": s.get("name") or f"League {league_id}",
        "season": season,
        "teams": int((s.get("size") or len(data.get("teams") or [])) or 0),
        "roster": slots,
        "scoring": scoring,
        "scoring_overrides": overrides,   # count of per-position overrides ignored
        "current_week": int(st.get("latestScoringPeriod")
                            or data.get("scoringPeriodId") or 1),
        "final_week": int(st.get("finalScoringPeriod") or 17),
        "my_teams": {int(t["id"]): (t.get("name")
                     or f"{t.get('location', '')} {t.get('nickname', '')}".strip())
                     for t in data.get("teams") or []},
    }


def write_league_yaml(info: dict, dest: Path, week_one_tuesday: str) -> Path:
    """Emit a leagues/<key>.yaml this repo's engines consume."""
    lines = [f"# {info['name']} - generated from ESPN league {info['league_id']} "
             f"(season {info['season']})",
             f"name: {info['name']}", f"teams: {info['teams']}", "roster:"]
    for slot, n in info["roster"].items():
        key = "BENCH" if slot == "BE" else slot
        lines.append(f'  "{key}": {n}')
    lines.append("scoring:")
    for k, v in sorted(info["scoring"].items()):
        lines.append(f"  {k}: {v}")
    lines += ["season:", f"  year: {info['season']}",
              f"  week_one_tuesday: {week_one_tuesday}",
              f"vbd: {{final_week: {info['final_week']}}}"]
    dest.write_text("\n".join(lines) + "\n")
    return dest


# ── player pool: projections + ownership + names, one call per week ─────────
PLAYER_FILTER = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                             "limit": 1500,
                             "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}}


def player_pool(league_id: int, season: int, week: int,
                cookie: str | None = None) -> list[dict]:
    """League-scored week-N projections for every rosterable player.

    statSourceId=1 (projection) + statSplitTypeId=1 (single week) at
    scoringPeriodId=week is the real weekly forecast; appliedTotal is already
    in this league's scoring. Byes come out as 0.0. (Port of scrape_weekly_proj;
    the filterStatsForTopScoringPeriodIds filter 400s — don't add it.)
    """
    cookie = cookie or auth_cookie()
    url = league_url(league_id, season, ["kona_player_info"], scoringPeriodId=week)
    data = fetch(url, cookie, {"x-fantasy-filter": json.dumps(PLAYER_FILTER)})
    out = []
    for entry in data.get("players") or []:
        pl = entry.get("player") or {}
        pos = POSITION_NAMES.get(int(pl.get("defaultPositionId") or 0))
        if pos is None:
            continue
        pts = 0.0
        for st in pl.get("stats") or []:
            if (st.get("statSourceId") == 1 and st.get("statSplitTypeId") == 1
                    and st.get("scoringPeriodId") == week
                    and st.get("seasonId") == season):
                pts = round(float(st.get("appliedTotal") or 0.0), 2)
                break
        out.append({"id": pl.get("id"), "player": pl.get("fullName") or "",
                    "pos": pos, "points": pts,
                    "team_id": entry.get("onTeamId") or 0,
                    "rostered": "yes" if entry.get("onTeamId") else "no",
                    "injury": pl.get("injuryStatus") or ""})
    return out


def ros_pool(league_id: int, season: int, from_week: int, final_week: int,
             cookie: str | None = None, pause: float = 0.4) -> list[dict]:
    """Rest-of-season = sum of each remaining week's weekly projections."""
    cookie = cookie or auth_cookie()
    totals: dict[int, dict] = {}
    for wk in range(from_week, final_week + 1):
        for p in player_pool(league_id, season, wk, cookie):
            agg = totals.setdefault(p["id"], dict(p, points=0.0))
            agg["points"] = round(agg["points"] + p["points"], 2)
            agg["rostered"] = p["rostered"]      # ownership from the latest pull
        time.sleep(pause)                        # be a polite client
    return list(totals.values())


def team_roster(league_id: int, season: int, team_id: int,
                cookie: str | None = None) -> list[dict]:
    """One team's roster entries, projections not attached: [{id,name,pos,injury}]."""
    cookie = cookie or auth_cookie()
    data = fetch(league_url(league_id, season, ["mRoster"]), cookie)
    out = []
    for team in data.get("teams") or []:
        if int(team.get("id") or 0) != int(team_id):
            continue
        for e in (team.get("roster") or {}).get("entries") or []:
            pl = (e.get("playerPoolEntry") or {}).get("player") or {}
            out.append({"id": pl.get("id"), "name": pl.get("fullName") or "",
                        "pos": POSITION_NAMES.get(int(pl.get("defaultPositionId") or 0), "?"),
                        "injury": pl.get("injuryStatus") or ""})
    return out


def my_roster(league_id: int, season: int, team_id: int, week: int,
              final_week: int = 17, cookie: str | None = None) -> list[dict]:
    """My roster with this-week and rest-of-season league-scored projections."""
    cookie = cookie or auth_cookie()
    wk = {p["id"]: p for p in player_pool(league_id, season, week, cookie)}
    ros = {p["id"]: p["points"]
           for p in ros_pool(league_id, season, week, final_week, cookie)}
    out = [{"name": r["name"], "pos": r["pos"],
            "wk": wk.get(r["id"], {}).get("points", 0.0),
            "ros": ros.get(r["id"], 0.0), "injury": r["injury"]}
           for r in team_roster(league_id, season, team_id, cookie)]
    out.sort(key=lambda r: -r["ros"])
    return out
