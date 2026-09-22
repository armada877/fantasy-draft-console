"""FantasyPros consensus-rankings download + cache (port of fp_api.py)."""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_URL = (
    "https://api.fantasypros.com/public/v2/json/nfl/{year}/consensus-rankings"
    "?position={position}&week={week}&scoring={scoring}"
)


@dataclass
class PlayerRow:
    rank: int
    name: str
    detail: str        # position(s) pre-draft, opponent in-season
    best: float
    worst: float
    avg: float
    std: float


class MissingApiKeyError(RuntimeError):
    pass


def resolve_api_key(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("FANTASYPROS_API_KEY")
    if env:
        return env
    key_file = Path(__file__).resolve().parents[1] / "api_key.txt"
    if key_file.exists():
        return key_file.read_text().strip()
    raise MissingApiKeyError(
        "No FantasyPros API key. Pass --api-key, set FANTASYPROS_API_KEY, "
        "or put the key in api_key.txt at the repo root."
    )


def cache_path(data_dir: Path, year: int, week: int, position: str, scoring: str) -> Path:
    return data_dir / str(year) / f"week-{week}-{position}-{scoring}.json"


def download(data_dir: Path, year: int, week: int, position: str, scoring: str,
             api_key: str) -> Path:
    dest = cache_path(data_dir, year, week, position, scoring)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = API_URL.format(year=year, position=position, week=week, scoring=scoring)
    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest.write_bytes(resp.read())
    return dest


def load_rows(json_path: Path) -> list[PlayerRow]:
    data = json.loads(json_path.read_text())
    rows = []
    for p in data.get("players", []):
        try:
            rows.append(PlayerRow(
                rank=int(p["rank_ecr"]),
                name=p["player_name"],
                detail=str(p.get("player_opponent") or p.get("player_positions") or ""),
                best=float(p["rank_min"]),
                worst=float(p["rank_max"]),
                avg=float(p["rank_ave"]),
                std=float(p["rank_std"]),
            ))
        except (KeyError, TypeError, ValueError):
            continue  # upstream skipped malformed player entries the same way
    rows.sort(key=lambda r: r.rank)
    return rows


def get_rankings(data_dir: Path, year: int, week: int, position: str, scoring: str,
                 refresh: bool, api_key: str | None) -> list[PlayerRow]:
    path = cache_path(data_dir, year, week, position, scoring)
    if refresh or not path.exists():
        download(data_dir, year, week, position, scoring, resolve_api_key(api_key))
    return load_rows(path)
