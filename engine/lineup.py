#!/usr/bin/env python3
"""Optimal lineup assignment — the primitive everything else reduces to.

The waiver board, both sides of every trade, and the buy-low/sell-high list are all
differences of `lineup_points()`. So this has to be exactly right, and it has to be
generic: a hand-rolled QB->RB->WR->TE->FLEX cascade silently mis-fills a 2QB or
superflex league, which is a real league in this account (Chi Phi starts QBx2).

We therefore solve it as a max-weight bipartite assignment: starting slots on one
side, players on the other, edges where the player's own `eligible_slots` (straight
off the platform, authoritative for FLEX/OP/IDP alike) admits the slot.

Bye weeks fall out for free: a player simply scores 0 in a week he does not play, so
summing the optimal lineup week by week values a bye-filler correctly instead of
averaging the hole away.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from .profile import BENCH_SLOTS, LeagueProfile, slot_name

INF = float("inf")


@dataclass
class Player:
    """A roster/pool player as the engine sees him.

    points_per_game is the horizon-agnostic rate; `bye_week` and `out_weeks` carve
    out the weeks he cannot score. Engine code never reads platform fields directly.
    """
    id: int
    name: str
    pos: str
    eligible_slots: frozenset          # slot ids this player may fill (from the platform)
    points_per_game: float = 0.0
    # Per-week ESPN projections {week: points}. When present these WIN over the flat
    # rate: they carry matchup strength week to week (Bo Nix ranges 15.8-20.7 across
    # weeks 2-14) and encode byes as a natural 0.0. points_per_game remains the
    # fallback for a player ESPN has no weekly line for, and for rate comparisons.
    week_points: dict = field(default_factory=dict)
    bye_week: int | None = None
    out_weeks: frozenset = field(default_factory=frozenset)
    team: str = ""
    injury: str = ""
    owner: str | None = None           # team/manager holding him, None = free agent

    def points_in(self, week: int | None) -> float:
        """Projected points in a given week. week=None means 'a generic game'.

        Prefers the real per-week projection; falls back to the flat rate. A bye is
        0.0 either way — explicitly via bye_week, or implicitly because ESPN projects
        0 for that week.
        """
        if week is None:
            return self.points_per_game
        if week in self.out_weeks:
            return 0.0
        if self.week_points:
            v = self.week_points.get(week, self.week_points.get(str(week)))
            if v is not None:
                return float(v)
        if self.bye_week is not None and week == self.bye_week:
            return 0.0
        return self.points_per_game


# ── max-weight bipartite assignment (Hungarian / Kuhn-Munkres, O(n^3)) ────────
def _hungarian(cost):
    """Min-cost assignment for a rectangular matrix (rows <= cols).

    Returns `assign`, where assign[i] is the column matched to row i (or -1).
    Classic e-maxx formulation; rosters are tiny (<=~25x25) so cubic is free.
    """
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    u = [0.0] * (n + 1)
    v = [0.0] * (m + 1)
    p = [0] * (m + 1)          # p[j] = row currently matched to column j
    way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (m + 1)
        used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = -1
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            if j1 == -1:                       # no augmenting column (shouldn't happen)
                break
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    assign = [-1] * n
    for j in range(1, m + 1):
        if p[j]:
            assign[p[j] - 1] = j - 1
    return assign


def _slot_rows(profile: LeagueProfile):
    """Expand {slotId: count} into one row per startable seat (RBx2 -> two rows)."""
    rows = []
    for slot, count in sorted(profile.starting_slots.items()):
        rows.extend([int(slot)] * int(count))
    return rows


def optimal_lineup(players, profile: LeagueProfile, week: int | None = None):
    """Best legal starting lineup for `week`.

    Returns (total_points, {slot_id: [Player, ...]}, benched[Player, ...]).
    Players whose eligible_slots admit no starting slot are simply benched.
    """
    rows = _slot_rows(profile)
    players = list(players)
    if not rows:
        return 0.0, {}, players
    if not players:
        return 0.0, {s: [] for s in profile.starting_slots}, []

    # Pad columns so every row can be filled (an unfilled seat scores 0).
    pad = max(0, len(rows) - len(players))
    n, m = len(rows), len(players) + pad
    cost = [[0.0] * m for _ in range(n)]
    for i, slot in enumerate(rows):
        for j, pl in enumerate(players):
            # negate: Hungarian minimises, we want max points
            cost[i][j] = -pl.points_in(week) if slot in pl.eligible_slots else INF
        for j in range(len(players), m):
            cost[i][j] = 0.0                      # empty seat

    # A row with no finite option must still match something; INF would break the
    # solver, so fall back to a large-but-finite penalty.
    big = 1e9
    for i in range(n):
        if all(c == INF for c in cost[i]):
            cost[i] = [big] * m
        else:
            cost[i] = [big if c == INF else c for c in cost[i]]

    assign = _hungarian(cost)
    lineup, used, total = {}, set(), 0.0
    for s in profile.starting_slots:
        lineup[int(s)] = []
    for i, j in enumerate(assign):
        slot = rows[i]
        if j < 0 or j >= len(players):
            continue
        pl = players[j]
        if slot not in pl.eligible_slots:          # only via the big-penalty fallback
            continue
        lineup[slot].append(pl)
        used.add(id(pl))
        total += pl.points_in(week)
    bench = [p for p in players if id(p) not in used]
    return total, lineup, bench


def lineup_points(players, profile: LeagueProfile, week: int | None = None) -> float:
    return optimal_lineup(players, profile, week)[0]


def lineup_points_over(players, profile: LeagueProfile, weeks) -> float:
    """Total optimal-lineup points across a set of weeks — this is what makes a
    bye-week filler worth more than a better player who duplicates a starter."""
    return sum(lineup_points(players, profile, w) for w in weeks)


# ── the decision primitives ──────────────────────────────────────────────────
def marginal_add(roster, candidate: Player, profile: LeagueProfile, weeks,
                 must_drop: bool = True):
    """Points `candidate` adds across `weeks`, after dropping whoever costs least.

    Returns (gain, dropped_player_or_None). This is the number the waiver board
    sorts on: not the player's value, but what he adds to *your* starting lineup.
    """
    base = lineup_points_over(roster, profile, weeks)
    full = len(roster) >= profile.roster_size
    if not (must_drop and full):
        return lineup_points_over(list(roster) + [candidate], profile, weeks) - base, None

    best_gain, best_drop = -INF, None
    for drop in roster:
        kept = [p for p in roster if p is not drop] + [candidate]
        gain = lineup_points_over(kept, profile, weeks) - base
        if gain > best_gain:
            best_gain, best_drop = gain, drop
    return best_gain, best_drop


def trade_delta(roster, send, receive, profile: LeagueProfile, weeks) -> float:
    """Change in your optimal-lineup points across `weeks` for send/receive lists.
    Positive = the trade helps you. Used for BOTH sides of every proposal."""
    send_ids = {id(p) for p in send}
    after = [p for p in roster if id(p) not in send_ids] + list(receive)
    return (lineup_points_over(after, profile, weeks)
            - lineup_points_over(roster, profile, weeks))


def starter_replacement_level(pool, profile: LeagueProfile):
    """Points of the last STARTED player at each slot across the league — the
    in-season replacement baseline. Derived from size x starting slots, never a
    constant, so it adapts to 8-team/14-team and to 2QB.
    """
    out = {}
    for slot, count in profile.starting_slots.items():
        elig = sorted((p for p in pool if int(slot) in p.eligible_slots),
                      key=lambda p: p.points_per_game, reverse=True)
        need = int(count) * profile.size
        out[int(slot)] = elig[need - 1].points_per_game if len(elig) >= need else 0.0
    return out
