"""CLI: fftiers-espn — live ESPN data into this repo's formats.

    fftiers-espn discover
    fftiers-espn sync-league 12345 [--season 2026] [--dest leagues/foo.yaml]
    fftiers-espn pull 12345 --horizon week|ros [--week N] [--out path.csv]
    fftiers-espn roster 12345 --team 11

`pull` writes a player,pos,points,rostered CSV directly consumable by
`fftiers-vbd --projections-csv` and `fftiers-csg --projections-csv`.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .paths import repo_root
from .espn import (discover_leagues, league_settings, my_roster, player_pool,
                   ros_pool, write_league_yaml)

WEEK_ONE_TUESDAY = {2026: "2026-09-08"}  # Tuesday before Week 1 kickoff


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fftiers-espn",
                                 description="Pull live ESPN league data.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("discover", help="List every FFL league on this ESPN account.")

    sp = sub.add_parser("sync-league", help="Fetch settings -> leagues/<key>.yaml")
    sp.add_argument("league_id", type=int)
    sp.add_argument("--season", type=int, default=2026)
    sp.add_argument("--dest", default=None)

    pp = sub.add_parser("pull", help="Projections + ownership -> engine CSV")
    pp.add_argument("league_id", type=int)
    pp.add_argument("--season", type=int, default=2026)
    pp.add_argument("--horizon", choices=["week", "ros"], default="week")
    pp.add_argument("--week", type=int, default=None, help="Default: current week")
    pp.add_argument("--out", default=None)

    rp = sub.add_parser("roster", help="My roster with wk + ROS projections (JSON)")
    rp.add_argument("league_id", type=int)
    rp.add_argument("--season", type=int, default=2026)
    rp.add_argument("--team", type=int, required=True)
    rp.add_argument("--week", type=int, default=None)

    args = ap.parse_args(argv)
    root = repo_root()

    if args.cmd == "discover":
        for lg in discover_leagues():
            print(f"{lg['season']}  {lg['league_id']:>10}  {lg['league_name']}"
                  f"  (my team: {lg['team_name']})")
        return 0

    info = league_settings(args.league_id, args.season)
    week = getattr(args, "week", None) or info["current_week"]

    if args.cmd == "sync-league":
        dest = Path(args.dest) if args.dest else (
            root / "leagues" / f"espn-{args.league_id}.yaml")
        write_league_yaml(info, dest, WEEK_ONE_TUESDAY.get(args.season, f"{args.season}-09-08"))
        print(f"{info['name']}: {info['teams']} teams, roster {info['roster']}")
        if info["scoring_overrides"]:
            print(f"  !! {info['scoring_overrides']} per-position scoring overrides "
                  f"ignored (flat scoring only)")
        print(f"  -> {dest}")
        return 0

    if args.cmd == "pull":
        if args.horizon == "week":
            pool = player_pool(args.league_id, args.season, week)
            label = f"week-{week}"
        else:
            print(f"summing weeks {week}-{info['final_week']} ...", file=sys.stderr)
            pool = ros_pool(args.league_id, args.season, week, info["final_week"])
            label = f"ros-from-{week}"
        pool = [p for p in pool if p["points"] > 0 or p["rostered"] == "yes"]
        pool.sort(key=lambda p: -p["points"])
        out = Path(args.out) if args.out else (
            root / "dat" / "espn" / f"{args.league_id}-{label}.csv")
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["player", "pos", "points", "rostered"],
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(pool)
        print(f"{info['name']}: {len(pool)} players ({label}) -> {out}")
        return 0

    if args.cmd == "roster":
        roster = my_roster(args.league_id, args.season, args.team, week,
                           info["final_week"])
        print(json.dumps(roster, indent=1))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
