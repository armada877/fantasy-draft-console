#!/usr/bin/env python3
"""Pipeline status collector — what actually exists on disk, and how old it is.

Produces `ctx.out("pipeline_status.json")` exactly as frozen in docs/contracts.md
("WS-7 artifacts"), by INSPECTING THE FILESYSTEM rather than trusting a run log:
file existence, mtime, byte size, row counts, and each artifact's own embedded
metadata (`generated` / `fetched` / `_meta`).

Why this exists (contracts.md): the engine's credibility rests on calibration
quality and source coverage. A confident recommendation built on week-old
projections, or on a manager profile that was silently *borrowed* rather than
calibrated, is the failure mode this console prevents. So freshness and
provenance are first-class outputs, not decoration.

Design rules
------------
* **Missing is the normal state.** Most artifacts do not exist yet. Every stage
  renders as MISSING with a reason; nothing raises.
* **No hardcoded league facts.** Stage outputs, the waiver curve filename, and
  which adapters exist are all derived from `LeagueContext` / `LeagueProfile` /
  the `scraping/sources` package.
* **Staleness is per stage, declared.** Waiver-relevant data ages in HOURS;
  calibration ages in MONTHS. A derived artifact is also stale when it is older
  than the artifact it was built from (`depends_on`) — a rebuilt payload that
  never got re-injected is stale even if it is two minutes old.
* **Additive fields only.** Everything in the frozen schema is emitted; extra
  keys (`state`, `max_age_hours`, `stale_reason`, `variant_detail`, ...) are
  added for the UI and are safe for any consumer that ignores them.

CLI
---
    python3 analysis/pipeline_status.py                 # every league, human readable
    python3 analysis/pipeline_status.py --league 2kdome --json
    python3 analysis/pipeline_status.py --write         # -> ctx.out("pipeline_status.json")
"""
from __future__ import annotations

import fnmatch
import json
import os
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import leagues  # noqa: E402

STATUS_NAME = "pipeline_status.json"
RUNLOG_NAME = "pipeline_runs.json"      # written by the run-trigger; read back for duration/error

HOUR = 3600.0
DAY = 24 * HOUR

# ── declared max ages ────────────────────────────────────────────────────────
# In-season data moves daily (waivers, injuries, projections); calibration is a
# seasonal artifact. These are the numbers `stale` is computed against.
MAX_AGE_H = {
    "season-scrape": 18,        # ESPN rosters/projections — must be same-day on waiver day
    "sources": 18,              # third-party consensus, trending adds
    "season-calibrate": 60 * 24,  # ~2 months: history only changes as the season accrues
    "season-build": 18,
    "season-inject": 18,
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(ts: float | None):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _parse_iso(s):
    if not s or not isinstance(s, str):
        return None
    try:
        t = s.replace("Z", "+00:00")
        d = datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _glob(dirpath, pattern):
    if not os.path.isdir(dirpath):
        return []
    return sorted(os.path.join(dirpath, f) for f in os.listdir(dirpath)
                  if fnmatch.fnmatch(f, pattern))


def _rows(obj):
    """Best-effort row count for an artifact, by shape — never guesses a schema."""
    if obj is None:
        return None
    if isinstance(obj, list):
        return len(obj)
    if isinstance(obj, dict):
        for key in ("records", "players", "transactions", "teams", "items"):
            v = obj.get(key)
            if isinstance(v, (list, dict)):
                return len(v)
        return len([k for k in obj if not str(k).startswith("_")])
    return None


def _artifact(path, rows=None, label=None):
    """One output file's provenance: bytes, mtime, rows, and its own timestamp."""
    if not os.path.exists(path):
        return {"path": os.path.relpath(path, ROOT), "bytes": None, "mtime": None,
                "rows": None, "exists": False, "label": label}
    st = os.stat(path)
    obj = None
    if path.endswith(".json") and st.st_size < 80_000_000:
        obj = _load(path)
    self_ts = None
    if isinstance(obj, dict):
        for k in ("generated", "fetched", "scraped", "updated"):
            self_ts = self_ts or _parse_iso(obj.get(k))
        meta = obj.get("_meta")
        if isinstance(meta, dict):
            self_ts = self_ts or _parse_iso(meta.get("generated"))
    return {"path": os.path.relpath(path, ROOT), "bytes": st.st_size,
            "mtime": _iso(st.st_mtime), "rows": rows if rows is not None else _rows(obj),
            "exists": True, "label": label,
            "self_reported": _iso(self_ts), "_ts": max(st.st_mtime, self_ts or 0), "_obj": obj}


# ── stage definitions ────────────────────────────────────────────────────────
# Each stage lists the artifacts it is responsible for. Paths come off the
# LeagueContext (never string-built), the waiver curve name comes off the
# profile's acquisition model, and week-numbered scrapes are globbed.
def _stage_outputs(ctx, profile, week):
    curve_name = ("faab_curve.json"
                  if (profile and profile.acquisition.is_faab) else "priority_curve.json")
    players = _glob(ctx.raw_dir, "players_wk*.json")
    sources = _glob(os.path.join(ctx.raw_dir, "sources"), "*.json")
    season_html = [os.path.join(ctx.static_dir, n) for n in ("season.html", "index.html")]
    return {
        "season-scrape": [
            (ctx.raw("league_full.json"), "league settings + teams + rosters"),
            *[(p, f"ESPN player pool ({os.path.basename(p)[:-5].replace('players_wk', 'week ')})")
              for p in players[-2:]],
            (ctx.raw("transactions.json"), "all-season transaction history"),
        ],
        "sources": [(p, f"{os.path.basename(p)[:-5]} envelope") for p in sources]
                   or [(os.path.join(ctx.raw_dir, "sources"), "adapter envelopes")],
        "season-calibrate": [
            (ctx.config("season_tendencies.json"), "per-manager calibrated tendencies"),
            (ctx.config(curve_name), f"{curve_name[:-5].replace('_', ' ')} + validation"),
        ],
        "season-build": [(ctx.out("season_data.json"), "the cockpit payload")],
        "season-inject": [(p, "generated cockpit") for p in season_html],
    }


# which stage's freshness a stage is downstream of
DEPENDS_ON = {
    "sources": None,
    "season-calibrate": None,
    "season-build": "season-scrape",
    "season-inject": "season-build",
}

STAGE_ORDER = ["season-scrape", "sources", "season-calibrate", "season-build", "season-inject"]

STAGE_DOC = {
    "season-scrape": "ESPN league + player pool + transactions for this league",
    "sources": "Tier-A research adapters -> raw/sources/*.json (self-skipping)",
    "season-calibrate": "transaction history -> per-manager tendencies + waiver curve",
    "season-build": "ROS valuation + merge -> the cockpit payload",
    "season-inject": "template + payload -> the served cockpit (the golden rule)",
}


def _runlog(ctx):
    """Durations and errors recorded by the run-trigger, newest per stage."""
    log = _load(ctx.out(RUNLOG_NAME)) or []
    out = {}
    if isinstance(log, list):
        for entry in log:
            if isinstance(entry, dict) and entry.get("stage"):
                out[entry["stage"]] = entry
    return out


def record_run(ctx, stage, *, started, finished, ok, error=None, exit_code=None, keep=60):
    """Append one run to `ctx.out("pipeline_runs.json")`.

    The run-trigger API calls this so `duration_s` / `error` in the status are
    real observations rather than always-null. Nothing else depends on it; if the
    file is absent those two fields are simply null.
    """
    path = ctx.out(RUNLOG_NAME)
    log = _load(path)
    if not isinstance(log, list):
        log = []
    log.append({"stage": stage, "started": _iso(started), "finished": _iso(finished),
                "duration_s": round(max(finished - started, 0.0), 1), "ok": bool(ok),
                "error": error, "exit_code": exit_code})
    log = log[-keep:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(log, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)
    return log[-1]


def _stage_summary(name, arts, profile):
    """One glanceable line per stage, built from what was actually counted."""
    live = [a for a in arts if a["exists"]]
    if not live:
        return None
    bits = []
    if name == "season-scrape":
        full = next((a for a in live if a["path"].endswith("league_full.json")), None)
        if full and isinstance(full.get("_obj"), (dict, list)):
            obj = full["_obj"]
            obj = obj[0] if isinstance(obj, list) and obj else obj
            teams = len((obj or {}).get("teams") or []) if isinstance(obj, dict) else 0
            if teams:
                bits.append(f"{teams} teams")
        for a in live:
            if "players_wk" in a["path"] and a["rows"]:
                bits.append(f"{a['rows']:,} players {a['label'].split('(')[-1].rstrip(')')}")
        tx = next((a for a in live if a["path"].endswith("transactions.json")), None)
        if tx and tx["rows"]:
            bits.append(f"{tx['rows']:,} transactions")
    elif name == "sources":
        ok = sum(1 for a in live if isinstance(a.get("_obj"), dict) and a["_obj"].get("ok"))
        bits.append(f"{ok}/{len(live)} adapters ok")
        recs = sum(a["rows"] or 0 for a in live)
        if recs:
            bits.append(f"{recs:,} records")
    elif name == "season-calibrate":
        tend = next((a for a in live if a["path"].endswith("season_tendencies.json")), None)
        if tend and isinstance(tend.get("_obj"), dict):
            mgrs = [k for k in tend["_obj"] if not str(k).startswith("_")]
            borrowed = sum(1 for k in mgrs if (tend["_obj"][k] or {}).get("borrowed"))
            bits.append(f"{len(mgrs)} managers ({borrowed} borrowed)")
        curve = next((a for a in live if a["path"].endswith("curve.json")), None)
        if curve and isinstance(curve.get("_obj"), dict):
            n = curve["_obj"].get("n_claims")
            bits.append(f"curve from {n:,} claims" if n else "curve present")
    elif name == "season-build":
        a = live[0]
        if a["rows"]:
            bits.append(f"{a['rows']:,} players")
        obj = a.get("_obj") or {}
        if isinstance(obj, dict):
            if obj.get("waivers") is not None:
                bits.append(f"{len(obj['waivers'])} waiver rows")
            if obj.get("week"):
                bits.append(f"week {obj['week']}")
    elif name == "season-inject":
        for a in live:
            bits.append(f"{os.path.basename(a['path'])} {a['bytes'] // 1024:,}kb")
    return " · ".join(bits) if bits else f"{len(live)} output(s)"


def _stage(ctx, name, outputs, profile, runs, upstream_ts):
    arts = [_artifact(p, label=lab) for p, lab in outputs]
    live = [a for a in arts if a["exists"]]
    run = runs.get(name) or {}
    max_age = MAX_AGE_H.get(name)
    newest = max((a["_ts"] for a in live), default=None)

    state, error, stale, reason = "ok", run.get("error"), False, None
    if not live:
        state = "missing"
        reason = "no outputs on disk — this stage has never produced anything here"
        stale = True
        if run and not run.get("ok"):
            state, error = "error", run.get("error") or "last run failed"
    else:
        age_h = (time.time() - newest) / HOUR
        if max_age is not None and age_h > max_age:
            state, stale = "stale", True
            reason = f"older than the declared max age ({max_age}h) for this stage"
        dep = DEPENDS_ON.get(name)
        dep_ts = upstream_ts.get(dep) if dep else None
        if dep_ts and newest and dep_ts > newest:
            state, stale = "stale", True
            reason = f"built before `{dep}` last ran — rebuild to pick that up"
        if run and not run.get("ok") and run.get("error"):
            state, error = "error", run.get("error")
        missing = [a for a in arts if not a["exists"]]
        if state == "ok" and missing:
            state = "partial"
            reason = f"{len(missing)} of {len(arts)} expected outputs missing"

    out_rows = []
    for a in arts:
        out_rows.append({k: a[k] for k in
                         ("path", "bytes", "mtime", "rows", "exists", "label", "self_reported")
                         if k in a})
    return {
        "name": name,
        "ok": state in ("ok", "partial"),
        "state": state,                       # ok | partial | stale | missing | error
        "last_run": _iso(newest) if newest else (run.get("finished") or None),
        "age_hours": round((time.time() - newest) / HOUR, 2) if newest else None,
        "stale": stale,
        "stale_reason": reason,
        "max_age_hours": max_age,
        "summary": _stage_summary(name, arts, profile),
        "outputs": out_rows,
        "error": error,
        "duration_s": run.get("duration_s"),
        "doc": STAGE_DOC.get(name),
    }, newest


# ── research sources ─────────────────────────────────────────────────────────
def _adapter_names():
    """Adapters that EXIST, discovered from the package — never a constant list."""
    d = os.path.join(ROOT, "scraping", "sources")
    if not os.path.isdir(d):
        return []
    return sorted(f[:-3] for f in os.listdir(d)
                  if f.endswith(".py") and not f.startswith(("_", "common")))


def _try(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def _variant_for(name, profile):
    """WHICH FILE this league pulls — the per-league correctness check.

    The WS-2 envelope (contracts.md) carries `scoring` but not the variant/URL it
    resolved to, so we re-derive it by asking the adapter module the same question
    it asks itself: purely introspective, no network. An adapter with no
    league-dependent helper is reported as league-independent BY DESIGN rather
    than silently showing the scoring label — the user is checking that
    FantasyPros / Boris Chen / FantasyCalc differ per league, and a flat label on
    every row would defeat that check.

    Returns (variant, urls, league_independent).
    """
    if not profile:
        return None, [], None
    label = profile.scoring_label
    try:
        sys.path.insert(0, os.path.join(ROOT, "scraping"))
        mod = __import__(f"scraping.sources.{name}", fromlist=["*"])
    except Exception:
        return label, [], None
    urls, parts = [], []

    if hasattr(mod, "ros_url"):
        urls.append(_try(mod.ros_url, label))
    if hasattr(mod, "weekly_url"):
        urls.append(_try(mod.weekly_url, label))
    if hasattr(mod, "positions_for") and hasattr(mod, "url_for"):
        for pos in (_try(mod.positions_for, profile) or []):
            urls.append(_try(mod.url_for, pos, label))
    elif hasattr(mod, "url_for"):
        urls.append(_try(mod.url_for, profile) or _try(mod.url_for, label))
    if hasattr(mod, "params_for"):
        got = _try(mod.params_for, profile)
        if isinstance(got, (list, tuple)):
            names = getattr(mod, "PARAM_NAMES", None) or ("qbs", "teams", "ppr")
            parts = [f"{n}={v}" for n, v in zip(names, got)]
    urls = [u for u in urls if isinstance(u, str) and u.startswith("http")]

    variant = getattr(mod, "VARIANT", None)
    suffix = getattr(mod, "SUFFIX", None)
    if isinstance(variant, dict) and label in variant:
        return (str(variant[label] or "standard").strip("-") or "standard"), urls, False
    if isinstance(suffix, dict) and label in suffix:
        return ((suffix[label] or "").lstrip("-") or "no-scoring-variant"), urls, False
    if parts:
        return " ".join(parts), urls, False
    # No league-dependent selector anywhere in the module: it pulls one feed for
    # everyone (Sleeper trending, nflverse usage). Say so plainly.
    return "league-independent", urls, True


def _blurb(name):
    """The adapter's own one-line description (its docstring's first sentence).

    Carries WS-2's honest caveats into the UI verbatim — e.g. Boris Chen publishes
    weekly tiers only and no scoring variants for QB/K/DST, which is a property of
    the feed, not a failure of the fetch.
    """
    path = os.path.join(ROOT, "scraping", "sources", f"{name}.py")
    try:
        with open(path) as f:
            head = f.read(2000)
    except Exception:
        return None
    i = head.find('"""')
    if i < 0:
        return None
    body = head[i + 3:]
    j = body.find('"""')
    doc = (body[:j] if j > 0 else body).strip()
    first = doc.split("\n\n")[0].strip().replace("\n", " ")
    return first[:400] or None


def _cache_manifest(ctx):
    """url -> {fetched_at, tier, bytes} from scraping/cache.py's manifests.

    Reading the manifest is a better freshness signal than re-fetching to find
    out: it says when each URL was last actually pulled, and under which tier.
    """
    roots = [ctx.raw("cache"), os.path.join(leagues.RAW, "_shared", "cache"),
             os.path.join(ROOT, "scraping", "raw", "cache")]
    out = {}
    for r in roots:
        man = _load(os.path.join(r, "_manifest.json"))
        if not isinstance(man, dict):
            continue
        for key, e in man.items():
            if not isinstance(e, dict) or not e.get("url"):
                continue
            prev = out.get(e["url"])
            if not prev or (e.get("fetched_at") or 0) > (prev.get("fetched_at") or 0):
                out[e["url"]] = {"fetched_at": e.get("fetched_at"), "tier": e.get("tier"),
                                 "bytes": e.get("bytes"), "key": key}
    return out


def _sources(ctx, profile):
    """Live/failed/never-run per adapter, with the variant this league resolves to."""
    sdir = os.path.join(ctx.raw_dir, "sources")
    envelopes = {}
    for p in _glob(sdir, "*.json"):
        obj = _load(p)
        if isinstance(obj, dict):
            envelopes[obj.get("source") or os.path.basename(p)[:-5]] = (p, obj)
    cache = _cache_manifest(ctx)

    def cached_urls(urls):
        rows = []
        for u in urls:
            e = cache.get(u)
            rows.append({"url": u,
                         "age_hours": (round((time.time() - e["fetched_at"]) / HOUR, 2)
                                       if e and e.get("fetched_at") else None),
                         "tier": (e or {}).get("tier"), "bytes": (e or {}).get("bytes")})
        return rows

    out = []
    for name in sorted(set(_adapter_names()) | set(envelopes)):
        variant, urls, indep = _variant_for(name, profile)
        p, env = envelopes.get(name, (None, None))
        if env is None:
            out.append({"name": name, "ok": False, "state": "missing", "fetched": None,
                        "age_hours": None, "variant": variant, "variant_detail": urls,
                        "league_independent": indep, "coverage": None, "error": None,
                        "blurb": _blurb(name), "urls_cached": cached_urls(urls),
                        "note": "never fetched for this league — run the `sources` stage",
                        "path": None})
            continue
        ts = _parse_iso(env.get("fetched")) or os.path.getmtime(p)
        cov = env.get("coverage") or {}
        pct = cov.get("pct_of_pool")
        if pct is None:
            pct = cov.get("pct")
        out.append({
            "name": name,
            "ok": bool(env.get("ok")),
            "state": "live" if env.get("ok") else "failed",
            "fetched": env.get("fetched") or _iso(ts),
            "age_hours": round((time.time() - ts) / HOUR, 2),
            # the envelope's own answer wins if WS-2 ever adds one; else ours
            "variant": env.get("variant") or variant,
            "variant_detail": env.get("urls") or urls,
            "league_independent": indep,
            "blurb": _blurb(name),
            "urls_cached": cached_urls(env.get("urls") or urls),
            "scoring": env.get("scoring"),
            "coverage": ({"matched": cov.get("matched"), "total": cov.get("total"),
                          "pct": round(pct, 4) if isinstance(pct, (int, float)) else None,
                          "basis": cov.get("basis")} if cov else None),
            "records": len(env.get("records") or []),
            "error": env.get("error"),
            "path": os.path.relpath(p, ROOT),
        })
    return out


# ── calibration ──────────────────────────────────────────────────────────────
def _league_manager_names(ctx):
    """Manager names for the CURRENT managers, as the rest of the app resolves them.

    PREFER season_data.json: build_season_data has already applied manager_canon.json
    (the member-GUID bridge, which fixes display drift like 'Jonathan' -> 'Jon') and
    league.json's manager_labels (a team known by its team name). Re-deriving names
    from the raw scrape here produced three phantom "unmatched" managers and inflated
    the borrowed count — the console must agree with the cockpit, or it is worse than
    useless. Falls back to the raw scrape when season_data has not been built yet.
    """
    sd = _load(ctx.out("season_data.json"))
    if isinstance(sd, dict) and sd.get("teams"):
        names = [t.get("manager") for t in sd["teams"] if t.get("manager")]
        if names:
            return names
    return _league_manager_names_from_scrape(ctx)


def _league_manager_names_from_scrape(ctx):
    """Fallback: manager display names straight off the scrape (no identity bridge)."""
    obj = _load(ctx.raw("league_full.json"))
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    if not isinstance(obj, dict):
        return []
    # members[] is everyone who was EVER in the league; the current managers are the
    # owners of the current teams, so join through teams[].owners.
    by_id = {}
    for m in obj.get("members") or []:
        nm = " ".join(x for x in [(m.get("firstName") or "").strip(),
                                  (m.get("lastName") or "").strip()] if x)
        by_id[m.get("id")] = nm or (m.get("displayName") or "")
    out = []
    for t in obj.get("teams") or []:
        owners = t.get("owners") or []
        nm = by_id.get(owners[0], "") if owners else ""
        if nm:
            out.append(nm)
    return out or [n for n in by_id.values() if n]


def _calibration(ctx, profile):
    tend = _load(ctx.config("season_tendencies.json"))
    curve_name = ("faab_curve.json"
                  if (profile and profile.acquisition.is_faab) else "priority_curve.json")
    curve = _load(ctx.config(curve_name))
    managers = {}
    meta = {}
    if isinstance(tend, dict):
        meta = tend.get("_meta") or {}
        managers = {k: v for k, v in tend.items()
                    if not str(k).startswith("_") and isinstance(v, dict)}
    # Count against the managers who are actually IN this league now — a tendencies
    # file that also carries departed managers must not inflate the headline.
    current = _league_manager_names(ctx)
    in_league = {k: v for k, v in managers.items() if k in set(current)} if current else {}
    counted = in_league or managers
    unmatched = ([n for n in current if n not in managers] if current else [])
    borrowed = sum(1 for v in counted.values() if v.get("borrowed"))
    cal = {
        "managers_total": (len(current) if current else len(managers)) or (profile.size if profile else 0),
        "managers_borrowed": borrowed + (len(unmatched) if in_league else 0),
        "managers_in_file": len(managers),
        "managers_unmatched": unmatched,
        "seasons_used": meta.get("seasons"),
        "excluded_seasons": meta.get("acquisition_regimes") and {
            s: f"{r} regime" for s, r in (meta.get("excluded_seasons") or {}).items()
        } or meta.get("excluded_seasons") or {},
        "method": meta.get("method"),
        "present": bool(managers),
        "path": os.path.relpath(ctx.config("season_tendencies.json"), ROOT),
        "curve": None,
    }
    if isinstance(curve, dict):
        val = curve.get("validation") or {}
        h, b = val.get("holdout_mae"), val.get("baseline_mae")
        ran = val.get("ran")
        if ran is None:
            ran = bool(val)
        # WS-3 validates SEVERAL targets (median bid, p80 pinball, contested Brier) and
        # promotes one only if it beats the baseline by a threshold. The frozen status
        # schema has a single boolean, so collapse conservatively — and never let a
        # tie read as a win.
        beats_map = val.get("beats_baseline")
        promoted = val.get("promoted")
        targets = []
        if isinstance(beats_map, dict) or isinstance(promoted, dict):
            names = sorted(set(list(beats_map or {})) | set(list(promoted or {})))
            gains = val.get("gain_pct") or {}
            for nm in names:
                targets.append({"name": nm, "gain_pct": gains.get(nm),
                                "beats": (beats_map or {}).get(nm),
                                "promoted": (promoted or {}).get(nm)})
        if not ran:
            beats = None
        elif isinstance(promoted, dict):
            beats = any(bool(v) for v in promoted.values())
        elif isinstance(beats_map, dict):
            beats = any(bool(v) for v in beats_map.values())
        elif isinstance(beats_map, bool):
            beats = beats_map
        elif isinstance(h, (int, float)) and isinstance(b, (int, float)):
            beats = h < b
        else:
            beats = None
        if beats and isinstance(h, (int, float)) and isinstance(b, (int, float)) and h >= b:
            beats = False                     # a tie is not a win, whatever the file says
        cal["curve"] = {
            "model": curve.get("model") or (profile.acquisition.model if profile else None),
            "present": True,
            "path": os.path.relpath(ctx.config(curve_name), ROOT),
            "seasons": curve.get("seasons"),
            "n_claims": curve.get("n_claims"),
            "validated": bool(ran),
            "reason": val.get("reason"),
            "verdict": val.get("verdict"),
            "scheme": val.get("scheme"),
            "promote_threshold_pct": val.get("promote_threshold_pct"),
            "targets": targets,
            "holdout_mae": h, "baseline_mae": b,
            "n_holdout": val.get("n_holdout"),
            "beats_baseline": beats,
        }
    else:
        cal["curve"] = {"model": profile.acquisition.model if profile else None,
                        "present": False, "path": os.path.relpath(ctx.config(curve_name), ROOT),
                        "validated": False, "holdout_mae": None, "baseline_mae": None,
                        "beats_baseline": None,
                        "note": "no curve on disk — run the `season-calibrate` stage"}
    return cal


def _week(ctx):
    data = _load(ctx.out("season_data.json"))
    if isinstance(data, dict) and data.get("week"):
        return int(data["week"])
    weeks = []
    for p in _glob(ctx.raw_dir, "players_wk*.json"):
        base = os.path.basename(p)[len("players_wk"):-len(".json")]
        if base.isdigit():
            weeks.append(int(base))
    return max(weeks) if weeks else None


def _warnings(st):
    """Derived from the status object itself, so a status produced ANYWHERE — this
    collector, a fixture, another workstream — gets the same reading."""
    key = st.get("league") or "league"
    w = []
    if st.get("profile") is None:
        w.append(f"{key}: no LeagueProfile — league_full.json has not been scraped, so this "
                 f"league's rules (size, scoring, waiver model) are unknown")
    for s_ in st.get("stages") or []:
        if s_.get("state") == "missing":
            w.append(f"{key}: stage `{s_['name']}` has never run")
        elif s_.get("stale"):
            age = f"{s_['age_hours']:.0f}h" if s_.get("age_hours") is not None else "?"
            w.append(f"{key}: stage `{s_['name']}` is STALE ({age} old)"
                     + (f" — {s_['stale_reason']}" if s_.get("stale_reason") else ""))
        elif s_.get("state") == "error":
            w.append(f"{key}: stage `{s_['name']}` last run FAILED: {s_.get('error')}")
    for s_ in st.get("sources") or []:
        if s_.get("state") == "failed":
            w.append(f"{key}: source `{s_['name']}` FAILED — {s_.get('error')}")
    cal = st.get("calibration") or {}
    tot, bor = cal.get("managers_total") or 0, cal.get("managers_borrowed") or 0
    if bor:
        w.append(f"{key}: {bor} of {tot} managers borrowed the league prior "
                 f"(no history of their own) — their numbers are an assumption, not a read")
    if cal.get("present") is False:
        w.append(f"{key}: no calibrated tendencies — every opponent is the league prior")
    curve = cal.get("curve") or {}
    if curve.get("present") and curve.get("beats_baseline") is False:
        w.append(f"{key}: the {curve.get('model')} curve DOES NOT beat the naive baseline "
                 f"(holdout MAE {curve.get('holdout_mae')} vs {curve.get('baseline_mae')}) — "
                 f"it is not authoritative")
    if curve.get("present") and not curve.get("validated"):
        w.append(f"{key}: the {curve.get('model')} curve carries no out-of-sample validation")
    return w


def normalize(st):
    """Fill the additive fields on a CONTRACT-SHAPED status that did not come from
    this collector (a fixture, or another producer).

    The frozen schema carries `ok` / `stale` / `error`; the console also reads
    `state`, `max_age_hours` and `doc`. Deriving them here means one rendering path
    for every producer, and it is idempotent on our own output.
    """
    if not isinstance(st, dict):
        return st
    for s_ in st.get("stages") or []:
        outs = s_.get("outputs") or []
        if "state" not in s_:
            if not s_.get("ok") and s_.get("error"):
                s_["state"] = "error"
            elif not s_.get("last_run") and not outs:
                s_["state"] = "missing"
            elif s_.get("stale"):
                s_["state"] = "stale"
            elif any(o.get("exists") is False for o in outs):
                s_["state"] = "partial"
            else:
                s_["state"] = "ok" if s_.get("ok") else "missing"
        s_.setdefault("max_age_hours", MAX_AGE_H.get(s_.get("name")))
        s_.setdefault("doc", STAGE_DOC.get(s_.get("name")))
        # The declared max age lives HERE, in one place. If a producer's `stale`
        # flag disagrees with the declaration, the declaration wins — otherwise
        # "stale" would mean a different thing depending on who wrote the file.
        # Age can only ever ADD staleness here: a stage may also be stale because it
        # is older than the artifact it was built from, and that reason must survive.
        mx, ah = s_.get("max_age_hours"), s_.get("age_hours")
        if mx is not None and ah is not None and ah > mx:
            if not s_.get("stale"):
                s_["stale_reason"] = (f"older than the declared max age ({mx}h) for this stage "
                                      f"(the producer did not flag it)")
            s_["stale"] = True
            s_.setdefault("stale_reason",
                          f"older than the declared max age ({mx}h) for this stage")
        if s_.get("stale") and s_.get("state") not in ("missing", "error"):
            s_["state"] = "stale"
    for s_ in st.get("sources") or []:
        if "state" not in s_:
            s_["state"] = "live" if s_.get("ok") else ("failed" if s_.get("error") else "missing")
        s_.setdefault("blurb", _blurb(s_.get("name") or ""))
    cal = st.get("calibration")
    if isinstance(cal, dict):
        cal.setdefault("present", bool(cal.get("managers_total")))
        curve = cal.get("curve")
        if isinstance(curve, dict):
            curve.setdefault("present", bool(curve.get("model")))
            if curve.get("beats_baseline") is None and curve.get("holdout_mae") is not None \
                    and curve.get("baseline_mae") is not None:
                curve["beats_baseline"] = curve["holdout_mae"] < curve["baseline_mae"]
    have = set(st.get("warnings") or [])
    st["warnings"] = list(st.get("warnings") or []) + [w for w in _warnings(st) if w not in have]
    return st


def collect(ctx):
    """The frozen `pipeline_status.json` object for one league. Never raises."""
    try:
        profile = ctx.profile()
    except Exception:
        profile = None
    week = _week(ctx)
    runs = _runlog(ctx)
    outputs = _stage_outputs(ctx, profile, week)
    stages, upstream_ts = [], {}
    for name in STAGE_ORDER:
        st, ts = _stage(ctx, name, outputs.get(name, []), profile, runs, upstream_ts)
        upstream_ts[name] = ts
        stages.append(st)
    sources = _sources(ctx, profile)
    cal = _calibration(ctx, profile)
    out = {
        "league": ctx.key,
        "league_name": ctx.name,
        "season": ctx.season,
        "week": week,
        "generated": _now_iso(),
        "profile": ({"size": profile.size, "scoring_label": profile.scoring_label,
                     "draft_type": profile.draft_type,
                     "acquisition": profile.acquisition.model,
                     "budget": profile.acquisition.budget,
                     "regular_weeks": profile.regular_weeks,
                     "playoff_teams": profile.playoff_teams,
                     "starters": profile.starters_per_team,
                     "summary": profile.summary()} if profile else None),
        "stages": stages,
        "sources": sources,
        "calibration": cal,
        "warnings": [],
        "run_enabled_hint": "POST /api/pipeline/run requires PIPELINE_RUN_ENABLED=1 on the server",
    }
    return normalize(out)


def collect_all(ctxs=None):
    return [collect(c) for c in (ctxs if ctxs is not None else leagues.all())]


def write(ctx, status=None):
    status = status or collect(ctx)
    ctx.ensure_dirs()
    path = ctx.out(STATUS_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(status, f, indent=1)
        f.write("\n")
    os.replace(tmp, path)
    return path


# ── CLI ──────────────────────────────────────────────────────────────────────
GLYPH = {"ok": "✓", "partial": "◐", "stale": "!", "missing": "·", "error": "✗"}


def _print(st):
    p = st.get("profile") or {}
    head = (f"{p.get('size')}tm {p.get('scoring_label')} {p.get('acquisition')}"
            if p else "no profile (league_full.json missing)")
    print(f"\n{st['league']}  — {st['league_name']}  season {st['season']} "
          f"week {st['week'] or '?'}   [{head}]")
    for s in st["stages"]:
        age = f"{s['age_hours']:>7.1f}h" if s["age_hours"] is not None else "      —"
        flag = "STALE" if s["stale"] and s["state"] != "missing" else s["state"].upper()
        print(f"  {GLYPH.get(s['state'], '?')} {s['name']:<17} {age}  "
              f"{flag:<8} {s['summary'] or s['stale_reason'] or ''}")
    for s in st["sources"]:
        cov = s.get("coverage") or {}
        covs = (f"{cov.get('matched')}/{cov.get('total')}"
                f" = {cov['pct']:.0%} of pool" if cov.get("pct") is not None
                else (f"{cov.get('matched')}/{cov.get('total')}" if cov else "—"))
        print(f"    {s['state']:<8} {s['name']:<13} variant={s['variant'] or '?':<18} {covs}"
              + (f"  ERROR {s['error']}" if s.get("error") else ""))
    c = st["calibration"]
    curve = c.get("curve") or {}
    verdict = ("beats baseline" if curve.get("beats_baseline") else
               "DOES NOT beat baseline" if curve.get("beats_baseline") is False else
               "unvalidated")
    print(f"    calibration: {c['managers_total']} managers, {c['managers_borrowed']} borrowed"
          f" | curve {curve.get('model')} {'present' if curve.get('present') else 'MISSING'}"
          f" ({verdict})")
    for w in st["warnings"]:
        print(f"    ! {w}")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv
    do_write = "--write" in argv
    key = None
    if "--league" in argv:
        key = argv[argv.index("--league") + 1]
    ctxs = [leagues.resolve(key)] if key else leagues.all()
    out = []
    for ctx in ctxs:
        st = collect(ctx)
        out.append(st)
        if do_write:
            path = write(ctx, st)
            if not as_json:
                print(f"wrote {os.path.relpath(path, ROOT)}")
        if not as_json:
            _print(st)
    if as_json:
        print(json.dumps(out if len(out) != 1 else out[0], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
