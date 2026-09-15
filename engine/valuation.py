#!/usr/bin/env python3
"""Rest-of-season valuation — raw ESPN player payloads -> engine.lineup.Player.

This is the bridge between what the platform sends and what the decision engine
consumes. Everything here is generic over `LeagueProfile`: scoring comes from the
league's own `scoringItems`, the horizons come from its `matchupPeriodCount`, and
a player's eligibility comes from his own `eligibleSlots`. No position list, no
team count, no week count is written down.

Stat decoding (verified live; the entry id is `{statSourceId}{statSplitTypeId}{season}`):

    src0 split0    season actual to date
    src1 split0    FULL-SEASON projection (live, ESPN updates it weekly)
    src1 split1    week projection, current scoring period only
    src1 split2    NOT rest-of-season. Gibbs' split2 347 exceeds his full-season
                   338, so it cannot be "what is left". DO NOT USE.

Future-week projections are not retrievable at all — `scoringPeriodId=2/5/10` all
answer HTTP 400 — so rest-of-season is a subtraction, not a sum:

    ros_points = projection(src1, split0) - actual(src0, split0)
    points_per_game = ros_points / games_remaining        # bye-week aware

`appliedTotal` already reproduces exactly when the raw `stats` dict is multiplied
by the scraped scoring map (checked to 1e-9 on all three real leagues), but we
recompute anyway so a mid-season settings change propagates instead of silently
leaving the valuation on ESPN's cached number. Same rule the draft builder follows.

Two horizons, because they are different questions and most tools conflate them:

    regular_weeks(profile, current_week)   the playoff race
    playoff_weeks(profile)                 the fantasy playoffs themselves
"""
from __future__ import annotations

from .lineup import Player
from .profile import LeagueProfile, position_name

# statSourceId / statSplitTypeId, as ESPN encodes them.
SRC_ACTUAL = 0
SRC_PROJECTED = 1
SPLIT_SEASON = 0
SPLIT_WEEK = 1
SPLIT_UNUSABLE = 2        # see module docstring — never read this one

# Injury states that zero a player out for the week in progress. Anything else
# (QUESTIONABLE, PROBABLE, ...) still plays, so it must not be zeroed.
OUT_STATUSES = frozenset({"OUT", "INJURY_RESERVE", "SUSPENSION", "NOT_ACTIVE"})

FREE_AGENT = 0            # ESPN's onTeamId for an unrostered player


# ── raw payload access ───────────────────────────────────────────────────────
def player_of(entry):
    """Accept either a kona `players[]` entry, an mRoster entry, or a bare player."""
    if not isinstance(entry, dict):
        return {}
    if "player" in entry and isinstance(entry["player"], dict):
        return entry["player"]
    ppe = entry.get("playerPoolEntry")
    if isinstance(ppe, dict) and isinstance(ppe.get("player"), dict):
        return ppe["player"]
    return entry


def _entries(raw_players):
    """Normalise the shapes scrape_season may hand us into a list of kona entries."""
    if isinstance(raw_players, dict):
        raw_players = raw_players.get("players") or []
    return [e for e in (raw_players or []) if isinstance(e, dict)]


def stat_entry(entry, source, split, season, week=None):
    """The one `stats[]` element matching (statSourceId, statSplitTypeId, season).

    `week` additionally pins scoringPeriodId, which only means anything for
    split=SPLIT_WEEK. Returns None when the platform did not send it.
    """
    for s in player_of(entry).get("stats") or []:
        if int(s.get("statSourceId", -1)) != source:
            continue
        if int(s.get("statSplitTypeId", -1)) != split:
            continue
        if season is not None and int(s.get("seasonId", 0) or 0) != int(season):
            continue
        if week is not None and int(s.get("scoringPeriodId", 0) or 0) != int(week):
            continue
        return s
    return None


# ── scoring ──────────────────────────────────────────────────────────────────
def position_overrides(league_payload) -> dict:
    """`{positionId: {statId: points}}` from each scoring item's `pointsOverrides`.

    CONTRACT GAP, reported not worked around silently: `LeagueProfile.scoring` is a
    flat `{statId: points}` and ESPN's scoring is not flat. Every league on this
    account overrides the D/ST position (id 16) — points-allowed tiers, sacks,
    takeaways all carry `points: 0.0` with the real value in
    `pointsOverrides["16"]`. Scoring a D/ST off `profile.scoring` alone is wrong for
    32 players per league (measured); with the overrides applied, every player in
    all three leagues reproduces `appliedTotal` exactly.

    Until `LeagueProfile` can carry this, pass the result to `build_players`.
    """
    payload = league_payload[0] if isinstance(league_payload, list) and league_payload \
        else league_payload
    items = (((payload or {}).get("settings") or {}).get("scoringSettings") or {}) \
        .get("scoringItems") or []
    out = {}
    for it in items:
        sid = it.get("statId")
        if sid is None:
            continue
        for pos_id, pts in (it.get("pointsOverrides") or {}).items():
            out.setdefault(int(pos_id), {})[int(sid)] = float(pts)
    return out


def effective_scoring(profile: LeagueProfile, overrides=None, position_id=None) -> dict:
    """The `{statId: points}` that actually applies to a player at `position_id`."""
    if not overrides or position_id is None:
        return profile.scoring
    extra = overrides.get(int(position_id))
    if not extra:
        return profile.scoring
    merged = dict(profile.scoring)
    merged.update(extra)
    return merged


def scoring_points(raw_stats: dict, profile: LeagueProfile, scoring: dict | None = None) -> float:
    """Fantasy points for a raw ESPN `stats` dict under THIS league's scoring.

    `raw_stats` is `{statId(as str): amount}`; the scoring table is
    `{statId(as int): points}` — `profile.scoring` unless `scoring` overrides it
    (see `effective_scoring`, needed for D/ST). Reproduces ESPN's `appliedTotal`
    exactly on all three leagues, PPR and half-PPR alike — but it is recomputed
    rather than trusted so a scoring change takes effect the moment it is scraped.
    """
    if not raw_stats:
        return 0.0
    table = profile.scoring if scoring is None else scoring
    total = 0.0
    for stat_id, pts in table.items():
        if not pts:
            continue
        amount = raw_stats.get(str(stat_id))
        if amount is None:
            amount = raw_stats.get(stat_id)
        if amount:
            total += float(amount) * float(pts)
    return total


def _scored(entry, profile, source, split, season, week=None, scoring=None):
    s = stat_entry(entry, source, split, season, week)
    if s is None:
        return None
    return scoring_points(s.get("stats") or {}, profile, scoring)


def projected_season_points(entry, profile: LeagueProfile, season, scoring=None) -> float:
    """src1/split0 — ESPN's live full-season projection, rescored for this league."""
    v = _scored(entry, profile, SRC_PROJECTED, SPLIT_SEASON, season, scoring=scoring)
    return 0.0 if v is None else v


def actual_season_points(entry, profile: LeagueProfile, season, scoring=None) -> float:
    """src0/split0 — what he has actually scored so far, rescored for this league."""
    v = _scored(entry, profile, SRC_ACTUAL, SPLIT_SEASON, season, scoring=scoring)
    return 0.0 if v is None else v


def week_points(entry, profile: LeagueProfile, season, week, projected=False, scoring=None):
    """One scoring period. Actual for a completed week, projection for the current
    one. Returns None when ESPN did not send that week (i.e. it is in the future —
    future-week projections are not retrievable, see the module docstring)."""
    src = SRC_PROJECTED if projected else SRC_ACTUAL
    return _scored(entry, profile, src, SPLIT_WEEK, season, week, scoring=scoring)


def ros_points(player_entry, profile: LeagueProfile, season, scoring=None) -> float:
    """Rest-of-season fantasy points: full-season projection minus season-to-date.

    Never negative: a player who has already beaten his own full-season projection
    is not a liability, he is simply un-forecastable from this signal.
    """
    proj = projected_season_points(player_entry, profile, season, scoring)
    actual = actual_season_points(player_entry, profile, season, scoring)
    return max(0.0, proj - actual)


# ── horizons ─────────────────────────────────────────────────────────────────
def final_week(profile: LeagueProfile) -> int:
    """Last scoring period that counts for this league (regular season + playoffs)."""
    post = profile.playoff_weeks
    return max([profile.regular_weeks] + list(post)) if (post or profile.regular_weeks) else 0


def season_weeks(profile: LeagueProfile) -> list:
    """Every scoring period this league plays, derived from its own schedule."""
    return list(range(1, final_week(profile) + 1))


def remaining_weeks(profile: LeagueProfile, current_week: int) -> list:
    """Weeks still to be played, INCLUDING the one in progress.

    This is the denominator `ros_points` is spread over: the projection-minus-actual
    subtraction leaves exactly the points still to come, across regular season and
    fantasy playoffs alike.
    """
    return [w for w in season_weeks(profile) if w >= int(current_week)]


def regular_weeks(profile: LeagueProfile, current_week: int) -> list:
    """Remaining REGULAR-season weeks — the playoff race horizon."""
    return [w for w in remaining_weeks(profile, current_week) if w <= profile.regular_weeks]


def playoff_weeks(profile: LeagueProfile) -> list:
    """The fantasy playoff weeks. A different question from the regular season and
    it must not be conflated with it: a team out of the race buys these weeks, a
    team in it buys the other ones."""
    return list(profile.playoff_weeks)


def games_remaining(player, profile: LeagueProfile, current_week: int) -> int:
    """How many of the remaining weeks this player can actually play.

    Accounts for the bye week and for any known out-weeks. Accepts an
    engine.lineup.Player, a raw ESPN entry, or a plain dict.
    """
    weeks = set(remaining_weeks(profile, current_week))
    if not weeks:
        return 0
    bye = _bye_of(player)
    if bye is not None:
        weeks.discard(int(bye))
    for w in _out_weeks_of(player):
        weeks.discard(int(w))
    return len(weeks)


def _bye_of(player):
    for attr in ("bye_week", "byeWeek", "bye"):
        v = getattr(player, attr, None) if not isinstance(player, dict) else player.get(attr)
        if v:
            return int(v)
    return None


def _out_weeks_of(player):
    v = getattr(player, "out_weeks", None) if not isinstance(player, dict) else player.get("out_weeks")
    return v or ()


# ── payload -> engine.Player ─────────────────────────────────────────────────
def owner_team_id(player) -> int | None:
    """The team holding this player, None when he is a free agent. Stored on
    `Player.owner` (the dataclass's documented 'who holds him' field) and mirrored
    onto `.owner_team_id` for callers that want the platform-flavoured name."""
    return getattr(player, "owner", None)


def build_players(raw_players, profile: LeagueProfile, current_week: int,
                  pro_teams: dict | None = None, season: int | None = None,
                  overrides: dict | None = None,
                  league_payload=None, weekly_proj: dict | None = None) -> dict:
    """`{player_id: engine.lineup.Player}` from a scraped kona_player_info payload.

    - `eligible_slots` comes straight off the platform's `eligibleSlots`, so 2QB,
      superflex/OP, K and D/ST leagues all work without a branch here.
    - `points_per_game` is `ros_points / games_remaining` — the rate the lineup
      solver wants, already carrying the bye.
    - `pro_teams` is `espn_client.pro_teams(season)`; without it byes are unknown
      (the player object does not carry one) and every week counts.
    - `overrides` is `position_overrides(league_full.json)`, or pass
      `league_payload=` and it is derived here. WITHOUT IT EVERY D/ST IS
      MIS-SCORED — see `position_overrides` for why.
    - `weekly_proj` is `scrape_weekly_proj`'s `{"<week>": {"<pid>": pts}}`. Attaching
      it here is what keeps WAIVERS, TRADES and the LINEUP consistent: all three
      reduce to `lineup_points_over`, which reads `Player.points_in(week)`, so one
      source of per-week truth feeds every decision. Without it each player falls
      back to a flat season rate and no consumer can tell week 3 from week 11.
    """
    season = int(season if season is not None else profile.season)
    # JSON round-trips dict keys to strings; accept either so callers can pass the
    # saved pro_teams.json straight through.
    pro_teams = {int(k): v for k, v in (pro_teams or {}).items()}
    # invert {week: {pid: pts}} -> {pid: {week: pts}} once, not per player
    by_player = {}
    for wk, vals in ((weekly_proj or {}).get("proj") or weekly_proj or {}).items():
        try:
            w = int(wk)
        except (TypeError, ValueError):
            continue
        if not isinstance(vals, dict):
            continue
        for pid, pts in vals.items():
            by_player.setdefault(int(pid), {})[w] = float(pts)
    if overrides is None and league_payload is not None:
        overrides = position_overrides(league_payload)
    # One merged scoring table per position, not per player — the D/ST override
    # would otherwise rebuild a 46-entry dict a thousand times.
    tables = {}
    out = {}
    for entry in _entries(raw_players):
        p = player_of(entry)
        pid = p.get("id")
        if pid is None:
            continue
        pro = pro_teams.get(int(p.get("proTeamId") or 0)) or {}
        injury = (p.get("injuryStatus") or "").upper()
        owner = entry.get("onTeamId")
        owner = int(owner) if owner else None
        if owner == FREE_AGENT:
            owner = None

        pl = Player(
            id=int(pid),
            name=p.get("fullName") or " ".join(
                f"{p.get('firstName', '')} {p.get('lastName', '')}".split()),
            pos=position_name(p.get("defaultPositionId") or 0),
            eligible_slots=frozenset(int(s) for s in (p.get("eligibleSlots") or [])),
            points_per_game=0.0,
            bye_week=pro.get("bye"),
            out_weeks=frozenset({int(current_week)}) if injury in OUT_STATUSES else frozenset(),
            team=pro.get("abbrev") or "",
            injury=injury,
            owner=owner,
        )
        pos_id = int(p.get("defaultPositionId") or 0)
        if pos_id not in tables:
            tables[pos_id] = effective_scoring(profile, overrides, pos_id)
        ros = ros_points(entry, profile, season, tables[pos_id])
        gr = games_remaining(pl, profile, current_week)
        pl.points_per_game = (ros / gr) if gr > 0 else 0.0
        wp = by_player.get(int(pid))
        if wp:
            pl.week_points = wp
        # Carried alongside the engine fields so downstream builders can emit the
        # platform's own vocabulary without re-deriving it.
        pl.ros_points = ros
        pl.games_remaining = gr
        pl.owner_team_id = owner
        pl.status = entry.get("status") or ""
        pl.pct_owned = float(((p.get("ownership") or {}).get("percentOwned")) or 0.0)
        pl.pct_owned_change = float(((p.get("ownership") or {}).get("percentChange")) or 0.0)
        out[int(pid)] = pl
    return out


def free_agents(players) -> list:
    """The add pool: everyone no team is holding."""
    vals = players.values() if isinstance(players, dict) else players
    return [p for p in vals if owner_team_id(p) is None]


def team_roster(players, team_id) -> list:
    """Every player a given team holds."""
    vals = players.values() if isinstance(players, dict) else players
    return [p for p in vals if owner_team_id(p) == int(team_id)]
