"""Pipeline: league config -> plans -> fetch -> cluster -> txt/csv/png."""
from __future__ import annotations

import csv
import datetime
from pathlib import Path

from .cluster import assign_tiers
from .config import LeagueConfig
from .depth import PositionPlan, plan_positions
from .fetch import PlayerRow, get_rankings
from .plot import draw_chart


def _fp_label(plan: PositionPlan) -> str:
    return plan.position if plan.scoring == "STD" else f"{plan.position}-{plan.scoring}"


def write_txt(path: Path, rows: list[PlayerRow], tiers: list[int]) -> None:
    lines = []
    for t in range(1, max(tiers) + 1):
        names = [r.name for r, tier in zip(rows, tiers) if tier == t]
        if names:
            lines.append(f"Tier {t}: " + ", ".join(names))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def write_csv(path: Path, rows: list[PlayerRow], tiers: list[int], week: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    detail_col = "Position" if week == 0 else "Matchup"
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Rank", "Player.Name", detail_col, "Best.Rank", "Worst.Rank",
                    "Avg.Rank", "Std.Dev", "Tier"])
        for r, t in zip(rows, tiers):
            w.writerow([r.rank, r.name, r.detail, r.best, r.worst, r.avg, r.std, t])


def run_league(cfg: LeagueConfig, repo_root: Path, week: int | None = None,
               refresh: bool = True, api_key: str | None = None,
               positions: list[str] | None = None) -> list[Path]:
    week = cfg.current_week() if week is None else week
    plans = plan_positions(cfg, week)
    if positions:
        wanted = {p.upper().replace("FLEX", "FLX") for p in positions}
        plans = [p for p in plans if p.position in wanted]

    data_dir = repo_root / "dat"
    out_dir = repo_root / "out" / cfg.slug / f"week-{week}"
    stamp = datetime.datetime.now().strftime("%a %b %d %Y %H:%M")
    written: list[Path] = []

    notes = cfg.nonstandard_scoring_notes()
    if notes:
        note_path = out_dir / "SCORING-NOTES.txt"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(
            "Non-standard point values in this league. Tiers are built from\n"
            "expert consensus ranks ({} source), which do not reflect these:\n  - "
            .format(cfg.scoring_source) + "\n  - ".join(notes) + "\n")
        written.append(note_path)

    for plan in plans:
        rows = get_rankings(data_dir, cfg.year, week, plan.position, plan.scoring,
                            refresh=refresh, api_key=api_key)
        if not rows:
            print(f"  !! no data for {plan.position} ({plan.scoring}), skipping")
            continue
        window = rows[plan.low - 1:plan.high]
        if len(window) < 2:
            print(f"  !! too few players for {plan.position}, skipping")
            continue
        tiers = assign_tiers([r.avg for r in window], plan.tiers)

        label = _fp_label(plan)
        title = f"{cfg.name} — Week {week} — {label} Tiers — {stamp}"
        if week == 0:
            title = f"{cfg.name} — {cfg.year} Draft — {label} Tiers — {stamp}"
        png = out_dir / "png" / f"week-{week}-{label}.png"
        txt = out_dir / "txt" / f"text_{label}.txt"
        csvp = out_dir / "csv" / f"week-{week}-{label}.csv"
        draw_chart(window, tiers, title, png)
        write_txt(txt, window, tiers)
        write_csv(csvp, window, tiers, week)
        written += [png, txt, csvp]
        print(f"  {label}: ranks {plan.low}-{plan.high}, "
              f"{max(tiers)} tiers -> {png.relative_to(repo_root)}")
    return written
