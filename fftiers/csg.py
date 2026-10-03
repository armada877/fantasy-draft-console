"""CSG Fantasy Football Sheet port (v14.1) — games-based VBD valuation.

Ports the calculation chain of the CSG workbook (ProjectionCalcs + VBDgames),
separate from the elboberto port in vbd.py. The methodology differences that
make this sheet distinct:

  * Games-based starter baselines: instead of "the 12th QB", the baseline is
    the position rank at which historical games-played covers every
    starter-game a league needs (slots x teams x 16). Injuries and byes push
    baselines deeper — a 12-team 1-QB league needs ~15 QBs, not 12.
    (VBDgames Y2 = MIN(IF(cumGames > slots*teams*16, rank)); the historical
    cumulative-games-by-rank curves are extracted from the workbook into
    data/csg_games.json.)
  * BenchVBD is measured against the last *rostered* player and is NOT
    clamped at zero (ProjectionCalcs S col), so AvgVBD decays smoothly.
  * Positional scarcity multiplier: VBDAdj = AvgVBD x (1.5 - 0.5 x share of
    the position's remaining VBD still available). During a draft "available"
    means undrafted; intra-season it means on waivers — a position whose value
    is all rostered gets scarcer.
  * Tiers: half a standard deviation of AvgVBD per tier, counted down from
    the position leader (ProjectionCalcs AE col).
  * Auction $: same starter/bench two-rate structure as the elboberto sheet,
    with a much higher starter share (workbook default 0.95-0.98 vs 0.88).

Offense only (QB/RB/WR/TE): the workbook's VBD engine covers only those
positions (K/DST are $1 plugs; IDP is not ported).
"""
from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path

from .config import LeagueConfig
from .projections import Projection, score

CORE = ("QB", "RB", "WR", "TE")
POS_CAP = {"QB": 32, "RB": 200, "WR": 200, "TE": 100}
SEASON_GAMES = 16  # the workbook's games curves are per-16-game season

FLEX_SLOT_ELIGIBILITY = {
    "FLEX": ("RB", "WR", "TE"),
    "WRTE": ("WR", "TE"),
    "WRRB": ("WR", "RB"),
    "OP": ("QB", "RB", "WR", "TE"),
}

_GAMES_CURVES: dict[str, list[tuple[int, float]]] | None = None


def games_curves() -> dict[str, list[tuple[int, float]]]:
    global _GAMES_CURVES
    if _GAMES_CURVES is None:
        raw = json.loads((Path(__file__).parent / "data" / "csg_games.json").read_text())
        _GAMES_CURVES = {pos: [(int(r), float(g)) for r, g in arr] for pos, arr in raw.items()}
    return _GAMES_CURVES


def games_rank(pos: str, games_needed: float) -> int:
    """First position rank whose historical cumulative games exceed the need
    (VBDgames Y col). Extrapolates past the table at the tail's rate."""
    if games_needed <= 0:
        return 0
    curve = games_curves()[pos]
    for rank, cum in curve:
        if cum > games_needed:
            return rank
    (r1, g1), (r0, g0) = curve[-1], curve[-2]
    per_rank = (g1 - g0) / (r1 - r0) or 1.0
    return r1 + int((games_needed - g1) / per_rank) + 1


@dataclass
class CsgPlayer:
    name: str
    pos: str
    team: str
    points: float
    rostered: bool = True     # drives the scarcity factor; draft-time "drafted"
    pos_rank: int = 0
    status: str = ""
    start_vbd: float = 0.0
    bench_vbd: float = 0.0    # unclamped
    avg_vbd: float = 0.0
    vbd_adj: float = 0.0
    tier: int = 0
    dollars: float = 0.0


def csg_score(pr: Projection, cfg: LeagueConfig) -> float:
    """Workbook Q col: the common stats via score(), plus CSG's extra
    dimensions — incompletions and first downs."""
    pts = score(pr, cfg)
    s, sc = pr.stats, cfg.scoring
    if s:
        pts += (s.get("pass_att", 0) - s.get("pass_cmp", 0)) * float(sc.get("incompletions", 0))
        pts += s.get("first_downs", 0) * float(sc.get("points_per_first_down", 0))
    return pts


def csg_starters(cfg: LeagueConfig, by_pos: dict[str, list[CsgPlayer]]) -> dict[str, int]:
    """Start counts: games-based baseline for dedicated slots, then flex seats
    (all four flex types) filled greedily by projected points."""
    counts = {}
    for pos in CORE:
        bl = games_rank(pos, cfg.starters(pos) * cfg.teams * SEASON_GAMES)
        counts[pos] = min(bl, len(by_pos.get(pos, [])))
    for slot, eligible in FLEX_SLOT_ELIGIBILITY.items():
        seats = cfg.teams * cfg.starters(slot)
        if seats <= 0:
            continue
        pool = []
        for pos in eligible:
            players = by_pos.get(pos, [])
            pool += [(p.points, pos) for p in players[counts[pos]:]]
        for _, pos in sorted(pool, reverse=True)[:seats]:
            counts[pos] += 1
    return {pos: min(n, POS_CAP[pos], len(by_pos.get(pos, []))) for pos, n in counts.items()}


def csg_bench(cfg: LeagueConfig, starters: dict[str, int]) -> dict[str, int]:
    """Bench pool = every roster spot not used by offense starters or K/DST
    (VBDgames AG13), split proportionally with the QB-32 spillover to RB/WR."""
    total_start = sum(starters.values()) or 1
    pool = (cfg.teams * cfg.roster_size - total_start
            - (cfg.starters("K") + cfg.starters("DST")) * cfg.teams)
    pool = max(pool, 0)
    share = {pos: starters[pos] / total_start * pool for pos in CORE}
    qb_room = max(32 - starters["QB"], 0)
    if share["QB"] > qb_room:
        spill = max(share["QB"] - qb_room, 1)
        share["QB"] = qb_room
        rw = (starters["RB"] + starters["WR"]) or 1
        share["RB"] += spill * starters["RB"] / rw
        share["WR"] += spill * starters["WR"] / rw
    return {pos: round(min(POS_CAP[pos] - starters[pos], share[pos])) for pos in CORE}


def _nth_points(players: list[CsgPlayer], n: int) -> float:
    if not players:
        return 0.0
    return players[min(n, len(players)) - 1].points


def csg_value(cfg: LeagueConfig, projections: list[Projection],
              unrostered: set[str] | None = None) -> list[CsgPlayer]:
    """Full CSG valuation. `unrostered` = names currently on waivers (or
    undrafted); players not listed count as rostered for scarcity purposes."""
    opts = cfg.csg_options
    unrostered = {n.lower() for n in (unrostered or set())}

    by_pos: dict[str, list[CsgPlayer]] = {}
    for pr in projections:
        if pr.pos not in CORE:
            continue
        by_pos.setdefault(pr.pos, []).append(CsgPlayer(
            pr.name, pr.pos, pr.team, round(csg_score(pr, cfg), 1),
            rostered=pr.name.lower() not in unrostered))
    for players in by_pos.values():
        players.sort(key=lambda p: -p.points)

    starters = dict(opts.get("starters", {})) or csg_starters(cfg, by_pos)
    bench = dict(opts.get("bench", {})) or csg_bench(cfg, starters)

    for pos in CORE:
        players = by_pos.get(pos, [])
        if not players:
            continue
        start_base = _nth_points(players, starters[pos])
        roster_base = _nth_points(players, starters[pos] + bench[pos])
        for i, p in enumerate(players, 1):
            p.pos_rank = i
            p.start_vbd = max(p.points - start_base, 0.0)
            p.bench_vbd = p.points - roster_base          # unclamped (S col)
            p.avg_vbd = max((p.start_vbd + p.bench_vbd) / 2, 0.0)
            p.status = ("Starter" if i <= starters[pos]
                        else "Undrafted" if i > starters[pos] + bench[pos]
                        else "Bench")
        # Scarcity: share of the position's positive VBD still available
        # (ProjectionCalcs AH3/AH4: factor = 1.5 - 0.5 x fraction available).
        pos_vbd = sum(p.avg_vbd for p in players if p.avg_vbd > 0)
        avail = sum(p.avg_vbd for p in players if p.avg_vbd > 0 and not p.rostered)
        factor = 1.5 - 0.5 * (avail / pos_vbd if pos_vbd else 1.0)
        # Tiers: half a stdev of AvgVBD per tier below the leader (AE col).
        nz = [p.avg_vbd for p in players if p.avg_vbd != 0]
        sd = statistics.pstdev(nz) if len(nz) > 1 else 0.0
        top = max(nz) if nz else 0.0
        for p in players:
            p.vbd_adj = round(p.avg_vbd * factor, 1)
            p.tier = round((top - p.avg_vbd) / (sd / 2) + 1) if sd else 1

    _price(cfg, by_pos, starters)
    out = [p for players in by_pos.values() for p in players]
    out.sort(key=lambda p: -p.avg_vbd)
    return out


def _price(cfg: LeagueConfig, by_pos: dict[str, list[CsgPlayer]],
           starters: dict[str, int]) -> None:
    """Auction $ (VBDgames AQ block): total pool less $1 per K/DST starter,
    split by starter_pct (workbook default 0.95) with the roster-to-starter
    baseline slice carved out at the bench rate; X = R*SPF + (S-R)*BPF."""
    opts = cfg.csg_options
    budget = float(opts.get("budget", cfg.vbd_options.get("budget", 200)))
    starter_pct = float(opts.get("starter_pct", 0.95))
    total = cfg.teams * budget - (cfg.starters("K") + cfg.starters("DST")) * cfg.teams

    core = [p for pos in CORE for p in by_pos.get(pos, [])]
    start_vbd = sum(p.start_vbd for p in core)
    bench_vbd = sum(p.bench_vbd for p in core if p.status == "Bench")
    bench_budget = total * (1 - starter_pct)
    bpf = bench_budget / bench_vbd if bench_vbd else 0.0
    carve = 0.0
    for pos in CORE:
        players = by_pos.get(pos, [])
        if players:
            carve += (players[0].bench_vbd - players[0].start_vbd) * starters[pos]
    spf = (total * starter_pct - carve * bpf) / start_vbd if start_vbd else 0.0
    for p in core:
        p.dollars = round(max(p.start_vbd * spf + (p.bench_vbd - p.start_vbd) * bpf, 0), 1)
