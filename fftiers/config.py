"""League configuration: teams, roster composition, and point values.

The original fftiers hardcoded player pools, tier counts, and three scoring
formats in main.R. Here every one of those knobs is derived from a YAML
league config (or overridden explicitly in it).
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Positions we can fetch consensus rankings for.
CORE_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")
FLEX_ELIGIBLE = ("RB", "WR", "TE")

# Roster-slot aliases as they appear in ESPN league settings.
SLOT_ALIASES = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE", "K": "K",
    "DST": "DST", "D/ST": "DST", "DEF": "DST",
    "FLEX": "FLEX", "RB/WR/TE": "FLEX", "W/R/T": "FLEX", "WR/RB/TE": "FLEX",
    "OP": "OP", "SUPERFLEX": "OP", "Q/W/R/T": "OP", "WR/RB/TE/QB": "OP",
    "WR/TE": "WRTE", "W/T": "WRTE", "WR/RB": "WRRB", "W/R": "WRRB",
    "BENCH": "BENCH", "BE": "BENCH", "IR": "IR",
}

# ESPN default point values, used to flag non-standard settings that
# consensus rankings cannot fully reflect.
ESPN_DEFAULT_SCORING = {
    "passing_yards": 0.04,
    "passing_td": 4.0,
    "interceptions": -2.0,
    "rushing_yards": 0.1,
    "rushing_td": 6.0,
    "receiving_yards": 0.1,
    "receiving_td": 6.0,
    "receptions": 0.0,  # varies by league; handled separately
    "fumbles_lost": -2.0,
    "two_point_conversions": 2.0,
    "te_reception_bonus": 0.0,
}


@dataclass
class LeagueConfig:
    name: str
    teams: int
    roster: dict[str, int]          # canonical slot -> count
    scoring: dict[str, float]
    year: int
    week_one_tuesday: datetime.date
    overrides: dict[str, dict] = field(default_factory=dict)  # per-position pool/tiers
    vbd_options: dict = field(default_factory=dict)  # budget, starter_pct, allow_negative, final_week
    csg_options: dict = field(default_factory=dict)  # budget, starter_pct, starters/bench overrides
    slug: str = ""

    @property
    def scoring_source(self) -> str:
        """Map the league's points-per-reception to the closest FantasyPros
        consensus-ranking variant (STD / HALF / PPR)."""
        rec = float(self.scoring.get("receptions", 0.0))
        buckets = {0.0: "STD", 0.5: "HALF", 1.0: "PPR"}
        return buckets[min(buckets, key=lambda v: abs(v - rec))]

    @property
    def bench_slots(self) -> int:
        return self.roster.get("BENCH", 6)

    @property
    def roster_size(self) -> int:
        return sum(n for slot, n in self.roster.items() if slot != "IR")

    def starters(self, pos: str) -> int:
        return self.roster.get(pos, 0)

    def current_week(self, today: datetime.date | None = None) -> int:
        today = today or datetime.date.today()
        week = (today - self.week_one_tuesday).days // 7 + 1
        return max(0, week)

    def nonstandard_scoring_notes(self) -> list[str]:
        """Point values that expert-consensus ranks can't fully capture.

        PPR/half/standard is handled by choosing the ranking source; anything
        else (6-pt passing TDs, TE premium, ...) shifts player values in ways
        a consensus rank list doesn't know about, so we surface it."""
        notes = []
        for key, default in ESPN_DEFAULT_SCORING.items():
            if key == "receptions":
                continue
            val = float(self.scoring.get(key, default))
            if abs(val - default) > 1e-9:
                notes.append(f"{key} = {val} (ESPN default {default})")
        rec = float(self.scoring.get("receptions", 0.0))
        if min(abs(rec - v) for v in (0.0, 0.5, 1.0)) > 1e-9:
            notes.append(
                f"receptions = {rec} has no exact consensus-ranking source; "
                f"using nearest ({self.scoring_source})"
            )
        return notes


def _canon_roster(raw: dict) -> dict[str, int]:
    roster: dict[str, int] = {}
    for slot, count in raw.items():
        key = SLOT_ALIASES.get(str(slot).strip().upper())
        if key is None:
            raise ValueError(f"Unknown roster slot {slot!r}. Known: {sorted(set(SLOT_ALIASES))}")
        roster[key] = roster.get(key, 0) + int(count)
    return roster


def load_league(path: str | Path) -> LeagueConfig:
    path = Path(path)
    raw = yaml.safe_load(path.read_text())
    season = raw.get("season", {})
    w1 = season.get("week_one_tuesday")
    if isinstance(w1, str):
        w1 = datetime.date.fromisoformat(w1)
    return LeagueConfig(
        name=raw["name"],
        teams=int(raw["teams"]),
        roster=_canon_roster(raw["roster"]),
        scoring={k: float(v) for k, v in raw.get("scoring", {}).items()},
        year=int(season.get("year", datetime.date.today().year)),
        week_one_tuesday=w1,
        overrides={str(k).upper(): v for k, v in raw.get("overrides", {}).items()},
        vbd_options=raw.get("vbd", {}) or {},
        csg_options=raw.get("csg", {}) or {},
        slug=raw.get("slug") or path.stem,
    )
