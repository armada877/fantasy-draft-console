"""CLI: fftiers-vbd --league leagues/my-league.yaml [--week N | --horizon ros]

Intra-season VBD boards (elboberto workbook calculations) from current
projections instead of the workbook's preseason numbers.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .paths import repo_root
from .config import load_league
from .projections import (fetch_ros_projections, fetch_week_projections,
                          load_csv_projections)
from .vbd import CORE, ValuedPlayer, bench_counts, starter_counts, value_players


def write_boards(players: list[ValuedPlayer], out_dir: Path, label: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"vbd-{label}.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Player", "Pos", "Team", "PosRank", "FPTS", "Status",
                    "StartVBD", "BenchVBD", "AvgVBD", "Tier", "$", "AvgVBD$"])
        for i, p in enumerate(players, 1):
            w.writerow([i, p.name, p.pos, p.team, p.pos_rank, p.points, p.status,
                        round(p.start_vbd, 1), round(p.bench_vbd, 1),
                        round(p.avg_vbd, 1), p.tier, p.dollars, p.avg_vbd_dollars])
    txt_path = out_dir / f"vbd-{label}.txt"
    lines = [f"{'':>4}{'Player':<28}{'Pos':<5}{'FPTS':>7}{'AvgVBD':>8}{'Tier':>6}{'$':>7}"]
    for i, p in enumerate(players[:60], 1):
        lines.append(f"{i:>3} {p.name:<28}{p.pos:<5}{p.points:>7.1f}"
                     f"{p.avg_vbd:>8.1f}{p.tier:>6}{p.dollars:>7.1f}")
    txt_path.write_text("\n".join(lines) + "\n")
    return csv_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fftiers-vbd",
        description="Intra-season VBD/auction values (elboberto method) per league.")
    ap.add_argument("--league", action="append", required=True)
    ap.add_argument("--week", type=int, default=None,
                    help="NFL week. Default: derived from config dates.")
    ap.add_argument("--horizon", choices=["week", "ros"], default="ros",
                    help="Single week or rest-of-season (default ros).")
    ap.add_argument("--projections-csv", default=None,
                    help="Load projections from CSV (stat columns or a points "
                         "column) instead of the FantasyPros projections API.")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args(argv)

    root = repo_root()
    for league_path in args.league:
        cfg = load_league(league_path)
        week = cfg.current_week() if args.week is None else args.week
        final_week = int(cfg.vbd_options.get("final_week", 17))
        label = f"week-{week}" if args.horizon == "week" else f"ros-from-{week}"

        if args.projections_csv:
            projs = load_csv_projections(Path(args.projections_csv))
        else:
            projs = []
            for pos in CORE + ("K", "DST"):
                if not (cfg.starters(pos) or pos in CORE):
                    continue
                if args.horizon == "week":
                    projs += fetch_week_projections(
                        root / "dat", cfg.year, week, pos,
                        not args.no_download, args.api_key)
                else:
                    projs += fetch_ros_projections(
                        root / "dat", cfg.year, week, pos, final_week,
                        not args.no_download, args.api_key)

        players = value_players(cfg, projs)
        if not players:
            print(f"{cfg.name}: no projections loaded", file=sys.stderr)
            continue
        by_pos = {}
        for p in players:
            by_pos.setdefault(p.pos, []).append(p)
        for pos in by_pos:
            by_pos[pos].sort(key=lambda p: p.pos_rank)
        starters = starter_counts(cfg, by_pos)
        bench = bench_counts(cfg, starters)

        out_dir = root / "out" / cfg.slug / f"week-{week}" / "vbd"
        path = write_boards(players, out_dir, label)
        print(f"{cfg.name}: {len(players)} players valued ({args.horizon}, week {week})")
        print(f"  starters/bench baselines: " + ", ".join(
            f"{pos} {starters.get(pos, 0)}+{bench.get(pos, 0)}" for pos in CORE))
        print(f"  -> {path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
