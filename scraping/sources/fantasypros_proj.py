#!/usr/bin/env python3
"""FantasyPros PROJECTIONS — the only candidate that could replace the baseline.

The `fantasypros` adapter pulls ECR, which is a RANKING. A ranking cannot be a
baseline: `ros_points` is measured in points, every downstream number (marginal
value, FAAB ceiling, trade delta) is a difference of points, and no monotone map
from rank to points is implied by the data. So "use FantasyPros instead of ESPN"
was not even expressible until this adapter existed.

    https://www.fantasypros.com/nfl/projections/{pos}.php?week={N}&scoring={SC}
    https://www.fantasypros.com/nfl/projections/ros-{pos}.php?scoring={SC}

**What makes this usable as a baseline is that the tables are COMPONENT STATS, not
just a points total** — rushing yards, receptions, passing TDs. That means the
projection can be re-scored in each league's own scoring exactly the way
`engine.valuation` re-scores ESPN's, instead of importing FantasyPros' generic PPR /
half-PPR total. Taking their FPTS column would silently import THEIR league's rules
(the mistake `extract_csg.py` documents for the CSG sheet's baked-in values).

So each record carries `stats` keyed by **ESPN statId**, and the consumer applies
`profile.scoring`. The map is the same one `build_tool_data.ESPN_STAT` uses:

    3 pass yds · 4 pass TD · 20 INT · 24 rush yds · 25 rush TD
    42 rec yds · 43 rec TD · 53 receptions · 72 fumbles lost

**THE FINDING THAT SETTLES THE BASELINE QUESTION (verified 2026-09-15).** The free
projections HTML serves exactly **TEN players per position** — a teaser; the rest of
the table is behind the paywall. Measured: `rb.php` 10 rows, `wr.php` 10, `te.php`
10, `flex.php` 10, and `&max=200`, `&filters=1` and the bare page all return the
same 10. The `ros-{pos}.php` slug does not exist at all — it silently serves the QB
page, so "FantasyPros rest-of-season projections" are not a public artifact either.

Consequence, stated plainly: **FantasyPros cannot be the projection baseline.** A
baseline has to price ~1,000 players including every free agent a waiver claim might
touch; 10 running backs cannot rank a waiver wire. What FantasyPros publishes in
full is the ECR *ranking* (~408 players, the `fantasypros` adapter) — an ordering,
not points, and an ordering cannot produce `marginal`, a FAAB ceiling or a trade
delta. So ESPN's projection stays the baseline on availability grounds, before any
question of accuracy, and the accuracy question is settled where it can be: ECR as a
RANKING against ESPN's ordering, forward-tested in `analysis/backtest_sources.py`.

This adapter is kept, and deliberately left OUT of the default fetch order, for two
reasons: the top-10 per position is a real cross-check on our own numbers at the
expensive end of the board, and its archive is the record that this avenue was
tried, measured and closed — so it does not get re-attempted every season.

HONEST COVERAGE LIMITS, none of them papered over:

* **QB / RB / WR / TE only.** The K and D/ST tables are not re-scorable: kickers are
  projected as FG/FGA/XPT with no distance split, while every league here scores
  field goals by distance bucket; defenses are projected with a points-allowed
  total, while ESPN scores points-allowed TIERS through `pointsOverrides`. Their
  FantasyPros FPTS is carried as advisory only, and both positions stay on the ESPN
  baseline. That is ~18% of a roster.
* **Two-point conversions and return TDs are not projected at all**, so a league
  that scores them (all three here do) gets a slightly low FantasyPros number. It is
  a small term; it is also a reason the head-to-head must be measured, not assumed.
* **Weekly is the CURRENT week only**, and `ros-{pos}.php` is not a real page, so
  `ros_*` fields are only populated when FantasyPros publishes them; today they are
  not. There is no `week=` archive for past weeks, so this source can never be
  backtested retroactively, only forward from the first snapshot.
  `common.archive()` is what makes that possible at all.
* **No `espn_id` is published**, so records join on the nflverse crosswalk and fall
  back to the normalised name, exactly like the ECR adapter.

Advisory until proven otherwise: nothing here replaces `ros_points`. It is the
challenger in `analysis/backtest_sources.py`, and it ships as the baseline only if
that forward test says it beat ESPN under `engine.projection_policy`'s bar.
"""
from __future__ import annotations

import html
import re

from . import common
from .common import _f, _i

NAME = "fantasypros_proj"
TTL = 6 * 3600
BASE = "https://www.fantasypros.com/nfl/projections/"

# scoring_label -> FantasyPros' `scoring` parameter.
SCORING = {"PPR": "PPR", "half-PPR": "HALF", "standard": "STD"}

# Positions whose component stats map cleanly onto ESPN statIds. K and DST do not
# (see the module docstring) and are fetched for advisory FPTS only.
RESCORABLE = ("qb", "rb", "wr", "te")
ADVISORY_ONLY = ("k", "dst")

# (column group, column header) -> ESPN statId. The group is what disambiguates
# YDS and TDS, which appear under both PASSING and RUSHING on the QB table.
STAT_ID = {
    ("PASSING", "YDS"): 3, ("PASSING", "TDS"): 4, ("PASSING", "INTS"): 20,
    ("RUSHING", "YDS"): 24, ("RUSHING", "TDS"): 25,
    ("RECEIVING", "REC"): 53, ("RECEIVING", "YDS"): 42, ("RECEIVING", "TDS"): 43,
    ("MISC", "FL"): 72,
}

TABLE_RE = re.compile(r'<table[^>]*id="data".*?</table>', re.S)
ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
TAG_RE = re.compile(r"<[^>]+>")
COLSPAN_RE = re.compile(r'<t[dh]([^>]*)>')
NAME_RE = re.compile(r'fp-player-name="([^"]*)"')
TEAM_RE = re.compile(r"</a>\s*([A-Z]{2,3})\s*$", re.S)


def url_for(pos, scoring_label, week=None):
    sc = SCORING.get(scoring_label, "STD")
    if week:
        return f"{BASE}{pos}.php?week={int(week)}&scoring={sc}"
    return f"{BASE}ros-{pos}.php?scoring={sc}"


def _text(cell):
    return html.unescape(TAG_RE.sub("", cell)).strip()


def _headers(thead_rows):
    """Flatten FantasyPros' two-row header into [(group, column), ...].

    The first row carries group labels with colspans (PASSING x5, RUSHING x3); the
    second carries the columns. Expanding the colspans is what lets RUSHING/YDS and
    PASSING/YDS resolve to different statIds instead of colliding.
    """
    if not thead_rows:
        return []
    if len(thead_rows) == 1:
        return [("", _text(c).upper()) for c in CELL_RE.findall(thead_rows[0])]
    groups = []
    cells = CELL_RE.findall(thead_rows[0])
    spans = COLSPAN_RE.findall(thead_rows[0])
    for cell, attrs in zip(cells, spans):
        m = re.search(r'colspan="?(\d+)', attrs)
        groups.extend([_text(cell).upper()] * (int(m.group(1)) if m else 1))
    cols = [_text(c).upper() for c in CELL_RE.findall(thead_rows[1])]
    while len(groups) < len(cols):
        groups.append("")
    return list(zip(groups, cols))


def parse_table(html_text, pos):
    """[(name, team, {statId: amount}, fpts), ...] from one projections page."""
    m = TABLE_RE.search(html_text)
    if not m:
        raise RuntimeError(f"no table#data on the {pos} projections page — layout changed")
    table = m.group(0)
    head_end = table.find("</thead>")
    header = _headers(ROW_RE.findall(table[:head_end]))
    if not header:
        raise RuntimeError(f"unreadable header on the {pos} projections page")
    out = []
    for row in ROW_RE.findall(table[head_end:]):
        cells = CELL_RE.findall(row)
        if len(cells) < 2:
            continue
        nm = NAME_RE.search(cells[0])
        if not nm:
            continue
        name = html.unescape(nm.group(1)).strip()
        # the team abbreviation trails the closing </a>, so this must run on the
        # RAW cell — stripping tags first removes the anchor it keys off.
        tm = TEAM_RE.search(cells[0])
        team = tm.group(1) if tm else None
        stats, fpts = {}, None
        for (group, col), cell in zip(header[1:], cells[1:]):
            val = _f(_text(cell).replace(",", ""), None)
            if val is None:
                continue
            if col == "FPTS":
                fpts = val
                continue
            sid = STAT_ID.get((group, col))
            if sid is not None:
                stats[sid] = val
        out.append((name, team, stats, fpts))
    return out


def _collect(ctx, week, profile, weekly=True, ros=False):
    label = profile.scoring_label
    recs = {}
    problems = []

    def merge(pos, prefix, wk=None):
        try:
            page = common.http_get(url_for(pos, label, wk),
                                   accept="text/html").decode("utf-8", "replace")
            rows = parse_table(page, pos)
        except Exception as e:                  # one page failing is not a source failure
            problems.append(f"{prefix}{pos}: {type(e).__name__}: {e}")
            return
        for name, team, stats, fpts in rows:
            key = common.norm_name(name)
            rec = recs.get(key)
            if rec is None:
                rec = common.record(name, pos=pos.upper(), team=team)
                recs[key] = rec
            f = rec["fields"]
            # statIds are carried as STRING keys so a JSON round-trip cannot turn
            # them into something `profile.scoring` (int-keyed) fails to find.
            if stats and pos in RESCORABLE:
                f[f"{prefix}stats"] = {str(k): v for k, v in stats.items()}
                f[f"{prefix}rescorable"] = True
            elif pos in ADVISORY_ONLY:
                f[f"{prefix}rescorable"] = False
            if fpts is not None:
                f[f"{prefix}fpts_fp"] = fpts       # FantasyPros' OWN scoring, advisory
            if wk:
                f["wk_week"] = int(wk)

    positions = list(RESCORABLE) + [p for p in ADVISORY_ONLY
                                    if p.upper().replace("DST", "D/ST")
                                    in {s.upper() for s in profile.startable_positions}
                                    or p == "dst" and "DST" in profile.startable_positions]
    for pos in positions:
        if ros:
            merge(pos, "ros_")
        if weekly and week:
            merge(pos, "wk_", wk=week)
    if not recs:
        raise RuntimeError("; ".join(problems) or "no rows parsed")
    for rec in recs.values():
        rec["fields"]["scoring_variant"] = SCORING.get(label, "STD")
        if problems:
            rec["fields"]["partial"] = "; ".join(problems)[:200]
    return list(recs.values())


def rescore(fields, scoring: dict, prefix="wk_"):
    """FantasyPros component stats -> points under THIS league's scoring.

    Returns None when the record is not re-scorable (K, D/ST, or a page that did not
    parse), which the caller must treat as "no opinion" rather than as zero.
    """
    stats = (fields or {}).get(f"{prefix}stats")
    if not stats:
        return None
    total = 0.0
    for sid, pts in (scoring or {}).items():
        if not pts:
            continue
        amount = stats.get(str(sid), stats.get(sid))
        if amount:
            total += float(amount) * float(pts)
    return round(total, 2)


def fetch(ctx, week=None, *, refresh=False, ttl=TTL, write=True, profile=None,
          weekly=True, ros=False, **_):
    return common.run_adapter(NAME,
                              lambda c, w, p: _collect(c, w, p, weekly=weekly, ros=ros),
                              ctx, week, refresh=refresh, ttl=ttl, write=write,
                              profile=profile)
