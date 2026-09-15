#!/usr/bin/env python3
"""Shared ESPN fantasy API client — the one place the HTTP details live.

`scrape.py` and `scrape_league.py` each grew their own copy of auth + fetch +
url-building. Those are on the shipped draft console's code path and are left
alone; everything in-season imports this module instead.

Two endpoint families, and the difference matters:

    current season   /seasons/{season}/segments/0/leagues/{id}      -> dict
    past seasons     /leagueHistory/{id}?seasonId={season}          -> [dict]

The historical endpoint wraps its payload in a SINGLE-ELEMENT LIST. `fetch()`
returns whatever ESPN sent; call `unwrap()` (or pass `unwrap=True`) so callers
never have to care which endpoint produced the object.

    from espn_client import auth_cookie, fetch, league_url, with_views, fantasy_filter

    cookie = auth_cookie()
    data = fetch(with_views(league_url(1234567, 2026), ["mSettings", "mTeam"]), cookie,
                 unwrap=True)

Auth comes from scrape_league.load_auth() — env ESPN_SWID/ESPN_S2, else
scraping/.espn_auth.json. Nothing here prints or persists a cookie.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from scrape_league import load_auth  # noqa: E402  (the single auth implementation)

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"

BASE_HEADERS = {
    "accept": "application/json",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "x-fantasy-platform": "kona",
    "x-fantasy-source": "kona",
}

RETRY_CODES = (429, 500, 502, 503, 504)


class EspnError(RuntimeError):
    """An ESPN response we could not use. Carries the HTTP status when there was one."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


# ── auth ─────────────────────────────────────────────────────────────────────
def auth_cookie(swid=None, s2=None) -> str:
    """The `Cookie:` header value for an authenticated ESPN read."""
    if not (swid and s2):
        swid, s2 = load_auth()
    return f"SWID={swid}; espn_s2={s2}"


# ── urls ─────────────────────────────────────────────────────────────────────
def league_url(league_id, season, historical: bool = False) -> str:
    """Endpoint for one league-season.

    `historical=True` selects /leagueHistory, which is the only endpoint that
    serves seasons the league has finished — and which answers with a
    single-element list. Callers that may hit either must `unwrap()`.
    """
    if historical:
        return f"{BASE}/leagueHistory/{league_id}?seasonId={season}"
    return f"{BASE}/seasons/{season}/segments/0/leagues/{league_id}"


def history_index_url(league_id) -> str:
    """/leagueHistory with no seasonId: one element per season the league existed."""
    return f"{BASE}/leagueHistory/{league_id}"


def game_url(season) -> str:
    """Season-level (not league-level) endpoint — pro-team schedules, bye weeks."""
    return f"{BASE}/seasons/{season}"


def with_views(url, views) -> str:
    """Append `view=` params. ESPN accepts the param repeated."""
    if isinstance(views, str):
        views = [views]
    if not views:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "&".join(f"view={v}" for v in views)


def with_params(url, **params) -> str:
    """Append arbitrary query params (scoringPeriodId, seasonId, ...)."""
    parts = [f"{k}={v}" for k, v in params.items() if v is not None]
    if not parts:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + "&".join(parts)


def fantasy_filter(spec: dict) -> dict:
    """Header dict for ESPN's `x-fantasy-filter` (it wants a JSON *string*)."""
    return {"x-fantasy-filter": json.dumps(spec)}


# ── fetch ────────────────────────────────────────────────────────────────────
def _unwrap(data):
    """The historical endpoint returns [payload]; every caller wants the payload."""
    if isinstance(data, list):
        return data[0] if data else {}
    return data


unwrap = _unwrap          # public name; `fetch(unwrap=True)` shadows it locally


def fetch(url, cookie, extra_headers=None, *, timeout: int = 45, retries: int = 4,
          unwrap: bool = False):
    """GET `url` as JSON, retrying transient failures with exponential backoff.

    Raises EspnError (with `.status`) on a non-retryable HTTP error, so callers
    can treat a 400/404 as "this view does not exist for this season" rather than
    as a crash. Modelled on scrape.py's fetch, with the status preserved.
    """
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
            return _unwrap(data) if unwrap else data
        except urllib.error.HTTPError as e:
            if e.code in RETRY_CODES and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            body = e.read().decode("utf-8", "ignore")[:300]
            if e.code in (401, 403):
                body += ("\n  -> auth rejected: SWID/espn_s2 are wrong or expired "
                         "(refresh scraping/.espn_auth.json).")
            raise EspnError(f"HTTP {e.code} for {url}\n  {body}", status=e.code)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise EspnError(f"{type(e).__name__} for {url}: {e}")
    raise EspnError(f"exhausted retries for {url}: {last}")


# ── small derived helpers ────────────────────────────────────────────────────
def available_seasons(league_id, cookie) -> list:
    """Every season this league has existed, straight from /leagueHistory.

    Cheaper and more honest than probing year by year: ESPN returns one element
    per season, so the answer is exact per league (they differ — that is the
    point of the multi-league work).
    """
    data = fetch(with_views(history_index_url(league_id), ["mStatus"]), cookie)
    if not isinstance(data, list):
        data = [data]
    out = {int(d["seasonId"]) for d in data if d and d.get("seasonId")}
    return sorted(out)


def pro_teams(season, cookie) -> dict:
    """{proTeamId: {"abbrev": "DET", "bye": 6}} — bye weeks are NOT on the player
    object, they live on the season's pro-team schedule."""
    data = fetch(with_views(game_url(season), ["proTeamSchedules_wl"]), cookie, unwrap=True)
    teams = (data.get("settings") or {}).get("proTeams") or []
    out = {}
    for t in teams:
        try:
            tid = int(t.get("id"))
        except (TypeError, ValueError):
            continue
        bye = t.get("byeWeek")
        out[tid] = {"abbrev": t.get("abbrev") or "",
                    "bye": int(bye) if bye else None}
    return out


def current_week(league_payload) -> int:
    """The scoring period the league is in, from the payload's own status block."""
    p = unwrap(league_payload)
    st = p.get("status") or {}
    for key in ("latestScoringPeriod", "currentMatchupPeriod"):
        v = st.get(key)
        if v:
            return int(v)
    return int(p.get("scoringPeriodId") or 1)
