#!/usr/bin/env python3
"""Which acquisition regime was a league running in a given season — and the filter
that keeps you from pooling across a switch.

`analysis/lib.py` has `regime(season)` for the AUCTION side: keeper seasons removed 12
elite players from supply, so keeper-era top-of-board prices encode a scarcity that will
not exist in a full-supply season, and CLAUDE.md is explicit that rescaling them is not
enough — you filter to `FULL_SUPPLY_SEASONS` instead. **The in-season side has exactly
the same hazard with a different name.** A league that changed how players are acquired
changed the units of every waiver statistic:

    priority order : the currency is your waiver position, a single-use depleting asset.
                     Every bid is $0. There is no such thing as a "median winning bid".
    FAAB           : the currency is money. Losing bids are recorded. Position is
                     irrelevant.

Two of the three leagues switched mid-history:

    the 12-team league            priority 2018  ->  FAAB from 2019
    chi-phi-american  priority 2022-2023  ->  FAAB from 2024

Pooling a priority season into a FAAB curve would pull every quantile toward zero with
observations that were never dollars in the first place. So: read the regime off each
season's own scraped settings, and filter.

    acquisition_regime(ctx, season)      -> "faab" | "priority"
    regime_seasons(ctx, regime)          -> [seasons in that regime]
    current_regime(ctx)                  -> the regime the league runs NOW
    regime_map(ctx)                      -> {season: regime}, for the _meta block
    switch_points(ctx)                   -> [(season, from, to)]  (audit / printing)

`python3 analysis/acquisition_regime.py [--league KEY]` prints the timeline for every
league, marking the switch.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine.profile import FAAB, PRIORITY          # noqa: E402  (the frozen names)
import analysis.league_history as H                # noqa: E402

__all__ = ["FAAB", "PRIORITY", "acquisition_regime", "regime_seasons",
           "current_regime", "regime_map", "switch_points", "filter_seasons",
           "budget", "regime_source", "regime_evidence", "infer_regime",
           "verify_inference"]


def regime_evidence(ctx, season) -> dict:
    """What that season's own waiver rows say about which currency was in use.

    A FAAB season prices claims in dollars, so `bidAmount` is non-zero somewhere; a
    priority season prices them in waiver position, so every `bidAmount` is 0 by
    construction. (Losing claimants are recorded in BOTH regimes — the 12-team league 2018 files
    them as FAILED_MATCHUPACQUISITIONLIMIT, chi-phi-american 2022-23 as
    FAILED_INVALIDPLAYERSOURCE — so the loser status identifies a contest, not a
    regime. Only the money does.)
    """
    rows = [t for t in H.load_transactions(ctx, season) if t.get("type") == "WAIVER"]
    bids = [int(t.get("bidAmount") or 0) for t in rows]
    return {"waiver_rows": len(rows),
            "nonzero_bids": sum(1 for b in bids if b > 0),
            "max_bid": max(bids) if bids else 0}


def infer_regime(ctx, season):
    """Regime from transaction evidence alone, or None if there is not enough of it."""
    ev = regime_evidence(ctx, season)
    if ev["nonzero_bids"]:
        return FAAB
    if ev["waiver_rows"] >= MIN_ROWS_TO_INFER:
        return PRIORITY
    return None


def regime_source(ctx, season) -> str:
    """"settings" | "inferred-from-transactions" | "unknown"."""
    if H.has_settings(ctx, season):
        return "settings"
    return ("inferred-from-transactions" if infer_regime(ctx, season) else "unknown")


def acquisition_regime(ctx, season) -> str:
    """"faab" | "priority" for THIS league in THIS season.

    Primary source is the season's own scraped settings — ESPN's
    `isUsingAcquisitionBudget`, read through `LeagueProfile.acquisition.model`, never a
    league name or a hardcoded year.

    WS-1's multi-season transaction bundle backfills seasons whose league_full.json was
    never scraped (chi-phi-american 2022-2025, inlaws-outlaws 2025). Throwing those away
    would discard most of two leagues' history; assuming they ran today's rules would
    reintroduce exactly the pooling bug this module exists to prevent. So they fall back
    to `infer_regime`, which reads the currency off the transactions themselves. The
    inference is checked against the settings on every season where both exist —
    `verify_inference()` / `--verify` — and it agrees on all nine the 12-team league seasons,
    including the 2018->19 switch.

    Raises only when neither source can answer.
    """
    if H.has_settings(ctx, season):
        return H.profile(ctx, season).acquisition.model
    got = infer_regime(ctx, season)
    if got:
        return got
    raise FileNotFoundError(
        f"{ctx.key}: season {season} has no league_full.json and too few waiver rows "
        f"({regime_evidence(ctx, season)}) to infer its acquisition regime. Scrape that "
        f"season's settings, or drop it from the season list.")


def verify_inference(ctx) -> list:
    """[(season, from_settings, inferred, agrees)] over seasons that have both.

    The honesty check on the fallback above: if the evidence rule disagreed with the
    settings anywhere, the fallback would be unusable and this prints it.
    """
    out = []
    for yr in H.settings_seasons(ctx):
        if not H.load_transactions(ctx, yr):
            continue
        truth = H.profile(ctx, yr).acquisition.model
        guess = infer_regime(ctx, yr)
        if guess:
            out.append((yr, truth, guess, truth == guess))
    return out


MIN_ROWS_TO_INFER = 20      # below this, "every bid was $0" is not evidence of anything


def budget(ctx, season):
    """FAAB dollars per team that season (None in a priority season).

    the 12-team league is a flat $100 in every FAAB season, so — unlike the auction side — no
    effective-wallet correction is needed here (inseason_plan 3d says so explicitly).
    The helper exists anyway so a league that DID change its FAAB budget can be
    normalized to the current one instead of pooling incomparable dollars.
    """
    prof, _exact = H.profile_or_current(ctx, season)
    if prof is None:
        return None
    if acquisition_regime(ctx, season) != FAAB:
        return None
    return prof.acquisition.budget


def regime_map(ctx, seasons=None) -> dict:
    """{season(int): regime} for every season with scraped settings."""
    out = {}
    for yr in (seasons if seasons is not None else H.available_seasons(ctx)):
        try:
            out[int(yr)] = acquisition_regime(ctx, yr)
        except FileNotFoundError:
            continue
    return out


def filter_seasons(ctx, seasons, regime) -> list:
    """THE FILTER. Keep only the seasons matching `regime` — the in-season analogue of
    `lib.FULL_SUPPLY_SEASONS`."""
    rm = regime_map(ctx, seasons)
    return [int(s) for s in seasons if rm.get(int(s)) == regime]


def regime_seasons(ctx, regime=None, with_transactions=True) -> list:
    """Seasons usable for calibration in `regime` (default: the league's current one).

    `with_transactions=True` also drops seasons whose transaction log is empty — ESPN's
    historical endpoint returns an empty list for the 12-team league's 2013-2017, so "the file
    exists" is not the same question as "there is history here".
    """
    regime = regime or current_regime(ctx)
    pool = H.transaction_seasons(ctx) if with_transactions else H.available_seasons(ctx)
    return filter_seasons(ctx, pool, regime)


def current_regime(ctx) -> str:
    """The regime the league runs NOW — the only one a live recommendation may use."""
    return acquisition_regime(ctx, ctx.season)


def switch_points(ctx) -> list:
    """[(season, from_regime, to_regime)] — every mid-history switch, for the audit."""
    rm = regime_map(ctx)
    out, prev = [], None
    for yr in sorted(rm):
        if prev is not None and rm[yr] != prev:
            out.append((yr, prev, rm[yr]))
        prev = rm[yr]
    return out


# ── audit ────────────────────────────────────────────────────────────────────
def describe(ctx) -> str:
    rm = regime_map(ctx)
    if not rm:
        return f"{ctx.key}: no scraped settings — regime unknown."
    txn = set(H.transaction_seasons(ctx))
    cur = current_regime(ctx)
    lines = [f"{ctx.key}  (season {ctx.season}, now: {cur.upper()})"]
    prev = None
    for yr in sorted(rm):
        mark = "  <- SWITCH" if prev and rm[yr] != prev else ""
        has = "txns" if yr in txn else "no txns"
        keep = "KEEP" if (rm[yr] == cur and yr in txn) else "drop"
        src = regime_source(ctx, yr)
        ev = ("" if src == "settings"
              else f"  [{regime_evidence(ctx, yr)['nonzero_bids']} non-$0 bids]")
        lines.append(f"    {yr}  {rm[yr]:<9} {has:<8} {keep:<5} via {src}{ev}{mark}")
        prev = rm[yr]
    usable = regime_seasons(ctx)
    lines.append(f"    -> {cur} calibration pool: {usable or 'EMPTY'}")
    for yr, a, b in switch_points(ctx):
        lines.append(f"    -> never pool across {yr} ({a} -> {b})")
    ver = verify_inference(ctx)
    if ver:
        bad = [v for v in ver if not v[3]]
        lines.append(f"    -> inference check: {len(ver) - len(bad)}/{len(ver)} seasons "
                     f"where settings AND transactions both exist agree"
                     + (f"  MISMATCHES: {bad}" if bad else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--league", help="league key; default: every registered league")
    args = ap.parse_args(argv)
    import leagues
    ctxs = [leagues.resolve(args.league)] if args.league else leagues.all()
    for ctx in ctxs:
        print(describe(ctx))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
