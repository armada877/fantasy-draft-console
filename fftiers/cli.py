"""CLI: fftiers --league leagues/my-league.yaml [--week N] [--no-download]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_league
from .depth import plan_positions
from .run import run_league


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fftiers",
        description="Fantasy football tiers (Boris Chen method), configurable per league.")
    ap.add_argument("--league", action="append", required=True,
                    help="Path to a league YAML config (repeatable).")
    ap.add_argument("--week", type=int, default=None,
                    help="NFL week (0 = pre-draft). Default: derived from config dates.")
    ap.add_argument("--no-download", action="store_true",
                    help="Use cached data in dat/ instead of hitting FantasyPros.")
    ap.add_argument("--api-key", default=None, help="FantasyPros API key.")
    ap.add_argument("--positions", default=None,
                    help="Comma-separated subset, e.g. qb,rb,flex.")
    ap.add_argument("--plan", action="store_true",
                    help="Print the derived pools/tiers per position and exit.")
    args = ap.parse_args(argv)

    positions = args.positions.split(",") if args.positions else None
    root = repo_root()
    for league_path in args.league:
        cfg = load_league(league_path)
        week = cfg.current_week() if args.week is None else args.week
        print(f"{cfg.name}: {cfg.teams} teams, {cfg.scoring_source} "
              f"(rec={cfg.scoring.get('receptions', 0)}), week {week}")
        if args.plan:
            for p in plan_positions(cfg, week):
                print(f"  {p.position:<4} ranks {p.low:>3}-{p.high:<3} "
                      f"{p.tiers:>2} tiers  [{p.scoring}]")
            continue
        run_league(cfg, root, week=week, refresh=not args.no_download,
                   api_key=args.api_key, positions=positions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
