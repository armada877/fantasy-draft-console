"""Convert plain-text ECR listings into fftiers JSON cache files.

Useful when you have rankings as text (e.g. from another tool) but no
FantasyPros API key. Input format, one section per position:

    ## QB STD
      1 (QB1)  Josh Allen (BUF) vs. DET  |  1.02 ± 0.15 [1–2]

Writes dat/{year}/week-{week}-{POS}-{SCORING}.json in the same shape the
FantasyPros consensus-rankings API returns, so `fftiers --no-download` can
consume them.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

LINE_RE = re.compile(
    r"^\s*(?P<rank>\d+)\s+\((?P<posrank>[A-Z]+\d+)\)\s+(?P<name>.+?)\s+\((?P<team>[A-Z]+)\)\s*"
    r"(?P<opp>[^|]*?)\s*\|\s*(?P<avg>[\d.]+)\s*±\s*(?P<std>[\d.]+)\s*"
    r"\[(?P<best>\d+)[–-](?P<worst>\d+)\]"
)


def parse_sections(text: str) -> dict[tuple[str, str], list[dict]]:
    sections: dict[tuple[str, str], list[dict]] = {}
    current: list[dict] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            pos, scoring = line[3:].split()
            current = sections.setdefault((pos, scoring), [])
            continue
        m = LINE_RE.match(line)
        if m and current is not None:
            current.append({
                "rank_ecr": int(m["rank"]),
                "player_name": m["name"],
                "player_team_id": m["team"],
                "player_opponent": m["opp"].strip(),
                "player_positions": m["posrank"].rstrip("0123456789"),
                "rank_min": m["best"],
                "rank_max": m["worst"],
                "rank_ave": m["avg"],
                "rank_std": m["std"],
            })
    return sections


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("text_file")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--out", default=None, help="dat/ directory (default: repo dat/)")
    args = ap.parse_args()

    out = Path(args.out) if args.out else Path(__file__).resolve().parents[1] / "dat"
    text = Path(args.text_file).read_text()
    for (pos, scoring), players in parse_sections(text).items():
        dest = out / str(args.year) / f"week-{args.week}-{pos}-{scoring}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"players": players}, indent=1))
        print(f"{dest}  ({len(players)} players)")


if __name__ == "__main__":
    main()
