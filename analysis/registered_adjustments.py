#!/usr/bin/env python3
"""Every projection tuning that has been MEASURED, wired into the policy registry.

`engine.projection_policy` decides what ships. This module is what gives it
something to decide about: it reads the two WS-8 artifacts

    reports/projection_accuracy.json    accuracy, leave-one-season-out   (WS-8b)
    reports/lineup_replay.json          realized points, lineup replay   (WS-8c)

and registers each candidate as an `Adjustment` carrying the numbers actually
measured. Nothing here decides anything — `Adjustment.ships()` does, and as of the
2026-09-15 run it says no to all five.

**A refused tuning is the deliverable, not a failure.** Registering them is what
puts them in the data console's projection-policy panel with their real folds
attached, so "we tried a positional bias correction and it cost 0.12 points a week
in Chi Phi" is visible rather than being a thing somebody re-invents next season.

Import is best-effort everywhere: with no reports on disk nothing registers, the
registry stays empty, and the baseline ships unmodified — the same outcome, just
without the evidence panel.

    from analysis.registered_adjustments import load
    load()                      # -> number of adjustments registered
    print(projection_policy.audit())

THE SEAM'S LIMIT, recorded honestly: `adjusted_points(points, player, profile)` has
no week and no player history, so a week-dependent or form-dependent correction
cannot be expressed through it even if one were earned. `week_decay` and
`form_blend` are therefore registered as measured-and-unimplementable; both were
refused on the evidence anyway, so widening the seam would buy nothing today.
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine import projection_policy as pp                # noqa: E402

ACCURACY = os.path.join(ROOT, "reports", "projection_accuracy.json")
REPLAY = os.path.join(ROOT, "reports", "lineup_replay.json")

# Candidates whose shape the `(points, player, profile)` seam can actually carry.
# The rest are registered for the record with a fn that refuses to pretend.
SEAM_CARRIES = {"positional_bias", "linear_recal", "shrink_to_pos"}


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _unimplementable(name):
    def fn(points, player=None, profile=None):
        raise NotImplementedError(
            f"{name} needs the week (or the player's own recent form), which "
            f"adjusted_points() does not carry. It was refused on the evidence, so "
            f"the seam was left alone — widen it only if the evidence changes.")
    return fn


def _positional(params):
    """params: {position_name: multiplier}. Pooled across leagues — a per-league
    multiplier would be a different claim, with a third of the data behind it."""
    def fn(points, player=None, profile=None):
        pos = getattr(player, "pos", None)
        return float(points) * float(params.get(pos, 1.0))
    return fn


def _linear(params):
    def fn(points, player=None, profile=None):
        a, b = params.get(getattr(player, "pos", None), (0.0, 1.0))
        return max(0.0, a + b * float(points))
    return fn


def load(registry=None) -> int:
    """Register every measured candidate. Returns how many were registered."""
    acc = _read(ACCURACY)
    rep = _read(REPLAY)
    if not acc or not (acc.get("candidates")):
        return 0
    verdicts = (rep or {}).get("verdict") or {}
    leagues_block = (rep or {}).get("leagues") or {}

    n = 0
    for name, c in sorted(acc["candidates"].items()):
        results = tuple(
            pp.LeagueResult(league=k, baseline_mae=v["baseline_mae"],
                            model_mae=v["model_mae"], n=v["n"],
                            seasons_held_out=tuple(v.get("seasons_held_out") or ()))
            for k, v in sorted((c.get("results") or {}).items()))
        decision = []
        for lg, block in sorted(leagues_block.items()):
            s = block.get("summary") or {}
            if f"{name}_vs_baseline" not in s:
                continue
            decision.append(pp.DecisionResult(
                league=lg, team_weeks=int(s.get("team_weeks") or 0),
                points_delta=float(s[f"{name}_vs_baseline"]),
                t=s.get(f"{name}_t"),
                moved_pct=float(s.get(f"{name}_moved_lineups_pct") or 0.0)))
        val = pp.Validation(
            method=("leave-one-season-out (whole seasons held out) for accuracy; "
                    "historical team-week lineup replay for realized points"),
            fitted_on=tuple(sorted((c.get("results") or {}))),
            results=results, decision=tuple(decision),
            notes=(c.get("description") or "")
                  + ((" · replay: " + verdicts[name]["reason"]) if name in verdicts else ""))
        # No fitted parameters are shipped: every candidate is refused, so the
        # identity is the honest body. If one ever clears the bar, fit its params
        # on all seasons here and pass them to the builder above.
        fn = (_positional({}) if name in SEAM_CARRIES else _unimplementable(name))
        adj = pp.Adjustment(name=name, fn=fn, validation=val,
                            description=c.get("description") or "")
        if registry is None:
            pp.register(adj)
        else:
            registry[name] = adj
        n += 1
    return n


if __name__ == "__main__":
    count = load()
    print(f"registered {count} measured candidate(s)\n")
    print(pp.audit())
    ref = pp.refused()
    print(f"\n{len(ref)} refused, {count - len(ref)} applied")
