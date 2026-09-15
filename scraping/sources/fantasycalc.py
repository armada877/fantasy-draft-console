#!/usr/bin/env python3
"""FantasyCalc redraft trade values — the market check for the trade half.

    https://api.fantasycalc.com/values/current
        ?isDynasty=false&numQbs={N}&numTeams={S}&ppr={P}

**Every one of those three parameters comes off the profile**, which matters more
here than anywhere else: FantasyCalc re-prices the board for the settings you pass,
so a wrong parameter is a wrong number rather than a missing one.

    numQbs   = QB-ish starting slots (QB + TQB + OP/superflex), clamped to 1..2
    numTeams = profile.size
    ppr      = 1.0 / 0.5 / 0.0 from profile.scoring_label

Verified live 2026-09-14 — the parameterisation visibly changes the board:

    numQbs=1 numTeams=12 ppr=0.5  ->  #1 Gibbs 10570, #2 Bijan 9994
    numQbs=2 numTeams=8  ppr=1.0  ->  #1 Gibbs 10272, #2 **Josh Allen** 9669
    numQbs=2 numTeams=14 ppr=0.0  ->  #1 **Josh Allen** 10302, #2 Gibbs 10298

Each row carries `player.espnId`, so this joins on the preferred key with no name
matching at all.

Advisory only. `value` is a market price, not our valuation — the trade tools use it
to price *acceptance odds*, never to decide whether a trade is good.
"""
from __future__ import annotations

from . import common
from .common import _f, _i

NAME = "fantasycalc"
TTL = 12 * 3600
URL = ("https://api.fantasycalc.com/values/current"
       "?isDynasty=false&numQbs={qbs}&numTeams={teams}&ppr={ppr}")

PPR = {"PPR": 1.0, "half-PPR": 0.5, "standard": 0.0}
QBISH_SLOTS = {"QB", "TQB", "OP"}          # OP/superflex counts as a QB seat here


def params_for(profile):
    """(numQbs, numTeams, ppr) — derived from the league, never assumed."""
    from engine.profile import slot_name
    qbish = sum(n for s, n in profile.starting_slots.items()
                if slot_name(s) in QBISH_SLOTS)
    return (min(max(int(qbish) or 1, 1), 2), int(profile.size),
            PPR.get(profile.scoring_label, 0.0))


def url_for(profile):
    qbs, teams, ppr = params_for(profile)
    return URL.format(qbs=qbs, teams=teams, ppr=ppr)


def _collect(ctx, week, profile):
    qbs, teams, ppr = params_for(profile)
    data = common.http_json(URL.format(qbs=qbs, teams=teams, ppr=ppr))
    out = []
    for row in data or []:
        p = row.get("player") or {}
        name = p.get("name") or ""
        if not name:
            continue
        out.append(common.record(
            name, pos=p.get("position"), team=p.get("maybeTeam"),
            espn_id=p.get("espnId"),
            trade_value=_i(row.get("value")),
            redraft_value=_i(row.get("redraftValue")),
            overall_rank=_i(row.get("overallRank")),
            position_rank=_i(row.get("positionRank")),
            tier=_i(row.get("maybeTier")),
            trend_30d=_i(row.get("trend30Day")),
            trade_frequency=_f(row.get("maybeTradeFrequency"), None),
            sleeper_id=p.get("sleeperId") or None,
            # the settings this price was computed FOR — so a consumer can never
            # mistake an 8-team 2QB price for a 12-team 1QB one
            fc_num_qbs=qbs, fc_num_teams=teams, fc_ppr=ppr))
    return out


def fetch(ctx, week=None, *, refresh=False, ttl=TTL, write=True, profile=None, **_):
    return common.run_adapter(NAME, _collect, ctx, week, refresh=refresh, ttl=ttl,
                              write=write, profile=profile)
