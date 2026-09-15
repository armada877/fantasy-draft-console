#!/usr/bin/env python3
"""Waiver RUNS — the temporal structure of a waiver market, measured per league.

A waiver claim does not happen "this week". It happens in a specific RUN: one
processing pass, on one day, at one hour, against everybody else who queued a
claim on the same player in the same pass. Which run you land in turns out to
matter far more than who the player is.

Measured from each league's own `transactions.json` (a run = one player in one
processing bucket; contested = more than one distinct team claiming him in it):

    the 12-team league     9 seasons, 2,591 runs.  Wednesday 47.6% of all runs and 38.5%
                 contested, against 14.2% on every other day — 2.7x. The same
                 player costs a mean $5.77 on Wednesday and $2.00 on Thursday.
    Chi Phi    122 runs. 91.8% land on Wednesday; 9.8% contested there, 0% else.
    Inlaws     128 runs. 88.3% Wednesday; 29.2% contested there, 6.7% else — 4.4x.

Two things follow, and they are the whole point of this module:

1. `p_contested` should be driven by WHICH RUN the claim lands in. A per-player
   contest model scored AUC 0.564 against this same history — no skill. The
   temporal split is 38.5% against 14.2%. One of these is worth shipping.
2. The advice is about timing, not amount. On the busy run you must bid to win;
   on a quiet run the minimum usually takes it. So when a player will plausibly
   still be there at the next quiet run, WAITING is the recommendation — and that
   holds in priority leagues too, where waiting costs nothing at all.

Everything here is a DESCRIPTIVE STATISTIC computed from the league's own
transactions, with its n reported. It is not a fitted model and makes no
out-of-sample claim, which is why it is allowed to move a number that a fitted
per-player model was not.

NOTHING about any particular day is hardcoded. Chi Phi is 92% Wednesday and
the 12-team league only 48%; the busiest and most-contested runs are found per league, and a
league that ran its waivers on Saturday would simply report Saturday.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

# Weekday names as the platform spells them in `waiverProcessDays`, indexed to
# match `datetime.weekday()`. A platform vocabulary constant, not a league fact.
DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")

# Claims that never reached processing tell us nothing about who wanted the
# player: a cancelled claim was withdrawn, a pending one has not run yet.
UNPROCESSED = frozenset({"CANCELED", "PENDING"})

RUN_BUCKET_MS = 3600_000
"""Losing claims carry millisecond-precision `processDate` while the winner's is
rounded to the hour, so grouping on the raw timestamp splits a run apart and
makes every contested claim look uncontested. Bucket to the hour."""

SHRINK_PRIOR = 10.0
"""Runs' worth of the league-wide rate mixed into every daily rate. Two of the
three leagues have only ~120 runs total, so an untouched Friday with n=2 would
otherwise report a confident 0%."""

MIN_DAY_N = 20
"""Below this, a day's own winning-bid prices are too thin to anchor a bid and
the league-wide numbers are used instead. The RATE is still reported, shrunk."""


@dataclass
class DayStats:
    """One weekday of a league's waiver history."""
    day: str
    runs: int = 0
    contested: int = 0
    wins: list = field(default_factory=list)      # winning bid amounts

    @property
    def raw_rate(self) -> float:
        return (self.contested / self.runs) if self.runs else 0.0

    @property
    def mean_win(self) -> float:
        return (sum(self.wins) / len(self.wins)) if self.wins else 0.0

    def quantile(self, q: float) -> float:
        if not self.wins:
            return 0.0
        s = sorted(self.wins)
        i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
        return float(s[i])

    def to_dict(self, overall_rate: float) -> dict:
        return {"day": self.day, "runs": self.runs, "contested": self.contested,
                "rate": round(self.shrunk_rate(overall_rate), 3),
                "raw_rate": round(self.raw_rate, 3),
                "median_win": self.quantile(0.5), "p80_win": self.quantile(0.8),
                "mean_win": round(self.mean_win, 2),
                "max_win": max(self.wins) if self.wins else 0,
                "n_wins": len(self.wins), "thin": self.runs < MIN_DAY_N}

    def shrunk_rate(self, overall_rate: float, prior: float = SHRINK_PRIOR) -> float:
        return (self.contested + prior * overall_rate) / (self.runs + prior)


@dataclass
class RunCalendar:
    """When this league's waivers run, and what happens when they do."""
    by_day: dict = field(default_factory=dict)         # {day: DayStats}
    total_runs: int = 0
    total_contested: int = 0
    process_days: tuple = ()
    process_hour: int = 0
    utc_offset: int = 0
    day_agreement: float = 0.0
    offset_stable: bool = True
    seasons: tuple = ()
    has_bids: bool = True
    """False in a priority-order league, where claims cost position rather than
    money. Quoting a '$0 median winning bid' there is not a cheap price, it is a
    category error, so the price side is simply omitted."""

    @property
    def overall_rate(self) -> float:
        return (self.total_contested / self.total_runs) if self.total_runs else 0.0

    @property
    def busiest(self):
        """The day most claims land on — found, never assumed."""
        if not self.by_day:
            return None
        return max(self.by_day.values(), key=lambda d: d.runs).day

    @property
    def hottest(self):
        """The day a claim is most likely to be contested, among days with enough
        history to mean anything."""
        real = [d for d in self.by_day.values() if d.runs >= MIN_DAY_N]
        if not real:
            return self.busiest
        return max(real, key=lambda d: d.shrunk_rate(self.overall_rate)).day

    def rate_for(self, day):
        """(rate, n, provenance) for a given run day."""
        d = self.by_day.get(day)
        if d is None or not d.runs:
            return self.overall_rate, self.total_runs, (
                f"league-wide contested rate {self.overall_rate:.0%} over "
                f"{self.total_runs} runs (no history for {day.title() if day else 'this day'})")
        rate = d.shrunk_rate(self.overall_rate)
        note = "" if d.runs >= MIN_DAY_N else ", shrunk toward the league rate (thin)"
        return rate, d.runs, (f"{day.title()} runs here are {rate:.0%} contested over "
                              f"{d.runs} runs{note}")

    def price_for(self, day):
        """(median, p80, provenance) winning bid for a run on this day."""
        if not self.has_bids:
            return None, None, None
        d = self.by_day.get(day)
        if d is not None and d.runs >= MIN_DAY_N and d.wins:
            return d.quantile(0.5), d.quantile(0.8), (
                f"{day.title()} winning bids here: median ${d.quantile(0.5):.0f}, "
                f"p80 ${d.quantile(0.8):.0f}, mean ${d.mean_win:.2f} over "
                f"{len(d.wins)} wins")
        wins = [w for s in self.by_day.values() for w in s.wins]
        if not wins:
            return None, None, None
        s = sorted(wins)
        med = float(s[len(s) // 2])
        p80 = float(s[min(len(s) - 1, int(0.8 * (len(s) - 1)))])
        return med, p80, (f"league-wide winning bids: median ${med:.0f}, p80 ${p80:.0f} "
                          f"over {len(wins)} wins ({day.title() if day else 'this day'} "
                          f"is too thin to price on its own)")

    def next_run(self, now: dt.datetime | None = None):
        """The next processing pass: (day, local datetime, hours away).

        Straight off the league's declared `waiverProcessDays` / `waiverProcessHour`.
        All three leagues here process six days a week — Tuesday is the only skip —
        so treating waivers as a weekly event is simply wrong for them.
        """
        if not self.process_days:
            return None, None, None
        now = now or dt.datetime.now(dt.timezone.utc)
        local = now + dt.timedelta(hours=self.utc_offset)
        allowed = {d.upper() for d in self.process_days}
        for ahead in range(0, 8):
            cand = local + dt.timedelta(days=ahead)
            day = DAYS[cand.weekday()]
            if day not in allowed:
                continue
            at = cand.replace(hour=int(self.process_hour), minute=0, second=0,
                              microsecond=0)
            if at <= local:
                continue
            return day, at, (at - local).total_seconds() / 3600.0
        return None, None, None

    def quieter_alternative(self, day):
        """The next processing day that is materially less contested than `day`.

        This is what makes 'wait' actionable rather than vague: not 'wait a bit',
        but 'Thursday's run is a third as contested and costs a third as much'.
        """
        if not day or not self.process_days:
            return None
        here = self.by_day.get(day)
        if here is None:
            return None
        base = here.shrunk_rate(self.overall_rate)
        allowed = [d.upper() for d in self.process_days]
        start = DAYS.index(day)
        for step in range(1, 8):
            nxt = DAYS[(start + step) % 7]
            if nxt not in allowed:
                continue
            s = self.by_day.get(nxt)
            # never advise waiting on the strength of a handful of runs: a day
            # with n=4 can show any rate at all
            if s is None or s.runs < MIN_DAY_N:
                continue
            if s.shrunk_rate(self.overall_rate) < base * 0.7:
                return nxt
        return None

    def to_dict(self, now: dt.datetime | None = None) -> dict:
        day, at, hours = self.next_run(now)
        rate, n, rate_src = self.rate_for(day) if day else (self.overall_rate,
                                                            self.total_runs, None)
        med, p80, price_src = self.price_for(day) if day else (None, None, None)
        quieter = self.quieter_alternative(day)
        out = {
            "process_days": list(self.process_days),
            "process_hour": self.process_hour,
            "utc_offset_hours": self.utc_offset,
            "day_agreement": round(self.day_agreement, 3),
            "seasons": list(self.seasons),
            "total_runs": self.total_runs,
            "overall_contested_rate": round(self.overall_rate, 3),
            "busiest_day": self.busiest, "hottest_day": self.hottest,
            "by_day": [self.by_day[d].to_dict(self.overall_rate) for d in DAYS
                       if d in self.by_day],
            "next_run": ({"day": day, "at_local": at.isoformat() if at else None,
                          "hours_away": round(hours, 1) if hours is not None else None,
                          "p_contested": round(rate, 3), "n_runs": n,
                          "median_win": med, "p80_win": p80,
                          "has_bids": self.has_bids,
                          "quieter_alternative": quieter} if day else None),
            "provenance": [s for s in (rate_src, price_src) if s],
        }
        return out


def _pick_offset(process_dates, process_days, process_hour):
    """The UTC offset that best reproduces the league's OWN declared process days.

    ESPN does not report a timezone, and `waiverProcessHour` does not always agree
    with the timestamps (two of these three leagues process hours away from what
    their settings claim). So rather than assume a timezone, try every whole-hour
    offset and keep the one that puts the most runs on a day the league says it
    runs waivers, tie-broken toward the declared hour. Returns
    (offset, agreement, stable) — `stable` is False when a neighbouring offset
    would reassign days, which is the case worth flagging rather than hiding.
    """
    if not process_dates:
        return 0, 0.0, True
    declared = {str(s).upper() for s in (process_days or [])}
    best = None
    dists = {}
    for off in range(-12, 13):
        ok, hours, dist = 0, defaultdict(int), defaultdict(int)
        for pd in process_dates:
            t = dt.datetime.fromtimestamp(pd / 1000, dt.timezone.utc) + dt.timedelta(hours=off)
            day = DAYS[t.weekday()]
            dist[day] += 1
            hours[t.hour] += 1
            if not declared or day in declared:
                ok += 1
        dists[off] = dict(dist)
        modal = max(hours.items(), key=lambda kv: kv[1])[0] if hours else 0
        score = (ok / len(process_dates), -abs(modal - int(process_hour or 0)), -abs(off))
        if best is None or score > best[0]:
            best = (score, off, ok / len(process_dates))
    _, off, agree = best
    stable = all(dists.get(off + d) == dists[off] for d in (-2, -1, 1, 2)
                 if off + d in dists)
    return off, agree, stable


def build_run_calendar(transactions, acquisition_settings: dict | None = None,
                       seasons=None) -> RunCalendar:
    """Measure a league's waiver runs from its own `transactions.json`.

    `transactions` is the saved payload (or its `transactions` list).
    `acquisition_settings` is the league's `settings.acquisitionSettings`, which
    supplies the declared process days and hour.
    """
    rows = transactions.get("transactions") if isinstance(transactions, dict) else transactions
    rows = rows or []
    acq = acquisition_settings or {}

    runs = defaultdict(list)
    for x in rows:
        if x.get("type") != "WAIVER" or not x.get("processDate"):
            continue
        if x.get("status") in UNPROCESSED or x.get("status") is None:
            continue
        if seasons and x.get("seasonId") not in seasons:
            continue
        add = next((i for i in (x.get("items") or []) if i.get("type") == "ADD"), None)
        if not add:
            continue
        # The transaction's own teamId is unusable in some seasons (it comes back
        # as INT_MIN), so ownership comes off the ADD item, which is always right.
        team = add.get("toTeamId")
        if team is None or int(team) <= 0:
            continue
        bucket = int(x["processDate"]) // RUN_BUCKET_MS
        runs[(bucket, add.get("playerId"))].append(
            (int(team), float(x.get("bidAmount") or 0), x.get("status")))

    offset, agree, stable = _pick_offset(
        [x["processDate"] for x in rows
         if x.get("type") == "WAIVER" and x.get("processDate")
         and x.get("status") == "EXECUTED"],
        acq.get("waiverProcessDays"), acq.get("waiverProcessHour"))

    cal = RunCalendar(
        process_days=tuple(acq.get("waiverProcessDays") or ()),
        process_hour=int(acq.get("waiverProcessHour") or 0),
        has_bids=bool(acq.get("isUsingAcquisitionBudget")),
        utc_offset=offset, day_agreement=agree, offset_stable=stable,
        seasons=tuple(sorted({x.get("seasonId") for x in rows
                              if x.get("seasonId") is not None})))
    for (bucket, _pid), claims in runs.items():
        local = (dt.datetime.fromtimestamp(bucket * 3600, dt.timezone.utc)
                 + dt.timedelta(hours=offset))
        day = DAYS[local.weekday()]
        stats = cal.by_day.setdefault(day, DayStats(day))
        stats.runs += 1
        cal.total_runs += 1
        if len({c[0] for c in claims}) > 1:
            stats.contested += 1
            cal.total_contested += 1
        won = [c[1] for c in claims if c[2] == "EXECUTED"]
        if won:
            stats.wins.append(max(won))
    return cal


def summary(cal: RunCalendar) -> str:
    """One-line-per-day report, in the repo's print-your-coverage style."""
    if not cal.total_runs:
        return "  runs: no waiver history to measure"
    lines = [f"  runs: {cal.total_runs:,} over seasons {cal.seasons[0]}-{cal.seasons[-1]}"
             f" | processes {len(cal.process_days)} day(s)/wk at hour "
             f"{cal.process_hour} | UTC{cal.utc_offset:+d} "
             f"({cal.day_agreement:.0%} on declared days"
             f"{'' if cal.offset_stable else ', OFFSET AMBIGUOUS'})"]
    for d in DAYS:
        s = cal.by_day.get(d)
        if not s:
            continue
        flag = "  <- busiest" if d == cal.busiest else ""
        flag += "  <- most contested" if d == cal.hottest else ""
        price = f"  mean win ${s.mean_win:>6.2f}" if cal.has_bids else ""
        thin = "  (thin)" if s.runs < MIN_DAY_N else ""
        lines.append(f"    {d.title():<10}{s.runs:>6} runs "
                     f"{s.runs / cal.total_runs:>6.1%}"
                     f"  {s.shrunk_rate(cal.overall_rate):>6.1%} contested"
                     f"{price}{thin}{flag}")
    lines.append(f"    overall    {cal.overall_rate:>19.1%} contested")
    return "\n".join(lines)
