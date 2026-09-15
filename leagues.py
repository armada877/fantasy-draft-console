#!/usr/bin/env python3
"""Multi-league context — one app, every team on your ESPN account.

Each league is a TOTALLY SEPARATE CONTEXT: its own raw data, its own calibrated
tendencies, its own briefing, its own generated payload. Nothing derived in one
league may leak into another; `LeagueContext` is how that isolation is enforced in
paths rather than by convention.

Zero-config: leagues are DISCOVERED from your ESPN cookies (the fan API), not
hand-listed. `python3 leagues.py` prints what it finds and writes the registry.

    import leagues
    for ctx in leagues.all():           # every team you manage
        prof = ctx.profile()            # engine.profile.LeagueProfile
        print(ctx.key, prof.summary())

    ctx = leagues.resolve("2kdome")

BACK-COMPAT (important): the original single-league layout put raw data at
scraping/raw/{season}/ and config at config/*.json. analysis/lib.py and the draft
console still read those paths, and tests/draft_regression.py guards them. So the
legacy league keeps the legacy layout; every other league is namespaced under its
key. `raw_dir` / `config_dir` hide the difference.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(ROOT, "scraping", "raw")
CONFIG = os.path.join(ROOT, "config")
REGISTRY = os.path.join(CONFIG, "leagues.json")
OUT = os.path.join(ROOT, "draft_sheets", "out")
STATIC = os.path.join(ROOT, "draft_app", "static", "l")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


STOPWORDS = {"the", "a", "an", "of", "and", "league", "fantasy", "football"}


def slug(name: str, maxlen: int = 22) -> str:
    """Short, stable, URL-safe league key. Drops filler words so
    'Chi Phi American Football League' -> 'chi-phi-american'."""
    words = [w for w in re.split(r"[^a-z0-9]+", (name or "").lower()) if w]
    kept = [w for w in words if w not in STOPWORDS] or words
    out = ""
    for w in kept:
        cand = f"{out}-{w}" if out else w
        if len(cand) > maxlen:
            break
        out = cand
    return out or "league"


def _legacy_league_id():
    """The league the single-league layout was built for (config/league.json)."""
    p = os.path.join(CONFIG, "league.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                return int(json.load(f).get("league_id") or 0)
        except Exception:
            return 0
    return 0


@dataclass
class LeagueContext:
    key: str
    name: str
    league_id: int
    season: int
    team_id: int            # YOUR team in this league
    team_name: str = ""
    platform: str = "espn"

    # ── isolation: every path is namespaced by league ────────────────────────
    @property
    def is_legacy(self) -> bool:
        """True for the league the original single-league layout was built around."""
        return self.league_id == _legacy_league_id()

    @property
    def raw_dir(self) -> str:
        return (os.path.join(RAW, str(self.season)) if self.is_legacy
                else os.path.join(RAW, self.key, str(self.season)))

    @property
    def config_dir(self) -> str:
        return CONFIG if self.is_legacy else os.path.join(CONFIG, "leagues", self.key)

    @property
    def out_dir(self) -> str:
        return os.path.join(OUT, self.key)

    @property
    def static_dir(self) -> str:
        return os.path.join(STATIC, self.key)

    def raw(self, name: str) -> str:
        return os.path.join(self.raw_dir, name)

    def config(self, name: str) -> str:
        return os.path.join(self.config_dir, name)

    def out(self, name: str) -> str:
        return os.path.join(self.out_dir, name)

    def ensure_dirs(self):
        for d in (self.raw_dir, self.config_dir, self.out_dir, self.static_dir):
            os.makedirs(d, exist_ok=True)

    # ── the league's rules, derived from scraped settings ────────────────────
    def profile(self):
        """engine.profile.LeagueProfile for this league. Requires league_full.json."""
        from engine.profile import LeagueProfile
        p = self.raw("league_full.json")
        if not os.path.exists(p):
            raise FileNotFoundError(
                f"{self.key}: no {p}. Run the season scrape for this league first.")
        return LeagueProfile.from_file(p)

    def to_dict(self):
        return {"key": self.key, "name": self.name, "league_id": self.league_id,
                "season": self.season, "team_id": self.team_id,
                "team_name": self.team_name, "platform": self.platform}


# ── discovery ────────────────────────────────────────────────────────────────
FAN_API = ("https://fan.api.espn.com/apis/v2/fans/{swid}"
           "?featureFlags=challengeEntries&showAirings=buy,live,replay"
           "&source=fantasyapp&lang=en&section=fantasy")
FFL_GAME_ID = 1          # ESPN gameId for fantasy football


def _fan_entries(swid, s2):
    """Parse the ESPN fan API into (league_id, season, team_id, names) tuples.

    scrape_league.discover_leagues() exists but drops entryId/team name, and it is
    on the draft console's code path — so we parse here rather than change it.
    """
    import urllib.parse, urllib.request
    url = FAN_API.format(swid=urllib.parse.quote(swid))
    headers = {"accept": "application/json", "cookie": f"SWID={swid}; espn_s2={s2}",
               "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")}
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=40) as r:
        data = json.load(r)
    out = []
    for pref in data.get("preferences") or []:
        entry = (pref.get("metaData") or {}).get("entry") or {}
        if not entry or int(entry.get("gameId") or 0) != FFL_GAME_ID:
            continue
        groups = entry.get("groups") or [{}]
        grp = groups[0] if groups else {}
        if not grp.get("groupId"):
            continue
        out.append({
            "league_id": int(grp["groupId"]),
            "league_name": grp.get("groupName") or "",
            "season": int(entry.get("seasonId") or 0),
            "team_id": int(entry.get("entryId") or 0),
            "team_name": (entry.get("entryMetadata") or {}).get("teamName")
                         or entry.get("name") or "",
        })
    # newest season first, de-duped on (league, season)
    seen, uniq = set(), []
    for e in sorted(out, key=lambda e: -e["season"]):
        k = (e["league_id"], e["season"])
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


def discover(save: bool = True, season: int | None = None):
    """Every fantasy-football team on the authenticated ESPN account."""
    sys.path.insert(0, os.path.join(ROOT, "scraping"))
    from scrape_league import load_auth   # noqa: E402  (auth helper only)
    swid, s2 = load_auth()
    found = _fan_entries(swid, s2)
    if season:
        found = [e for e in found if e["season"] == season]
    ctxs, used = [], set()
    for lg in found:
        key = slug(lg.get("league_name"))
        if key in used:                       # keep keys unique and stable
            key = f"{key}-{lg['league_id']}"
        used.add(key)
        ctxs.append(LeagueContext(
            key=key, name=lg.get("league_name") or "", league_id=int(lg["league_id"]),
            season=int(lg["season"]), team_id=int(lg.get("team_id") or 0),
            team_name=lg.get("team_name") or "",
        ))
    if save and ctxs:
        os.makedirs(CONFIG, exist_ok=True)
        with open(REGISTRY, "w") as f:
            json.dump([c.to_dict() for c in ctxs], f, indent=2)
            f.write("\n")
    return ctxs


def all(refresh: bool = False):
    """Registered leagues; discovers on first use (or with refresh=True)."""
    if refresh or not os.path.exists(REGISTRY):
        return discover()
    with open(REGISTRY) as f:
        return [LeagueContext(**d) for d in json.load(f)]


def resolve(key: str):
    for c in all():
        if c.key == key or str(c.league_id) == str(key):
            return c
    raise KeyError(f"unknown league {key!r}; have {[c.key for c in all()]}")


def default():
    """The legacy single-league context, for code that predates multi-league."""
    for c in all():
        if c.is_legacy:
            return c
    got = all()
    return got[0] if got else None


if __name__ == "__main__":
    for c in discover(save="--no-save" not in sys.argv):
        flag = "  [legacy layout]" if c.is_legacy else ""
        print(f"{c.key:34s} id={c.league_id:<12} season={c.season} "
              f"team #{c.team_id} {c.team_name!r}{flag}")
        print(f"{'':34s} raw={os.path.relpath(c.raw_dir, ROOT)}  "
              f"config={os.path.relpath(c.config_dir, ROOT)}")
