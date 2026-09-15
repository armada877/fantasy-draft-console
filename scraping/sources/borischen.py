#!/usr/bin/env python3
"""Boris Chen weekly tiers — where the consensus has a real gap, not a rounding one.

    https://s3-us-west-1.amazonaws.com/fftiers/out/weekly-{POS}{SUFFIX}.csv

`SUFFIX` comes from `profile.scoring_label`; the positions fetched come from
`profile.startable_positions` plus FLX when the league has any FLEX/OP slot. Both
are read off the profile — a 2QB league with a kicker and a 1QB league without one
pull different file sets, by construction.

Verified live 2026-09-14:

    RB / WR / TE / FLX     -PPR, -HALF and bare all 200
    QB / K / DST           bare only; **every scoring suffix 403s** (reception
                           scoring does not change these rankings, so Chen does
                           not publish variants)
    rest-of-season         **not published** — every ROS URL shape 403s
                           (`ros-RB-HALF.csv`, `weekly-RB-ros.csv`, ...). Weekly only.

Columns: `Rank, Player.Name, Matchup, Best.Rank, Worst.Rank, Avg.Rank, Std.Dev, Tier`.

`Std.Dev` is as interesting as the tier: a high-variance player is one the experts
disagree about, which is exactly where a league-accurate valuation can be right and
consensus wrong.

Known join limit, reported not hidden: D/ST rows are full team names ("Jacksonville
Jaguars") while ESPN uses "Jaguars D/ST", so DST tiers do not join on `norm` and
carry no espn_id. Left as an honest miss rather than a hand-rolled team-name map.
"""
from __future__ import annotations

from . import common
from .common import _f, _i

NAME = "borischen"
TTL = 6 * 3600
BASE = "https://s3-us-west-1.amazonaws.com/fftiers/out/weekly-{pos}{suffix}.csv"

SUFFIX = {"PPR": "-PPR", "half-PPR": "-HALF", "standard": ""}
# these have no scoring variants published (see docstring)
SCORING_FREE = {"QB", "K", "DST"}
PUBLISHED = {"QB", "RB", "WR", "TE", "K", "DST", "FLX"}
FLEXY_SLOTS = {"FLEX", "OP", "RB/WR", "WR/TE"}


def positions_for(profile):
    """Which Boris files this league needs — derived, never a constant list."""
    want = [p for p in sorted(profile.startable_positions) if p in PUBLISHED]
    from engine.profile import slot_name
    if any(slot_name(s) in FLEXY_SLOTS for s in profile.starting_slots):
        want.append("FLX")
    return want


def url_for(pos, scoring_label):
    suffix = "" if pos in SCORING_FREE else SUFFIX.get(scoring_label, "")
    return BASE.format(pos=pos, suffix=suffix)


def _collect(ctx, week, profile):
    out, errors = {}, []
    for pos in positions_for(profile):
        url = url_for(pos, profile.scoring_label)
        try:
            rows = common.http_csv(url)
        except Exception as e:
            errors.append(f"{pos}: {e}")
            continue
        for r in rows:
            name = (r.get("Player.Name") or "").strip()
            if not name:
                continue
            prefix = "flex_" if pos == "FLX" else ""
            fields = {
                f"{prefix}tier": _i(r.get("Tier")),
                f"{prefix}rank": _i(r.get("Rank")),
                f"{prefix}rank_best": _i(r.get("Best.Rank")),
                f"{prefix}rank_worst": _i(r.get("Worst.Rank")),
                f"{prefix}rank_avg": _f(r.get("Avg.Rank"), None),
                f"{prefix}rank_std": _f(r.get("Std.Dev"), None),
            }
            if pos != "FLX":
                fields["tier_pos"] = pos
            rec = common.record(name, pos=None if pos == "FLX" else pos,
                                matchup=(r.get("Matchup") or "").strip() or None,
                                week=week, **fields)
            prev = out.get(rec["norm"])
            if prev:
                prev["fields"].update(rec["fields"])
                prev["pos"] = prev["pos"] or rec["pos"]
            else:
                out[rec["norm"]] = rec
    if not out and errors:
        raise RuntimeError("; ".join(errors)[:300])
    return list(out.values())


def fetch(ctx, week=None, *, refresh=False, ttl=TTL, write=True, profile=None, **_):
    return common.run_adapter(NAME, _collect, ctx, week, refresh=refresh, ttl=ttl,
                              write=write, profile=profile)
