#!/usr/bin/env python3
"""Shared plumbing for the Tier-A research adapters.

Every adapter in this package is a thin `collect()` that turns one external feed
into a list of records. Everything else — HTTP politeness, caching, the WS-2
envelope, the join key, coverage accounting, and the "never raise" rule — lives
here so the adapters stay small and behave identically.

The envelope is frozen in docs/contracts.md:

    {"source","fetched","season","week","scoring","ok","error","coverage","records"}

Guardrails honoured here, not per-adapter:
  * `norm` comes from `extract_csg.norm_name` — imported, never forked. It is the
    single join key used everywhere in this repo.
  * A failing source returns `ok:false` + an error string. `collect()` may raise
    whatever it likes; `run_adapter()` catches it. Nothing here can fail a pipeline.
  * Polite client: one descriptive User-Agent, a per-host minimum delay, bounded
    retries with backoff, no parallelism.
  * Cache TTL, so re-running the stage five times in an afternoon hits the network
    once. `--refresh` bypasses it.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# The one shared join key. Imported from the draft pipeline, never reimplemented.
sys.path.insert(0, os.path.join(ROOT, "draft_sheets"))
from extract_csg import norm_name  # noqa: E402,F401  (re-exported on purpose)

import leagues  # noqa: E402

# ── the one join key the D/ST case needs on top of norm_name ────────────────
# ESPN says "Texans D/ST"; FantasyPros and Boris Chen say "Houston Texans". Both
# reduce to the nickname, which is the last word in every one of the 32 team names
# — no team table, no fork of norm_name, and every league here starts a D/ST, so
# losing them would cost ~8% of the pool on a position people stream weekly.
_DST_NOISE = ("d/st", "dst", "d st", "defense", "special teams", "def")


def dst_key(name, pos=None):
    """'Texans D/ST' and 'Houston Texans' -> 'dst:texans'. None if not a defense."""
    s = str(name or "").lower()
    looks_dst = (str(pos or "").upper() in ("DST", "D/ST", "DEF")
                 or any(t in s for t in ("d/st", "dst", "defense")))
    if not looks_dst:
        return None
    for t in _DST_NOISE:
        s = s.replace(t, " ")
    toks = norm_name(s).split()
    return f"dst:{toks[-1]}" if toks else None

# ── polite client ────────────────────────────────────────────────────────────
UA = ("fantasy-research-engine/1.0 (personal fantasy-football tool; "
      "low-volume, cached, non-commercial)")
MIN_HOST_DELAY = 1.0          # seconds between two hits on the same host
DEFAULT_TIMEOUT = 90
DEFAULT_TTL = 6 * 3600        # adapters override

_last_hit: dict[str, float] = {}

# Where league-independent blobs (nflverse crosswalk, Sleeper's player dump) live.
# Per-league envelopes always go to ctx.raw("sources/{name}.json") per the contract.
SHARED = os.path.join(leagues.RAW, "_shared")


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


def http_get(url, *, timeout=DEFAULT_TIMEOUT, accept="*/*", headers=None,
             retries=3, delay=MIN_HOST_DELAY) -> bytes:
    """One GET, politely. Retries 429/5xx with backoff; raises on anything else."""
    h = {"user-agent": UA, "accept": accept, "accept-encoding": "identity"}
    h.update(headers or {})
    host = _host(url)
    last_err = None
    for attempt in range(retries + 1):
        wait = _last_hit.get(host, 0.0) + delay - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} for {url}"
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(last_err) from None
        except Exception as e:                       # URLError, timeout, DNS, TLS…
            last_err = f"{type(e).__name__}: {e} for {url}"
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(last_err) from None
    raise RuntimeError(last_err or f"unreachable: {url}")


def http_json(url, **kw):
    return json.loads(http_get(url, accept="application/json", **kw))


def http_csv(url, **kw):
    """CSV -> list[dict]. nflverse and Boris Chen both ship plain UTF-8 CSV."""
    text = http_get(url, accept="text/csv", **kw).decode("utf-8", "replace")
    return list(csv.DictReader(io.StringIO(text)))


# ── blob cache for the big league-independent downloads ──────────────────────
def shared_blob(name, url, *, ttl, refresh=False, parse="csv", **kw):
    """Fetch-and-cache a large league-independent file (nflverse CSVs, Sleeper's
    ~10MB player dump). Kept out of any single league's directory because it is
    not that league's data — and re-downloading it per league would be rude."""
    os.makedirs(SHARED, exist_ok=True)
    path = os.path.join(SHARED, name)
    if not refresh and os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl:
        raw = open(path, "rb").read()
    else:
        raw = http_get(url, **kw)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, path)
    if parse == "csv":
        return list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    if parse == "json":
        return json.loads(raw)
    return raw


def shared_age(name):
    p = os.path.join(SHARED, name)
    return (time.time() - os.path.getmtime(p)) if os.path.exists(p) else None


# ── the league's profile, without depending on WS-1 having run ───────────────
SETTINGS_URL = ("https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
                "{season}/segments/0/leagues/{lid}?view=mSettings")
SETTINGS_TTL = 7 * 24 * 3600


def league_profile(ctx, refresh=False):
    """LeagueProfile for `ctx`, or None.

    Prefers WS-1's `league_full.json`. If that is not there yet, falls back to a
    cached `league_settings.json` (a DIFFERENT filename — it never shadows or
    overwrites WS-1's artifact), refreshed from ESPN at most weekly. Everything
    an adapter parameterises on (scoring label, size, QB slots) lives in mSettings.
    """
    from engine.profile import LeagueProfile
    full = ctx.raw("league_full.json")
    if os.path.exists(full):
        try:
            return LeagueProfile.from_file(full)
        except Exception:
            pass
    cached = ctx.raw("league_settings.json")
    fresh = (os.path.exists(cached)
             and (time.time() - os.path.getmtime(cached)) < SETTINGS_TTL)
    if fresh and not refresh:
        try:
            return LeagueProfile.from_file(cached)
        except Exception:
            pass
    try:
        sys.path.insert(0, os.path.join(ROOT, "scraping"))
        from scrape_league import load_auth
        swid, s2 = load_auth()
        data = http_json(SETTINGS_URL.format(season=ctx.season, lid=ctx.league_id),
                         headers={"cookie": f"SWID={swid}; espn_s2={s2}",
                                  "x-fantasy-platform": "kona",
                                  "x-fantasy-source": "kona"})
        os.makedirs(os.path.dirname(cached), exist_ok=True)
        with open(cached, "w") as f:
            json.dump(data, f)
        return LeagueProfile.from_espn(data)
    except Exception:
        if os.path.exists(cached):                       # stale beats nothing
            try:
                return LeagueProfile.from_file(cached)
            except Exception:
                return None
        return None


# ── the reference pool coverage is measured against ──────────────────────────
FANTASY_POSITIONS = ("QB", "RB", "WR", "TE", "K")
ESPN_POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST"}


def pool_cap(profile):
    """How many players this league can actually hold: size x roster_size.

    That is the honest denominator for coverage. ESPN's player dump carries ~1,000
    entries including third-string practice-squad bodies nobody in a 12-team league
    will ever roster; counting a source as 'missing' them would understate it. Both
    the numbers and the cap are league-derived — an 8-team league's pool is smaller
    than a 14-team one's, which is the point.
    """
    return max(int(profile.size) * int(profile.roster_size), 1) if profile else 300


def _pool_from_espn_players(path, profile=None):
    """WS-1's kona_player_info dump: the league's real add pool, weighted by
    ownership — the closest in-season analogue of the draft builder's '$ pool'."""
    with open(path) as f:
        data = json.load(f)
    items = data.get("players") if isinstance(data, dict) else data
    pool = []
    for it in items or []:
        p = it.get("player") if isinstance(it, dict) and "player" in it else it
        if not isinstance(p, dict):
            continue
        name = p.get("fullName") or p.get("name") or ""
        if not name:
            continue
        own = ((p.get("ownership") or {}).get("percentOwned") or 0.0)
        pool.append({
            "espn_id": str(p.get("id") or it.get("id") or "") or None,
            "norm": norm_name(name), "name": name,
            "pos": ESPN_POS.get(int(p.get("defaultPositionId") or 0), ""),
            "weight": max(float(own), 0.0) / 100.0 or 0.001,
        })
    pool.sort(key=lambda p: -p["weight"])
    return pool[:pool_cap(profile)]


def _pool_from_rosters(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        data = data[0] if data else {}
    pool = []
    for t in data.get("teams") or []:
        for e in ((t.get("roster") or {}).get("entries") or []):
            p = (e.get("playerPoolEntry") or {}).get("player") or {}
            name = p.get("fullName") or ""
            if not name:
                continue
            pool.append({"espn_id": str(p.get("id") or "") or None,
                         "norm": norm_name(name), "name": name,
                         "pos": ESPN_POS.get(int(p.get("defaultPositionId") or 0), ""),
                         "weight": 1.0})
    return pool


def _pool_from_nflverse(ctx, profile, refresh=False):
    """Fallback pool: this season's producers, truncated to what the league can
    actually roster (size x roster_size) and weighted by realised fantasy points.

    So `pct_of_pool` reads as 'share of this season's fantasy production covered',
    which is the honest in-season analogue of CSG's '% of the $ pool'. It is
    league-parameterised: an 8-team league's pool is smaller than a 14-team one's.
    """
    from . import nflverse
    rows = nflverse.week_stats(ctx.season, refresh=refresh)
    xw = crosswalk(refresh=refresh)
    agg = {}
    for r in rows:
        if (r.get("season_type") or "REG") != "REG":
            continue
        gsis = r.get("player_id")
        name = r.get("player_display_name") or r.get("player_name") or ""
        pos = (r.get("position") or "").upper()
        if not name or pos not in FANTASY_POSITIONS:
            continue
        a = agg.setdefault(gsis or name, {"name": name, "pos": pos, "pts": 0.0})
        a["pts"] += _f(r.get("fantasy_points_ppr"))
    ranked = sorted(agg.items(), key=lambda kv: -kv[1]["pts"])[:pool_cap(profile)]
    pool = []
    for gsis, a in ranked:
        ids = xw["by_gsis"].get(gsis) or {}
        pool.append({"espn_id": ids.get("espn_id"), "norm": norm_name(a["name"]),
                     "name": a["name"], "pos": a["pos"], "weight": max(a["pts"], 0.0)})
    return pool


_POOL_CACHE: dict = {}


def reference_pool(ctx, week=None, profile=None, refresh=False):
    """(pool, basis). Honest about which denominator it managed to build."""
    key = (ctx.key, week)
    if key in _POOL_CACHE and not refresh:
        return _POOL_CACHE[key]
    candidates = []
    if week:
        candidates.append((ctx.raw(f"players_wk{week}.json"), "espn:players_wk%s" % week))
    d = ctx.raw_dir
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d), reverse=True):
            if fn.startswith("players_wk") and fn.endswith(".json"):
                candidates.append((os.path.join(d, fn), f"espn:{fn[:-5]}"))
    for path, basis in candidates:
        if os.path.exists(path):
            try:
                pool = _pool_from_espn_players(path, profile)
                if pool:
                    basis = (f"{basis}: top {len(pool)} by ESPN %owned "
                             f"(= size x roster_size; weight = %owned)")
                    _POOL_CACHE[key] = (pool, basis)
                    return _POOL_CACHE[key]
            except Exception:
                continue
    full = ctx.raw("league_full.json")
    if os.path.exists(full):
        try:
            pool = _pool_from_rosters(full)
            if pool:
                _POOL_CACHE[key] = (pool, "espn:rosters")
                return _POOL_CACHE[key]
        except Exception:
            pass
    try:
        pool = _pool_from_nflverse(ctx, profile, refresh=refresh)
        if pool:
            basis = (f"nflverse:top{len(pool)}-by-fantasy-points "
                     f"(= size x roster_size; weight = season PPR points)")
            _POOL_CACHE[key] = (pool, basis)
            return _POOL_CACHE[key]
    except Exception as e:
        _POOL_CACHE[key] = ([], f"unavailable ({type(e).__name__})")
        return _POOL_CACHE[key]
    _POOL_CACHE[key] = ([], "unavailable")
    return _POOL_CACHE[key]


def coverage_of(records, pool, basis):
    """How much of the league's pool this source actually reaches.

    Matched on espn_id first, norm name second — and reported honestly: a source
    that only ranks 40 running backs covers 40 of the pool, not 40 of 40.
    """
    if not pool:
        n = len(records)
        return {"matched": n, "total": n, "pct_of_pool": None,
                "basis": f"no league pool available ({basis}); "
                         f"'matched' is simply the record count"}
    by_id, by_norm, by_dst = {}, {}, {}
    for rec in records:
        if rec.get("espn_id"):
            by_id[str(rec["espn_id"])] = rec
        if rec.get("norm"):
            by_norm.setdefault(rec["norm"], rec)
        dk = dst_key(rec.get("name"), rec.get("pos"))
        if dk:
            by_dst.setdefault(dk, rec)
    matched = 0
    wt_hit = wt_all = 0.0
    for p in pool:
        w = float(p.get("weight") or 0.0)
        wt_all += w
        dk = dst_key(p.get("name"), p.get("pos"))
        if ((p.get("espn_id") and str(p["espn_id"]) in by_id)
                or (p["norm"] in by_norm)
                or (dk and dk in by_dst)):
            matched += 1
            wt_hit += w
    return {"matched": matched, "total": len(pool),
            "pct_of_pool": round(wt_hit / wt_all, 4) if wt_all else None,
            "basis": basis}


# ── the nflverse espn_id crosswalk (used by every name-only source) ──────────
_XW = {}


def crosswalk(refresh=False, ttl=7 * 24 * 3600):
    """nflverse players.csv -> id maps. 790/794 of this season's fantasy-position
    players carry an `espn_id`, which is why we prefer it over name matching."""
    if _XW and not refresh:
        return _XW
    from . import nflverse
    rows = nflverse.players_csv(refresh=refresh, ttl=ttl)
    by_norm, by_gsis, by_pfr = {}, {}, {}
    for r in rows:
        name = r.get("display_name") or ""
        rec = {"espn_id": (r.get("espn_id") or "").strip() or None,
               "gsis_id": (r.get("gsis_id") or "").strip() or None,
               "pos": (r.get("position") or "").strip(),
               "team": (r.get("latest_team") or "").strip(),
               "name": name,
               "last_season": r.get("last_season") or ""}
        if rec["gsis_id"]:
            by_gsis[rec["gsis_id"]] = rec
        if (r.get("pfr_id") or "").strip():
            by_pfr[r["pfr_id"].strip()] = rec
        if name:
            k = norm_name(name)
            prev = by_norm.get(k)
            # prefer the more recent player when two share a normalised name
            if not prev or str(rec["last_season"]) > str(prev.get("last_season") or ""):
                by_norm[k] = rec
    _XW.clear()
    _XW.update({"by_norm": by_norm, "by_gsis": by_gsis, "by_pfr": by_pfr})
    return _XW


def resolve_espn_id(name=None, norm=None, gsis=None, pfr=None):
    try:
        xw = crosswalk()
    except Exception:
        return None
    for src, key in ((xw["by_gsis"], gsis), (xw["by_pfr"], pfr),
                     (xw["by_norm"], norm or (norm_name(name) if name else None))):
        if key and key in src:
            eid = src[key].get("espn_id")
            if eid:
                return eid
    return None


# ── record + envelope construction ───────────────────────────────────────────
def record(name, pos=None, team=None, espn_id=None, gsis=None, pfr=None, **fields):
    n = norm_name(name)
    if espn_id is None:
        espn_id = resolve_espn_id(norm=n, gsis=gsis, pfr=pfr)
    return {"name": name, "norm": n, "pos": (pos or "").upper() or None,
            "team": (team or "").upper() or None,
            "espn_id": int(espn_id) if str(espn_id or "").isdigit() else (espn_id or None),
            "fields": {k: v for k, v in fields.items() if v is not None}}


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def cache_file(ctx, name):
    return ctx.raw(os.path.join("sources", f"{name}.json"))


def archive_file(ctx, name, season, week):
    return ctx.raw(os.path.join("sources", "archive",
                                f"{name}_{season}_wk{int(week):02d}.json"))


def archive(ctx, name, env) -> str | None:
    """Freeze this week's snapshot of a source — WS-8e's prerequisite.

    External rankings publish only their CURRENT values. There is no historical
    archive to buy or scrape, so "did FantasyPros add anything over ESPN?" is
    permanently unanswerable unless we start keeping the evidence NOW. One file per
    (source, season, week).

    **First write wins.** A snapshot taken on Tuesday is what you actually knew when
    you set a lineup; one taken on Monday night knows the results. Overwriting would
    quietly convert a forecast into a postdiction, which is the single easiest way to
    manufacture a source that looks prescient. `graded_at` records when it was taken
    so the backtest can check the snapshot predates the games rather than trust us.
    """
    week = env.get("week")
    if not env.get("ok") or week in (None, ""):
        return None
    path = archive_file(ctx, name, env.get("season") or ctx.season, week)
    if os.path.exists(path):
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({**env, "_archived": _now()}, f, separators=(",", ":"))
    os.replace(tmp, path)
    return path


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_adapter(name, collect, ctx, week=None, *, refresh=False, ttl=DEFAULT_TTL,
                write=True, profile=None):
    """Wrap a `collect(ctx, week, profile) -> records` in the frozen WS-2 envelope.

    This is the only place that decides ok/error, so no adapter can raise past it
    and no third-party outage can fail the pipeline.
    """
    path = cache_file(ctx, name)
    if not refresh and os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < ttl:
            try:
                with open(path) as f:
                    env = json.load(f)
                env["_cache"] = {"hit": True, "age_s": int(age)}
                return env
            except Exception:
                pass

    prof = profile if profile is not None else league_profile(ctx)
    scoring = prof.scoring_label if prof else None
    ok, error, records = True, None, []
    try:
        if prof is None:
            raise RuntimeError("no LeagueProfile (ESPN settings unreachable and "
                               "no league_full.json / league_settings.json cached)")
        records = list(collect(ctx, week, prof) or [])
        if not records:
            ok, error = False, "source returned no records"
    except Exception as e:
        ok, error = False, f"{type(e).__name__}: {e}"[:400]

    if ok:
        pool, basis = reference_pool(ctx, week, profile=prof, refresh=refresh)
        cov = coverage_of(records, pool, basis)
    else:
        cov = {"matched": 0, "total": 0, "pct_of_pool": None, "basis": "source failed"}

    env = {"source": name, "fetched": _now(), "season": ctx.season, "week": week,
           "scoring": scoring, "ok": ok, "error": error, "coverage": cov,
           "records": records}
    if write:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(env, f, indent=1)
            f.write("\n")
        os.replace(tmp, path)
        archive(ctx, name, env)
    env["_cache"] = {"hit": False, "age_s": 0}
    return env
