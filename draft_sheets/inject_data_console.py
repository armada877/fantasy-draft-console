#!/usr/bin/env python3
"""Build + inject the data & research console (WS-7).

The golden rule (CLAUDE.md) applies here exactly as it does to the draft console:
the served HTML is GENERATED. Edit `draft_sheets/data_console_template.html`
(it carries a `/*DATA*/` marker) and re-inject; never hand-edit anything under
`draft_app/static/`.

    python3 draft_sheets/inject_data_console.py                 # every league, live from disk
    python3 draft_sheets/inject_data_console.py --league 2kdome
    python3 draft_sheets/inject_data_console.py --fixtures      # build from tests/fixtures/
    python3 draft_sheets/inject_data_console.py --json-only     # payload only, no HTML

Writes:
    draft_app/static/data_console.html     the console  (-> /data_console.html)
    draft_app/static/data_console.json     the same payload, for the page to re-fetch

What it assembles, per league:
  * `analysis/pipeline_status.collect(ctx)` — the frozen status artifact
  * the calibration roster (calibrated vs BORROWED, per manager)
  * the waiver curve + its out-of-sample validation
  * the player pool with its market fields, for the provenance / divergence views
  * coverage joins: which adapter reached which player, weighted by ROS value

Everything degrades: a league with no scrape, no calibration and no build still
renders — as a board full of honest MISSING states, which is the normal state of
this repo today.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import leagues                                    # noqa: E402
from analysis import pipeline_status as ps        # noqa: E402

TEMPLATE = os.path.join(HERE, "data_console_template.html")
STATIC = os.path.join(ROOT, "draft_app", "static")
OUT_HTML = os.path.join(STATIC, "data_console.html")
OUT_JSON = os.path.join(STATIC, "data_console.json")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
MARKER = "/*DATA*/"

# Which adapter is responsible for each market field on a player. This is a
# contract fact (docs/contracts.md, WS-4 `players[].mkt`), not a league fact, so
# the console can attribute a join failure to the source that caused it.
# `expect` says whether a gap is a DEFECT or the shape of the feed. Sleeper
# publishes a ~100-player trending list, not a ranking of everyone: 47% coverage
# there is correct, and flagging it red would train the user to ignore this page.
FIELD_SOURCE = [
    {"field": "ros_ecr", "source": "fantasypros", "label": "ROS ECR", "kind": "rank",
     "expect": "full",
     "why": "ranks the whole startable pool — a miss here is a join failure"},
    {"field": "tier", "source": "borischen", "label": "Weekly tier", "kind": "tier",
     "expect": "partial",
     "why": "weekly tiers only (no ROS tiers are published), and only for the positions "
            "this league starts — QB/K/DST have no scoring variant at source"},
    {"field": "trade_value", "source": "fantasycalc", "label": "Trade value", "kind": "value",
     "expect": "full",
     "why": "values the tradeable pool at this league's qbs/teams/ppr"},
    {"field": "trend_adds", "source": "sleeper", "label": "Trending adds", "kind": "demand",
     "expect": "partial",
     "why": "a ~100-player trending feed BY DESIGN — absence means 'nobody is adding him', "
            "which is itself the signal. Not a coverage defect"},
    {"field": "snap_share", "source": "nflverse", "label": "Snap share", "kind": "usage",
     "expect": "partial", "why": "offensive snaps only — K and D/ST have none"},
    {"field": "target_share", "source": "nflverse", "label": "Target share", "kind": "usage",
     "expect": "partial", "why": "pass-catchers only — QB/K/DST structurally have none"},
]

# Fields the provenance contract asks for that no upstream artifact currently
# carries. Rendered as explicit gaps rather than quietly omitted — a number the
# user cannot trace is a bug in this console, and so is a silently absent input.
WHY_INPUTS = [
    ("espn_season_proj", "season_data.players[].inputs — not produced by WS-4 today"),
    ("espn_actual", "derivable from actual_ppg x games played; not carried explicitly"),
    ("games_remaining", "derivable from week + regular_weeks; not carried explicitly"),
    ("recomputed_from_scoring", "WS-1/WS-4 recompute FPTS from scraped scoring, but do not flag it"),
]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


# ── the pieces ───────────────────────────────────────────────────────────────
def _curve(ctx, status):
    """The waiver curve file itself (bid levels / survival odds), if it exists."""
    cal = status.get("calibration") or {}
    info = cal.get("curve") or {}
    name = ("faab_curve.json" if info.get("model") == "faab" else "priority_curve.json")
    obj = _load(ctx.config(name))
    if not isinstance(obj, dict):
        return {"present": False, "path": os.path.relpath(ctx.config(name), ROOT), **info}
    return {"present": True, "path": os.path.relpath(ctx.config(name), ROOT), **info,
            "by_quality": obj.get("by_quality") or [], "by_week": obj.get("by_week") or [],
            "seasons": obj.get("seasons"), "n_claims": obj.get("n_claims"),
            "validation": obj.get("validation") or {}}


def _managers_from_league_full(ctx):
    """team_id -> {name, manager} straight off the scrape, so the calibration board
    can list every manager even when nothing downstream has been built."""
    obj = _load(ctx.raw("league_full.json"))
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    if not isinstance(obj, dict):
        return []
    members = {}
    for m in obj.get("members") or []:
        nm = " ".join(x for x in [(m.get("firstName") or "").strip(),
                                  (m.get("lastName") or "").strip()] if x)
        members[m.get("id")] = nm or m.get("displayName") or ""
    out = []
    for t in obj.get("teams") or []:
        owners = t.get("owners") or []
        out.append({"team_id": t.get("id"), "name": t.get("name") or t.get("abbrev") or "",
                    "manager": members.get(owners[0], "") if owners else ""})
    return out


def _tx_counts(ctx):
    """Observed transaction counts for THIS season, per team — the independent
    sanity check on a calibrated profile ('does this manager really add 4/week?')."""
    obj = _load(ctx.raw("transactions.json"))
    tx = obj.get("transactions") if isinstance(obj, dict) else obj
    if not isinstance(tx, list):
        return {}, None
    seasons = set()
    out = {}
    for t in tx:
        if not isinstance(t, dict):
            continue
        s = t.get("seasonId")
        if s:
            seasons.add(int(s))
        if int(s or 0) != int(ctx.season):
            continue
        tid = t.get("teamId")
        if tid is None:
            continue
        rec = out.setdefault(int(tid), {"adds": 0, "drops": 0, "trades": 0, "faab_spent": 0,
                                        "waiver_claims": 0})
        typ = (t.get("type") or "").upper()
        if typ in ("TRADE_ACCEPT", "TRADE_PROPOSAL"):
            rec["trades"] += 1
        if typ == "WAIVER":
            rec["waiver_claims"] += 1
            rec["faab_spent"] += int(t.get("bidAmount") or 0)
        for it in t.get("items") or []:
            k = (it.get("type") or "").upper()
            if k == "ADD":
                rec["adds"] += 1
            elif k == "DROP":
                rec["drops"] += 1
    return out, sorted(seasons)


def _calibration_rows(ctx, status, season_data):
    """One row per manager: calibrated or BORROWED, and the evidence behind it."""
    tend = _load(ctx.config("season_tendencies.json"))
    tmap = {}
    if isinstance(tend, dict):
        tmap = {k: v for k, v in tend.items()
                if not str(k).startswith("_") and isinstance(v, dict)}
    default = (tend or {}).get("_league_default") if isinstance(tend, dict) else None

    teams = (season_data or {}).get("teams") or []
    if not teams:
        teams = [{**t, "tendencies": None, "borrowed": None}
                 for t in _managers_from_league_full(ctx)]
    counts, seasons_seen = _tx_counts(ctx)

    rows = []
    for t in teams:
        mgr = t.get("manager") or t.get("name") or ""
        cal = tmap.get(mgr) or {}
        if not cal and t.get("tendencies"):
            # WS-4 merges the tendency blob onto the team; use it when the raw
            # calibration file is not on this box.
            cal = dict(t["tendencies"])
            cal.setdefault("borrowed", bool(t.get("borrowed")))
        borrowed = cal.get("borrowed")
        if borrowed is None:
            borrowed = t.get("borrowed")
        rows.append({
            "team_id": t.get("team_id"),
            "team": t.get("name") or "",
            "manager": mgr,
            "calibrated": bool(cal) and not borrowed,
            "borrowed": bool(borrowed),
            "has_profile": bool(cal),
            "n_seasons": cal.get("n_seasons"),
            "shrink": cal.get("shrink"),
            "waiver": cal.get("waiver") or {},
            "trade": cal.get("trade") or {},
            "drop": cal.get("drop") or {},
            "observed": counts.get(int(t.get("team_id") or -1), None),
            "faab_left": t.get("faab_left"),
            "waiver_priority": t.get("waiver_priority"),
        })
    summary = {"total": len(rows),
               "calibrated": sum(1 for r in rows if r["calibrated"]),
               "borrowed": sum(1 for r in rows if r["borrowed"]),
               "no_profile": sum(1 for r in rows if not r["has_profile"])}
    # The status collector joins the tendencies FILE to the scraped manager names;
    # these rows join it to season_data's teams. When the two disagree, the cause is
    # a name join, and naming it is the point of this console.
    file_names = set(tmap)
    unmatched = [r["manager"] for r in rows if r["manager"] and r["manager"] not in file_names]
    orphans = sorted(file_names - {r["manager"] for r in rows})
    # Diagnose, don't paper over: a manager who is one collapsed space away from a
    # real profile is a JOIN BUG, not a manager without history, and the difference
    # matters — one is fixable in a line, the other is a fact about the league.
    def _norm(n):
        return " ".join(str(n or "").split()).casefold()
    byn = {}
    for k in file_names:
        byn.setdefault(_norm(k), k)
    near = [{"league_name": n, "file_name": byn[_norm(n)]}
            for n in unmatched if _norm(n) in byn]
    return {
        "rows": rows,
        "summary": summary,
        "unmatched_managers": unmatched,
        "orphan_profiles": orphans,
        "near_matches": near,
        "league_default": default,
        "tendencies_present": bool(tmap),
        "tendencies_path": os.path.relpath(ctx.config("season_tendencies.json"), ROOT),
        "observed_season": ctx.season,
        "observed_seasons_available": seasons_seen,
        "method": (status.get("calibration") or {}).get("method"),
    }


def _players(season_data):
    """Trim the pool to what the console reasons about, and fold the waiver
    engine's roster-specific numbers (`marginal`, bid/claim) onto each player —
    marginal is the in-season edge, so it travels with the player, not in a
    separate table."""
    players = (season_data or {}).get("players") or {}
    waivers = {str(w.get("player_id")): w for w in (season_data or {}).get("waivers") or []}
    out = []
    for pid, p in players.items():
        w = waivers.get(str(pid)) or {}
        out.append({
            "id": p.get("id", pid), "name": p.get("name"), "pos": p.get("pos"),
            "team": p.get("team"), "owner_team_id": p.get("owner_team_id"),
            "injury": p.get("injury"), "bye": p.get("bye"),
            "eligible_slots": p.get("eligible_slots") or [],
            "ros_points": p.get("ros_points"), "ros_ppg": p.get("ros_ppg"),
            "actual_ppg": p.get("actual_ppg"), "last3_ppg": p.get("last3_ppg"),
            "pct_owned": p.get("pct_owned"), "pct_owned_change": p.get("pct_owned_change"),
            "mkt": p.get("mkt") or {},
            "why": p.get("why") if isinstance(p.get("why"), dict) else None,
            "marginal_reg": w.get("marginal_reg"), "marginal_post": w.get("marginal_post"),
            "drop_player_id": w.get("drop_player_id"),
            "bid": w.get("bid"), "claim": w.get("claim"),
            "waiver_why": w.get("why") if isinstance(w.get("why"), list) else None,
        })
    out.sort(key=lambda p: -(p.get("ros_points") or 0))
    return [_compact(p) for p in out]


def _compact(obj):
    """Drop nulls and round floats — the pool is ~1k players x 3 leagues and this
    page is read on a phone."""
    if isinstance(obj, dict):
        return {k: _compact(v) for k, v in obj.items()
                if v is not None and v != {} and v != []}
    if isinstance(obj, list):
        return [_compact(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 3)
    return obj


def _coverage_join(players):
    """Who failed to join, and what share of the ROS-value pool they represent.

    The draft builder's convention ("238/264 = 99% of the $ pool"), carried into
    the season: a miss that is all waiver-wire flotsam does not matter; a miss on
    a rostered starter does.
    """
    total_val = sum(p.get("ros_points") or 0 for p in players) or 1.0
    out = []
    for spec in FIELD_SOURCE:
        f = spec["field"]
        missed = [p for p in players if (p.get("mkt") or {}).get(f) in (None, "")]
        miss_val = sum(p.get("ros_points") or 0 for p in missed)
        out.append({
            **spec,
            "joined": len(players) - len(missed), "total": len(players),
            "pct_of_pool": round(1 - miss_val / total_val, 4),
            "missed": len(missed),
            "missed_rostered": sum(1 for p in missed if p.get("owner_team_id")),
            "top_misses": [{"id": p.get("id"), "name": p.get("name"), "pos": p.get("pos"),
                            "ros_points": p.get("ros_points"),
                            "owner_team_id": p.get("owner_team_id")}
                           for p in sorted(missed, key=lambda p: -(p.get("ros_points") or 0))[:10]],
        })
    return out


def _slot_names_all():
    try:
        from engine.profile import SLOT_NAMES
        return {str(k): v for k, v in SLOT_NAMES.items()}
    except Exception:
        return {}


def _projection_policy():
    """What the valuation is ALLOWED to deviate from the baseline by, and what was
    refused (engine/projection_policy.py).

    The companion to the curve viewer: both answer "has this been paid for with
    out-of-sample evidence?". A REFUSED tuning is a finding worth showing — it is
    the system saying no to itself.
    """
    try:
        from engine import projection_policy as pp
    except Exception as e:
        return {"present": False, "error": f"{type(e).__name__}: {e}"}
    rows = []
    for name, adj in sorted(pp.REGISTRY.items()):
        ok, why = adj.ships()
        val = adj.validation
        rows.append({
            "name": name, "description": getattr(adj, "description", ""),
            "ships": bool(ok), "reason": why,
            "validation": ({"method": val.method, "fitted_on": list(val.fitted_on or []),
                            "notes": val.notes,
                            "results": [{"league": r.league, "n": r.n,
                                         "baseline_mae": r.baseline_mae, "model_mae": r.model_mae,
                                         "improvement": r.improvement, "powered": r.powered,
                                         "seasons_held_out": list(r.seasons_held_out or [])}
                                        for r in val.results]} if val else None),
        })
    return {"present": True, "audit": pp.audit(), "adjustments": rows,
            "refused": [{"name": n, "why": w} for n, w in pp.refused()],
            "bar": {"min_n": pp.MIN_N, "min_powered_leagues": pp.MIN_POWERED_LEAGUES,
                    "harm_tolerance": pp.HARM_TOLERANCE},
            "baseline": ("ESPN projection, recomputed against this league's own scoring — "
                         "any deviation must beat it out-of-sample in at least "
                         f"{pp.MIN_POWERED_LEAGUES} powered leagues or it does not ship")}


def _tierb(ctx):
    """The Tier-B allow-list (WS-2). Dead entries are carried through DEAD, not
    hidden: a third of a curated directory having rotted is exactly the sort of
    thing this console exists to show."""
    for p in (ctx.config("research_sources.json"),
              os.path.join(ROOT, "config", "research_sources.json")):
        raw = _load(p)
        if raw is None:
            continue
        meta, allow, dead = {}, [], []
        if isinstance(raw, dict):
            meta = raw.get("_meta") or {}
            allow = raw.get("allow") or raw.get("sources") or []
            dead = raw.get("dead") or []
        else:
            allow = raw
        def norm(it, i, alive):
            return {"id": str(it.get("id") or it.get("name") or f"src{i}"),
                    "name": it.get("name") or it.get("id") or it.get("url"),
                    "url": it.get("url") or "",
                    "category": it.get("category") or ("dead" if not alive else "uncategorised"),
                    "good_for": (it.get("what_its_good_for") or it.get("good_for")
                                 or it.get("note") or it.get("notes") or ""),
                    "verified": it.get("verified") or it.get("checked") or "",
                    "status": it.get("status") or ("ok" if alive else "dead"),
                    "alive": alive}
        return {"present": True, "path": os.path.relpath(p, ROOT), "meta": meta,
                "sources": [norm(it, i, True) for i, it in enumerate(allow)
                            if isinstance(it, dict) and str(it.get("url") or "").startswith("http")],
                "dead": [norm(it, i, False) for i, it in enumerate(dead)
                         if isinstance(it, dict)]}
    return {"present": False, "meta": {},
            "path": os.path.relpath(os.path.join(ROOT, "config", "research_sources.json"), ROOT),
            "sources": [], "dead": []}


def _trace(ctx):
    obj = _load(ctx.out("research_trace.json"))
    entries = obj.get("entries") if isinstance(obj, dict) else obj
    return {"present": isinstance(entries, list),
            "path": os.path.relpath(ctx.out("research_trace.json"), ROOT),
            "entries": list(entries or [])[-25:][::-1] if isinstance(entries, list) else []}


def _league_block(ctx, status, season_data):
    """The league's own rules, from season_data if built, else from the profile."""
    if season_data and season_data.get("league"):
        return season_data["league"]
    prof = status.get("profile") or {}
    if not prof:
        return {"key": ctx.key, "name": ctx.name, "season": ctx.season}
    try:
        p = ctx.profile()
        from engine.profile import slot_name
        return {"key": ctx.key, "name": ctx.name, "season": ctx.season, "size": p.size,
                "scoring_label": p.scoring_label, "regular_weeks": p.regular_weeks,
                "playoff_teams": p.playoff_teams, "veto_votes": p.veto_votes,
                "trade_deadline": p.trade_deadline_ms,
                "acquisition": {"model": p.acquisition.model, "budget": p.acquisition.budget,
                                "min_bid": p.acquisition.min_bid,
                                "continuous": p.acquisition.continuous},
                "lineup_slots": {str(k): v for k, v in p.starting_slots.items()},
                "slot_names": {str(k): slot_name(k) for k in p.starting_slots}}
    except Exception:
        return {"key": ctx.key, "name": ctx.name, "season": ctx.season, **prof}


def _reconcile_calibration_warning(ctx, status, cal):
    """One number, not two. The status collector joins the tendencies file to the
    SCRAPED manager names; the board joins it to season_data's teams. When they
    disagree the board wins (it is what the user is looking at) and the warning is
    rewritten to say why."""
    sm = cal.get("summary") or {}
    if not sm:
        return
    bor = sm.get("borrowed", 0) + sm.get("no_profile", 0)
    st_cal = status.get("calibration") or {}
    if st_cal.get("managers_borrowed") == bor:
        return
    warns = [w for w in (status.get("warnings") or [])
             if "managers borrowed the league prior" not in w]
    if bor:
        warns.append(f"{ctx.key}: {bor} of {sm.get('total')} managers are on the league prior — "
                     f"their numbers are an assumption, not a read")
    near = cal.get("near_matches") or []
    if near:
        warns.append(f"{ctx.key}: {len(near)} of those are a NAME-JOIN BUG, not missing history — "
                     + "; ".join(f"{n['league_name']!r} vs {n['file_name']!r}" for n in near)
                     + " differ only by whitespace/case, so a calibrated profile is being ignored")
    status["warnings"] = warns
    st_cal["managers_borrowed_board"] = bor


def build_league(ctx, *, fixtures=False, fixtures_dir=None):
    """One league's console payload. Never raises — missing input is a rendered state."""
    if fixtures:
        d = fixtures_dir or FIXTURES
        status = _load(os.path.join(d, f"pipeline_status.{ctx.key}.json"))
        season_data = (_load(os.path.join(d, f"season_data.{ctx.key}.json"))
                       or _load(os.path.join(FIXTURES, f"season_data.{ctx.key}.json")))
        mode = "fixture"
        if status is None:
            status = ps.collect(ctx)
            mode = "live (no fixture for this league)"
        else:
            # a fixture carries only the frozen contract fields; derive the additive
            # ones so there is ONE rendering path for every producer
            status = ps.normalize(status)
    else:
        status = ps.collect(ctx)
        season_data = _load(ctx.out("season_data.json"))
        mode = "live"
        if season_data is None:
            # WS-4's payload has not landed yet. Everything on the DATA half is real;
            # borrow the fixture pool so the research half renders — and SAY SO, both
            # in the payload and on the page footer.
            season_data = _load(os.path.join(FIXTURES, f"season_data.{ctx.key}.json"))
            if season_data is not None:
                mode = "live pipeline · fixture player pool (season-build has not run)"

    if not fixtures:
        # the contract artifact exists however the stage was invoked
        try:
            ps.write(ctx, status)
        except Exception:
            pass
    cal = _calibration_rows(ctx, status, season_data)
    _reconcile_calibration_warning(ctx, status, cal)
    players = _players(season_data)
    gaps = []
    if players and not any(p.get("why") for p in players):
        gaps = [{"field": f, "note": note} for f, note in WHY_INPUTS]

    return {
        "key": ctx.key, "name": ctx.name, "season": ctx.season,
        "week": (season_data or {}).get("week") or status.get("week"),
        "mode": mode,
        "status": status,
        "league": _league_block(ctx, status, season_data),
        "me": (season_data or {}).get("me"),
        "teams": (season_data or {}).get("teams") or [],
        "calibration": cal,
        "curve": _curve(ctx, status),
        "players": players,
        "waivers": (season_data or {}).get("waivers") or [],
        "trades": (season_data or {}).get("trades") or {},
        "coverage_join": _coverage_join(players) if players else [],
        "field_source": FIELD_SOURCE,
        "tierb": _tierb(ctx),
        "trace": _trace(ctx),
        "provenance_gaps": gaps,
        "season_data_path": os.path.relpath(ctx.out("season_data.json"), ROOT),
    }


def build(ctxs=None, *, fixtures=False, fixtures_dir=None):
    ctxs = ctxs if ctxs is not None else leagues.all()
    return {"generated": _now(),
            "source": ("fixtures" + (f" ({os.path.basename(fixtures_dir)})" if fixtures_dir else "")
                       if fixtures else "live"),
            "root": os.path.basename(ROOT),
            "policy": _projection_policy(),
            # platform constant (engine/profile.SLOT_NAMES), not a league fact — so the
            # provenance card can name a slot the league does not start
            "slot_names_all": _slot_names_all(),
            "leagues": [build_league(c, fixtures=fixtures, fixtures_dir=fixtures_dir)
                        for c in ctxs]}


def inject(payload, template=TEMPLATE, out_html=OUT_HTML, out_json=OUT_JSON):
    """template + payload -> served console. The golden-rule step."""
    with open(template) as f:
        tpl = f.read()
    if MARKER not in tpl:
        raise SystemExit(f"{template} has no {MARKER} marker")
    # `</` cannot appear raw inside a <script> block; `<\/` is a legal JSON escape
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    os.makedirs(os.path.dirname(out_html), exist_ok=True)
    with open(out_html, "w") as f:
        f.write(tpl.replace(MARKER, blob))
    with open(out_json, "w") as f:
        f.write(blob)
    return out_html, out_json


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    fixtures = "--fixtures" in argv or "--fixtures-dir" in argv
    fixtures_dir = argv[argv.index("--fixtures-dir") + 1] if "--fixtures-dir" in argv else None
    json_only = "--json-only" in argv
    key = argv[argv.index("--league") + 1] if "--league" in argv else None
    out_html = argv[argv.index("--out") + 1] if "--out" in argv else OUT_HTML

    # `--league KEY` FOCUSES the console on one league; it does not shrink the page.
    # The served console carries every league (the switcher and the cross-league
    # variant matrix need them), so a per-league pipeline run must not drop the rest.
    # `--only` is the escape hatch for a genuinely single-league payload.
    only = "--only" in argv
    try:
        ctxs = [leagues.resolve(key)] if (key and only) else leagues.all()
        if key and not only:
            leagues.resolve(key)                      # validate even when focusing
    except Exception as e:
        raise SystemExit(f"unknown league {key!r}: {e}")
    payload = build(ctxs, fixtures=fixtures, fixtures_dir=fixtures_dir)
    if key:
        payload["focus"] = leagues.resolve(key).key

    for lg in payload["leagues"]:
        st = lg["status"]
        stale = sum(1 for s in st["stages"] if s["stale"])
        missing = sum(1 for s in st["stages"] if s["state"] == "missing")
        bad = sum(1 for s in st["sources"] if s["state"] == "failed")
        cal = st["calibration"]
        print(f"{lg['key']:18s} {lg['mode']:8s} wk{str(lg['week'] or '?'):<3} "
              f"stages: {len(st['stages'])} ({missing} missing, {stale} stale) · "
              f"sources: {len(st['sources'])} ({bad} failed) · "
              f"managers: {cal['managers_total']} ({cal['managers_borrowed']} borrowed) · "
              f"players: {len(lg['players'])}")

    if json_only:
        out = argv[argv.index("--out") + 1] if "--out" in argv else OUT_JSON
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        print(f"wrote {os.path.relpath(out, ROOT)}")
        return 0

    # keep the sidecar JSON next to whatever HTML we were asked to write, so a
    # demo build to another path cannot clobber the served payload
    out_js = (os.path.splitext(out_html)[0] + ".json") if out_html != OUT_HTML else OUT_JSON
    html, js = inject(payload, out_html=out_html, out_json=out_js)
    print(f"wrote {os.path.relpath(html, ROOT)} ({os.path.getsize(html):,}b) "
          f"and {os.path.relpath(js, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
