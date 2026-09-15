#!/usr/bin/env python3
"""LeagueProfile — every league-specific fact, derived from scraped settings.

The engine's one rule (docs/local/inseason_plan.md 0b): no module may hardcode a league
property. Team count, scoring, which positions start, whether waivers cost money,
how many QBs you must field — all of it is read off the platform payload here and
passed around as a profile object.

Build one with:
    prof = LeagueProfile.from_espn(league_full_json)

`league_full_json` is the object scraping/ saves as raw/{season}/league_full.json
(a `?view=mSettings...` response; the historical endpoint's single-element list
form is unwrapped for you).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

# ── ESPN id maps ─────────────────────────────────────────────────────────────
# Slot ids are a platform constant, not a league property, so they live here.
# NOTE: we never infer a slot's eligible positions from this table — each player
# carries its own `eligibleSlots`, which is authoritative and covers IDP/OP too.
SLOT_NAMES = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S",
    14: "DB", 15: "DP", 16: "DST", 17: "K", 18: "P", 19: "HC",
    20: "BE", 21: "IR", 23: "FLEX", 24: "ER",
}
BENCH_SLOTS = frozenset({20, 21})          # not starting slots
POSITION_NAMES = {
    1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 7: "OP", 9: "DL", 10: "LB",
    11: "DB", 12: "DP", 13: "DT", 14: "DE", 16: "DST",
}

# Acquisition models. FAAB spends money; PRIORITY spends waiver position. They are
# different decision problems (see plan 3d), so the engine branches on this, not on
# a league name.
FAAB = "faab"
PRIORITY = "priority"


def slot_name(slot_id) -> str:
    return SLOT_NAMES.get(int(slot_id), f"SLOT{slot_id}")


def position_name(pos_id) -> str:
    return POSITION_NAMES.get(int(pos_id), f"POS{pos_id}")


@dataclass(frozen=True)
class Acquisition:
    """How players are added. `budget` is None in priority-order leagues."""
    model: str                  # FAAB | PRIORITY
    budget: int | None          # FAAB dollars per team
    min_bid: int
    continuous: bool            # WAIVERS_CONTINUOUS vs weekly-batch TRADITIONAL
    waiver_hours: int
    order_resets: bool          # priority resets to last after a successful claim
    limit: int                  # -1 = unlimited acquisitions

    @property
    def is_faab(self) -> bool:
        return self.model == FAAB


@dataclass(frozen=True)
class LeagueProfile:
    """Everything the engine needs to know about a league. Built, never hardcoded."""
    league_id: int
    season: int
    name: str
    size: int
    scoring: dict                    # {statId: points}
    lineup_slots: dict               # {slotId: count}, starters only
    bench: int
    ir: int
    acquisition: Acquisition
    draft_type: str                  # AUCTION | SNAKE | ...
    auction_budget: int | None
    keeper_count: int
    regular_weeks: int
    playoff_teams: int
    trade_deadline_ms: int | None
    veto_votes: int
    position_limits: dict = field(default_factory=dict)

    # ── derived ──────────────────────────────────────────────────────────────
    @property
    def starting_slots(self) -> dict:
        """{slotId: count} for slots that actually start. Excludes bench and IR."""
        return {s: n for s, n in self.lineup_slots.items()
                if n and int(s) not in BENCH_SLOTS}

    @property
    def starters_per_team(self) -> int:
        return sum(self.starting_slots.values())

    @property
    def roster_size(self) -> int:
        return self.starters_per_team + self.bench + self.ir

    @property
    def scoring_label(self) -> str:
        """'PPR' / 'half-PPR' / 'standard' — a DERIVED label for picking the right
        external-source file (FantasyPros, Boris Chen), never an input."""
        rec = float(self.scoring.get(53, 0) or 0)
        if rec >= 1.0:
            return "PPR"
        if rec > 0:
            return "half-PPR"
        return "standard"

    @property
    def playoff_weeks(self) -> list:
        """Fantasy playoff weeks: everything after the regular season, up to 17."""
        return list(range(self.regular_weeks + 1, 18))

    def starts_position(self, pos: str) -> bool:
        """Does any starting slot plausibly field this position? Used only for
        display grouping — real eligibility comes from each player's eligibleSlots."""
        return pos in self.startable_positions

    @property
    def startable_positions(self) -> set:
        """Positions with a dedicated starting slot, by slot NAME. FLEX-only
        positions still appear via players' eligibleSlots at lineup time."""
        out = set()
        for s in self.starting_slots:
            nm = slot_name(s)
            if nm in ("FLEX", "OP", "RB/WR", "WR/TE", "TQB", "ER"):
                continue
            out.add(nm)
        return out

    # ── construction ─────────────────────────────────────────────────────────
    @staticmethod
    def from_espn(payload: dict) -> "LeagueProfile":
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        s = payload.get("settings") or {}
        acq_s = s.get("acquisitionSettings") or {}
        roster_s = s.get("rosterSettings") or {}
        draft_s = s.get("draftSettings") or {}
        sched_s = s.get("scheduleSettings") or {}
        trade_s = s.get("tradeSettings") or {}

        slots = {int(k): int(v) for k, v in (roster_s.get("lineupSlotCounts") or {}).items()
                 if int(v) > 0}
        scoring = {}
        for item in (s.get("scoringSettings") or {}).get("scoringItems") or []:
            if item.get("statId") is not None:
                scoring[int(item["statId"])] = float(item.get("points", 0) or 0)

        uses_budget = bool(acq_s.get("isUsingAcquisitionBudget"))
        budget = acq_s.get("acquisitionBudget") if uses_budget else None
        acquisition = Acquisition(
            model=FAAB if uses_budget else PRIORITY,
            budget=int(budget) if budget else None,
            min_bid=int(acq_s.get("minimumBid") or 0),
            continuous=(acq_s.get("acquisitionType") == "WAIVERS_CONTINUOUS"),
            waiver_hours=int(acq_s.get("waiverHours") or 0),
            order_resets=bool(acq_s.get("waiverOrderReset")),
            limit=int(acq_s.get("acquisitionLimit", -1) if acq_s.get("acquisitionLimit") is not None else -1),
        )

        return LeagueProfile(
            league_id=int(payload.get("id") or 0),
            season=int(payload.get("seasonId") or 0),
            name=s.get("name") or "",
            size=int(s.get("size") or len(payload.get("teams") or []) or 0),
            scoring=scoring,
            lineup_slots=slots,
            bench=int(slots.get(20, 0)),
            ir=int(slots.get(21, 0)),
            acquisition=acquisition,
            draft_type=draft_s.get("type") or "",
            auction_budget=draft_s.get("auctionBudget"),
            keeper_count=int(draft_s.get("keeperCount") or 0),
            regular_weeks=int(sched_s.get("matchupPeriodCount") or 0),
            playoff_teams=int(sched_s.get("playoffTeamCount") or 0),
            trade_deadline_ms=trade_s.get("deadlineDate"),
            veto_votes=int(trade_s.get("vetoVotesRequired") or 0),
            position_limits={int(k): int(v) for k, v in (roster_s.get("positionLimits") or {}).items()},
        )

    @staticmethod
    def from_file(path: str) -> "LeagueProfile":
        with open(path) as f:
            return LeagueProfile.from_espn(json.load(f))

    def summary(self) -> str:
        a = self.acquisition
        acq = (f"FAAB ${a.budget} (min ${a.min_bid}, "
               f"{'continuous' if a.continuous else 'weekly'})" if a.is_faab
               else f"PRIORITY order ({'resets' if a.order_resets else 'static'})")
        lineup = " ".join(f"{slot_name(s)}x{n}" for s, n in sorted(self.starting_slots.items()))
        return (f"{self.name} ({self.league_id}, {self.season})\n"
                f"  {self.size} teams | {self.scoring_label} | {self.draft_type}"
                f"{f' ${self.auction_budget}' if self.draft_type == 'AUCTION' else ''}\n"
                f"  lineup: {lineup}  (bench {self.bench}, IR {self.ir})\n"
                f"  waivers: {acq}\n"
                f"  {self.regular_weeks}wk regular, {self.playoff_teams} playoff teams, "
                f"veto {self.veto_votes}")
