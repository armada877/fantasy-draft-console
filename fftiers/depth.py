"""Derive per-position player pools and tier counts from league settings.

The upstream main.R hardcoded these for a 12-team, 1QB/2RB/2WR/1TE/1FLEX
league (QB 26 players / 8 tiers, RB 40/9, WR 60/12, TE 24/8, K 20/5,
DST 20/6, FLX ranks 20-95 / 14 tiers). The factors below are calibrated so a
league with those settings reproduces roughly those numbers, and everything
scales with team count, roster slots, flex slots, and bench size.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import CORE_POSITIONS, FLEX_ELIGIBLE, LeagueConfig

# How deep beyond the raw starter count each position stays relevant
# (waiver churn, streaming, injury replacement).
DEPTH_FACTOR = {"QB": 2.2, "RB": 1.4, "WR": 2.0, "TE": 1.75, "K": 1.65, "DST": 1.65}

# Typical share of FLEX starts by position.
FLEX_USAGE = {"RB": 0.40, "WR": 0.45, "TE": 0.15}

# Roughly how many players land in one tier at each position (from the
# upstream pool/tier ratios).
PLAYERS_PER_TIER = {"QB": 3.3, "RB": 4.5, "WR": 5.0, "TE": 3.0, "K": 4.0, "DST": 3.3, "FLX": 5.3}

# Don't ask for more players than experts meaningfully rank.
POOL_CAP = {"QB": 36, "RB": 80, "WR": 100, "TE": 40, "K": 32, "DST": 32, "FLX": 120}


@dataclass
class PositionPlan:
    position: str        # FantasyPros position code (QB/RB/WR/TE/K/DST/FLX)
    low: int             # first positional rank to include (1-based)
    high: int            # last positional rank to include
    tiers: int
    scoring: str         # STD / HALF / PPR

    @property
    def pool(self) -> int:
        return self.high - self.low + 1


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def effective_starters(cfg: LeagueConfig, pos: str) -> float:
    """League-wide starters at a position, counting flex/superflex shares."""
    eff = cfg.teams * cfg.starters(pos)
    if pos in FLEX_ELIGIBLE:
        eff += cfg.teams * cfg.starters("FLEX") * FLEX_USAGE[pos]
    if pos == "QB":
        # A superflex (OP) slot is a QB slot in practice.
        eff += cfg.teams * cfg.starters("OP") * 0.85
    return eff


def plan_positions(cfg: LeagueConfig, week: int) -> list[PositionPlan]:
    """Turn league settings into a list of (position, pool, tiers) to draw."""
    # Bench size stretches or shrinks how deep rosters go. Calibrated so a
    # 6-bench league matches the upstream defaults.
    bench_scale = _clamp(1 + 0.06 * (cfg.bench_slots - 6), 0.8, 1.3)

    plans: list[PositionPlan] = []
    for pos in CORE_POSITIONS:
        eff = effective_starters(cfg, pos)
        if eff <= 0:
            continue  # e.g. leagues with no kicker slot
        override = cfg.overrides.get(pos, {})
        pool = override.get("pool")
        if pool is None:
            pool = round(eff * DEPTH_FACTOR[pos] * bench_scale)
            # Never tier fewer than starters plus half a team's worth of backups.
            pool = max(pool, round(eff) + max(4, cfg.teams // 2))
            pool = min(pool, POOL_CAP[pos])
        tiers = override.get("tiers")
        if tiers is None:
            tiers = _clamp(round(pool / PLAYERS_PER_TIER[pos]), 3, 16)
        scoring = cfg.scoring_source if pos in FLEX_ELIGIBLE else "STD"
        plans.append(PositionPlan(pos, 1, int(pool), int(tiers), scoring))

    # FLEX board (in-season only, like upstream): skip the locked-in studs at
    # the top, cover the actual flex-decision range.
    if cfg.starters("FLEX") > 0 and week >= 1:
        override = cfg.overrides.get("FLX", cfg.overrides.get("FLEX", {}))
        low = override.get("low", max(1, round(cfg.teams * 1.67)))
        # Calibrated to upstream's ranks 20-95 at 12 teams / 1 flex slot.
        span = round(cfg.teams * (5.0 + 1.25 * cfg.starters("FLEX")))
        high = override.get("pool_high", min(low + span, POOL_CAP["FLX"]))
        tiers = override.get("tiers") or _clamp(round((high - low + 1) / PLAYERS_PER_TIER["FLX"]), 3, 18)
        plans.append(PositionPlan("FLX", int(low), int(high), int(tiers), cfg.scoring_source))

    return plans
