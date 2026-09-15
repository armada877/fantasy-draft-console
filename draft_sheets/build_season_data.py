#!/usr/bin/env python3
"""Build `ctx.out("season_data.json")` — the payload the in-season cockpit runs on.

    python3 draft_sheets/build_season_data.py --league 2kdome --show
    python3 draft_sheets/build_season_data.py --all
    python3 draft_sheets/build_season_data.py --league inlaws-outlaws --fixture --show

One league per run, one output file per league, every path from `LeagueContext`, so
two leagues can never contaminate each other. The schema is the frozen WS-4 contract
in docs/contracts.md and this builder emits exactly it — `bid` only in FAAB leagues,
`claim` only in priority leagues, chosen off `profile.acquisition.model`.

EVERY INPUT MAY BE MISSING, so every input is optional and its absence is reported
rather than papered over:

    raw/league_full.json        settings, teams, records, budgets, waiver order
    raw/players_wk{N}.json      the player pool with projections and ownership
    raw/pro_teams.json          bye weeks (the player object does not carry one)
    raw/sources/*.json          external rankings/usage — advisory only
    config/season_tendencies.json                  per-manager calibration
    config/faab_curve.json | priority_curve.json   the measured acquisition curve

Nothing is fabricated to fill a gap. A missing source drops a `mkt` column and says
so; missing calibration makes every manager `borrowed` and says so; with no scrape
at all the builder falls back to the checked-in fixture, loudly, so the engines stay
developable against real league SHAPES. Notes land in stdout and in the payload's
`notes` array; coverage is printed the way the draft builder prints it.

Valuation is the league's own and stays that way. `ros_points` comes from
`engine.valuation`, which recomputes from raw ESPN stats times the scraped scoring
map INCLUDING this league's per-position `pointsOverrides` — all three leagues use
them for D/ST, and flat scoring mis-prices every defense, which is exactly the
position people stream off waivers. External rankings never touch that number; they
feed rival-demand estimation and the advisory `mkt` block only.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import leagues                                                      # noqa: E402
from engine import projection_policy, valuation                     # noqa: E402

# Load every MEASURED projection tuning into the policy registry. None of them
# ship (see analysis/registered_adjustments.py), and that is the point: without
# this import the console's policy panel is empty and cannot tell "nothing was
# ever tried" apart from "five things were tried and all five were refused".
try:
    from analysis.registered_adjustments import load as _load_adjustments
    _load_adjustments()
except Exception as _e:                                             # pragma: no cover
    print(f"  • projection policy: no measured candidates loaded ({_e})")
from engine.profile import LeagueProfile, slot_name                 # noqa: E402
from engine.state import SeasonState                                # noqa: E402
from engine.trades import buy_low_sell_high, find_trades            # noqa: E402
from engine.waiver_runs import build_run_calendar, summary as run_summary  # noqa: E402
from engine.waivers import waiver_board                             # noqa: E402

# The join keys, imported never forked: `norm_name` is the shared one, `dst_key`
# is the extra the defenses need because ESPN writes "Texans D/ST" and every
# external source writes "Houston Texans".
from extract_csg import norm_name                                   # noqa: E402
try:
    sys.path.insert(0, os.path.join(ROOT, "scraping"))
    from sources.common import dst_key                              # noqa: E402
except Exception:                                                   # pragma: no cover
    def dst_key(name, pos=None):
        return None

FIXTURES = os.path.join(ROOT, "tests", "fixtures")

# Which `fields` from an external source are carried onto a player's advisory
# `mkt` block. An allow-list, so a source adding twenty columns does not silently
# triple the payload. The contract's six keys plus what the divergence view needs.
MKT_KEYS = (
    "ros_ecr", "ros_pos_rank", "ros_rank_std", "wk_ecr", "tier", "rank",
    "trend_adds", "trend_adds_rank", "trade_value", "redraft_value", "overall_rank",
    "snap_share", "snap_share_last", "target_share", "target_share_last",
    "touches_per_game", "epa_per_game", "owned_avg", "depth_chart_order",
)

# When two adapters supply the same field, the earlier one wins. Anything not
# listed is appended alphabetically, so an adapter WS-2 adds later still merges
# deterministically.
SOURCE_PREFERENCE = ("fantasypros", "borischen", "nflverse", "fantasycalc", "sleeper")


# ── tiny io helpers: never raise, always report ──────────────────────────────
def read_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"  ! unreadable {os.path.relpath(path, ROOT)}: {e}")
        return None


def pct(a, b):
    return (a / b) if b else 0.0


class Inputs:
    """What we actually found, and what we had to do without."""

    def __init__(self):
        self.notes = []
        self.degraded = False

    def note(self, msg, degraded=True):
        self.notes.append(msg)
        self.degraded = self.degraded or degraded
        print(f"  • {msg}")


# ── league payload -> profile, week, teams ───────────────────────────────────
def load_manager_canon(ctx):
    """config/manager_canon.json for this league: {memberGUID: canonical name}.
    Optional — a league without one falls back to collapsed scraped names."""
    p = ctx.config("manager_canon.json")
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def manager_names(payload, canon=None):
    """{memberId: canonical manager name}.

    Two traps, both of which silently cost a manager their calibration:
      1. ESPN ships trailing spaces inside the name parts — firstName 'Kevin ' plus
         lastName 'Kim' joins to 'Kevin  Kim' (DOUBLE space), which then fails to match
         'Kevin Kim' in season_tendencies.json. `.strip()` does not fix an INTERNAL
         double space; collapsing on split() does.
      2. A manager's ESPN display name drifts between seasons ('Jon' -> 'Jonathan'),
         so the name is not a stable key at all. config/manager_canon.json maps the
         stable member GUID to the canonical name — the same bridge analysis/lib.py
         uses for the draft side. Prefer it; fall back to the collapsed scraped name.
    """
    canon = canon or {}
    out = {}
    for m in payload.get("members") or []:
        mid = str(m.get("id") or "")
        raw = " ".join(x for x in (m.get("firstName"), m.get("lastName")) if x)
        name = " ".join(raw.split()) or (m.get("displayName") or "")
        out[mid] = canon.get(mid) or canon.get(mid.upper()) or name
    return out


def detect_week(payload) -> int:
    st = payload.get("status") or {}
    for key in ("currentMatchupPeriod", "latestScoringPeriod"):
        try:
            w = int(st.get(key) or 0)
            if w > 0:
                return w
        except (TypeError, ValueError):
            continue
    try:
        return max(1, int(payload.get("scoringPeriodId") or 1))
    except (TypeError, ValueError):
        return 1


def team_entries(payload, profile: LeagueProfile, canon=None):
    """The `teams` block minus rosters/needs/tendencies, which are filled later."""
    members = manager_names(payload, canon)
    out = []
    for t in payload.get("teams") or []:
        tid = int(t.get("id"))
        owners = t.get("owners") or []
        rec = (t.get("record") or {}).get("overall") or {}
        tc = t.get("transactionCounter") or {}
        entry = {
            "team_id": tid,
            "name": (t.get("name")
                     or " ".join(x for x in (t.get("location"), t.get("nickname")) if x).strip()
                     or f"Team {tid}"),
            "manager": members.get(str(owners[0])) if owners else "",
            "record": {"w": int(rec.get("wins") or 0), "l": int(rec.get("losses") or 0),
                       "t": int(rec.get("ties") or 0)},
            "points_for": round(float(rec.get("pointsFor") or 0.0), 1),
            "roster": [],
        }
        if profile.acquisition.is_faab:
            spent = tc.get("acquisitionBudgetSpent")
            budget = int(profile.acquisition.budget or 0)
            entry["faab_left"] = max(0, budget - int(spent)) if spent is not None else budget
            entry["waiver_priority"] = None
        else:
            entry["faab_left"] = None
            entry["waiver_priority"] = t.get("waiverRank")
        out.append(entry)
    return out


def roster_entries(payload):
    """{player_id: team_id} straight off mRoster — the authoritative ownership."""
    out = {}
    for t in payload.get("teams") or []:
        for e in ((t.get("roster") or {}).get("entries") or []):
            pl = (e.get("playerPoolEntry") or {}).get("player") or {}
            if pl.get("id") is not None:
                out[int(pl["id"])] = int(t.get("id"))
    return out


# ── the player pool, valued by the league's own scoring ──────────────────────
def build_pool(ctx, inp: Inputs, payload, profile: LeagueProfile, week: int):
    """`{player_id: record}` in the WS-4 `players` shape, plus the raw entries.

    Players come from `engine.valuation.build_players`, which carries this league's
    per-position scoring overrides — without them every D/ST is mis-scored, and
    D/ST is a position all three leagues start and stream.
    """
    raw = read_json(ctx.raw(f"players_wk{week}.json"))
    used = f"players_wk{week}.json"
    if raw is None:
        found = sorted(glob.glob(ctx.raw("players_wk*.json")))
        if found:
            raw = read_json(found[-1])
            used = os.path.basename(found[-1])
            inp.note(f"no players_wk{week}.json — using {used}; ownership, form and "
                     f"the free-agent pool may be a week stale")
    pro_teams = read_json(ctx.raw("pro_teams.json")) or {}
    if not pro_teams:
        inp.note("no raw/pro_teams.json — bye weeks unknown, so a bye-week filler "
                 "cannot be told apart from a duplicate starter")

    entries = []
    if raw is not None:
        entries = raw.get("players") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            inp.note(f"{used} is not in the documented kona_player_info shape — ignored")
            entries = []
    if not entries:
        inp.note("no usable raw/players_wk*.json — falling back to the rostered "
                 "players inside league_full.json; the waiver board will be empty")
        entries = [e for t in (payload.get("teams") or [])
                   for e in ((t.get("roster") or {}).get("entries") or [])]
    else:
        print(f"  • pool: {len(entries):,} players from {used}")

    overrides = valuation.position_overrides(payload)
    if overrides:
        print(f"  • scoring: {len(overrides)} position override table(s) applied "
              f"(D/ST and friends score differently here)")
    else:
        inp.note("no per-position scoring overrides found in the settings — if this "
                 "league uses them, D/ST valuation is wrong")

    # Per-week projections. One source of truth: attaching them to the Player here
    # makes waivers, trades and the lineup planner all read the same weekly numbers,
    # because every one of them reduces to lineup.points_in(week).
    weekly = read_json(ctx.raw("weekly_proj.json"))
    if weekly and weekly.get("proj"):
        wk_list = weekly.get("weeks") or []
        inp.note(f"weekly projections: {len(wk_list)} weeks "
                 f"({wk_list[0] if wk_list else '?'}-{wk_list[-1] if wk_list else '?'}), "
                 f"{len(weekly['proj'].get(str(wk_list[0]), {})) if wk_list else 0} players/week")
    else:
        inp.note("no weekly_proj.json — every player falls back to a FLAT season rate, "
                 "so the lineup planner cannot tell one week from another "
                 "(run: python3 scraping/scrape_weekly_proj.py)", degraded=True)
    proj_weeks = [int(w) for w in ((weekly or {}).get("weeks") or [])]
    inp.proj_weeks = proj_weeks          # published as league.proj_weeks
    players = valuation.build_players(entries, profile, week, pro_teams=pro_teams,
                                      overrides=overrides, league_payload=payload,
                                      weekly_proj=weekly)
    by_entry = {}
    for e in entries:
        p = valuation.player_of(e)
        if p.get("id") is not None:
            by_entry[int(p["id"])] = e

    owners = roster_entries(payload)
    season = int(profile.season or ctx.season)
    tables, out, policy_applied = {}, {}, set()
    for pid, pl in players.items():
        entry = by_entry.get(pid)
        pos_id = int((valuation.player_of(entry) or {}).get("defaultPositionId") or 0)
        if pos_id not in tables:
            tables[pos_id] = valuation.effective_scoring(profile, overrides, pos_id)
        scoring = tables[pos_id]
        actual = valuation.actual_season_points(entry, profile, season, scoring)
        played = [w for w in range(1, int(week)) if w != (pl.bye_week or 0)]
        last3, got = 0.0, 0
        for w in sorted(played, reverse=True)[:3]:
            v = valuation.week_points(entry, profile, season, w, scoring=scoring)
            if v is None:
                continue
            last3 += float(v)
            got += 1
        owner = owners.get(pid, getattr(pl, "owner_team_id", None))
        # The one seam where a projection could be bent. engine.projection_policy
        # refuses any adjustment that has not beaten the baseline out of sample in
        # at least two leagues, so with an empty registry this is the untouched
        # league-scored vendor projection — which is the correct default, not a
        # placeholder. Routing through it makes that structural instead of a promise.
        ros, applied = projection_policy.adjusted_points(
            float(getattr(pl, "ros_points", 0.0)), player=pl, profile=profile)
        policy_applied.update(applied)
        gr = max(0, int(getattr(pl, "games_remaining", 0) or 0))
        out[str(pid)] = {
            "id": pid, "name": pl.name, "pos": pl.pos, "team": pl.team,
            "eligible_slots": sorted(pl.eligible_slots),
            "owner_team_id": owner,
            "injury": pl.injury or "ACTIVE",
            "bye": pl.bye_week,
            "ros_points": round(ros, 1),
            "ros_ppg": round(ros / gr, 2) if gr else 0.0,
            "actual_ppg": round(actual / len(played), 2) if played else 0.0,
            "last3_ppg": round(last3 / got, 2) if got else None,
            "pct_owned": round(float(getattr(pl, "pct_owned", 0.0)), 1),
            "pct_owned_change": round(float(getattr(pl, "pct_owned_change", 0.0)), 2),
            "mkt": {},
        }
        # Per-week projections as a compact array aligned to league.proj_weeks, so the
        # frontend's lineup solver reads the SAME numbers the waiver and trade engines
        # do. A dict keyed by week string would roughly triple this.
        wp = getattr(pl, "week_points", None)
        if wp and proj_weeks:
            out[str(pid)]["wk"] = [round(float(wp.get(w, 0.0)), 1) for w in proj_weeks]

    refused = projection_policy.refused()
    print("  • projections: "
          + ("baseline, unmodified" if not policy_applied
             else "adjusted by " + ", ".join(sorted(policy_applied)))
          + (f"; {len(refused)} registered adjustment(s) REFUSED" if refused else ""))
    for name, why in refused:
        inp.note(f"projection adjustment `{name}` refused: {why}", degraded=False)
    return out


# ── external sources: advisory only, joined espn_id -> norm -> dst_key ───────
def merge_sources(ctx, inp: Inputs, players: dict):
    files = sorted(glob.glob(ctx.raw(os.path.join("sources", "*.json"))))
    if not files:
        inp.note("no raw/sources/*.json — no external rankings, usage or trending "
                 "data; rival demand falls back to roster need alone")
        return []

    by_espn, by_norm, by_dst = {}, {}, {}
    for p in players.values():
        by_espn[int(p["id"])] = p
        by_norm.setdefault(norm_name(p.get("name")), p)
        k = dst_key(p.get("name"), p.get("pos"))
        if k:
            by_dst.setdefault(k, p)
    total_value = sum(float(p.get("ros_points") or 0) for p in players.values())

    order = {n: i for i, n in enumerate(SOURCE_PREFERENCE)}
    files.sort(key=lambda f: (order.get(os.path.splitext(os.path.basename(f))[0],
                                        len(order)), f))
    report = []
    for path in files:
        name = os.path.splitext(os.path.basename(path))[0]
        data = read_json(path)
        if not isinstance(data, dict):
            inp.note(f"source {name}: unreadable — skipped")
            continue
        if not data.get("ok", True):
            inp.note(f"source {name}: reported down ({data.get('error')}) — skipped")
            report.append({"name": name, "ok": False, "error": data.get("error")})
            continue
        records = data.get("records") or []
        matched, matched_value, by_key = 0, 0.0, {"espn_id": 0, "norm": 0, "dst": 0}
        for r in records:
            p = None
            if r.get("espn_id") is not None:
                try:
                    p = by_espn.get(int(r["espn_id"]))
                except (TypeError, ValueError):
                    p = None
                if p is not None:
                    by_key["espn_id"] += 1
            if p is None:
                p = by_norm.get(r.get("norm") or norm_name(r.get("name")))
                if p is not None:
                    by_key["norm"] += 1
            if p is None:
                k = dst_key(r.get("name"), r.get("pos"))
                p = by_dst.get(k) if k else None
                if p is not None:
                    by_key["dst"] += 1
            if p is None:
                continue
            matched += 1
            matched_value += float(p.get("ros_points") or 0)
            fields = r.get("fields") or {}
            if fields.get("bye") and not p.get("bye"):
                p["bye"] = fields["bye"]
            for k in MKT_KEYS:
                if k in fields and k not in p["mkt"] and fields[k] is not None:
                    p["mkt"][k] = fields[k]
        share = pct(matched_value, total_value)
        cov = data.get("coverage") or {}
        print(f"  • source {name}: matched {matched}/{len(records)} "
              f"= {share:.0%} of the value pool "
              f"(espn_id {by_key['espn_id']}, norm {by_key['norm']}, dst {by_key['dst']})"
              + (f"  [{data.get('scoring')}]" if data.get("scoring") else ""))
        if records and matched == 0:
            inp.note(f"source {name}: 0/{len(records)} joined — the join key found "
                     f"nothing (expected when building from the fixture)")
        report.append({"name": name, "ok": True, "matched": matched,
                       "total": len(records), "pct_of_pool": round(share, 3),
                       "adapter_coverage": cov.get("pct"), "scoring": data.get("scoring")})
    have = {k for p in players.values() for k in p["mkt"]}
    for k in ("ros_ecr", "trend_adds", "trade_value", "snap_share", "tier"):
        if k not in have:
            inp.note(f"no source supplied `{k}` — that signal is missing from "
                     f"rival-demand estimation")
    return report


# ── calibration ──────────────────────────────────────────────────────────────
def load_runs(ctx, inp: Inputs, payload, profile: LeagueProfile):
    """Measure this league's waiver RUNS from its own transaction history.

    This is the strongest signal the waiver engine has, and it is descriptive —
    counted, not fitted — so it is allowed to set `p_contested` where a fitted
    per-player model was refused.
    """
    tx = read_json(ctx.raw("transactions.json"))
    if tx is None:
        inp.note("no raw/transactions.json — the waiver-run calendar cannot be "
                 "measured, so contest odds fall back to the season-wide base rate "
                 "and no run-timing advice is possible")
        return None
    acq = ((payload or {}).get("settings") or {}).get("acquisitionSettings") or {}
    cal = build_run_calendar(tx, acq)
    if not cal.total_runs:
        inp.note("transactions.json carried no processed waiver runs — no run "
                 "calendar")
        return None
    print(run_summary(cal))
    if not cal.offset_stable:
        inp.note("the league's timezone is ambiguous from its own data — a "
                 "neighbouring UTC offset would reassign runs to different days, "
                 "so treat the per-day split as indicative")
    thin = [d.day.title() for d in cal.by_day.values() if 0 < d.runs < 20]
    if thin:
        inp.note(f"thin run history on {', '.join(thin)} (<20 runs) — those daily "
                 f"rates are shrunk toward the league rate and should not be read "
                 f"as precise")
    return cal


def load_tendencies(ctx, inp: Inputs):
    data = read_json(ctx.config("season_tendencies.json"))
    if data is None:
        inp.note("no config/season_tendencies.json — every manager falls back to the "
                 "neutral prior and is flagged borrowed")
        return {}, {}, {}
    default = data.get("_league_default") or {}
    labels = (read_json(ctx.config("league.json")) or {}).get("manager_labels") or {}
    meta = data.get("_meta") or {}
    if meta.get("seasons"):
        print(f"  • tendencies: seasons {meta['seasons']}, method "
              f"{meta.get('method', 'unstated')}")
    return data, default, labels


def load_curve(ctx, inp: Inputs, profile: LeagueProfile):
    name = "faab_curve.json" if profile.acquisition.is_faab else "priority_curve.json"
    curve = read_json(ctx.config(name))
    if curve is None:
        inp.note(f"no config/{name} — no measured base rate or price history for "
                 f"this league, so bids/claims fall back to the opponent model and "
                 f"are labelled UNVALIDATED in every rationale")
        return None
    val = curve.get("validation") or {}
    try:
        beats = float(val["holdout_mae"]) < float(val["baseline_mae"])
    except (KeyError, TypeError, ValueError):
        beats = None
    if beats is None:
        inp.note(f"{name} carries no usable out-of-sample validation — used, but "
                 f"unvalidated")
    else:
        print(f"  • {name}: {curve.get('n_claims', '?')} claims, holdout MAE "
              f"{val.get('holdout_mae')} vs baseline {val.get('baseline_mae')} — "
              f"{'beats' if beats else 'DOES NOT BEAT'} baseline")
        if not beats:
            inp.note(f"{name} does not beat its baseline out of sample — only its "
                     f"measured base rate and week-by-week prices are used; no "
                     f"fitted per-player curve is applied")
    return curve


# ── the build ────────────────────────────────────────────────────────────────
def build(ctx, week=None, use_fixture=False, limit_waivers=25, limit_trades=10,
          max_candidates=40, max_priced=15):
    print(f"\n\033[1m{ctx.key}\033[0m  ({ctx.name}, season {ctx.season}, team #{ctx.team_id})")
    inp = Inputs()
    payload_raw = None if use_fixture else read_json(ctx.raw("league_full.json"))
    fixture = None

    if payload_raw is None:
        fx_path = os.path.join(FIXTURES, f"season_data.{ctx.key}.json")
        fixture = read_json(fx_path)
        if fixture is None:
            print(f"  ! nothing to build from: no raw/league_full.json and no "
                  f"{os.path.relpath(fx_path, ROOT)}")
            return None, inp
        inp.note(("--fixture: built from " if use_fixture else
                  "no raw/league_full.json — falling back to ")
                 + f"{os.path.relpath(fx_path, ROOT)}; league SHAPE is real, players "
                   f"and points are synthetic")
        state0 = SeasonState.from_season_data(fixture)
        for n in state0.notes:
            inp.note(n)
        profile = state0.profile
        wk = int(week or fixture.get("week") or 1)
        teams = [dict(t) for t in fixture.get("teams") or []]
        players = {k: dict(v) for k, v in (fixture.get("players") or {}).items()}
        scoring_label = state0.scoring_label
    else:
        if isinstance(payload_raw, list):
            payload_raw = payload_raw[0] if payload_raw else {}
        profile = LeagueProfile.from_espn(payload_raw)
        wk = int(week) if week else detect_week(payload_raw)
        teams = team_entries(payload_raw, profile, load_manager_canon(ctx))
        players = build_pool(ctx, inp, payload_raw, profile, wk)
        scoring_label = profile.scoring_label
        if not teams:
            print("  ! league_full.json carried no teams — nothing to advise on.")
            return None, inp
        owned = {}
        for p in players.values():
            if p.get("owner_team_id") is not None:
                owned.setdefault(int(p["owner_team_id"]), []).append(int(p["id"]))
        for t in teams:
            t["roster"] = owned.get(t["team_id"], [])
        empty = [t["team_id"] for t in teams if not t["roster"]]
        if empty:
            inp.note(f"teams {empty} have empty rosters in this scrape — their needs "
                     f"and trade fit cannot be modelled")

    src_report = merge_sources(ctx, inp, players)
    runs = load_runs(ctx, inp, payload_raw, profile)
    tendencies, default, labels = load_tendencies(ctx, inp)
    curve = load_curve(ctx, inp, profile)

    borrowed_n = 0
    for t in teams:
        name = labels.get(t.get("manager"), t.get("manager"))
        t["manager"] = name
        own = tendencies.get(name) if tendencies else None
        if own and not str(name or "").startswith("_"):
            t["tendencies"] = own
            t["borrowed"] = bool(own.get("borrowed"))
        else:
            t["tendencies"] = default or t.get("tendencies") or {}
            t["borrowed"] = True
        borrowed_n += 1 if t["borrowed"] else 0
    if tendencies:
        print(f"  • calibration: {len(teams) - borrowed_n}/{len(teams)} managers "
              f"calibrated, {borrowed_n} borrowed")

    me = next((t for t in teams if t["team_id"] == ctx.team_id), teams[0])
    if me["team_id"] != ctx.team_id:
        inp.note(f"team #{ctx.team_id} is not in this payload — advising for "
                 f"#{me['team_id']} instead")

    out = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "week": wk,
        "league": {
            "key": ctx.key, "name": profile.name or ctx.name,
            "season": profile.season or ctx.season, "size": profile.size,
            "scoring_label": scoring_label, "regular_weeks": profile.regular_weeks,
            "playoff_teams": profile.playoff_teams,
            "playoff_weeks": list(profile.playoff_weeks),
            "trade_deadline": profile.trade_deadline_ms, "veto_votes": profile.veto_votes,
            "draft_type": profile.draft_type, "auction_budget": profile.auction_budget,
            "bench": profile.bench, "ir": profile.ir,
            "acquisition": {"model": profile.acquisition.model,
                            "budget": profile.acquisition.budget,
                            "min_bid": profile.acquisition.min_bid,
                            "continuous": profile.acquisition.continuous},
            "proj_weeks": list(getattr(inp, "proj_weeks", []) or []),
            "lineup_slots": {str(k): v for k, v in sorted(profile.starting_slots.items())},
            "slot_names": {str(k): slot_name(k) for k in sorted(profile.starting_slots)},
        },
        "me": {"team_id": me["team_id"], "name": me.get("name"), "manager": me.get("manager")},
        "teams": teams, "players": players,
        "waivers": [], "trades": {"finder": [], "buy_low": [], "sell_high": []},
    }
    if fixture is not None:
        out["fixture"] = True

    state = SeasonState.from_season_data(out, profile=profile)
    odds = state.playoff_odds()
    for t in out["teams"]:
        ts = state.team(t["team_id"])
        t["needs"] = {str(k): v for k, v in state.needs(ts).items()} if ts else {}
        t["playoff_odds"] = odds.get(t["team_id"], 0.0)
    inp.notes.append("playoff_odds is a roster-strength + record estimator, not a "
                     "season simulation — read it as a ranking, not a forecast")


    print(f"  • {profile.size} teams | {scoring_label} | {profile.acquisition.model}"
          f"{' $' + str(profile.acquisition.budget) if profile.acquisition.is_faab else ''}"
          f" | wk {wk} of {profile.regular_weeks} | playoff wks "
          f"{state.post_weeks or '-'} | {len(players):,} players "
          f"({len(state.free_agents):,} free)")

    if runs is not None:
        out["runs"] = runs.to_dict()
    out["waivers"] = waiver_board(state, tendencies=tendencies, curve=curve, runs=runs,
                                  limit=limit_waivers, max_candidates=max_candidates,
                                  max_priced=max_priced)
    out["trades"]["finder"] = find_trades(state, tendencies=tendencies, limit=limit_trades)
    form = buy_low_sell_high(state)
    out["trades"]["buy_low"] = form["buy_low"]
    out["trades"]["sell_high"] = form["sell_high"]
    out["notes"] = inp.notes
    out["sources"] = src_report
    return out, inp


# ── reporting ────────────────────────────────────────────────────────────────
def show(payload, limit=10, horizon_note=True):
    lg = payload["league"]
    players = payload["players"]
    faab = lg["acquisition"]["model"] == "faab"
    slots = "/".join(f"{lg['slot_names'][k]}x{v}" for k, v in lg["lineup_slots"].items())
    print(f"\n  \033[1mTop {limit} waiver targets — {lg['name']}\033[0m")
    print(f"  {lg['size']}tm {lg['scoring_label']} | {slots} | "
          f"{lg['acquisition']['model']} | week {payload['week']}")
    nr = ((payload.get("runs") or {}).get("next_run")) or {}
    if nr:
        rd = payload["runs"]
        print(f"  next run: {str(nr['day']).title()} "
              f"{int(rd.get('process_hour') or 0):02d}:00 "
              f"({nr.get('hours_away')}h) — {float(nr.get('p_contested') or 0):.0%} "
              f"contested over {nr.get('n_runs')} runs"
              + (f", median win ${nr.get('median_win'):.0f} / p80 "
                 f"${nr.get('p80_win'):.0f}" if nr.get("p80_win") is not None else "")
              + (f"  |  quieter: {str(nr['quieter_alternative']).title()}"
                 if nr.get("quieter_alternative") else ""))
    print("  {:<3}{:<26}{:<5}{:>8}{:>9}  ".format("#", "player", "pos", "reg", "playoff")
          + ("{:>6}{:>6}{:>9}".format("bid", "p80", "p(cont)") if faab
             else "{:>8}{:>11}".format("claim", "p(clears)")))
    for i, w in enumerate(payload["waivers"][:limit], 1):
        p = players.get(str(w["player_id"])) or {}
        line = "  {:<3}{:<26}{:<5}{:>8.1f}{:>9.1f}  ".format(
            i, (p.get("name") or "?")[:25], p.get("pos") or "",
            w["marginal_reg"], w["marginal_post"])
        if faab and w.get("bid"):
            b = w["bid"]
            line += "{:>6}{:>6}{:>9.0%}".format(f"${b['suggested']}", f"${b['p80']}",
                                                b["p_contested"])
        elif w.get("claim"):
            c = w["claim"]
            line += "{:>8}{:>11.0%}".format("CLAIM" if c["recommend"] else "wait",
                                            c["p_survives"])
        print(line)
        if i <= 2:
            for why in w.get("why", [])[:4]:
                print(f"        - {why}")

    fin = payload["trades"]["finder"]
    teams = {t["team_id"]: t for t in payload["teams"]}
    print(f"\n  \033[1mTop {min(5, len(fin))} trades\033[0m")

    def names(ids):
        return ", ".join((players.get(str(x)) or {}).get("name", str(x)) for x in ids)

    for i, tr in enumerate(fin[:5], 1):
        partner = teams.get(tr["partner_team_id"], {})
        print(f"  {i}. {partner.get('name', tr['partner_team_id'])} "
              f"({partner.get('manager', '')})"
              f"   you {tr['my_delta']:+.1f} | them {tr['their_delta']:+.1f} | "
              f"accept {tr['accept_odds']:.0%} | veto {tr['veto_risk']:.0%}")
        print(f"       send    {names(tr['send'])}")
        print(f"       receive {names(tr['receive'])}")
        if i <= 2:
            for why in tr.get("why", [])[:3]:
                print(f"        - {why}")
    bl, sh = payload["trades"]["buy_low"], payload["trades"]["sell_high"]
    if bl or sh:
        print("\n  \033[1mForm divergence\033[0m")
        for b in bl[:3]:
            print(f"    BUY  {(players.get(str(b['player_id'])) or {}).get('name')}"
                  f" — {b['why'][1] if len(b['why']) > 1 else b['why'][0]}")
        for s in sh[:3]:
            print(f"    SELL {(players.get(str(s['player_id'])) or {}).get('name')}"
                  f" — {s['why'][1] if len(s['why']) > 1 else s['why'][0]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--league", help="league key (see config/leagues.json)")
    ap.add_argument("--all", action="store_true", help="every registered league")
    ap.add_argument("--week", type=int, help="override the detected week")
    ap.add_argument("--fixture", action="store_true",
                    help="build from tests/fixtures (real shape, synthetic players)")
    ap.add_argument("--show", action="store_true", help="print the board and trades")
    ap.add_argument("--limit", type=int, default=10, help="rows to print with --show")
    ap.add_argument("--out", help="write here instead of ctx.out('season_data.json')")
    args = ap.parse_args()

    if not args.league and not args.all:
        ap.error("pass --league KEY or --all")
    try:
        ctxs = leagues.all() if args.all else [leagues.resolve(args.league)]
    except Exception as e:
        print(f"cannot resolve league: {e}")
        return 2

    rc = 0
    for ctx in ctxs:
        try:
            payload, inp = build(ctx, week=args.week, use_fixture=args.fixture)
        except Exception as e:                    # never take down a multi-league run
            import traceback
            traceback.print_exc()
            print(f"  ! {ctx.key} failed to build: {e}")
            rc = 1
            continue
        if payload is None:
            rc = 1
            continue
        out = args.out or ctx.out("season_data.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w") as f:
            json.dump(payload, f, indent=1)
            f.write("\n")
        print(f"  -> {os.path.relpath(out, ROOT)} ({os.path.getsize(out):,} bytes, "
              f"{len(payload['waivers'])} waiver rows, "
              f"{len(payload['trades']['finder'])} trades)"
              + ("   [DEGRADED — see notes above]" if inp.degraded else ""))
        if args.show:
            show(payload, args.limit)
    return rc


if __name__ == "__main__":
    sys.exit(main())
