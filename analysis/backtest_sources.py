#!/usr/bin/env python3
"""WS-8e — does any external source beat ESPN's projection at picking a lineup?

The honest version of "our numbers vs FantasyPros". It cannot be answered from
history: every source here publishes only its CURRENT values, and none sells an
archive, so the question is answerable ONLY forward, from the first snapshot
`scraping/sources/common.archive()` froze. Until enough weeks accumulate this file
prints **insufficient data** and refuses to produce a number. That is the finding,
not a placeholder — a harness that guesses while it waits is worse than no harness.

Why a RANKING test rather than an accuracy test: FantasyPros publishes a full
~408-player consensus rank and a ten-player projections teaser (see
`scraping/sources/fantasypros_proj.py`). You cannot score a rank with MAE. But you
can do the thing that actually matters — order the roster by it, start the lineup it
implies, and count the points that really came in. Boris Chen's tiers and
FantasyCalc's trade values are orderings too, so the same test covers all of them
and the comparison stays like-for-like.

    baseline   rank the roster by ESPN's weekly projection
    source     rank it by the source's own ordering, falling back to ESPN's for any
               player the source has no opinion on (a source is not penalised for
               silence — otherwise a 400-player list would be punished for not
               covering the 12th man on a bench)
    hindsight  the ceiling, for scale

Each archived snapshot is checked against the games it is supposed to precede: a
snapshot taken after that week's Thursday is flagged `contaminated` and excluded,
because a "projection" that has seen the results is not a projection. Everything is
reported per league, never pooled.

Run:
    python3 analysis/backtest_sources.py
    python3 analysis/backtest_sources.py --league 2kdome --min-weeks 1
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import leagues                                                       # noqa: E402
from analysis.backtest_projections import (ACT, PID, POS, PROJ,      # noqa: E402
                                           SEASON, SLOT, TEAM, WEEK, load_rows)
from analysis.backtest_lineups import (MIN_STARTERS, learn_eligibility,  # noqa: E402
                                       realized, team_weeks)
from engine.profile import BENCH_SLOTS                               # noqa: E402

# Weeks of archived snapshots before the harness will report a number at all. One
# week is an anecdote; the bar exists so nobody quotes a single Sunday as evidence.
MIN_WEEKS = 4

ARCHIVE_RE = re.compile(r"^(?P<name>.+)_(?P<season>\d{4})_wk(?P<week>\d{2})\.json$")

# The ordering field each source publishes, best (lowest) first. `None` means the
# field is a VALUE, where higher is better, and the sign is flipped.
ORDER_FIELD = {
    "fantasypros": ("wk_ecr", "ros_ecr"),
    "borischen": ("tier", "rank"),
    "fantasycalc": (None, "trade_value", "redraft_value"),
    "sleeper": (None, "trend_adds"),
}


def archives(ctx):
    """[(source, season, week, path), ...] of every frozen snapshot for this league."""
    out = []
    for path in sorted(glob.glob(ctx.raw(os.path.join("sources", "archive", "*.json")))):
        m = ARCHIVE_RE.match(os.path.basename(path))
        if m:
            out.append((m["name"], int(m["season"]), int(m["week"]), path))
    return out


def contaminated(env, season, week) -> str | None:
    """A snapshot that postdates the games it claims to forecast is not evidence.

    NFL weeks open on Thursday, so a snapshot archived on a Friday or later saw at
    least one result. We cannot read kickoff times here, so the test is deliberately
    crude and conservative: flag it, exclude it, and say which one.
    """
    stamp = env.get("_archived") or env.get("fetched")
    if not stamp:
        return "no timestamp"
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return f"unparseable timestamp {stamp!r}"
    # Monday=0 ... Thursday=3. Tue/Wed are the waiver-run days these leagues use.
    return (f"archived {when:%a %Y-%m-%d}, after the week opened" if when.weekday() >= 4
            else None)


def source_order(env):
    """{espn_id or norm: sort key}, lower = better. None when the source has no
    usable ordering field in this snapshot."""
    fields = ORDER_FIELD.get(env.get("source")) or ()
    if not fields:
        return None, None
    ascending = fields[0] is not None
    names = [f for f in fields if f]
    out, used = {}, None
    for rec in env.get("records") or []:
        f = rec.get("fields") or {}
        for nm in names:
            v = f.get(nm)
            if v is None:
                continue
            used = used or nm
            if nm != used:
                continue
            key = rec.get("espn_id")
            if key is None:
                continue
            out[int(key)] = float(v) if ascending else -float(v)
            break
    return (out or None), used


def week_result(rows, elig, order, season, week):
    """Realized points per team-week for baseline vs the source's ordering."""
    recs = []
    for (s, w, team), group in team_weeks(rows).items():
        if s != season or w != week:
            continue
        started = [r for r in group if int(r[SLOT]) not in BENCH_SLOTS and int(r[SLOT]) >= 0]
        if len(started) < MIN_STARTERS:
            continue
        slots = defaultdict(int)
        for r in started:
            slots[int(r[SLOT])] += 1
        actual_by_id = {int(r[PID]): float(r[ACT]) for r in group}

        # The source ranks players; the solver maximises a score. Rank 1 must beat
        # rank 50, so the score is the negated rank — and a player the source is
        # silent about keeps his ESPN ordering rather than being buried, which would
        # measure coverage instead of skill.
        by_proj = sorted(group, key=lambda r: -float(r[PROJ]))
        espn_rank = {int(r[PID]): i for i, r in enumerate(by_proj)}
        covered = sum(1 for r in group if int(r[PID]) in order)

        def src_score(r):
            pid = int(r[PID])
            return -order[pid] if pid in order else -float(espn_rank[pid])

        recs.append({
            "team": team, "covered": covered, "roster": len(group),
            "baseline": realized(group, elig, slots, lambda r: float(r[PROJ]), actual_by_id),
            "source": realized(group, elig, slots, src_score, actual_by_id),
            "hindsight": realized(group, elig, slots, lambda r: float(r[ACT]), actual_by_id),
        })
    return recs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 analysis/backtest_sources.py",
        description="WS-8e — forward test: does an external source pick better lineups?")
    ap.add_argument("--league", "-l", action="append", metavar="KEY")
    ap.add_argument("--min-weeks", type=int, default=MIN_WEEKS)
    ap.add_argument("--out", default=os.path.join(ROOT, "reports", "source_value.json"))
    args = ap.parse_args(argv)

    ctxs = ([leagues.resolve(k) for k in args.league] if args.league else leagues.all())
    art = {"generated": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
           "min_weeks": args.min_weeks,
           "method": ("forward test only — no source publishes a historical archive, "
                      "so evidence starts at the first frozen snapshot"),
           "leagues": {}}
    print("WS-8e — external sources vs the ESPN projection, as lineup pickers\n")

    for ctx in ctxs:
        rows, _, err = load_rows(ctx)
        graded = {(int(r[SEASON]), int(r[WEEK])) for r in rows}
        elig = learn_eligibility(rows) if rows else {}
        snaps = archives(ctx)
        block = {"snapshots": len(snaps), "sources": {}, "skipped": []}
        by_source = defaultdict(list)
        for name, season, week, path in snaps:
            if (season, week) not in graded:
                block["skipped"].append(f"{name} {season} wk{week}: not played yet")
                continue
            try:
                with open(path) as f:
                    env = json.load(f)
            except (OSError, ValueError) as e:
                block["skipped"].append(f"{name} {season} wk{week}: unreadable ({e})")
                continue
            bad = contaminated(env, season, week)
            if bad:
                block["skipped"].append(f"{name} {season} wk{week}: EXCLUDED — {bad}")
                continue
            order, field = source_order(env)
            if not order:
                block["skipped"].append(f"{name} {season} wk{week}: no usable ordering field")
                continue
            recs = week_result(rows, elig, order, season, week)
            if recs:
                by_source[name].append({"season": season, "week": week,
                                        "field": field, "recs": recs})

        for name, weeks in sorted(by_source.items()):
            flat = [r for w in weeks for r in w["recs"]]
            deltas = [r["source"] - r["baseline"] for r in flat]
            mean = statistics.fmean(deltas) if deltas else 0.0
            sd = statistics.pstdev(deltas) if len(deltas) > 1 else 0.0
            se = sd / (len(deltas) ** 0.5) if deltas else 0.0
            enough = len(weeks) >= args.min_weeks
            block["sources"][name] = {
                "weeks": len(weeks), "team_weeks": len(flat),
                "field": weeks[0]["field"],
                "mean_coverage": round(statistics.fmean(
                    [r["covered"] / r["roster"] for r in flat]), 3) if flat else None,
                "points_delta": round(mean, 4) if enough else None,
                "se": round(se, 4) if enough else None,
                "t": (round(mean / se, 2) if (enough and se) else None),
                "verdict": ("insufficient data — "
                            f"{len(weeks)} graded week(s), need {args.min_weeks}"
                            if not enough else
                            ("beats the ESPN projection" if mean > 0 and se and mean / se >= 2
                             else "no significant difference from the ESPN projection")),
            }
        art["leagues"][ctx.key] = block

        print(f"[{ctx.key}] {len(snaps)} archived snapshot(s), "
              f"{len(by_source)} gradeable source(s)")
        if err:
            print(f"  ! {err}")
        for name, s in sorted(block["sources"].items()):
            d = f"{s['points_delta']:+.3f}" if s["points_delta"] is not None else "  n/a"
            print(f"  {name:16s} {s['weeks']:2d} wk · {s['team_weeks']:4d} team-weeks · "
                  f"Δ {d} pts · {s['verdict']}")
        if not block["sources"]:
            print("  no source-week is gradeable yet — the archive starts from the "
                  "first snapshot taken after this harness shipped")
        for s in block["skipped"][:6]:
            print(f"    · {s}")
        if len(block["skipped"]) > 6:
            print(f"    · ... and {len(block['skipped']) - 6} more")
        print()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(art, f, indent=1)
    print(f"-> {os.path.relpath(args.out, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
