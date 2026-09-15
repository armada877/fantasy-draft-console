#!/usr/bin/env python3
"""Sleeper — trending adds/drops. League-independent, and it moves first.

    /v1/state/nfl                                   -> the live NFL week
    /v1/players/nfl/trending/add?lookback_hours=&limit=
    /v1/players/nfl/trending/drop?lookback_hours=&limit=
    /v1/players/nfl                                 -> ~10 MB player metadata

Why it earns a slot: ESPN's `percentOwned` updates on ESPN's clock, but Sleeper's
add counts move within hours of a news break. A player with 900k adds in 24h will
be gone from your league's pool before ESPN's ownership number notices.

The metadata dump is large, so it is cached to `scraping/raw/_shared/` and refreshed
**at most daily**. It is also the best espn_id source available here: each Sleeper
player carries `espn_id` directly, so trending records join without a name lookup.

`week` for the whole stage defaults to `/v1/state/nfl`'s `week` when the caller does
not pass one — see `current_week()`.
"""
from __future__ import annotations

from . import common
from .common import _f, _i

NAME = "sleeper"
TTL = 3600                       # trending is the whole point; keep it fresh
API = "https://api.sleeper.app/v1"
META_TTL = 24 * 3600
DEFAULT_LOOKBACK = 24
DEFAULT_LIMIT = 100


def state(refresh=False):
    return common.shared_blob("sleeper_state.json", f"{API}/state/nfl",
                              ttl=900, refresh=refresh, parse="json")


def current_week(default=1):
    """The live NFL week, or `default` if Sleeper is unreachable. Never raises —
    the stage must run even when the week can only be guessed."""
    try:
        s = state()
        return _i(s.get("week")) or _i(s.get("display_week")) or default
    except Exception:
        return default


def players_meta(refresh=False, ttl=META_TTL):
    """The ~10 MB dump, cached to disk and refreshed at most daily."""
    return common.shared_blob("sleeper_players_nfl.json", f"{API}/players/nfl",
                              ttl=ttl, refresh=refresh, parse="json")


def _trending(kind, lookback, limit):
    return common.http_json(
        f"{API}/players/nfl/trending/{kind}?lookback_hours={lookback}&limit={limit}")


def _collect(ctx, week, profile, lookback=DEFAULT_LOOKBACK, limit=DEFAULT_LIMIT,
             refresh=False):
    meta = players_meta(refresh=refresh)
    adds = _trending("add", lookback, limit)
    drops = _trending("drop", lookback, limit)

    rows = {}
    for kind, data in (("adds", adds), ("drops", drops)):
        for i, row in enumerate(data or []):
            pid = str(row.get("player_id") or "")
            if not pid:
                continue
            r = rows.setdefault(pid, {})
            r[f"trend_{kind}"] = _i(row.get("count"))
            r[f"trend_{kind}_rank"] = i + 1

    out = []
    for pid, f in rows.items():
        m = meta.get(pid) or {}
        name = (m.get("full_name")
                or " ".join(x for x in (m.get("first_name"), m.get("last_name")) if x)
                or m.get("last_name") or "")
        if not name:
            continue
        out.append(common.record(
            name,
            pos=m.get("position") or (m.get("fantasy_positions") or [None])[0],
            team=m.get("team"),
            espn_id=m.get("espn_id"),
            sleeper_id=pid,
            lookback_hours=lookback,
            status=m.get("status") or None,
            injury_status=m.get("injury_status") or None,
            depth_chart_order=_i(m.get("depth_chart_order")),
            years_exp=_i(m.get("years_exp")),
            **f))
    return out


def fetch(ctx, week=None, *, refresh=False, ttl=TTL, write=True, profile=None,
          lookback=DEFAULT_LOOKBACK, limit=DEFAULT_LIMIT, **_):
    return common.run_adapter(
        NAME,
        lambda c, w, p: _collect(c, w, p, lookback=lookback, limit=limit, refresh=refresh),
        ctx, week, refresh=refresh, ttl=ttl, write=write, profile=profile)
