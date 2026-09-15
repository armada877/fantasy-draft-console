"""Local-league tools for the research agent — your leagues, teams, tendencies.

Reads only what the repo's pipelines already generate:

  draft_sheets/out/<key>/season_data.json   the in-season payload (league profile,
                                            teams + rosters + merged tendencies,
                                            players, waiver/trade boards)
  ctx.config("tendencies.json")             calibrated AUCTION tendencies
                                            (mult/conc/maxbuy) where the league
                                            drafts by auction

Nothing here hits the network or re-derives modeling — if the payload is stale,
these tools say so and point at `python3 pipeline.py week` rather than guess.
All league-identifying content stays in those local files; this module is
generic and trackable.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import leagues  # noqa: E402

STALE_HOURS = 48


# ── loading ──────────────────────────────────────────────────────────────────
def _resolve(league: str | None):
    ctxs = leagues.all()
    if not ctxs:
        raise RuntimeError("no leagues registered — run `python3 leagues.py` first")
    if not league:
        if len(ctxs) == 1:
            return ctxs[0]
        raise ValueError(
            "which league? options: "
            + "; ".join(f"{c.key} ({c.name!r}, my team {c.team_name!r})" for c in ctxs))
    q = league.lower().strip()
    for c in ctxs:
        if q == c.key or q == str(c.league_id):
            return c
    for c in ctxs:
        if q in c.key or q in c.name.lower() or q in (c.team_name or "").lower():
            return c
    raise ValueError(f"no league matches {league!r}; options: "
                     + ", ".join(c.key for c in ctxs))


def _payload(ctx) -> dict:
    path = ctx.out("season_data.json")
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{ctx.key}: no generated payload at {path} — run the in-season "
            "pipeline for this league first (python3 pipeline.py week)")
    with open(path) as f:
        return json.load(f)


def _freshness(payload: dict) -> str:
    gen = payload.get("generated", "")
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(gen)).total_seconds() / 3600
    except ValueError:
        return f"generated: {gen or 'unknown'}"
    note = f"generated {age_h:.0f}h ago (week {payload.get('week', '?')})"
    if age_h > STALE_HOURS:
        note += " — STALE; numbers predate recent games/waivers. Suggest a re-run."
    return note


def _slot_names(payload: dict) -> dict:
    return payload.get("league", {}).get("slot_names", {})


def _find_team(payload: dict, team: str | None) -> dict:
    teams = payload["teams"]
    if team is None:
        my_id = payload.get("me", {}).get("team_id")
        for t in teams:
            if t["team_id"] == my_id:
                return t
        raise ValueError("my team not found in payload")
    q = str(team).lower().strip()
    for t in teams:
        if q == str(t["team_id"]):
            return t
    for t in teams:
        if q in (t.get("name") or "").lower() or q in (t.get("manager") or "").lower():
            return t
    raise ValueError(
        f"no team matches {team!r}; teams: "
        + "; ".join(f"#{t['team_id']} {t.get('name')} ({t.get('manager')})"
                    for t in teams))


# ── formatting helpers ───────────────────────────────────────────────────────
def _n(v, d=0.0):
    """None-safe number: payload fields can be null in young leagues."""
    return v if isinstance(v, (int, float)) else d


def _week_index(payload: dict) -> int | None:
    """Index into each player's wk[] series for the payload's current week."""
    pw = payload.get("league", {}).get("proj_weeks") or []
    wk = payload.get("week")
    return pw.index(wk) if wk in pw else None


def _this_week(payload: dict, p: dict) -> float | None:
    i = _week_index(payload)
    series = p.get("wk") or []
    return series[i] if i is not None and i < len(series) else None


def _fmt_player(p: dict, wk_pts: float | None = None) -> str:
    mkt = p.get("mkt") or {}
    bits = [f"{p.get('pos', '?'):3s} {p.get('name', '?')} ({p.get('team', '?')})"]
    if wk_pts is not None:
        bits.append(f"THIS WK {wk_pts:.1f}")
    bits += [f"ros {_n(p.get('ros_ppg', 0)):.1f} ppg",
            f"last3 {_n(p.get('last3_ppg', 0)):.1f}"]
    if mkt.get("ros_pos_rank"):
        bits.append(f"ecr {mkt['ros_pos_rank']}")
    if p.get("injury") and p["injury"] != "ACTIVE":
        bits.append(p["injury"])
    if p.get("bye"):
        bits.append(f"bye {p['bye']}")
    return "  ".join(bits)


def _fmt_record(t: dict) -> str:
    r = t.get("record") or {}
    rec = f"{r.get('w', 0)}-{r.get('l', 0)}" + (f"-{r['t']}" if r.get("t") else "")
    acq = (f"FAAB ${t['faab_left']}" if t.get("faab_left") is not None
           else f"waiver priority {t.get('waiver_priority')}")
    return (f"{rec}, {_n(t.get('points_for', 0)):.1f} PF, {acq}, "
            f"playoff odds {_n(t.get('playoff_odds', 0)):.0%}")


def _fmt_season_tendencies(t: dict) -> list[str]:
    td = t.get("tendencies") or {}
    if not td:
        return ["    no in-season tendencies on file"]
    w, tr, dr = td.get("waiver") or {}, td.get("trade") or {}, td.get("drop") or {}
    lines = []
    tag = " [borrowed league default — no personal history]" if td.get("borrowed") else \
          f" [{td.get('n_seasons', '?')} seasons, shrink {td.get('shrink', '?')}]"
    lines.append(f"    calibration:{tag}")
    if w:
        lines.append(
            f"    waivers: aggression {_n(w.get('aggression', 0)):.2f}, "
            f"{_n(w.get('adds_per_season', 0)):.0f} adds/season, wins "
            f"{_n(w.get('contested_win_rate', 0)):.0%} of contested claims, "
            f"{_n(w.get('zero_bid_share', 0)):.0%} $0 bids, max bid ${w.get('max_bid', '?')}")
    if tr:
        flow = tr.get("pos_flow") or {}
        buys = ", ".join(f"{k} {v:+.2f}" for k, v in
                         sorted(flow.items(), key=lambda kv: -abs(kv[1]))[:3])
        lines.append(
            f"    trades: {_n(tr.get('rate_per_season', 0)):.1f}/season, accepts "
            f"{_n(tr.get('accept_rate', 0)):.0%} of offers, "
            f"{'responds' if tr.get('responds') else 'often ignores offers'}; "
            f"position flow (buys +/sells -): {buys or 'n/a'}")
    if dr:
        lines.append(f"    drops: holds fading players ~{_n(dr.get('latency_weeks', 0)):.1f} "
                     "weeks before cutting")
    return lines


# ── the tool functions ───────────────────────────────────────────────────────
def list_leagues() -> str:
    lines = ["Your leagues (from the local registry):", ""]
    for c in leagues.all():
        line = f"- {c.key}: {c.name!r}, season {c.season}, my team {c.team_name!r}"
        try:
            p = _payload(c)
            line += f" — payload {_freshness(p)}"
        except FileNotFoundError:
            line += " — NO generated payload yet"
        lines.append(line)
    lines.append("\nPass the key (e.g. '2kdome') as `league` to the other local tools.")
    return "\n".join(lines)


def league_details(league: str | None = None) -> str:
    ctx = _resolve(league)
    p = _payload(ctx)
    lg = p["league"]
    acq = lg.get("acquisition") or {}
    slots = ", ".join(f"{lg['slot_names'].get(k, k)}×{v}"
                      for k, v in (lg.get("lineup_slots") or {}).items())
    lines = [
        f"League: {lg.get('name')} ({ctx.key}), season {lg.get('season')}, "
        f"{_freshness(p)}",
        f"  {lg.get('size')} teams, scoring {lg.get('scoring_label')}, "
        f"draft {lg.get('draft_type')}"
        + (f" (${lg.get('auction_budget')})" if lg.get("auction_budget") else ""),
        f"  lineup: {slots}; bench {lg.get('bench')}, IR {lg.get('ir')}",
        f"  acquisitions: {acq.get('model')}"
        + (f", budget ${acq.get('budget')}" if acq.get("budget") else "")
        + (f", min bid ${acq.get('min_bid')}" if acq.get("min_bid") is not None else ""),
        f"  regular weeks {lg.get('regular_weeks')}, playoffs "
        f"{lg.get('playoff_teams')} teams in weeks {lg.get('playoff_weeks')}",
    ]
    lines.append("\nStandings (by playoff odds):")
    for t in sorted(p["teams"], key=lambda t: -t.get("playoff_odds", 0)):
        me = " ← me" if t["team_id"] == p["me"]["team_id"] else ""
        lines.append(f"  #{t['team_id']:>2} {t.get('name')} ({t.get('manager')}): "
                     f"{_fmt_record(t)}{me}")
    for n in p.get("notes") or []:
        lines.append(f"note: {n}")
    return "\n".join(lines)


def team_details(league: str | None = None, team: str | None = None) -> str:
    ctx = _resolve(league)
    p = _payload(ctx)
    t = _find_team(p, team)
    me = t["team_id"] == p["me"]["team_id"]
    snames = _slot_names(p)
    players = p["players"]
    lines = [f"{'MY TEAM — ' if me else ''}{t.get('name')} "
             f"(manager {t.get('manager')}, #{t['team_id']}) in {ctx.key} — "
             f"{_freshness(p)}",
             f"  {_fmt_record(t)}"]
    needs = t.get("needs") or {}
    if needs:
        lines.append("  needs (starter-slot gaps): "
                     + ", ".join(f"{snames.get(k, k)}: {v}" for k, v in needs.items()))
    lines.append("  roster:")
    roster = [players[str(pid)] for pid in t.get("roster") or []
              if str(pid) in players]
    for pl in sorted(roster, key=lambda x: (x.get("pos", ""), -_n(x.get("ros_ppg", 0)))):
        lines.append("    " + _fmt_player(pl, _this_week(p, pl)))
    lines.append("  (THIS WK = this week's projection — the start/sit number; "
                 "ros = rest-of-season rate, the trade/waiver value number)")
    lines.append("  in-season tendencies:")
    lines += _fmt_season_tendencies(t)
    return "\n".join(lines)


def opponent_tendencies(league: str | None = None, manager: str | None = None) -> str:
    """The cached opponent modeling: in-season (waivers/trades/drops) for every
    manager, plus calibrated auction tendencies where the league has them."""
    ctx = _resolve(league)
    p = _payload(ctx)
    teams = p["teams"]
    if manager:
        q = manager.lower()
        teams = [t for t in teams
                 if q in (t.get("manager") or "").lower()
                 or q in (t.get("name") or "").lower()]
        if not teams:
            raise ValueError(f"no manager matches {manager!r}")

    auction = {}
    tpath = ctx.config("tendencies.json")
    if os.path.exists(tpath):
        with open(tpath) as f:
            auction = json.load(f)

    lines = [f"Opponent tendency model — {ctx.key}, {_freshness(p)}",
             "(calibrated from this league's own history; 'borrowed' = no "
             "personal history, league-average assumed)", ""]
    for t in sorted(teams, key=lambda t: t["team_id"]):
        me = " ← me" if t["team_id"] == p["me"]["team_id"] else ""
        lines.append(f"#{t['team_id']} {t.get('name')} — {t.get('manager')}{me}")
        lines += _fmt_season_tendencies(t)
        a = auction.get(t.get("manager") or "")
        if a:
            mult = ", ".join(f"{k} {v:.2f}" for k, v in sorted((a.get("mult") or {}).items()))
            lines.append(f"    auction (draft-day): pays {mult} of projected value; "
                         f"concentration {a.get('conc')} (stars-and-scrubs ↑), "
                         f"max single buy ${a.get('maxbuy')}")
        lines.append("")
    if auction and "_league_default" in auction:
        d = auction["_league_default"]
        mult = ", ".join(f"{k} {v:.2f}" for k, v in sorted((d.get("mult") or {}).items()))
        lines.append(f"league default (auction): {mult}; conc {d.get('conc')}, "
                     f"maxbuy ${d.get('maxbuy')}")
    if not auction:
        lines.append("(no auction tendencies file for this league — it does not "
                     "draft by auction, or calibration hasn't run)")
    return "\n".join(lines)


def console_boards(league: str | None = None, board: str = "waivers",
                   limit: int = 8) -> str:
    """The in-season console's precomputed recommendations, verbatim from the
    generated payload: 'waivers' (bid targets), 'trades' (finder), 'buy_low',
    'sell_high'."""
    ctx = _resolve(league)
    p = _payload(ctx)
    players, teams = p["players"], {t["team_id"]: t for t in p["teams"]}

    def pname(pid):
        pl = players.get(str(pid)) or {}
        return f"{pl.get('name', pid)} ({pl.get('pos', '?')})"

    board = board.lower().strip()
    rows = (p.get("waivers") if board == "waivers"
            else p.get("trades", {}).get("finder") if board == "trades"
            else p.get("trades", {}).get(board))
    if rows is None:
        raise ValueError("board must be one of: waivers, trades, buy_low, sell_high")
    lines = [f"{ctx.key} console board: {board} — {_freshness(p)}", ""]
    if not rows:
        lines.append("(board is empty in the current payload)")
    for row in rows[: max(1, int(limit))]:
        if board == "waivers":
            bid = row.get("bid") or {}
            head = (f"- ADD {pname(row.get('player_id'))}"
                    + (f", drop {pname(row['drop_player_id'])}"
                       if row.get("drop_player_id") else "")
                    + (f" — suggested bid ${bid.get('suggested')}"
                       f" (p80 ${bid.get('p80')}, contested "
                       f"{bid.get('p_contested', 0):.0%})" if bid else ""))
        elif board == "trades":
            partner = teams.get(row.get("partner_team_id"), {})
            head = (f"- with {partner.get('name')} ({partner.get('manager')}): send "
                    + " + ".join(pname(x) for x in row.get("send") or [])
                    + " for " + " + ".join(pname(x) for x in row.get("receive") or [])
                    + f" — me {row.get('my_delta', 0):+.1f}, them "
                      f"{row.get('their_delta', 0):+.1f}, "
                      f"P(accept) {row.get('accept_odds', 0):.0%}")
        else:
            owner = teams.get(row.get("owner_team_id"), {})
            head = (f"- {pname(row.get('player_id'))} — owner {owner.get('name')} "
                    f"({owner.get('manager')})")
        lines.append(head)
        for why in row.get("why") or []:
            lines.append(f"    · {why}")
    return "\n".join(lines)


# ── weekly lineup (rosters lock Tue–Tue: set by THIS week's projection) ───────
def weekly_lineup_data(league: str | None = None, team: str | None = None) -> dict:
    """Optimal legal lineup for the payload's current week, via the engine's
    Hungarian assignment (2QB/superflex-safe). Structured for the UI."""
    from engine.lineup import optimal_lineup
    from engine.profile import slot_name
    from engine.state import SeasonState

    ctx = _resolve(league)
    payload = _payload(ctx)
    profile = ctx.profile()
    wk = payload.get("week")
    t_raw = _find_team(payload, team)
    state = SeasonState.from_season_data(payload, profile=profile)
    ts = state.team(t_raw["team_id"])
    if ts is None:
        raise ValueError(f"team {t_raw['team_id']} not in engine state")
    total, lineup, bench = optimal_lineup(ts.roster, profile, wk)

    players_raw = payload["players"]

    def row(pl):
        raw = players_raw.get(str(pl.id), {})
        return {"name": pl.name, "pos": pl.pos,
                "team": raw.get("team") or "",
                "wk_pts": round(pl.points_in(wk), 1),
                "ros_ppg": round(_n(raw.get("ros_ppg")), 1),
                "last3": round(_n(raw.get("last3_ppg")), 1),
                "injury": (raw.get("injury") or "").replace("ACTIVE", ""),
                "bye": raw.get("bye") == wk}

    slots = []
    for slot_id, players in lineup.items():
        for pl in players:
            slots.append({"slot": slot_name(slot_id), **row(pl)})
    bench_rows = sorted((row(pl) for pl in bench), key=lambda r: -r["wk_pts"])
    return {"league": ctx.key, "week": wk,
            "team": {"id": t_raw["team_id"], "name": t_raw.get("name"),
                     "manager": t_raw.get("manager"),
                     "mine": t_raw["team_id"] == payload["me"]["team_id"]},
            "total": round(total, 1), "slots": slots, "bench": bench_rows,
            "freshness": _freshness(payload)}


def weekly_lineup(league: str | None = None, team: str | None = None) -> str:
    d = weekly_lineup_data(league, team)
    lines = [f"Optimal WEEK {d['week']} lineup — {d['team']['name']} "
             f"({'my team' if d['team']['mine'] else d['team']['manager']}) in "
             f"{d['league']}, by this week's projections ({d['freshness']})",
             f"projected total: {d['total']} pts", ""]
    for r in d["slots"]:
        inj = f" [{r['injury']}]" if r["injury"] else ""
        bye = " [BYE]" if r["bye"] else ""
        lines.append(f"  {r['slot']:>6}  {r['name']} ({r['pos']}, {r['team']})"
                     f"  wk {r['wk_pts']}{inj}{bye}  (ros {r['ros_ppg']}, "
                     f"last3 {r['last3']})")
    lines.append("  bench:")
    for r in d["bench"]:
        inj = f" [{r['injury']}]" if r["injury"] else ""
        bye = " [BYE]" if r["bye"] else ""
        lines.append(f"          {r['name']} ({r['pos']})  wk {r['wk_pts']}"
                     f"{inj}{bye}  (ros {r['ros_ppg']}, last3 {r['last3']})")
    lines.append("\nRosters lock weekly (Tue–Tue): this board is by THIS "
                 "week's projection. Close calls (within ~2 pts) deserve a "
                 "matchup/news/weather check before lock.")
    return "\n".join(lines)
