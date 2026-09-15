#!/usr/bin/env python3
"""End-to-end smoke test for the in-season valuation (WS-1).

Loads what `scraping/scrape_season.py` wrote for EVERY league on the account,
builds `engine.lineup.Player` objects through `engine.valuation`, and prints:

  * the league profile as scraped (team count, scoring, lineup shape)
  * the top free agents by rest-of-season points
  * your own team's optimal starting lineup

There is not one league-specific line below. The 8-team league fields two QBs and
a kicker, the 12-team league fields neither, and that falls out of each league's
own `lineupSlotCounts` plus each player's own `eligibleSlots`.

    python3 tests/season_smoke.py
    python3 tests/season_smoke.py --league chi-phi-american --top 10
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import leagues  # noqa: E402
from engine import valuation as val  # noqa: E402
from engine.lineup import marginal_add, optimal_lineup  # noqa: E402
from engine.profile import LeagueProfile, slot_name  # noqa: E402


def _load(path):
    with open(path) as f:
        return json.load(f)


def _players_file(ctx):
    """The most recent players_wk{N}.json this league has."""
    hits = glob.glob(ctx.raw("players_wk*.json"))
    if not hits:
        return None
    return max(hits, key=lambda p: int("".join(c for c in os.path.basename(p) if c.isdigit())))


def run_league(ctx, top=5) -> bool:
    lf = ctx.raw("league_full.json")
    pf = _players_file(ctx)
    if not (os.path.exists(lf) and pf):
        print(f"\n== {ctx.key}: no scrape yet — run scraping/scrape_season.py --league {ctx.key}")
        return False

    payload = _load(lf)
    profile = LeagueProfile.from_espn(payload)
    week = _week_of(payload, pf)
    pro = {int(k): v for k, v in _load(ctx.raw("pro_teams.json")).items()} \
        if os.path.exists(ctx.raw("pro_teams.json")) else {}

    raw = _load(pf)
    players = val.build_players(raw, profile, week, pro_teams=pro, league_payload=payload)

    # Fidelity gate: our recomputed scoring must reproduce ESPN's own appliedTotal.
    # If it does not, every number below is quietly wrong.
    bad = _scoring_mismatches(raw, profile, payload)

    reg = val.regular_weeks(profile, week)
    post = val.playoff_weeks(profile)

    print(f"\n\033[1m== {ctx.key} — {profile.name}\033[0m")
    print("   " + profile.summary().splitlines()[1].strip())
    print("   " + profile.summary().splitlines()[2].strip())
    print(f"   week {week} | {len(players)} players priced | "
          f"regular horizon wk{reg[0]}-{reg[-1]} ({len(reg)}) | "
          f"playoff horizon wk{post[0]}-{post[-1]} ({len(post)})")
    print(f"   scoring fidelity: {'PASS' if not bad else 'FAIL'} — recomputed FPTS "
          f"matches ESPN appliedTotal on every projected player"
          + (f" EXCEPT {len(bad)}: {bad[:3]}" if bad else ""))

    # ── top free agents by rest-of-season points ─────────────────────────────
    fas = sorted(val.free_agents(players), key=lambda p: -p.ros_points)[:top]
    print(f"\n   top {top} free agents by ROS points")
    for i, p in enumerate(fas, 1):
        bye = f"bye {p.bye_week}" if p.bye_week else "bye —"
        print(f"     {i}. {p.name:<24} {p.pos:<4} {p.team:<4} "
              f"ROS {p.ros_points:6.1f}  {p.points_per_game:5.2f}/gm  "
              f"({p.games_remaining} gm, {bye}) own {p.pct_owned:5.1f}%"
              + (f"  [{p.injury}]" if p.injury not in ("", "ACTIVE") else ""))

    # ── my optimal lineup ────────────────────────────────────────────────────
    roster = val.team_roster(players, ctx.team_id)
    if not roster:
        print(f"\n   !! no players found on team #{ctx.team_id} in the pool")
        return False
    total, lineup, bench = optimal_lineup(roster, profile)
    print(f"\n   {ctx.team_name} (team #{ctx.team_id}) — optimal lineup, "
          f"{total:.2f} pts/wk from {len(roster)} rostered")
    for slot in sorted(lineup):
        for p in lineup[slot]:
            print(f"     {slot_name(slot):<5} {p.name:<24} {p.pos:<4} {p.team:<4} "
                  f"{p.points_per_game:5.2f}/gm")
        if not lineup[slot]:
            print(f"     {slot_name(slot):<5} \033[33m(empty — no eligible player)\033[0m")
    print(f"     BE    {', '.join(p.name for p in sorted(bench, key=lambda x: -x.points_per_game)) or '—'}")

    started = {slot_name(s): len(v) for s, v in lineup.items() if v}
    print(f"   started: {', '.join(f'{k}x{n}' for k, n in sorted(started.items()))}")

    # Proof the valuation composes with the decision primitives: raw ROS points are
    # NOT the waiver ranking — what a pickup is worth is what he adds to THIS lineup
    # over THIS horizon, after dropping whoever costs least.
    print("\n   same free agents, valued as adds to this roster (marginal_add)")
    for p in fas[:3]:
        gain, drop = marginal_add(roster, p, profile, reg)
        print(f"     {p.name:<24} ROS {p.ros_points:6.1f} -> "
              f"{gain:+7.1f} pts over wk{reg[0]}-{reg[-1]}"
              + (f"  (drop {drop.name})" if drop else "  (no drop needed)"))
    return not bad


def _scoring_mismatches(raw, profile, payload, tol=0.01):
    """Players whose recomputed full-season projection differs from ESPN's."""
    overrides = val.position_overrides(payload)
    tables, out = {}, []
    for e in val._entries(raw):
        p = val.player_of(e)
        s = val.stat_entry(e, val.SRC_PROJECTED, val.SPLIT_SEASON, profile.season)
        if s is None or s.get("appliedTotal") is None:
            continue
        pid = int(p.get("defaultPositionId") or 0)
        if pid not in tables:
            tables[pid] = val.effective_scoring(profile, overrides, pid)
        got = val.scoring_points(s.get("stats") or {}, profile, tables[pid])
        if abs(got - float(s["appliedTotal"])) > tol:
            out.append((p.get("fullName"), round(got, 2), round(float(s["appliedTotal"]), 2)))
    return out


def _week_of(payload, players_path):
    st = (payload or {}).get("status") or {}
    for key in ("latestScoringPeriod", "currentMatchupPeriod"):
        if st.get(key):
            return int(st[key])
    digits = "".join(c for c in os.path.basename(players_path) if c.isdigit())
    return int(digits) if digits else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--league", default=None)
    ap.add_argument("--top", type=int, default=5)
    args = ap.parse_args(argv)

    ctxs = [leagues.resolve(args.league)] if args.league else leagues.all()
    ok = [run_league(c, args.top) for c in ctxs]
    print(f"\n{sum(ok)}/{len(ok)} league(s) valued end-to-end.")
    return 0 if all(ok) and ok else 1


if __name__ == "__main__":
    sys.exit(main())
