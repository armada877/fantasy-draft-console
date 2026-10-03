"""CLI: fftiers-csg --league leagues/my-league.yaml [--week N | --horizon ros]

Intra-season CSG-sheet valuations (games-based baselines), separate from the
elboberto port (fftiers-vbd) and the tier charts (fftiers).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .paths import repo_root
from .config import load_league
from .csg import CORE, csg_bench, csg_starters, csg_value
from .projections import (fetch_ros_projections, fetch_week_projections,
                          load_csv_projections)


def load_unrostered(path: Path) -> set[str]:
    """Names marked rostered=no/false in a projections CSV's optional column."""
    out = set()
    with path.open() as f:
        for row in csv.DictReader(f):
            low = {k.strip().lower(): (v or "") for k, v in row.items() if k}
            if low.get("rostered", "").strip().lower() in ("no", "false", "0", "fa"):
                out.add((low.get("player") or low.get("name") or "").strip())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fftiers-csg",
        description="CSG Fantasy Football Sheet valuations, fed intra-season data.")
    ap.add_argument("--league", action="append", required=True)
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--horizon", choices=["week", "ros"], default="ros")
    ap.add_argument("--projections-csv", default=None,
                    help="Stat or points CSV; optional `rostered` column (yes/no) "
                         "drives the positional-scarcity factor.")
    ap.add_argument("--no-download", action="store_true")
    ap.add_argument("--api-key", default=None)
    args = ap.parse_args(argv)

    root = repo_root()
    for league_path in args.league:
        cfg = load_league(league_path)
        week = cfg.current_week() if args.week is None else args.week
        final_week = int(cfg.csg_options.get("final_week",
                         cfg.vbd_options.get("final_week", 17)))
        label = f"week-{week}" if args.horizon == "week" else f"ros-from-{week}"

        unrostered: set[str] = set()
        if args.projections_csv:
            projs = load_csv_projections(Path(args.projections_csv))
            unrostered = load_unrostered(Path(args.projections_csv))
        else:
            projs = []
            for pos in CORE:
                if args.horizon == "week":
                    projs += fetch_week_projections(root / "dat", cfg.year, week, pos,
                                                    not args.no_download, args.api_key)
                else:
                    projs += fetch_ros_projections(root / "dat", cfg.year, week, pos,
                                                   final_week, not args.no_download,
                                                   args.api_key)

        players = csg_value(cfg, projs, unrostered=unrostered)
        if not players:
            print(f"{cfg.name}: no projections loaded", file=sys.stderr)
            continue

        by_pos: dict[str, list] = {}
        for p in players:
            by_pos.setdefault(p.pos, []).append(p)
        for pos in by_pos:
            by_pos[pos].sort(key=lambda p: p.pos_rank)
        starters = cfg.csg_options.get("starters") or csg_starters(cfg, by_pos)
        bench = cfg.csg_options.get("bench") or csg_bench(cfg, starters)

        out_dir = root / "out" / cfg.slug / f"week-{week}" / "csg"
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_path = out_dir / f"csg-{label}.csv"
        with csv_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Rank", "Player", "Pos", "Team", "PosRank", "FPTS", "Status",
                        "Rostered", "StartVBD", "BenchVBD", "AvgVBD", "VBDAdj",
                        "PosTier", "$"])
            for i, p in enumerate(players, 1):
                w.writerow([i, p.name, p.pos, p.team, p.pos_rank, p.points, p.status,
                            "yes" if p.rostered else "no",
                            round(p.start_vbd, 1), round(p.bench_vbd, 1),
                            round(p.avg_vbd, 1), p.vbd_adj, p.tier, p.dollars])
        txt_path = out_dir / f"csg-{label}.txt"
        lines = [f"{'':>4}{'Player':<28}{'Pos':<5}{'FPTS':>7}{'AvgVBD':>8}{'VBDAdj':>8}"
                 f"{'Tier':>5}{'$':>7}"]
        for i, p in enumerate(players[:60], 1):
            lines.append(f"{i:>3} {p.name:<28}{p.pos:<5}{p.points:>7.1f}"
                         f"{p.avg_vbd:>8.1f}{p.vbd_adj:>8.1f}{p.tier:>5}{p.dollars:>7.1f}")
        txt_path.write_text("\n".join(lines) + "\n")

        print(f"{cfg.name}: {len(players)} players valued ({args.horizon}, week {week})")
        print("  games-based starters/bench: " + ", ".join(
            f"{pos} {starters.get(pos, 0)}+{bench.get(pos, 0)}" for pos in CORE))
        print(f"  -> {csv_path.relative_to(root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
