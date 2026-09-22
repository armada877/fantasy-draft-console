"""FantasyPros consensus-rankings download + cache (port of fp_api.py)."""
from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

API_URL = (
    "https://api.fantasypros.com/public/v2/json/nfl/{year}/consensus-rankings"
    "?position={position}&week={week}&scoring={scoring}"
)
# Rest-of-season ranks are a different ranking TYPE, not a week — and the API is
# doubly unusable for them: it clamps out-of-range weeks to the current week
# (week=90 returns weekly ranks), and `type=ros` truncates the players array to 10
# while reporting the full count. The public ranking PAGES carry the complete pool
# (verified: RB 105, WR 141, QB 61) as an embedded `var ecrData = {...}` blob with
# the same field names, so ROS is fetched from the page — no API key involved.
# Locally the ROS caches keep living under the week-90 sentinel filename.
ROS_WEEK = 90
ROS_PAGE = "https://www.fantasypros.com/nfl/rankings/ros-{variant}{pos}.php"
PAGE_VARIANT = {"STD": "", "HALF": "half-point-ppr-", "PPR": "ppr-"}
SCORING_FREE_POS = {"QB", "K", "DST"}   # reception scoring doesn't move these pages
ECR_RE = re.compile(r"var\s+ecrData\s*=\s*(\{.*?\});", re.S)
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


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
    if week == ROS_WEEK:
        return _download_ros_page(dest, position, scoring)
    url = API_URL.format(year=year, position=position, week=week, scoring=scoring)
    req = urllib.request.Request(url, headers={"x-api-key": api_key})
    with urllib.request.urlopen(req, timeout=30) as resp:
        dest.write_bytes(resp.read())
    return dest


def _download_ros_page(dest: Path, position: str, scoring: str) -> Path:
    """Full-pool ROS ranks from the public ranking page's ecrData blob."""
    variant = "" if position in SCORING_FREE_POS else PAGE_VARIANT.get(scoring, "")
    url = ROS_PAGE.format(variant=variant, pos=position.lower())
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = ECR_RE.search(html)
    if not m:
        raise RuntimeError(f"no `var ecrData` blob in {url} — page layout changed")
    src = json.loads(m.group(1))
    players = [{
        "rank_ecr": p.get("rank_ecr"),
        "player_name": p.get("player_name"),
        "player_positions": position,
        "rank_min": p.get("rank_min", p.get("rank_ave")),
        "rank_max": p.get("rank_max", p.get("rank_ave")),
        "rank_ave": p.get("rank_ave"),
        "rank_std": p.get("rank_std"),
    } for p in src.get("players", [])]
    dest.write_text(json.dumps({
        "type": src.get("type") or f"ROS {scoring}", "week": ROS_WEEK,
        "ranking_type_name": "ros", "position": position, "scoring": scoring,
        "count": len(players), "players": players}))
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
