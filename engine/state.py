#!/usr/bin/env python3
"""In-season state — the shared adapter the decision engines run on.

`waivers.py` and `trades.py` ask the same questions of the same objects: who is on
each roster, who is free, what are the two horizons, what does the league pay for a
starter at this slot. Building that twice would be two places to get bye handling
wrong, so it lives here once.

Nothing in this module knows a league fact. Horizons come from
`profile.regular_weeks` / `profile.playoff_weeks`; rosters come from the payload;
eligibility comes from each player's own `eligible_slots`. The same code path runs
an 8-team 2QB PPR league and a 14-team priority-order league.

Two horizons, always kept apart (plan 3b): the rest of the regular season is the
playoff *race*; the fantasy playoff weeks are the *prize*. A player who wins you
week 16 and a player who wins you week 6 are different recommendations, so they are
never averaged into one number.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .lineup import (Player, lineup_points_over, optimal_lineup,
                     starter_replacement_level)
from .profile import Acquisition, LeagueProfile, FAAB, PRIORITY

# Platform injury statuses that mean "will not play". DOUBTFUL/QUESTIONABLE are
# deliberately absent: they are a discount, not a zero, and the projection already
# carries the discount.
OUT_STATUSES = frozenset({"OUT", "INJURY_RESERVE", "SUSPENSION", "NOT_ACTIVE"})

EPS = 1e-9


# ── players ──────────────────────────────────────────────────────────────────
def to_player(raw: dict, week: int | None = None, proj_weeks=None) -> Player:
    """A `season_data.json` player record -> the engine's Player.

    `out_weeks` carries the current week only when the platform says he is out;
    we do not speculate about future weeks we cannot see.

    `proj_weeks` is `league.proj_weeks`; with it, the record's compact `wk` array
    becomes `week_points` and every downstream decision reads REAL per-week
    projections. WITHOUT IT every player silently collapses to one flat season rate
    — which is how the waiver board and the trade engine ended up disagreeing with
    the lineup planner even though all three call the same solver.
    """
    out = set()
    if week is not None and str(raw.get("injury") or "").upper() in OUT_STATUSES:
        out.add(int(week))
    bye = raw.get("bye")
    return Player(
        id=int(raw.get("id")),
        name=raw.get("name") or "",
        pos=raw.get("pos") or "",
        eligible_slots=frozenset(int(s) for s in (raw.get("eligible_slots") or ())),
        points_per_game=float(raw.get("ros_ppg") or 0.0),
        bye_week=int(bye) if bye else None,
        week_points=({int(w): float(v) for w, v in zip(proj_weeks, raw.get("wk") or [])}
                     if proj_weeks and raw.get("wk") else {}),
        out_weeks=frozenset(out),
        team=raw.get("team") or "",
        injury=raw.get("injury") or "",
        owner=raw.get("owner_team_id"),
    )


@dataclass
class TeamState:
    """One roster plus everything that constrains what its manager can do next."""
    team_id: int
    name: str
    manager: str
    roster: list                       # [Player]
    faab_left: int | None = None
    waiver_priority: int | None = None
    tendencies: dict = field(default_factory=dict)
    borrowed: bool = True              # assume borrowed until calibration says otherwise
    record: dict = field(default_factory=dict)
    points_for: float = 0.0
    playoff_odds: float | None = None

    # ── calibrated behaviour, with documented fallbacks ──────────────────────
    @property
    def waiver_tendencies(self) -> dict:
        return (self.tendencies or {}).get("waiver") or {}

    @property
    def trade_tendencies(self) -> dict:
        return (self.tendencies or {}).get("trade") or {}

    @property
    def aggression(self) -> float:
        """Waiver aggression, 1.0 = league-average. Absent calibration, average."""
        try:
            return max(0.0, float(self.waiver_tendencies.get("aggression", 1.0)))
        except (TypeError, ValueError):
            return 1.0

    def claims_per_week(self, profile: LeagueProfile) -> float:
        """How many players this manager actually picks up in a typical week.

        This is the constraint that stops every rival wanting every player: a
        manager with one claim a week cannot be a 40% threat on ten players at
        once. Calibrated `adds_per_season` when WS-3 has it, spread over the
        league's own regular season; otherwise his relative aggression, where
        1.0 is the league-average manager.
        """
        adds = self.waiver_tendencies.get("adds_per_season")
        weeks = int(profile.regular_weeks or 0)
        try:
            if adds is not None and weeks > 0:
                return max(0.0, float(adds) / weeks)
        except (TypeError, ValueError):
            pass
        return self.aggression

    def can_bid(self, profile: LeagueProfile) -> bool:
        """Can this team still make a claim at all?

        In FAAB, a team is out only when it cannot afford the league's minimum bid —
        which in a $0-minimum league is never, so a broke team still claims for free.
        """
        acq = profile.acquisition
        if acq.model != FAAB:
            return True
        if self.faab_left is None:
            return True
        return self.faab_left >= acq.min_bid

    def budget(self, profile: LeagueProfile) -> int:
        if self.faab_left is not None:
            return int(self.faab_left)
        return int(profile.acquisition.budget or 0)


@dataclass
class SeasonState:
    """Everything the engines read. One league, one moment in the season."""
    profile: LeagueProfile
    week: int
    teams: list                        # [TeamState]
    players: dict                      # {player_id: Player}
    raw: dict                          # {player_id: original record} — mkt/form fields
    me_id: int
    scoring_label: str = ""
    key: str = ""
    name: str = ""
    notes: list = field(default_factory=list)

    # ── horizons ─────────────────────────────────────────────────────────────
    @property
    def reg_weeks(self) -> list:
        """The rest of the regular season, inclusive of the current week."""
        last = int(self.profile.regular_weeks or 0)
        return [w for w in range(int(self.week), last + 1)]

    @property
    def post_weeks(self) -> list:
        """The fantasy playoff weeks, straight off the profile."""
        return [w for w in self.profile.playoff_weeks if w >= int(self.week)]

    # ── lookups ──────────────────────────────────────────────────────────────
    def team(self, team_id):
        for t in self.teams:
            if t.team_id == team_id:
                return t
        return None

    @property
    def my_team(self) -> TeamState:
        return self.team(self.me_id) or self.teams[0]

    @property
    def opponents(self) -> list:
        return [t for t in self.teams if t.team_id != self.me_id]

    @property
    def rostered(self) -> list:
        return [p for t in self.teams for p in t.roster]

    @property
    def free_agents(self) -> list:
        owned = {id(p) for t in self.teams for p in t.roster}
        return [p for p in self.players.values() if id(p) not in owned]

    @property
    def pool(self) -> list:
        """Every player the league can field — the replacement-level denominator."""
        return list(self.players.values())

    def replacement(self) -> dict:
        """{slot_id: ppg of the last started player league-wide}."""
        return starter_replacement_level(self.pool, self.profile)

    def replacement_for(self, player: Player, reps: dict | None = None) -> float:
        """Replacement PPG at the best starting slot this player is eligible for."""
        reps = reps if reps is not None else self.replacement()
        vals = [reps[int(s)] for s in self.profile.starting_slots
                if int(s) in player.eligible_slots and int(s) in reps]
        return max(vals) if vals else 0.0

    def replacement_floor(self, player: Player, reps: dict | None = None) -> float:
        """Replacement PPG at the EASIEST starting slot he is eligible for.

        The screening bar: the lowest wall he has to clear to start anywhere. Using
        the highest one instead is how a board ends up listing forty quarterbacks —
        quarterbacks out-score everyone in absolute terms, so ranking free agents by
        raw points buries every flex piece who would actually improve a lineup. That
        is the same "raw points, not marginal" error the engine exists to avoid,
        just committed one step earlier.
        """
        reps = reps if reps is not None else self.replacement()
        vals = [reps[int(s)] for s in self.profile.starting_slots
                if int(s) in player.eligible_slots and int(s) in reps]
        return min(vals) if vals else 0.0

    def horizon_value(self, player: Player, weeks, reps: dict | None = None) -> float:
        """What a replacement starter at this player's slot produces over `weeks`.

        This is the engine's unit of 'meaningful'. An add worth a full replacement
        season is enormous; one worth a tenth of it is a bench flyer. Expressing
        interest as a fraction of this is what keeps the model league-agnostic —
        it rescales itself for a 14-team league, for 2QB, for a short horizon.
        """
        return self.replacement_for(player, reps) * max(1, len(weeks))

    def needs(self, team: TeamState, weeks=None) -> dict:
        """{slot_id: seats short} — starting seats with nobody, or with somebody
        below the league's replacement level for that slot."""
        weeks = weeks if weeks is not None else self.reg_weeks
        reps = self.replacement()
        _, lineup, _ = optimal_lineup(team.roster, self.profile, None)
        out = {}
        for slot, count in self.profile.starting_slots.items():
            filled = lineup.get(int(slot)) or []
            short = int(count) - len(filled)
            short += sum(1 for p in filled
                         if p.points_per_game < reps.get(int(slot), 0.0))
            if short > 0:
                out[int(slot)] = short
        return out

    def playoff_odds(self) -> dict:
        """{team_id: P(makes the playoffs)} — roster strength plus record so far.

        An estimator, not a simulation: each team's projected optimal-lineup points
        per week places it on the league's own distribution, its record to date
        shifts it by however much of the season has been played, and the cutoff is
        the strength of the last team that currently qualifies. Confidence grows
        with the number of weeks left to be wrong about, which is why an 0-0 week-1
        league lands everyone near `playoff_teams / size` instead of pretending to
        know. Every input is the league's own — size, playoff berths and schedule
        length all come off the profile.
        """
        prof = self.profile
        berths = int(prof.playoff_teams or 0)
        n = len(self.teams)
        if not n:
            return {}
        if berths <= 0 or berths >= n:
            return {t.team_id: (1.0 if berths >= n else 0.0) for t in self.teams}

        ppw = {t.team_id: lineup_points_over(t.roster, prof, [None]) for t in self.teams}
        vals = list(ppw.values())
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        sd = var ** 0.5 or 1.0

        played, left = 0, max(1, len(self.reg_weeks))
        for t in self.teams:
            r = t.record or {}
            played = max(played, int(r.get("w") or 0) + int(r.get("l") or 0)
                         + int(r.get("t") or 0))
        elapsed = played / max(1, played + left)

        score = {}
        for t in self.teams:
            r = t.record or {}
            games = int(r.get("w") or 0) + int(r.get("l") or 0) + int(r.get("t") or 0)
            win_pct = ((int(r.get("w") or 0) + 0.5 * int(r.get("t") or 0)) / games
                       if games else 0.5)
            z = (ppw[t.team_id] - mean) / sd
            score[t.team_id] = z * (1 - elapsed) + (win_pct - 0.5) * 4.0 * elapsed

        cutoff = sorted(score.values(), reverse=True)[berths - 1]
        # more weeks left = more chances for the gap to close, so a given edge is
        # worth less; fewer left and the standings harden
        sharp = (max(1, played + left) / max(1, left)) ** 0.5
        out = {}
        for tid, s in score.items():
            x = (s - cutoff) * sharp
            out[tid] = round(clamp(1.0 / (1.0 + pow(2.718281828459045, -x))), 3)
        return out

    # ── construction ─────────────────────────────────────────────────────────
    @staticmethod
    def from_season_data(data: dict, profile: LeagueProfile | None = None) -> "SeasonState":
        """Build state from a `season_data.json` payload (or a fixture of one).

        The payload carries everything the profile needs — bench, IR, playoff
        weeks, draft type — so a consumer that only ever sees `season_data.json`
        (a deploy bundle with no scraped `league_full.json`) reconstructs the same
        league. Only bench falls back to inference, and only if the producer
        omitted it, in which case it is recorded in `notes` rather than assumed
        silently: bench decides whether a waiver add forces a drop.
        """
        lg = data.get("league") or {}
        week = int(data.get("week") or 1)
        notes = []
        if profile is None:
            slots = {int(k): int(v) for k, v in (lg.get("lineup_slots") or {}).items()}
            starters = sum(slots.values())
            bench, ir = lg.get("bench"), lg.get("ir")
            if bench is None:
                biggest = max((len(t.get("roster") or []) for t in data.get("teams") or []),
                              default=starters)
                bench = max(0, biggest - starters)
                notes.append(f"payload carries no `bench` — inferred {bench} from the "
                             f"largest roster present")
            bench, ir = int(bench), int(ir or 0)
            acq = lg.get("acquisition") or {}
            # playoff_weeks is authoritative in the payload; LeagueProfile derives it
            # from regular_weeks, so back out the regular_weeks that reproduces it.
            reg = int(lg.get("regular_weeks") or 0)
            pw = [int(w) for w in (lg.get("playoff_weeks") or [])]
            if pw and min(pw) - 1 != reg:
                notes.append(f"payload playoff_weeks {pw} disagree with regular_weeks "
                             f"{reg}; trusting playoff_weeks")
                reg = min(pw) - 1
            profile = LeagueProfile(
                league_id=0, season=int(lg.get("season") or 0), name=lg.get("name") or "",
                size=int(lg.get("size") or len(data.get("teams") or [])),
                scoring={}, lineup_slots={**slots, 20: bench, 21: ir},
                bench=bench, ir=ir,
                acquisition=Acquisition(
                    model=acq.get("model") or FAAB, budget=acq.get("budget"),
                    min_bid=int(acq.get("min_bid") or 0),
                    continuous=bool(acq.get("continuous")), waiver_hours=0,
                    order_resets=True, limit=-1),
                draft_type=lg.get("draft_type") or "",
                auction_budget=lg.get("auction_budget"), keeper_count=0,
                regular_weeks=reg,
                playoff_teams=int(lg.get("playoff_teams") or 0),
                trade_deadline_ms=lg.get("trade_deadline"),
                veto_votes=int(lg.get("veto_votes") or 0),
            )

        proj_weeks = [int(w) for w in (lg.get("proj_weeks") or [])]
        if not proj_weeks:
            notes.append("payload carries no `proj_weeks` — every player falls back to a "
                         "FLAT season rate, so week-to-week matchups are invisible")
        players = {int(pid): to_player(rec, week, proj_weeks)
                   for pid, rec in (data.get("players") or {}).items()}
        raws = {int(pid): rec for pid, rec in (data.get("players") or {}).items()}

        teams = []
        for t in data.get("teams") or []:
            roster = [players[int(i)] for i in (t.get("roster") or []) if int(i) in players]
            teams.append(TeamState(
                team_id=int(t.get("team_id")), name=t.get("name") or "",
                manager=t.get("manager") or "", roster=roster,
                faab_left=t.get("faab_left"), waiver_priority=t.get("waiver_priority"),
                tendencies=t.get("tendencies") or {}, borrowed=bool(t.get("borrowed")),
                record=t.get("record") or {}, points_for=float(t.get("points_for") or 0.0),
                playoff_odds=t.get("playoff_odds")))

        return SeasonState(
            profile=profile, week=week, teams=teams, players=players, raw=raws,
            me_id=int((data.get("me") or {}).get("team_id") or (teams[0].team_id if teams else 0)),
            scoring_label=lg.get("scoring_label") or "", key=lg.get("key") or "",
            name=lg.get("name") or "", notes=notes)


# ── small shared numerics ────────────────────────────────────────────────────
def clamp(x, lo=0.0, hi=1.0) -> float:
    return lo if x < lo else (hi if x > hi else x)


def none_of(probs) -> float:
    """P(nobody acts), given independent per-manager probabilities."""
    out = 1.0
    for q in probs:
        out *= (1.0 - clamp(q))
    return out


def saturating(x: float, half: float) -> float:
    """x/(x+half): 0 at 0, 0.5 at `half`, asymptotes to 1. The shape used to turn
    'how much does this add help you' into 'how likely are you to go get him'."""
    if x <= 0:
        return 0.0
    return x / (x + max(half, EPS))


def percentiles(values) -> dict:
    """{index: percentile in [0,1]} by rank. Used to place a player inside the
    league's own distribution instead of against an absolute threshold."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    n = max(1, len(values) - 1)
    return {i: (rank / n if n else 1.0) for rank, i in enumerate(order)}


def horizon_points(players, profile: LeagueProfile, weeks) -> float:
    """Optimal-lineup points over `weeks` — thin alias so callers do not reach
    past this module into the solver for the one thing they all need."""
    return lineup_points_over(players, profile, weeks)
