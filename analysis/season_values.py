#!/usr/bin/env python3
"""Per-season player values on exactly the basis the console prices with.

Why this exists
---------------
Opponent multipliers are fitted as `paid / projected_value`, and the console applies them
to its own `worth`. If the value used to FIT the multiplier allocates the auction pool
differently by position than the console's `worth` does, the multiplier is applied to a
denominator it was never fitted against, and predicted prices skew by that positional gap.

That was a live bug. Multipliers were fitted against the workbook's own CheatSheet
`proj_value`, which put ~14% of its pool on quarterbacks — but the console recomputes
worth from the workbook's RAW STATS through the league's real scoring and roster, landing
at ~7% for QB, matching what the league actually spends. Fitting on one and applying to
the other read as "this room underpays QBs by 2x" when it only meant the two valuations
disagree about quarterbacks. Receivers skewed the other way and predicted prices ran ~25%
hot.

This module rebuilds the console's basis for any past season: workbook raw stats -> that
season's scraped scoring -> VBD against that season's roster/teams/budget. Fitting
multipliers against these makes numerator and denominator commensurate.

Note it deliberately does NOT use ESPN's own projections. Those are a third basis again
(they allocate ~14% to QB), so they would reintroduce exactly the mismatch this fixes.
A season is usable only if it has BOTH a tracked *_elboberto.xlsm workbook and a scrape.

    PYTHONPATH=analysis python3 analysis/season_values.py            # all seasons
    PYTHONPATH=analysis python3 analysis/season_values.py 2022 2025  # specific
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "draft_sheets"))

import build_tool_data as btd  # noqa: E402

POSITIONS = ("QB", "RB", "WR", "TE")
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
_CACHE = {}


def norm(n):
    """Name key shared with the analysis pipeline (a18.norm) so lookups line up."""
    n = re.sub(r"\([^)]*\)", "", str(n))
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def workbook_for(season):
    """The tracked projection workbook for a season, or None."""
    for path in sorted(glob.glob(os.path.join(ROOT, "draft_sheets", "*_elboberto.xlsm"))):
        m = re.search(r"(20\d{2})", os.path.basename(path))
        if m and int(m.group(1)) == int(season):
            return path
    return None


def _valued(season):
    """The season's players with fpts+vbd+worth set, or []. Cached: openpyxl is slow and
    a workbook is ~1 MB, so this must be read at most once per season per process."""
    if season in _CACHE:
        return _CACHE[season]
    _CACHE[season] = []
    path = workbook_for(season)
    league = btd.read_scraped_league(season)
    if not path or not league:
        return _CACHE[season]
    raw = btd.read_raw_projections(btd._wb(path))
    if not raw:
        # workbook predates the raw stat sheets (older layout) — no console basis for it
        return _CACHE[season]
    scoring = league.get("scoring") or btd.DEFAULT_SCORING
    for x in raw:
        x["fpts"] = btd.compute_fpts(x["stats"], scoring)
    btd.compute_values(raw, league["starters"], league["flex"], league["bench"],
                       len(league["managers"]) or 12, league["budget"])
    _CACHE[season] = raw
    return raw


def season_values(season):
    """{normalised name: worth} on the console's basis; {} when unavailable."""
    return {norm(x["name"]): x["worth"] for x in _valued(season) if x.get("worth")}


def available(seasons):
    """Which of `seasons` can be valued on the console's basis."""
    return [s for s in seasons if _valued(s)]


def positional_share(season):
    """{pos: share of the season's total worth} — the diagnostic that exposed the bug."""
    share = {}
    for x in _valued(season):
        share[x["pos"]] = share.get(x["pos"], 0.0) + max(0.0, x.get("worth") or 0.0)
    tot = sum(share.values()) or 1.0
    return {p: share.get(p, 0.0) / tot for p in POSITIONS}


if __name__ == "__main__":
    import lib
    yrs = [int(a) for a in sys.argv[1:]] or lib.ALL_SEASONS
    print("%-8s %8s %10s   %s" % ("season", "players", "pool $", "positional share"))
    for yr in yrs:
        v = season_values(yr)
        if not v:
            why = "no workbook" if not workbook_for(yr) else "no scrape / old layout"
            print("%-8d %8s   (%s)" % (yr, "—", why))
            continue
        sh = positional_share(yr)
        print("%-8d %8d %10.0f   %s"
              % (yr, len(v), sum(v.values()),
                 "  ".join("%s %.0f%%" % (p, 100 * sh.get(p, 0)) for p in POSITIONS)))
