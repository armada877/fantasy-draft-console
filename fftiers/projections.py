"""Player stat-line projections: fetch, load, and score with league point values.

This feeds the VBD engine (vbd.py, ported from the elboberto draft workbook).
Unlike the workbook — which ships with preseason full-season projections — the
inputs here are intra-season: a single week's projections, or rest-of-season
built by summing every remaining week's projections.

Two input modes:
  * stat lines (preferred): raw stat projections scored with the league's exact
    point values, including non-standard ones (6-pt passing TDs, TE premium...)
  * pre-scored points: when the source already applied league scoring
    (e.g. ESPN league projections), skip the scoring step.
"""
from __future__ import annotations

import csv
import json
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import LeagueConfig
from .fetch import resolve_api_key

PROJ_URL = (
    "https://api.fantasypros.com/public/v2/json/nfl/{year}/projections"
    "?position={position}&week={week}&scoring={scoring}"
)

# Canonical stat keys -> aliases seen in FantasyPros payloads / CSV headers.
STAT_ALIASES = {
    "pass_att": ("pass_att", "passatt", "att"),
    "pass_cmp": ("pass_cmp", "passcmp", "cmp"),
    "pass_yds": ("pass_yds", "passyds"),
    "pass_tds": ("pass_tds", "passtds", "pass_td"),
    "pass_ints": ("pass_ints", "passints", "ints", "pass_int"),
    "rush_att": ("rush_att", "rushatt"),
    "rush_yds": ("rush_yds", "rushyds"),
    "rush_tds": ("rush_tds", "rushtds", "rush_td"),
    "receptions": ("receptions", "rec", "recs", "rec_rec"),
    "rec_yds": ("rec_yds", "recyds"),
    "rec_tds": ("rec_tds", "rectds", "rec_td"),
    "fumbles_lost": ("fumbles_lost", "fl", "fumbles", "fum"),
    "two_pts": ("two_pts", "2pt", "two_pt"),
    "first_downs": ("first_downs", "1d", "fd"),
}
_ALIAS_TO_CANON = {a: k for k, aliases in STAT_ALIASES.items() for a in aliases}


@dataclass
class Projection:
    name: str
    pos: str                      # QB / RB / WR / TE / K / DST
    team: str = ""
    stats: dict[str, float] = field(default_factory=dict)
    points: float | None = None   # set directly in pre-scored mode


def score(proj: Projection, cfg: LeagueConfig) -> float:
    """Fantasy points from a stat line and the league's point values.

    Port of the FPTS column: a dot product of stats and LeagueInfo scoring.
    """
    if proj.points is not None and not proj.stats:
        return proj.points
    s = proj.stats
    sc = cfg.scoring
    g = lambda k, d=0.0: float(sc.get(k, d))
    pts = (
        s.get("pass_yds", 0) * g("passing_yards", 0.04)
        + s.get("pass_tds", 0) * g("passing_td", 4)
        + s.get("pass_ints", 0) * g("interceptions", -2)
        + s.get("pass_att", 0) * g("passing_attempts")
        + s.get("pass_cmp", 0) * g("completions")
        + s.get("rush_yds", 0) * g("rushing_yards", 0.1)
        + s.get("rush_tds", 0) * g("rushing_td", 6)
        + s.get("rush_att", 0) * g("rushing_attempts")
        + s.get("rec_yds", 0) * g("receiving_yards", 0.1)
        + s.get("rec_tds", 0) * g("receiving_td", 6)
        + s.get("receptions", 0) * g("receptions")
        + s.get("fumbles_lost", 0) * g("fumbles_lost", -2)
        + s.get("two_pts", 0) * g("two_point_conversions", 2)
    )
    if proj.pos == "TE":
        pts += s.get("receptions", 0) * g("te_reception_bonus")
    return pts


def _canon_stats(raw: dict) -> dict[str, float]:
    out = {}
    for k, v in raw.items():
        canon = _ALIAS_TO_CANON.get(str(k).strip().lower().replace(" ", "_"))
        if canon is not None:
            try:
                out[canon] = float(v)
            except (TypeError, ValueError):
                pass
    return out


# ---------------------------------------------------------------- FantasyPros

def fetch_week_projections(data_dir: Path, year: int, week: int, position: str,
                           refresh: bool, api_key: str | None) -> list[Projection]:
    """One week of stat-line projections for one position (cached like rankings)."""
    path = data_dir / str(year) / f"proj-week-{week}-{position}.json"
    if refresh or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        url = PROJ_URL.format(year=year, position=position, week=week, scoring="PPR")
        req = urllib.request.Request(url, headers={"x-api-key": resolve_api_key(api_key)})
        with urllib.request.urlopen(req, timeout=30) as resp:
            path.write_bytes(resp.read())
    data = json.loads(path.read_text())
    projs = []
    for p in data.get("players", []):
        stats = _canon_stats(p.get("stats", {}))
        if not stats:
            continue
        projs.append(Projection(
            name=p.get("name") or p.get("player_name", ""),
            pos=position, team=p.get("team_id", ""), stats=stats))
    return projs


def fetch_ros_projections(data_dir: Path, year: int, from_week: int, position: str,
                          final_week: int, refresh: bool, api_key: str | None) -> list[Projection]:
    """Rest-of-season = sum of each remaining week's projections.

    This is the intra-season replacement for the workbook's preseason
    full-season numbers: it reflects what's happened (roles, injuries,
    depth-chart changes) because each weekly projection does.
    """
    totals: dict[str, Projection] = {}
    for wk in range(from_week, final_week + 1):
        for p in fetch_week_projections(data_dir, year, wk, position, refresh, api_key):
            agg = totals.setdefault(p.name, Projection(p.name, p.pos, p.team))
            for k, v in p.stats.items():
                agg.stats[k] = agg.stats.get(k, 0.0) + v
    return list(totals.values())


# ----------------------------------------------------------------------- CSV

def load_csv_projections(path: Path) -> list[Projection]:
    """Load projections from CSV.

    Required columns: player, pos. Then either stat columns (any aliases in
    STAT_ALIASES, e.g. the workbook's *_Raw headers: PASSATT, RUSHYDS, REC...)
    or a `points` column for pre-scored mode.
    """
    projs = []
    with Path(path).open() as f:
        for row in csv.DictReader(f):
            low = {k.strip().lower(): v for k, v in row.items() if k}
            name = low.get("player") or low.get("name") or ""
            pos = (low.get("pos") or low.get("position") or "").upper().replace("DEF", "DST")
            if not name or not pos:
                continue
            pts = low.get("points") or low.get("fpts")
            projs.append(Projection(
                name=name.strip(), pos=pos, team=(low.get("team") or "").strip(),
                stats=_canon_stats(low),
                points=float(pts) if pts not in (None, "") else None))
    return projs
