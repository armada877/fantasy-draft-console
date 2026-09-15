#!/usr/bin/env python3
"""FantasyPros expert consensus (ECR) — the highest-value Tier-A adapter.

Rest-of-season consensus over ~408 players, refreshed continuously, free, no key.

    https://www.fantasypros.com/nfl/rankings/ros-{variant}-overall.php   (week 0)
    https://www.fantasypros.com/nfl/rankings/{variant}-{pos}.php         (weekly)

`variant` is chosen from `profile.scoring_label`, never from a constant:

    PPR        -> "ppr-"                 half-PPR -> "half-point-ppr-"
    standard   -> ""  (the bare page)

Verified live 2026-09-14: ros-ppr-overall 408 players, ros-half-point-ppr-overall
408, ros-overall 409 — all three carry `scoring` in the payload, which we assert
against the league so a silently-wrong variant cannot pass unnoticed.

**Parse the embedded `var ecrData = {...}` JSON, not the HTML tables.** The tables
are lazily rendered and change shape; the JSON blob is the same object the page's
own JS consumes and has been stable. It carries `player_name`, `player_team_id`,
`player_position_id`, `player_bye_week`, `player_owned_avg`, `rank_ecr`,
`rank_min/max/ave/std` and `pos_rank`.

No `espn_id` is published, so records join on the crosswalk (nflverse espn_id by
normalised name) and fall back to `norm` — reported honestly in coverage.

Advisory only: ECR never replaces the league's own `ros_points`. Its value is the
*disagreement* with our valuation.
"""
from __future__ import annotations

import json
import re

from . import common
from .common import _f, _i

NAME = "fantasypros"
TTL = 6 * 3600
BASE = "https://www.fantasypros.com/nfl/rankings/"

# scoring_label -> URL fragment. The one place scoring enters this module.
VARIANT = {"PPR": "ppr-", "half-PPR": "half-point-ppr-", "standard": ""}
# what FantasyPros calls it back, so we can verify we got what we asked for
EXPECT_SCORING = {"PPR": "PPR", "half-PPR": "HALF", "standard": "STD"}

# Positions whose FantasyPros page has no scoring variant (reception scoring is
# irrelevant to them), so the bare page is the only one.
SCORING_FREE = {"qb", "k", "dst"}

ECR_RE = re.compile(r"var\s+ecrData\s*=\s*(\{.*?\});", re.S)


def ros_url(scoring_label):
    v = VARIANT.get(scoring_label, "")
    return f"{BASE}ros-{v}overall.php"


def weekly_url(scoring_label, pos="flex"):
    v = "" if pos.lower() in SCORING_FREE else VARIANT.get(scoring_label, "")
    return f"{BASE}{v}{pos.lower()}.php"


def _ecr(url):
    html = common.http_get(url, accept="text/html").decode("utf-8", "replace")
    m = ECR_RE.search(html)
    if not m:
        raise RuntimeError(f"no `var ecrData` blob in {url} — page layout changed")
    return json.loads(m.group(1))


def _rows(data, prefix, extra=None):
    out = []
    for p in data.get("players") or []:
        name = p.get("player_name") or ""
        if not name:
            continue
        fields = {
            f"{prefix}ecr": _i(p.get("rank_ecr")),
            f"{prefix}rank_min": _i(p.get("rank_min")),
            f"{prefix}rank_max": _i(p.get("rank_max")),
            f"{prefix}rank_ave": _f(p.get("rank_ave"), None),
            f"{prefix}rank_std": _f(p.get("rank_std"), None),
            f"{prefix}pos_rank": p.get("pos_rank") or None,
        }
        if extra:
            fields.update(extra)
        out.append(common.record(
            name, pos=p.get("player_position_id"), team=p.get("player_team_id"),
            bye=_i(p.get("player_bye_week")),
            owned_avg=_f(p.get("player_owned_avg"), None),
            eligibility=p.get("player_eligibility") or None,
            note=(p.get("note") or None),
            **fields))
    return out


def _collect(ctx, week, profile, weekly=True):
    label = profile.scoring_label
    data = _ecr(ros_url(label))
    got = (data.get("scoring") or "").upper()
    want = EXPECT_SCORING.get(label)
    if want and got and got != want:
        raise RuntimeError(f"asked for {label} ({want}) but the page returned {got}")
    recs = {r["norm"]: r for r in _rows(data, "ros_")}
    for r in recs.values():
        r["fields"]["ros_experts"] = _i(data.get("total_experts"))
        r["fields"]["ros_updated"] = data.get("last_updated")

    # Weekly consensus, merged onto the same records. `flex` is the broadest single
    # page (RB/WR/TE); QB / K / DST only exist as their own pages, and we only ask
    # for the ones this league actually starts.
    if weekly:
        pages = ["flex"]
        for nm in sorted(profile.startable_positions):
            p = nm.lower().replace("/", "")
            if p in ("qb", "k", "dst") and p not in pages:
                pages.append(p)
        for page in pages:
            try:
                wd = _ecr(weekly_url(label, page))
            except Exception:
                continue                       # one weekly page failing is not a source failure
            wk = _i(wd.get("week"))
            for r in _rows(wd, "wk_", extra={"wk_week": wk}):
                tgt = recs.get(r["norm"])
                if tgt:
                    tgt["fields"].update(r["fields"])
                else:
                    recs[r["norm"]] = r
    return list(recs.values())


def fetch(ctx, week=None, *, refresh=False, ttl=TTL, write=True, profile=None,
          weekly=True, **_):
    return common.run_adapter(NAME, lambda c, w, p: _collect(c, w, p, weekly=weekly),
                              ctx, week, refresh=refresh, ttl=ttl, write=write,
                              profile=profile)
