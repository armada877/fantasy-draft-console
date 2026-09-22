"""Value-Based Drafting engine — port of the elboberto fantasy workbook.

Ports the calculation chain of 2026_FantasyFootball_elboberto.xlsm:

  FPTS        stats x league point values          (projections.score)
  StartVBD    max(FPTS - points of last starter, 0)
  BenchVBD    FPTS - points of last rostered player (negatives optional)
  AvgVBD      mean(StartVBD, BenchVBD)
  Tier        z-score ladder over AvgVBD (per-position cutoffs from the sheets)
  $           StartVBD priced at the starter rate + the bench-value slice at
              the bench rate, from an 88/12 budget split (LeagueInfo D15)
  AvgVBD $    AvgVBD x (total budget / total AvgVBD)   — the simpler H5 method

Starter baselines come from actually filling lineups: dedicated slots first,
then FLEX/OP slots with the best remaining eligible players (the workbook's
hidden Flex sheets). Bench spots are allocated to QB/RB/WR/TE proportionally
to starter counts, with QBs capped at 32 league-wide and the spill going to
RB/WR (LeagueInfo B20:B23). K/DST use the sheet's simpler method: VBD vs the
last starter, tiers by half-standard-deviations off the leader.

Intra-season difference vs the workbook: it ships with preseason full-season
projections and static baselines; here the same math re-runs on this week's or
rest-of-season projections, so values and baselines move with the season.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

from .config import FLEX_ELIGIBLE, LeagueConfig
from .projections import Projection, score

CORE = ("QB", "RB", "WR", "TE")

# Z-score tier cutoffs, top tier first (QB sheet AA3 vs RB/WR/TE AA3).
TIER_LADDER = {
    "QB": (2.0, 1.5, 1.0, 0.5, 0.0, -0.5),
    "RB": (3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0.0),
    "WR": (3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0.0),
    "TE": (3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0.0),
}


@dataclass
class ValuedPlayer:
    name: str
    pos: str
    team: str
    points: float
    pos_rank: int = 0
    status: str = ""          # Starter / Bench / Undrafted
    start_vbd: float = 0.0
    bench_vbd: float = 0.0
    avg_vbd: float = 0.0
    tier: str = ""
    dollars: float = 0.0      # starter/bench split method
    avg_vbd_dollars: float = 0.0


def starter_counts(cfg: LeagueConfig, by_pos: dict[str, list[ValuedPlayer]]) -> dict[str, int]:
    """League-wide starters per position by filling lineups greedily.

    Dedicated slots take the top N x teams at each position; FLEX (and OP
    superflex) slots then go to the best remaining eligible players by points.
    Equivalent to the workbook's FlexWRRBTE-style combined-rank sheets.
    """
    counts = {pos: min(cfg.teams * cfg.starters(pos), len(by_pos.get(pos, [])))
              for pos in CORE}
    for slot, eligible in (("FLEX", FLEX_ELIGIBLE), ("OP", ("QB",) + FLEX_ELIGIBLE)):
        remaining = cfg.teams * cfg.starters(slot)
        if remaining <= 0:
            continue
        # Next-best-available at each eligible position, merged by points.
        pool = []
        for pos in eligible:
            players = by_pos.get(pos, [])
            pool += [(p.points, pos) for p in players[counts[pos]:]]
        for _, pos in sorted(pool, reverse=True)[:remaining]:
            counts[pos] += 1
    return counts


def bench_counts(cfg: LeagueConfig, starters: dict[str, int]) -> dict[str, int]:
    """Bench spots per position, proportional to starters (LeagueInfo B20:B23).

    QBs are capped at 32 rostered league-wide (there are only 32 starting
    NFL QBs); the overflow spills to RB/WR proportionally.
    """
    total_starters = sum(starters.values()) or 1
    bench_total = cfg.teams * cfg.bench_slots
    share = {pos: starters[pos] / total_starters * bench_total for pos in CORE}
    qb_room = max(32 - starters["QB"], 0)
    if share["QB"] > qb_room:
        spill = share["QB"] - qb_room
        share["QB"] = qb_room
        rw = (starters["RB"] + starters["WR"]) or 1
        share["RB"] += spill * starters["RB"] / rw
        share["WR"] += spill * starters["WR"] / rw
    return {pos: round(v) for pos, v in share.items()}


def _nth_points(players: list[ValuedPlayer], n: int) -> float:
    """Points of the n-th best player (LARGE(pts, n)); worst player if n > pool."""
    if not players:
        return 0.0
    return players[min(n, len(players)) - 1].points


def _zscore_tiers(players: list[ValuedPlayer], pos: str) -> None:
    vals = [p.avg_vbd for p in players]
    mean = statistics.fmean(vals)
    sd = statistics.pstdev(vals)
    for p in players:
        z = (p.avg_vbd - mean) / sd if sd else 0.0
        tier = len(TIER_LADDER[pos]) + 1
        for i, cut in enumerate(TIER_LADDER[pos], 1):
            if z > cut:
                tier = i
                break
        p.tier = f"{pos}{tier}"


def _kdst_value(cfg: LeagueConfig, players: list[ValuedPlayer], pos: str,
                allow_negative: bool) -> None:
    """K/DST: VBD vs last starter; tiers by half-stdev behind the leader."""
    n_start = cfg.teams * cfg.starters(pos)
    base = _nth_points(players, n_start) if n_start else 0.0
    pts = [p.points for p in players]
    sd = statistics.pstdev(pts) if len(pts) > 1 else 0.0
    top = max(pts) if pts else 0.0
    label = "DEF" if pos == "DST" else pos
    for i, p in enumerate(players, 1):
        p.pos_rank = i
        v = (p.points - base) if n_start else 0.0
        p.start_vbd = p.bench_vbd = p.avg_vbd = (v if allow_negative else max(v, 0.0))
        p.status = "Starter" if i <= n_start else "Undrafted"
        p.tier = f"{label}{round((top - p.points) / sd / 2 + 1) if sd else 1}"


def value_players(cfg: LeagueConfig, projections: list[Projection]) -> list[ValuedPlayer]:
    """Run the full workbook calculation over a set of projections."""
    opts = cfg.vbd_options
    allow_negative = bool(opts.get("allow_negative", True))
    starter_pct = float(opts.get("starter_pct", 0.88))

    by_pos: dict[str, list[ValuedPlayer]] = {}
    for pr in projections:
        if pr.pos not in CORE + ("K", "DST"):
            continue
        by_pos.setdefault(pr.pos, []).append(
            ValuedPlayer(pr.name, pr.pos, pr.team, round(score(pr, cfg), 1)))
    for players in by_pos.values():
        players.sort(key=lambda p: -p.points)

    starters = starter_counts(cfg, by_pos)
    bench = bench_counts(cfg, starters)

    for pos in CORE:
        players = by_pos.get(pos, [])
        if not players:
            continue
        start_base = _nth_points(players, starters[pos])
        roster_base = _nth_points(players, starters[pos] + bench[pos])
        for i, p in enumerate(players, 1):
            p.pos_rank = i
            p.start_vbd = max(p.points - start_base, 0.0)
            bv = p.points - roster_base
            p.bench_vbd = bv if allow_negative else max(bv, 0.0)
            p.avg_vbd = (p.start_vbd + p.bench_vbd) / 2
            p.status = ("Starter" if i <= starters[pos]
                        else "Bench" if i <= starters[pos] + bench[pos]
                        else "Undrafted")
        _zscore_tiers(players, pos)

    for pos in ("K", "DST"):
        if by_pos.get(pos):
            _kdst_value(cfg, by_pos[pos], pos, allow_negative)

    _price(cfg, by_pos, starter_pct)
    out = [p for players in by_pos.values() for p in players]
    out.sort(key=lambda p: -p.avg_vbd)
    return out


def _price(cfg: LeagueConfig, by_pos: dict[str, list[ValuedPlayer]],
           starter_pct: float) -> None:
    """Auction-style dollar values (LeagueInfo G/H columns).

    Mid-season these read as a trade-value index in league dollars rather
    than literal auction prices.
    """
    budget = float(cfg.vbd_options.get("budget", 200))
    total = cfg.teams * budget - cfg.teams * (cfg.starters("K") + cfg.starters("DST"))

    core = [p for pos in CORE for p in by_pos.get(pos, [])]
    starter_vbd = sum(p.start_vbd for p in core if p.start_vbd > 0)
    bench_vbd = sum(p.bench_vbd for p in core if p.status == "Bench")
    bench_budget = total * (1 - starter_pct)
    bench_pf = bench_budget / bench_vbd if bench_vbd else 0.0
    # Each starter also carries the roster-baseline-to-starter-baseline slice,
    # priced at the bench rate; carve that out of the starter budget (D19).
    # (S3-R3) in the sheet == starter_base - roster_base, constant per position.
    carve = 0.0
    for pos in CORE:
        players = by_pos.get(pos, [])
        if players:
            carve += ((players[0].bench_vbd - players[0].start_vbd)
                      * cfg.teams * cfg.starters(pos))
    starter_budget = total * starter_pct - carve * bench_pf
    starter_pf = starter_budget / starter_vbd if starter_vbd else 0.0

    avg_vbd_total = sum(p.avg_vbd for p in core if p.avg_vbd > 0)
    total_pf = total / avg_vbd_total if avg_vbd_total else 0.0
    for p in core:
        p.dollars = round(p.start_vbd * starter_pf
                          + (p.bench_vbd - p.start_vbd) * bench_pf, 1)
        p.avg_vbd_dollars = round(p.avg_vbd * total_pf, 1)
