#!/usr/bin/env python3
"""Projection policy — the baseline ships unless a deviation is earned.

In-season we have no independent projection source: ESPN's projection, recomputed
against the league's own scoring, IS our baseline. Any deviation from it (a
positional bias correction, a late-season decay term, a usage-based adjustment, a
blend with an external ranking) is a CLAIM that we beat a vendor with a large
modelling team. That claim must be paid for with out-of-sample evidence across
leagues — or it does not ship.

This module makes that structural rather than advisory: an Adjustment with no
Validation, or one whose Validation fails the bar, is REFUSED at apply time. The
default behaviour of the whole system is therefore "baseline, unmodified", and a
tuning has to earn its way in.

Precedent: CLAUDE.md records a "validated 56%" strategy reading that turned out to
be an artifact of normalising by a nominal budget. The bar below exists so that
class of error cannot silently reach the console.

    from engine.projection_policy import baseline_points, adjusted_points, audit
    pts, applied = adjusted_points(raw_pts, player=p, profile=prof, league="2kdome")
    print(audit())          # what ships, what is refused, and why
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ── the bar ──────────────────────────────────────────────────────────────────
MIN_N = 500                 # player-weeks before a league counts as POWERED
MIN_POWERED_LEAGUES = 2     # must clear the bar in at least this many leagues
HARM_TOLERANCE = 0.02       # an underpowered league may not get >2% worse


@dataclass(frozen=True)
class LeagueResult:
    """Out-of-sample result in ONE league. `n` is held-out observations."""
    league: str
    baseline_mae: float
    model_mae: float
    n: int
    seasons_held_out: tuple = ()

    @property
    def powered(self) -> bool:
        return self.n >= MIN_N

    @property
    def improvement(self) -> float:
        """Fractional MAE reduction. Positive = better than baseline."""
        if not self.baseline_mae:
            return 0.0
        return (self.baseline_mae - self.model_mae) / self.baseline_mae


@dataclass(frozen=True)
class Validation:
    """Evidence that a deviation beats the baseline. Whole SEASONS held out —
    random rows leak, because adjacent weeks share injuries, roles and defenses."""
    method: str
    fitted_on: tuple
    results: tuple                      # (LeagueResult, ...)
    notes: str = ""

    @property
    def powered(self):
        return [r for r in self.results if r.powered]

    def verdict(self):
        """(ships: bool, reason: str) — the whole bar, in one place."""
        if not self.results:
            return False, "no validation results"
        powered = self.powered
        if len(powered) < MIN_POWERED_LEAGUES:
            return False, (f"only {len(powered)} league(s) with n>={MIN_N}; "
                           f"need {MIN_POWERED_LEAGUES}")
        losers = [r for r in powered if r.improvement <= 0]
        if losers:
            return False, ("fails to beat baseline in "
                           + ", ".join(f"{r.league} ({r.improvement:+.1%})" for r in losers))
        # sign consistency: a real effect does not reverse league to league
        harmed = [r for r in self.results
                  if not r.powered and r.improvement < -HARM_TOLERANCE]
        if harmed:
            return False, ("harms underpowered league(s): "
                           + ", ".join(f"{r.league} ({r.improvement:+.1%})" for r in harmed))
        gain = sum(r.improvement for r in powered) / len(powered)
        return True, (f"beats baseline in all {len(powered)} powered leagues, "
                      f"mean {gain:+.1%}")


@dataclass
class Adjustment:
    """A deviation from baseline. `fn(points, player, profile) -> points`."""
    name: str
    fn: object
    validation: Validation | None = None
    description: str = ""

    def ships(self):
        if self.validation is None:
            return False, "UNVALIDATED — no backtest attached"
        return self.validation.verdict()


REGISTRY: dict = {}


def register(adj: Adjustment):
    """Register a deviation. Registering does NOT mean it applies — see ships()."""
    REGISTRY[adj.name] = adj
    return adj


def baseline_points(raw_points: float) -> float:
    """The unmodified, league-scored vendor projection. Always available."""
    return raw_points


def adjusted_points(raw_points: float, player=None, profile=None, league: str | None = None):
    """Apply every adjustment that has EARNED it. Returns (points, applied_names).

    With an empty registry — or a registry of unvalidated ideas — this returns the
    baseline unchanged. That is the intended default.
    """
    pts = baseline_points(raw_points)
    applied = []
    for name, adj in sorted(REGISTRY.items()):
        ok, _ = adj.ships()
        if not ok:
            continue
        pts = adj.fn(pts, player, profile)
        applied.append(name)
    return pts, applied


def refused():
    """Registered adjustments that are NOT applied, with the reason. The data
    console should show this — a rejected tuning is a finding, not a failure."""
    out = []
    for name, adj in sorted(REGISTRY.items()):
        ok, why = adj.ships()
        if not ok:
            out.append((name, why))
    return out


def audit() -> str:
    lines = ["projection policy — baseline: ESPN projection recomputed on league scoring"]
    if not REGISTRY:
        lines.append("  no adjustments registered; baseline ships unmodified")
        return "\n".join(lines)
    for name, adj in sorted(REGISTRY.items()):
        ok, why = adj.ships()
        lines.append(f"  [{'APPLIED ' if ok else 'REFUSED '}] {name}: {why}")
        if adj.validation:
            for r in adj.validation.results:
                lines.append(f"       {r.league:20s} n={r.n:<6d} baseline {r.baseline_mae:.3f} "
                             f"-> {r.model_mae:.3f}  ({r.improvement:+.1%})"
                             f"{'' if r.powered else '  [underpowered]'}")
    return "\n".join(lines)
