#!/usr/bin/env python3
"""League-generic transaction history loaders for the in-season tendency pipeline.

`analysis/lib.py` is the canonical loader set, but it is hard-wired to the ORIGINAL
single-league layout (`scraping/raw/{season}/`) and to that league's
`config/manager_canon.json`. It is also read-only (the draft console's regression test
guards its output), so this module does not change it — it *delegates* to it for the
legacy league and mirrors its semantics, path-for-path off `LeagueContext`, for every
other league. Guardrail 4 (each league is a separate context) is the reason the
delegation is conditional: `lib.MANAGER_CANON` is the legacy league's identity table and
must never be applied to another league's GUIDs.

What it gives you:
    available_seasons(ctx)            seasons with a scraped league_full.json
    transaction_seasons(ctx)          ... that also have a non-empty transactions.json
    profile(ctx, season)              engine.profile.LeagueProfile for that season
    managers(ctx, season)             {team_id: canonical manager name}
    acquisitions(ctx, season)         normalized add/claim events (see Acq below)
    contests(ctx, season)             reconstructed waiver contests (winner + losers)
    drops(ctx, season)                normalized drop events with add->drop latency
    trades(ctx, season)               executed trades + offers that died
    quality(ctx, season)              {player_id: outcome percentile within position}

Two data facts this module encodes, both verified against the 12-team league 2018-2025:

1. **Attribute a transaction from its ADD item, not its `teamId`.** 2018's export has
   `teamId = -2147483648` on all 134 EXECUTED waiver rows; the item-level `toTeamId` is
   intact. Reading the top-level field would silently drop a whole season of winners.
2. **A waiver "run" is a processDate bucket, not a scoring period.** Grouping the
   EXECUTED + outbid rows for one player by a 10-minute `processDate` bucket yields
   exactly one winner in every one of the 12-team league's 2,075 contests; grouping by
   `scoringPeriodId` merges re-adds in the same week and breaks 3 of them.
"""
from __future__ import annotations

import bisect
import collections
import dataclasses
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.profile import LeagueProfile  # noqa: E402

# ── ESPN transaction vocabulary ──────────────────────────────────────────────
# The one status that means "another team outbid/outranked me for this player".
# Everything else that FAILED is mechanical (roster full, player already gone, IR
# slot, position limit, budget exceeded) and must be excluded from contest stats.
OUTBID = "FAILED_INVALIDPLAYERSOURCE"
# In priority-order seasons ESPN files the losing claimants under the acquisition-limit
# status instead; verified on the 12-team league 2018 (111 rows, always alongside one EXECUTED row
# for the same player in the same run).
OUTRANKED = "FAILED_MATCHUPACQUISITIONLIMIT"
LOSER_STATUSES = (OUTBID, OUTRANKED)
RUN_BUCKET_MS = 600_000          # 10 minutes: one waiver processing run

_CACHE: dict = {}


def _cached(key, fn):
    if key not in _CACHE:
        _CACHE[key] = fn()
    return _CACHE[key]


def _norm_name(s: str) -> str:
    return " ".join((s or "").split())


# ── season-addressable contexts ──────────────────────────────────────────────
def season_ctx(ctx, season: int):
    """The same league addressed at a different season (paths follow)."""
    if int(season) == int(ctx.season):
        return ctx
    return dataclasses.replace(ctx, season=int(season))


def settings_seasons(ctx) -> list:
    """Seasons with a scraped league_full.json — i.e. seasons whose RULES we know."""
    def scan():
        base = os.path.dirname(season_ctx(ctx, 1).raw_dir)   # .../raw or .../raw/<key>
        out = []
        if os.path.isdir(base):
            for name in os.listdir(base):
                if name.isdigit() and os.path.exists(
                        os.path.join(base, name, "league_full.json")):
                    out.append(int(name))
        return sorted(out)
    return _cached(("settings_seasons", ctx.key), scan)


def bundle_seasons(ctx) -> list:
    """Seasons carried inside the multi-season transactions bundle.

    These may have NO league_full.json of their own — WS-1's bundle backfills
    transactions for chi-phi-american 2022-2025 and inlaws-outlaws 2025 without
    backfilling those seasons' settings, rosters or player outcomes. They are real
    history and must not be thrown away, but everything derived from them has to say
    which facts were known and which were assumed.
    """
    def scan():
        p = ctx.raw("transactions.json")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            d = json.load(f)
        if not isinstance(d, dict):
            return []
        counts = d.get("counts") or {}
        return sorted(int(k) for k, v in counts.items() if v)
    return _cached(("bundle_seasons", ctx.key), scan)


def available_seasons(ctx) -> list:
    """Every season this league has ANY data for, ascending."""
    return sorted(set(settings_seasons(ctx)) | set(bundle_seasons(ctx)))


def has_settings(ctx, season) -> bool:
    return bool(load_league(ctx, season))


def transaction_seasons(ctx) -> list:
    """Seasons with actual transaction rows. the 12-team league's 2013-2017 files exist but are
    empty lists — ESPN's historical endpoint does not backfill them — so 'the file is
    there' is not the same question as 'there is history here'."""
    def scan():
        out = []
        for yr in available_seasons(ctx):
            if load_transactions(ctx, yr):
                out.append(yr)
        return out
    return _cached(("txn_seasons", ctx.key), scan)


# ── raw payloads ─────────────────────────────────────────────────────────────
def load_league(ctx, season) -> dict:
    def rd():
        p = season_ctx(ctx, season).raw("league_full.json")
        if not os.path.exists(p):
            return {}
        with open(p) as f:
            d = json.load(f)
        return (d[0] if d else {}) if isinstance(d, list) else d
    return _cached(("league", ctx.key, int(season)), rd)


def profile(ctx, season):
    """LeagueProfile for that season, or None when its settings were never scraped."""
    def build():
        d = load_league(ctx, season)
        return LeagueProfile.from_espn(d) if d else None
    return _cached(("profile", ctx.key, int(season)), build)


def profile_or_current(ctx, season):
    """(profile, exact) — that season's profile, else the current season's.

    `exact=False` means the shape (size, budget, schedule) is ASSUMED to have been the
    same as today's. Callers must surface that, never bury it.
    """
    p = profile(ctx, season)
    if p is not None:
        return p, True
    return profile(ctx, ctx.season), False


def _rows_for(payload, season) -> list:
    """Normalize either shape of transactions.json down to a list of rows for `season`.

    The original per-season export is a bare LIST. WS-1's export is a multi-season BUNDLE
    — `{league_id, fetched, seasons, counts, unavailable_seasons,
    transactions:[{... "seasonId": N}]}` — written into the current season's directory.
    Both are live on disk right now (the 12-team league's archived per-season files predate the
    bundle), so every read goes through here; picking one shape would silently drop
    either eight seasons of history or the live season.
    """
    if isinstance(payload, dict):
        rows = payload.get("transactions") or []
        return [t for t in rows if int(t.get("seasonId") or season) == int(season)]
    return payload or []


def _read_txn_file(path, season) -> list:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return _rows_for(json.load(f), season)


def load_transactions(ctx, season) -> list:
    """Rows for one season, from whichever file has them.

    Order: lib's loader (legacy league, so the draft side and this side read the exact
    same bytes) -> that season's own file -> the current season's multi-season bundle.
    """
    def rd():
        if ctx.is_legacy:                       # reuse lib's loader
            import lib
            got = _rows_for(lib.load_transactions(season), season)
            if got:
                return got
        got = _read_txn_file(season_ctx(ctx, season).raw("transactions.json"), season)
        if got:
            return got
        return _read_txn_file(ctx.raw("transactions.json"), season)
    return _cached(("txn", ctx.key, int(season)), rd)


def load_playercards(ctx, season) -> list:
    def rd():
        if ctx.is_legacy:
            import lib
            return lib.load_playercards(season)
        p = season_ctx(ctx, season).raw("playercards.json")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return json.load(f)
    return _cached(("cards", ctx.key, int(season)), rd)


def load_players_raw(ctx, season) -> list:
    def rd():
        p = season_ctx(ctx, season).raw("players.json")
        if not os.path.exists(p):
            return []
        with open(p) as f:
            d = json.load(f)
        return d.get("players", []) if isinstance(d, dict) else d
    return _cached(("players", ctx.key, int(season)), rd)


# ── manager identity ─────────────────────────────────────────────────────────
def _team_owner_scraped(ctx, season) -> dict:
    def build():
        if ctx.is_legacy and load_league(ctx, season):
            import lib
            return lib.team_owner(season)
        d = load_league(ctx, season)
        out = {}
        for t in d.get("teams", []):
            owners = t.get("owners") or []
            out[t["id"]] = {
                "name": t.get("name") or f"{t.get('location','')} {t.get('nickname','')}".strip(),
                "owner": owners[0] if owners else None,
                "owners": owners,
            }
        return out
    return _cached(("owners_scraped", ctx.key, int(season)), build)


def identity_source(ctx, season) -> str:
    """How a season's team -> manager mapping was obtained.

    "scraped"  that season's own league_full.json named the owners.
    "assumed"  it did not, so the CURRENT season's franchise map was reused. ESPN team
               ids are per-franchise and stable (verified: chi-phi-american is 1-8 in
               every season 2022-2026; inlaws-outlaws 2025's 1-12 are a subset of
               2026's 1-14), so this is usually right — but "usually" is not "known",
               and anything built on it is flagged all the way to the artifact.
    "none"     no map at all; those events are dropped rather than guessed.
    """
    if _team_owner_scraped(ctx, season):
        return "scraped"
    return "assumed" if _team_owner_scraped(ctx, ctx.season) else "none"


def team_owner(ctx, season) -> dict:
    """{team_id: {'name','owner'(GUID),'owners'}} — lib.team_owner's shape."""
    own = _team_owner_scraped(ctx, season)
    return own if own else _team_owner_scraped(ctx, ctx.season)


def guid_names(ctx) -> dict:
    """{memberGUID: 'First Last'} merged over EVERY scraped season of THIS league.

    lib.member_names() merges over lib.ALL_SEASONS, which stops at 2025 — so the
    current season's members (two brand-new the 12-team league managers in 2026) resolve to a bare
    GUID through it. Merging over `available_seasons(ctx)` fixes that without touching
    lib, and keeps co-owners resolvable the way lib intends.
    """
    def build():
        out = {}
        for yr in settings_seasons(ctx):
            for mem in load_league(ctx, yr).get("members", []):
                nm = _norm_name(f"{mem.get('firstName','')} {mem.get('lastName','')}")
                if mem.get("id") and nm:
                    out[mem["id"]] = nm
        return out
    return _cached(("guids", ctx.key), build)


def _canon(ctx) -> dict:
    """{GUID: canonical name}. The legacy league's table lives in lib (shared with the
    draft console, so identity cannot drift); other leagues keep their own in their own
    config dir. Never cross-applied — guardrail 4."""
    def build():
        if ctx.is_legacy:
            import lib
            return dict(lib.MANAGER_CANON)
        p = ctx.config("manager_canon.json")
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        return {}
    return _cached(("canon", ctx.key), build)


def _labels(ctx) -> dict:
    """config league.json `manager_labels` — rename a manager on the board. The draft
    console merges tendencies on the RELABELLED name (CLAUDE.md), so we must too."""
    def build():
        p = ctx.config("league.json")
        if os.path.exists(p):
            with open(p) as f:
                return (json.load(f).get("manager_labels") or {})
        return {}
    return _cached(("labels", ctx.key), build)


def manager(ctx, season, team_id):
    """Canonical manager name for a team in a season, or None if unknowable.

    GUID first (survives a display-name drift — 'Jon' vs 'Jonathan'), then the merged
    member table, then the scraped owner name, then the team name. Relabelled last.
    Returns None rather than inventing a "team#5" placeholder: a fabricated manager
    would silently become a row in the tendencies artifact.
    """
    info = team_owner(ctx, season).get(team_id)
    if not info:
        return None
    guid = info.get("owner")
    name = (_canon(ctx).get(guid)
            or guid_names(ctx).get(guid)
            or _norm_name(info.get("ownerName") or "")
            or _norm_name(info.get("name") or ""))
    if not name:
        return None
    return _labels(ctx).get(name, name)


def managers(ctx, season) -> dict:
    """{team_id: canonical manager name}. Teams, not the members array — the members
    array carries co-owners and departed managers and would over-count the league."""
    out = {}
    for tid in team_owner(ctx, season):
        nm = manager(ctx, season, tid)
        if nm:
            out[tid] = nm
    return out


# ── normalized events ────────────────────────────────────────────────────────
def _add_item(t):
    for i in t.get("items") or []:
        if i.get("type") == "ADD":
            return i
    return None


def _team_of(t):
    """Attribute to the ADD item's destination; fall back to the transaction's own
    teamId. See the 2018 `teamId = INT_MIN` note at the top of this module."""
    it = _add_item(t)
    if it and (it.get("toTeamId") or 0) > 0:
        return it["toTeamId"]
    tid = t.get("teamId") or 0
    return tid if tid > 0 else None


def _run_key(t):
    return (t.get("processDate") or t.get("proposedDate") or 0) // RUN_BUCKET_MS


Acq = collections.namedtuple(
    "Acq", "season week team manager player bid won contested channel status")


def contests(ctx, season) -> list:
    """Waiver contests for a season, one entry per (player, waiver run):

        {'week','player','winner':Acq|None,'losers':[Acq],'n_bidders':int}

    Mechanical failures are dropped before grouping, so `n_bidders` counts only teams
    that actually competed. The losing rows are the whole point — ESPN records them, and
    they are the only direct evidence of what a claim was worth to the rest of the room.
    """
    def build():
        rows = []
        for t in load_transactions(ctx, season):
            if t.get("type") != "WAIVER" or t.get("executionType") != "PROCESS":
                continue
            st = t.get("status")
            if st != "EXECUTED" and st not in LOSER_STATUSES:
                continue                      # mechanical failure — not a contest
            it = _add_item(t)
            if not it:
                continue
            tid = _team_of(t)
            rows.append(Acq(int(season), t.get("scoringPeriodId") or 0, tid,
                            manager(ctx, season, tid) if tid else None,
                            it.get("playerId"), int(t.get("bidAmount") or 0),
                            st == "EXECUTED", True, "waiver", st))
        g = collections.defaultdict(list)
        for r, t in zip(rows, [t for t in load_transactions(ctx, season)
                               if t.get("type") == "WAIVER"
                               and t.get("executionType") == "PROCESS"
                               and (t.get("status") == "EXECUTED"
                                    or t.get("status") in LOSER_STATUSES)
                               and _add_item(t)]):
            g[(r.player, _run_key(t))].append(r)
        out = []
        for (pid, _), group in g.items():
            wins = [r for r in group if r.won]
            out.append({"week": group[0].week, "player": pid,
                        "winner": wins[0] if len(wins) == 1 else None,
                        "losers": [r for r in group if not r.won],
                        "n_bidders": len(group)})
        out.sort(key=lambda c: (c["week"], c["player"]))
        return out
    return _cached(("contests", ctx.key, int(season)), build)


def acquisitions(ctx, season) -> list:
    """Every successful add: won waiver claims + straight free-agent pickups.

    Both count as churn (`adds_per_season`); only the waiver ones carry a price.
    """
    def build():
        out = [c["winner"] for c in contests(ctx, season) if c["winner"]]
        contested = {(c["winner"].player, c["week"]) for c in contests(ctx, season)
                     if c["winner"] and c["n_bidders"] > 1}
        out = [r._replace(contested=((r.player, r.week) in contested)) for r in out]
        for t in load_transactions(ctx, season):
            if t.get("type") != "FREEAGENT" or t.get("status") != "EXECUTED":
                continue
            it = _add_item(t)
            tid = _team_of(t)
            if not it or not tid:
                continue
            out.append(Acq(int(season), t.get("scoringPeriodId") or 0, tid,
                           manager(ctx, season, tid), it.get("playerId"),
                           0, True, False, "freeagent", "EXECUTED"))
        return out
    return _cached(("acq", ctx.key, int(season)), build)


def drops(ctx, season) -> list:
    """{'week','team','manager','player','held_weeks'|None} for every drop.

    `held_weeks` is the gap back to the most recent acquisition of that player by that
    same team in the same season — undefined (None) for players the team drafted or
    acquired before the transaction log starts, which are excluded from latency.
    """
    def build():
        last_add = {}
        events = []
        for t in load_transactions(ctx, season):
            if t.get("executionType") == "CANCEL" or t.get("status") not in (
                    "EXECUTED", None):
                continue
            wk = t.get("scoringPeriodId") or 0
            when = t.get("processDate") or t.get("proposedDate") or 0
            for i in t.get("items") or []:
                if i.get("type") in ("ADD", "DRAFT") and (i.get("toTeamId") or 0) > 0:
                    events.append((when, "add", wk, i["toTeamId"], i.get("playerId")))
                elif i.get("type") == "DROP" and (i.get("fromTeamId") or 0) > 0:
                    events.append((when, "drop", wk, i["fromTeamId"], i.get("playerId")))
        events.sort()
        out = []
        for when, kind, wk, tid, pid in events:
            if kind == "add":
                last_add[(tid, pid)] = wk
            else:
                held = last_add.pop((tid, pid), None)
                out.append({"week": wk, "team": tid,
                            "manager": manager(ctx, season, tid), "player": pid,
                            "held_weeks": (wk - held) if held is not None else None})
        return out
    return _cached(("drops", ctx.key, int(season)), build)


def active_teams(ctx, season) -> set:
    """Team ids that actually transacted in a season.

    With an ASSUMED identity map the current season's franchises are projected back, so
    an expansion team would otherwise be credited with a season it did not play — and
    would come out looking calibrated on zero events. This is the guard.
    """
    def build():
        out = set()
        for t in load_transactions(ctx, season):
            if (t.get("teamId") or 0) > 0:
                out.add(t["teamId"])
            for i in t.get("items") or []:
                for k in ("toTeamId", "fromTeamId"):
                    if (i.get(k) or 0) > 0:
                        out.add(i[k])
        return out
    return _cached(("active", ctx.key, int(season)), build)


def trade_detail(ctx, season) -> str:
    """"playercards" | "transactions" | "none" — how much of a trade we can see.

    Executed trades only carry their item detail (who sent whom) in playercards.json:
    the `TRADE_ACCEPT` rows in transactions.json hold the losing side's roster trims, or
    nothing at all. WS-1 has scraped playercards for the 12-team league only, so for the other two
    leagues we can count trades and see who accepted, but not what moved.
    """
    if load_playercards(ctx, season):
        return "playercards"
    rows = load_transactions(ctx, season)
    if any(t.get("type") in ("TRADE_ACCEPT", "TRADE_PROPOSAL") for t in rows):
        return "transactions"
    return "none"


def trades(ctx, season) -> dict:
    """{'executed': [...], 'offers_died': [...], 'detail': str}.

    Executed trades come from playercards (lib.executed_trades — the only source with
    full item detail; `TRADE_ACCEPT` rows in transactions.json carry the roster-trim
    DROPs, not the traded players). Offers that died are the `TRADE_PROPOSAL` rows,
    whose player sets do NOT match any executed trade — ESPN consumes a proposal when it
    is accepted — so the two sets partition cleanly into "taken" and "not taken".
    """
    def build():
        detail = trade_detail(ctx, season)
        if detail == "playercards":
            if ctx.is_legacy:
                import lib
                ex = lib.executed_trades(season)
            else:
                ex = _executed_trades_generic(ctx, season)
        else:
            # No item detail: a TRADE_ACCEPT row still tells us a trade happened and who
            # accepted it. Recorded with empty sides so no caller mistakes it for the
            # full picture — `detail` is the flag to branch on.
            ex = [{"sp": t.get("scoringPeriodId"),
                   "date": t.get("proposedDate"), "sides": {},
                   "acceptor": t.get("teamId"), "pids": []}
                  for t in load_transactions(ctx, season)
                  if t.get("type") == "TRADE_ACCEPT"]
        for tr in ex:
            tr["managers"] = {tm: manager(ctx, season, tm) for tm in tr["sides"]}
            if tr.get("acceptor"):
                tr["acceptor_manager"] = manager(ctx, season, tr["acceptor"])
        taken = {frozenset(tr["pids"]) for tr in ex}
        died = []
        for t in load_transactions(ctx, season):
            if t.get("type") != "TRADE_PROPOSAL":
                continue
            items = [i for i in t.get("items") or [] if i.get("type") == "TRADE"]
            if not items or frozenset(i["playerId"] for i in items) in taken:
                continue
            proposer = t.get("teamId")
            sides = collections.defaultdict(list)
            for i in items:
                sides[i["toTeamId"]].append(i["playerId"])
            died.append({"week": t.get("scoringPeriodId") or 0,
                         "proposer": proposer,
                         "targets": [tm for tm in sides if tm != proposer],
                         "sides": dict(sides),
                         "status": t.get("status")})
        return {"executed": ex, "offers_died": died, "detail": detail}
    return _cached(("trades", ctx.key, int(season)), build)


def _executed_trades_generic(ctx, season) -> list:
    """lib.executed_trades() for a non-legacy league (same parse, ctx paths)."""
    seen = {}
    for card in load_playercards(ctx, season):
        for t in card.get("transactions", []):
            if t.get("type") != "TRADE_ACCEPT" or not t.get("items"):
                continue
            moves = [(i["playerId"], i["fromTeamId"], i["toTeamId"])
                     for i in t["items"] if i.get("type") == "TRADE"]
            if not moves:
                continue
            key = (t.get("acceptedDate") or t.get("proposedDate"), tuple(sorted(moves)))
            if key in seen:
                continue
            sides = collections.defaultdict(list)
            for pid, _ft, tt in moves:
                sides[tt].append(pid)
            seen[key] = {"sp": t.get("scoringPeriodId"),
                         "date": t.get("acceptedDate") or t.get("proposedDate"),
                         "sides": dict(sides), "pids": [m[0] for m in moves]}
    return list(seen.values())


# ── outcome quality ──────────────────────────────────────────────────────────
def player_positions(ctx, season) -> dict:
    from engine.profile import position_name
    return _cached(("ppos", ctx.key, int(season)), lambda: {
        e["player"]["id"]: position_name(e["player"].get("defaultPositionId"))
        for e in load_players_raw(ctx, season) if e.get("player", {}).get("id")})


def season_points(ctx, season) -> dict:
    """{player_id: season actual fantasy points, scored with THIS league's settings}.

    Guardrail 6: recomputed from the raw `stats` map x `profile.scoring` rather than
    trusting `appliedTotal`, so a settings change propagates. Falls back to
    `appliedTotal` for the handful of rows with no raw stats.
    """
    def build():
        prof, _exact = profile_or_current(ctx, season)
        scoring = prof.scoring if prof else {}
        out = {}
        for e in load_players_raw(ctx, season):
            p = e.get("player") or {}
            pid = p.get("id")
            if pid is None:
                continue
            pts = 0.0
            for s in p.get("stats") or []:
                if s.get("statSourceId") != 0 or s.get("statSplitTypeId") != 0:
                    continue
                raw = s.get("stats") or {}
                if raw:
                    pts = sum(float(v or 0) * scoring.get(int(k), 0.0)
                              for k, v in raw.items() if str(k).lstrip("-").isdigit())
                else:
                    pts = float(s.get("appliedTotal") or 0)
            out[pid] = pts
        return out
    return _cached(("spts", ctx.key, int(season)), build)


def quality(ctx, season) -> dict:
    """{player_id: 0..1} — how good the player turned out to be, POST HOC.

    Percentile of the player's season fantasy points among everyone at his position who
    scored at all that season. This is an outcome measure, not a name/ADP prior, which
    is what the curve needs.

    **Known limitation, stated rather than hidden:** ESPN's archived player payload
    carries season totals only — no weekly splits, in players.json or playercards.json —
    so this cannot be narrowed to production *after* the add date. For a week-1 pickup
    the two are nearly the same thing; for a week-13 pickup the percentile is inflated by
    production the adding team never got. The curve bins by week, which absorbs most of
    that, and `faab_curve.py` reports what it is worth out of sample either way.
    """
    def build():
        pos = player_positions(ctx, season)
        pts = season_points(ctx, season)
        pools = collections.defaultdict(list)
        for pid, v in pts.items():
            if v > 0:
                pools[pos.get(pid, "?")].append(v)
        for k in pools:
            pools[k].sort()
        out = {}
        for pid, v in pts.items():
            arr = pools.get(pos.get(pid, "?")) or [0.0]
            out[pid] = bisect.bisect_left(arr, v) / max(1, len(arr) - 1)
        return out
    return _cached(("qual", ctx.key, int(season)), build)


def quality_reference(ctx, seasons) -> dict:
    """{position: [points at decile 0.0, 0.1, ... 1.0]} averaged over `seasons`.

    Emitted with the curve so a consumer can turn a live rest-of-season projection into
    the same percentile the curve was fitted on, without re-deriving the pool.
    """
    acc = collections.defaultdict(list)
    for yr in seasons:
        pos, pts = player_positions(ctx, yr), season_points(ctx, yr)
        pools = collections.defaultdict(list)
        for pid, v in pts.items():
            if v > 0:
                pools[pos.get(pid, "?")].append(v)
        for k, arr in pools.items():
            arr.sort()
            acc[k].append([arr[min(len(arr) - 1, int(d / 10 * (len(arr) - 1)))]
                           for d in range(11)])
    return {k: [round(sum(c) / len(c), 1) for c in zip(*v)] for k, v in acc.items() if v}
