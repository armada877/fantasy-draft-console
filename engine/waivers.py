#!/usr/bin/env python3
"""The waiver engine — pluggable by acquisition model, never by league name.

Two leagues on this account spend money for a player; one spends its place in a
queue. Those are different decision problems, not a parameter change, so the engine
branches on `profile.acquisition.model` and nothing else.

    FAAB      -> suggest_bid(...)   -> {"suggested", "p80", "p_contested"}
    PRIORITY  -> claim_or_wait(...) -> {"recommend", "p_survives"}

Both reduce to one shared quantity, `p_contested`: will somebody else take him
before he clears? The two models then SPEND that estimate differently — one prices
it, one times it.

Ranking is always `engine.lineup.marginal_add` — what he adds to YOUR starting
lineup after the drop he forces — never raw projected points. A 14-ppg back is worth
a lot to a roster with a hole and close to nothing to one stacked at that slot, and
no national ranking prices that. Both horizons are computed for every candidate and
reported side by side: the rest of the regular season is the playoff race, the
playoff weeks are the prize, and averaging them hides the difference.

LEVEL AND SHAPE — the thing to understand before reading anything below.

Fitting a per-player contest model against this league's own history earned
nothing out of sample: leave-one-season-out over 2,075 claims gives a Brier score
of 0.1994 against a 0.2012 flat-constant baseline, AUC 0.564. That is a coin flip
with extra steps, so it does not ship. Concretely:

  LEVEL — how often a claim is contested here at all — is the league's own
          MEASURED BASE RATE, read off the calibrated curve. the 12-team league: 28%.
          Chi Phi: 12%. Inlaws: 13%, and unvalidated at that (one season).
          It does not vary by player, because we cannot show that it should.
  SHAPE — WHICH managers would be the ones contesting, and in what order — is
          modelled: who actually gains a starting-lineup point from him (the
          lineup solver run over every rival roster), each manager's calibrated
          claim appetite, and the market's demand signals. Shape is what the
          priority queue and the rival-bid model consume, and it is never allowed
          to move the level.

With no calibrated curve there is no measured level, so the structural estimate
is all there is — and it is labelled UNVALIDATED everywhere it surfaces rather
than quietly presented as equivalent.

PRICING likewise separates what the room does from what we are worth:

  what it takes    `p80_win` from the curve. This is the one metric the
                   calibration actually earned (+5.4% pinball loss against the
                   baseline), so it ships as measured, unblended. `median_win`
                   did NOT clear the bar, so the going rate comes from the pooled
                   empirical value instead of the fitted curve.
  what it is worth your ceiling — his value ABOVE the best substitute you would
                   have got for nothing anyway, as a share of the budget you have
                   left. This is the roster-specific part, the only part nobody
                   else can price, and it is computed, not predicted.

Bid = min(ceiling, p80), and the minimum bid whenever the going rate already
exceeds the ceiling. Week beats player quality as a price driver by a distance —
the same league pays a mean winning bid of $7.74 in week 2 and $0.10 by week 17 —
so the week row anchors both numbers. That is why 58% of winning claims here cost
nothing: the advice is which weeks to spend, not what to bid.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import exp

from .lineup import Player, marginal_add
from .profile import FAAB, LeagueProfile, PRIORITY, slot_name
# How many upcoming weeks count as "now". A waiver claim is an option you can
# exercise again in three days, so value that lands in week 10 does NOT need to be
# bought in week 2 — you fill a week-10 bye in week 9, from that week's pool. Summing
# the whole rest-of-season horizon into one number hides that completely: it ranked
# six week-10 backup QBs above every player who could help this Sunday.
NEAR_WEEKS = 3

from .state import (EPS, SeasonState, TeamState, clamp, horizon_points, none_of,
                    percentiles, saturating)
from .waiver_runs import RunCalendar

# ── model priors ─────────────────────────────────────────────────────────────
# These are the shape of the behavioural model, NOT league facts. Every league
# quantity — budget, minimum bid, team count, week count, which slots start — is
# read off the profile. Each is overridable per call.

HALF_INTEREST = 0.25
"""An add worth a quarter of a replacement starter's remaining output is a
manager's obvious weekly target. Expressed as a FRACTION of replacement so it
rescales itself for league size, horizon length and 2QB."""

MAX_TEAM_CLAIM = 0.95
"""No single manager is a certainty."""

BLEND = {"demand": 0.45, "needs": 0.30, "curve": 0.25}
"""Rival demand leads: consensus rank, trending adds and %owned change are the
best in-season predictors of who else wants him this week. Roster-need modelling
is structural and league-specific; the measured curve is a base rate, not
player-specific. Renormalised over whichever components are available."""

P80 = 0.80
"""'The bid that wins ~80% of the time' — the definition of the p80 field."""


# ── shared core: will somebody else take him? ────────────────────────────────
@dataclass
class Contest:
    """P(contested) plus every input that produced it, so any number is traceable."""
    p: float
    by_team: list = field(default_factory=list)      # [{team_id, gain, p_claim, ...}]
    components: dict = field(default_factory=dict)   # {name: probability}
    weights: dict = field(default_factory=dict)
    demand: dict = field(default_factory=dict)       # the raw market signals used
    curve_source: str | None = None                  # which reading set the level
    run: dict | None = None                          # the run this was priced against

    @property
    def interested(self) -> list:
        return [r for r in self.by_team if r["p_claim"] > 0]

    def scaled_claims(self) -> list:
        """Per-manager claim probabilities rescaled to reproduce the blended `p`.

        The headline number blends market demand into the roster-need model. The
        bid curve and the priority calculation both need per-manager probabilities
        that AGREE with that headline, so the per-team numbers are scaled by one
        factor found by bisection rather than left inconsistent with it.
        """
        qs = [r["p_claim"] for r in self.by_team]
        if not qs or self.p <= 0 or max(qs) <= 0:
            return [0.0] * len(qs)
        lo, hi = 0.0, 1.0 / max(qs)
        for _ in range(40):
            mid = (lo + hi) / 2
            if 1.0 - none_of([q * mid for q in qs]) < self.p:
                lo = mid
            else:
                hi = mid
        return [clamp(q * hi) for q in qs]


def _demand_signal(player: Player, state: SeasonState, pool_ids=None) -> tuple:
    """Market demand for this player as a probability, plus the raw signals.

    Percentile within the league's OWN available pool, not an absolute threshold:
    the most-added free agent on the board is nearly certain to be claimed by
    somebody, the median one is not. Three signals, averaged over whichever the
    sources actually delivered — any of them may be absent or down.
    """
    ids = list(pool_ids) if pool_ids is not None else [p.id for p in state.free_agents]
    if player.id not in ids:
        ids = ids + [player.id]
    raws = [state.raw.get(i) or {} for i in ids]
    idx = ids.index(player.id)

    def pct_of(key, in_mkt=True, invert=False):
        vals, have = [], []
        for r in raws:
            src = (r.get("mkt") or {}) if in_mkt else r
            v = src.get(key)
            ok = v is not None
            try:
                vals.append(float(v) if ok else 0.0)
            except (TypeError, ValueError):
                vals.append(0.0)
                ok = False
            have.append(ok)
        if not have[idx] or not any(have):
            return None, None
        if invert:                       # a better (lower) rank means more demand
            top = max(vals) + 1
            vals = [(top - v) if h else 0.0 for v, h in zip(vals, have)]
        else:
            floor = min(v for v, h in zip(vals, have) if h)
            vals = [v if h else floor for v, h in zip(vals, have)]
        return percentiles(vals)[idx], vals[idx]

    parts, raw_signals = [], {}
    for key, in_mkt, invert in (("trend_adds", True, False),
                                ("ros_ecr", True, True),
                                ("pct_owned_change", False, False)):
        pct, val = pct_of(key, in_mkt, invert)
        if pct is None:
            continue
        parts.append(pct)
        raw_signals[key] = val
    if not parts:
        return None, {}
    return sum(parts) / len(parts), raw_signals


# Which out-of-sample metric in a curve's `validation.promoted` block governs which
# lookup. A curve that did not beat its baseline on a metric must not be used for
# that metric — the file says so itself, and the repo's rule is to believe it.
CURVE_METRIC = {"median_win": "median_mae", "p80_win": "p80_pinball",
                "p_contested": "contested_brier",
                "p_survives_to_free_agency": "contested_brier"}
# What to fall back to when the curve is not promoted for that metric: the pooled
# constant the same file measured. Strictly better than our model prior — it is
# this league's own base rate — and strictly weaker than a validated curve.
CURVE_POOLED = {"median_win": ("median_win_contested", "median_win_uncontested"),
                "p80_win": ("p80_win_contested",),
                "p_contested": ("p_contested",),
                "p_survives_to_free_agency": ()}


def _week_bin(curve: dict, week, profile: LeagueProfile):
    """Which of the curve's own week bins this week falls in, if it has any."""
    bins = curve.get("week_bins") or {}
    if not bins:
        return None
    season = int(profile.regular_weeks or 0)
    if week is None or season <= 0:
        return None
    if int(week) <= 1:
        return 0
    frac = int(week) / season
    return 1 if frac <= 0.35 else (2 if frac <= 0.65 else 3)


def _pooled(curve: dict, field_name: str):
    pool = ((curve.get("concentration") or {}).get("pooled")) or {}
    for key in CURVE_POOLED.get(field_name, ()):
        v = pool.get(key)
        try:
            return float(v), "pooled constant (curve not promoted for this metric)"
        except (TypeError, ValueError):
            continue
    return None, None


def curve_value(curve: dict | None, quality_pct: float | None, field_name: str, *,
                week=None, profile: LeagueProfile | None = None):
    """A calibrated curve's number for one metric — or its pooled constant.

    The curve files carry their own leave-one-season-out verdict. Where a metric
    was NOT promoted, this returns the league's pooled constant instead of the
    quality bucket, because that is precisely what the validation block says to do.
    Returns (value, provenance) so the caller can say which it used.
    """
    if not curve:
        return None, None
    promoted = ((curve.get("validation") or {}).get("promoted")) or {}
    metric = CURVE_METRIC.get(field_name)
    if metric in promoted and not promoted.get(metric):
        v, why = _pooled(curve, field_name)
        if v is not None:
            return v, why
        return None, None
    if quality_pct is None:
        return _pooled(curve, field_name)

    # the quality x week table when the file has one, else the flat quality curve
    wb = _week_bin(curve, week, profile) if profile is not None else None
    rows = curve.get("by_quality_week") or []
    if rows and wb is not None:
        q_bin = min(4, max(0, int(float(quality_pct) * 5)))
        hit = next((r for r in rows if r.get("q_bin") == q_bin and r.get("week_bin") == wb),
                   None)
        if hit and hit.get(field_name) is not None:
            try:
                return float(hit[field_name]), "curve (quality x week bucket)"
            except (TypeError, ValueError):
                pass

    best, best_d = None, None
    for b in curve.get("by_quality") or []:
        try:
            d = abs(float(b.get("pct")) - float(quality_pct))
        except (TypeError, ValueError):
            continue
        if best_d is None or d < best_d:
            best, best_d = b, d
    if best is not None and best.get(field_name) is not None:
        try:
            return float(best[field_name]), "curve (quality bucket)"
        except (TypeError, ValueError):
            pass
    return _pooled(curve, field_name)


def _curve_lookup(curve, quality_pct, field_name, **kw):
    return curve_value(curve, quality_pct, field_name, **kw)[0]


def base_rate(curve: dict | None, field_name: str):
    """The league's measured BASE RATE for a metric, with its provenance.

    Deliberately player-independent. This is the number that ships as the level
    of `p_contested` / `p_survives`, because that is the only part of those
    quantities the history actually supports. Three places it can live, in order:
    the pooled concentration block, the `by_quality` catch-all row a league writes
    when it could not measure quality at all, and failing both an n-weighted mean
    over the weekly rows.
    """
    if not curve:
        return None, None
    pool = ((curve.get("concentration") or {}).get("pooled")) or {}
    v = pool.get(field_name)
    try:
        return float(v), f"league base rate {float(v):.0%} over {pool.get('n_claims', '?')} claims"
    except (TypeError, ValueError):
        pass
    for row in curve.get("by_quality") or []:
        if row.get("pct") is None and row.get(field_name) is not None:
            try:
                return float(row[field_name]), (
                    f"league base rate {float(row[field_name]):.0%} over "
                    f"{row.get('n', '?')} claims (quality unmeasured)")
            except (TypeError, ValueError):
                continue
    num, den = 0.0, 0
    for row in curve.get("by_week") or []:
        try:
            n = int(row.get("n") or 0)
            num += float(row[field_name]) * n
            den += n
        except (KeyError, TypeError, ValueError):
            continue
    if den:
        return num / den, f"league base rate {num / den:.0%} over {den} weekly claims"
    return None, None


def _week_multiplier(curve: dict | None, week, profile: LeagueProfile | None = None):
    """How expensive THIS week of the season is relative to the league's average.

    Used to scale a run-day price, so a busy day in week 2 and the same busy day
    in week 16 do not price alike. Clamped, because it is one descriptive marginal
    scaling another and neither was fitted jointly.
    """
    row = week_price(curve, week, profile)
    rows = (curve or {}).get("by_week") or []
    num = den = 0.0
    for r in rows:
        try:
            n = float(r.get("n") or 0)
            num += float(r["mean_win"]) * n
            den += n
        except (KeyError, TypeError, ValueError):
            continue
    try:
        here = float(row["mean_win"])
    except (KeyError, TypeError, ValueError):
        return 1.0, None
    if den <= 0 or num <= 0:
        return 1.0, None
    overall = num / den
    mult = clamp(here / overall, 0.25, 4.0)
    return mult, (f"week {week} price level {mult:.2f}x the league average "
                  f"(n={row.get('n', '?')})")


def week_price(curve: dict | None, week, profile: LeagueProfile | None = None):
    """What this league has historically paid in THIS week of the season.

    Week dominates the price far more than player quality does — the same league
    pays a mean winning bid of $7.74 in week 2 and $0.10 by week 17, because the
    board is full early and empty late. So the week row is the anchor, not a
    refinement. Returns the raw row so the caller can quote it.
    """
    if not curve or week is None:
        return None
    rows = curve.get("by_week") or []
    best = None
    for r in rows:
        try:
            if int(r.get("week")) == int(week):
                best = r
                break
        except (TypeError, ValueError):
            continue
    return best


def rival_interest(player: Player, teams, profile: LeagueProfile, weeks, *,
                   state: SeasonState | None = None, reps=None,
                   exclude_team_id=None) -> list:
    """For each rival: does this player actually improve their starting lineup?

    Answered with the lineup solver on their roster, not with a positional guess.
    An upper bound (adding him without dropping anybody) is computed first, because
    it settles the common case in one solve instead of one per droppable player.
    """
    ref = state.horizon_value(player, weeks, reps) if state is not None else 0.0
    out = []
    for t in teams:
        if exclude_team_id is not None and t.team_id == exclude_team_id:
            continue
        rec = {"team_id": t.team_id, "manager": t.manager, "gain": 0.0,
               "interest": 0.0, "p_claim": 0.0, "blocked": None}
        if not t.can_bid(profile):
            rec["blocked"] = f"cannot meet the ${profile.acquisition.min_bid} minimum bid"
            out.append(rec)
            continue
        base = horizon_points(t.roster, profile, weeks)
        upper = horizon_points(list(t.roster) + [player], profile, weeks) - base
        if upper > EPS:
            gain, _ = marginal_add(t.roster, player, profile, weeks, must_drop=True)
            rec["gain"] = max(0.0, gain)
            rec["interest"] = (clamp(rec["gain"] / ref) if ref > EPS
                               else (1.0 if rec["gain"] > 0 else 0.0))
        out.append(rec)
    return out


def _blend(components: dict, blend: dict) -> tuple:
    weights = {k: blend.get(k, 0.0) for k in components}
    total = sum(weights.values())
    if total <= 0:
        return components.get("needs", 0.0), {"needs": 1.0}
    weights = {k: v / total for k, v in weights.items()}
    return sum(components[k] * weights[k] for k in components), weights


def contest_from_interest(player: Player, rows, state, *, shares=None, teams=None,
                          curve=None, quality_pct=None, pool_ids=None, runs=None,
                          half_interest: float = HALF_INTEREST,
                          blend: dict | None = None) -> Contest:
    """Turn per-rival interest into P(contested).

    LEVEL vs SHAPE, and the difference is the whole design. Fitting a per-player
    contest model to this league's own history produced essentially no
    out-of-sample skill (leave-one-season-out over 2,075 claims: Brier 0.1994 vs
    a 0.2012 constant baseline, AUC 0.564). So whenever the league has a measured
    base rate, THAT is the level — a flat, honest "this is how often a claim is
    contested here" — and the roster-need and demand modelling is demoted to
    SHAPE: which managers, in what order, would be the ones contesting it. Shape
    is what the priority calculation and the rival-bid model need; level is a
    prediction, and we have not earned one.

    With no measured base rate the structural blend is all we have, and it is
    labelled unvalidated rather than quietly presented as equivalent.

    `shares` (from the board) is each manager's share of his OWN weekly claim
    budget that this player represents. Without it — the single-player call — a
    manager's interest is treated as his top target, the upper bound on how
    likely he is to spend a claim here.
    """
    profile = state.profile
    by_id = {t.team_id: t for t in (teams or state.opponents)}
    for r in rows:
        t = by_id.get(r["team_id"])
        if t is None or r["blocked"]:
            continue
        appetite = t.claims_per_week(profile)
        if shares is not None:
            weight = shares.get(t.team_id, 0.0)
        else:
            weight = saturating(r.get("interest", 0.0), half_interest)
        r["p_claim"] = clamp(appetite * weight, 0.0, MAX_TEAM_CLAIM)

    components = {"needs": 1.0 - none_of([r["p_claim"] for r in rows])}
    demand_p, demand_raw = _demand_signal(player, state, pool_ids)
    if demand_p is not None:
        components["demand"] = clamp(demand_p)

    # LEVEL, best source first. The run the claim lands in is by far the
    # strongest signal available — 38.5% contested on this league's busy day
    # against 14.2% on every other, where a per-player model managed AUC 0.564 —
    # and it is a descriptive statistic from the league's own transactions, not a
    # fitted tuning. So it outranks the flat season-wide base rate.
    run_block = runs.to_dict() if isinstance(runs, RunCalendar) else (runs or None)
    nxt = (run_block or {}).get("next_run") if run_block else None
    rate, rate_src = base_rate(curve, "p_contested")
    if nxt and nxt.get("p_contested") is not None:
        components["next_run"] = clamp(float(nxt["p_contested"]))
        p, weights = components["next_run"], {"next_run": 1.0}
        src = "; ".join((run_block or {}).get("provenance") or []) or (
            f"next run ({str(nxt.get('day') or '').title()}) is "
            f"{float(nxt['p_contested']):.0%} contested")
    elif rate is not None:
        components["league_base_rate"] = clamp(rate)
        p, weights = clamp(rate), {"league_base_rate": 1.0}
        src = (f"{rate_src} — a per-player contest model showed no out-of-sample "
               f"skill, so the level is the league's own base rate and only the "
               f"ordering across managers is modelled")
    else:
        p, weights = _blend(components, dict(blend or BLEND))
        src = ("no measured base rate for this league — structural estimate from "
               "rival demand and roster need, UNVALIDATED")
    return Contest(p=clamp(p), by_team=rows, components=components, weights=weights,
                   demand=demand_raw, curve_source=src, run=run_block)


def contest_detail(player: Player, teams, profile: LeagueProfile, tendencies=None, *,
                   state: SeasonState, weeks=None, reps=None, exclude_team_id=None,
                   curve=None, quality_pct=None, pool_ids=None, runs=None,
                   half_interest: float = HALF_INTEREST,
                   blend: dict | None = None) -> Contest:
    """The full P(contested) working for one player, not just the number."""
    weeks = weeks if weeks is not None else state.reg_weeks
    rows = rival_interest(player, teams, profile, weeks, state=state, reps=reps,
                          exclude_team_id=exclude_team_id)
    return contest_from_interest(player, rows, state, teams=teams, curve=curve,
                                 quality_pct=quality_pct, pool_ids=pool_ids, runs=runs,
                                 half_interest=half_interest, blend=blend)


def p_contested(player: Player, teams, profile: LeagueProfile, tendencies=None,
                **kw) -> float:
    """Will somebody else take him before he clears? -> probability in [0, 1].

    The one quantity both acquisition models are built on. `contest_detail`
    returns the same number with the working attached.
    """
    return contest_detail(player, teams, profile, tendencies, **kw).p


# ── the budget rule: value above the best free substitute ────────────────────
def budget_shares(values, contests=None) -> list:
    """Each target's share of the budget, from his value ABOVE his substitutes.

    The quantity that matters is never the add's value, it is the value you would
    LOSE by not winning him — which is what he is worth beyond the best substitute
    you expect to get for nothing anyway. That is the whole reason most winning
    bids in a real FAAB league are free: on a deep board every target has a near
    twin, so nobody is worth paying for.

    Scarcity is therefore measured against the board's total value, not against
    the total excess. Normalising by total excess would hand the entire budget to
    whoever happens to be one point better than his twin; normalising by total
    value says a board of near-identical players is cheap across the board, and a
    board with one irreplaceable player is worth emptying the wallet for. Shares
    sum to at most 1 — the rest of the budget is not unallocated, it is being
    kept for the weeks that have a real target in them.
    """
    n = len(values)
    if n == 0:
        return []
    contests = list(contests or [0.0] * n)
    free = [max(0.0, v) * (1.0 - clamp(c)) for v, c in zip(values, contests)]
    excess = []
    for i in range(n):
        sub = max([free[j] for j in range(n) if j != i] or [0.0])
        excess.append(max(0.0, values[i] - sub))
    total = sum(max(0.0, v) for v in values)
    return [clamp(e / total) for e in excess] if total > EPS else [0.0] * n


# ── FAAB: price the contest ──────────────────────────────────────────────────
@dataclass
class BidPlan:
    suggested: int
    p80: int
    contest: Contest
    ceiling: float
    share: float = 0.0
    rivals: list = field(default_factory=list)
    source: str = "opponent-model"
    week_row: dict | None = None        # this league's measured price in this week

    def as_bid(self) -> dict:
        return {"suggested": int(self.suggested), "p80": int(self.p80),
                "p_contested": round(self.contest.p, 3)}


def _p_win(bid: float, rivals, min_bid: int) -> float:
    """P(my bid beats the field).

    Each rival bids with probability q; conditional on bidding, his amount is
    modelled as exponential around his expected bid, so P(he beats X) decays
    smoothly — nobody's bid is knowable to the dollar. At the league minimum this
    collapses to P(nobody bids at all), which is exactly 1 - p_contested. That
    identity is what makes the minimum bid the correct recommendation whenever
    nothing is contested.
    """
    surv = 1.0
    for r in rivals:
        scale = max(r["bid"], EPS)
        surv *= (1.0 - clamp(r["q"] * exp(-(max(0.0, bid - min_bid) / scale))))
    return clamp(surv)


def plan_bid(player: Player, me: TeamState, teams, profile: LeagueProfile, *,
             state: SeasonState, marginal: float, weeks=None, tendencies=None,
             curve=None, quality_pct=None, alternatives=None, reps=None,
             pool_ids=None, runs=None, contest: Contest | None = None,
             share: float | None = None, rival_bids=None) -> BidPlan:
    """The full FAAB working: what he is worth to you, and what the room will pay."""
    weeks = weeks if weeks is not None else state.reg_weeks
    acq = profile.acquisition
    if contest is None:
        contest = contest_detail(player, teams, profile, tendencies, state=state,
                                 weeks=weeks, reps=reps, exclude_team_id=me.team_id,
                                 curve=curve, quality_pct=quality_pct,
                                 pool_ids=pool_ids, runs=runs)
    if share is None:
        vals = [marginal] + [v for v in (alternatives or []) if v > 0]
        share = budget_shares(vals)[0]

    budget = me.budget(profile)
    ceiling = budget * clamp(share)

    rivals = list(rival_bids or [])
    if not rivals:
        qs = contest.scaled_claims()
        by_id = {t.team_id: t for t in teams}
        for rec, q in zip(contest.by_team, qs):
            if q <= 0:
                continue
            t = by_id.get(rec["team_id"])
            if t is None:
                continue
            rivals.append({"team_id": t.team_id, "manager": t.manager, "q": q,
                           "bid": max(float(acq.min_bid),
                                      t.budget(profile) * clamp(share)),
                           "budget": t.budget(profile)})
    if not rivals and contest.p > 0:
        # demand signals say somebody wants him even though no rival roster models
        # a gain — carry that as one anonymous rival at the league's median budget
        budgets = sorted(t.budget(profile) for t in teams) or [budget]
        med = budgets[len(budgets) // 2]
        rivals = [{"team_id": None, "manager": "market demand", "q": contest.p,
                   "bid": max(float(acq.min_bid), med * clamp(share)), "budget": med}]

    lo, hi = int(acq.min_bid), int(max(acq.min_bid, budget))

    # ── what it takes to win: the league's own measured price ────────────────
    # `p80_win` is the one metric this league's curve actually earned out of
    # sample (+5.4% pinball loss against the constant baseline), so it ships
    # directly rather than blended with anything of ours. `median_win` did NOT
    # clear the bar, so the going rate comes from the pooled empirical value.
    # The week row is the anchor underneath both: the same league pays several
    # dollars in week 2 and nothing at all by week 17.
    wk = week_price(curve, state.week, profile)
    p80_win, p80_src = curve_value(curve, quality_pct, "p80_win",
                                   week=state.week, profile=profile)
    med_win, med_src = curve_value(curve, quality_pct, "median_win",
                                   week=state.week, profile=profile)
    if wk is not None:
        if wk.get("p80_win") is not None:
            try:
                p80_win = float(wk["p80_win"])
                p80_src = f"week {state.week} of league history (n={wk.get('n', '?')})"
            except (TypeError, ValueError):
                pass
        if wk.get("median_win") is not None:
            try:
                med_win = float(wk["median_win"])
                med_src = f"week {state.week} of league history (n={wk.get('n', '?')})"
            except (TypeError, ValueError):
                pass

    # The RUN beats the week as a price anchor. The same league pays a mean $5.77
    # on its busy day and $2.00 the next morning; that 3x is bigger and steadier
    # than anything the week-of-season row carries on its own. When both are
    # measured, the day sets the level and the week scales it — two descriptive
    # marginals multiplied, clamped, and labelled as exactly that.
    nxt = (contest.run or {}).get("next_run") if contest.run else None
    if nxt and nxt.get("p80_win") is not None:
        mult, mult_src = _week_multiplier(curve, state.week, profile)
        p80_win = float(nxt["p80_win"]) * mult
        if nxt.get("median_win") is not None:
            med_win = float(nxt["median_win"]) * mult
        p80_src = (f"{str(nxt.get('day') or '').title()} run prices over "
                   f"{nxt.get('n_runs', '?')} runs" + (f" x {mult_src}" if mult_src else ""))
        med_src = p80_src

    if p80_win is not None:
        # measured league: willingness to pay is ours, the price is theirs
        going = med_win if med_win is not None else 0.0
        if ceiling < going:
            best_x = lo          # not worth the going rate — this is not your week
        else:
            best_x = int(round(min(ceiling, float(p80_win))))
        p80_x = int(round(float(p80_win)))
        source = f"{p80_src}, capped by your roster-derived ceiling"
    else:
        # No measured curve. Fall back to the opponent model and SAY SO.
        # First-price sealed bid: you pay only if you win, so the objective is
        # (value - bid) x P(win). Charging the bid on a loss prices every
        # contested player at the minimum, which is wrong in the other direction.
        best_x, best_surplus, p80_x, seen80 = lo, None, hi, False
        for x in range(lo, hi + 1):
            pw = _p_win(x, rivals, acq.min_bid)
            surplus = (ceiling - x) * pw
            if best_surplus is None or surplus > best_surplus + EPS:
                best_surplus, best_x = surplus, x
            if not seen80 and pw >= P80:
                p80_x, seen80 = x, True
        source = "opponent model (no calibrated curve for this league) — UNVALIDATED"

    # A player who does not improve your starters is worth the minimum no matter
    # what the room paid for his equivalent last season. p80 is NOT capped: it
    # answers "what would it take", a fact about the room, not about your roster.
    best_x = int(clamp(best_x, lo, max(lo, min(int(ceiling), hi))))
    p80_x = int(clamp(p80_x, lo, hi))

    return BidPlan(suggested=best_x, p80=p80_x, contest=contest, ceiling=ceiling,
                   share=clamp(share), rivals=rivals, source=source, week_row=wk)


def suggest_bid(player: Player, me: TeamState, teams, profile: LeagueProfile,
                **kw) -> dict:
    """FAAB recommendation -> {"suggested", "p80", "p_contested"}."""
    return plan_bid(player, me, teams, profile, **kw).as_bid()


# ── PRIORITY: time the contest ───────────────────────────────────────────────
@dataclass
class ClaimPlan:
    recommend: bool
    p_survives: float
    p_win_now: float
    contest: Contest
    gain_now: float
    option_cost: float
    position: int | None

    def as_claim(self) -> dict:
        return {"recommend": bool(self.recommend), "p_survives": round(self.p_survives, 3)}


def plan_claim(player: Player, me: TeamState, teams, profile: LeagueProfile, *,
               state: SeasonState, marginal: float, weeks=None, tendencies=None,
               curve=None, quality_pct=None, next_best: float = 0.0, reps=None,
               pool_ids=None, runs=None, contest: Contest | None = None) -> ClaimPlan:
    """Claim now, or wait and keep your place in the queue?

    There is no price here, so there is no surplus to capture by valuing a player
    above the room — what consensus buys you is TIMING, not value. Waiver position
    is a single-use asset that resets to last the moment you spend it, so:

        claim   ->  marginal x P(win from where you sit)
        wait    ->  marginal x P(he clears to free agency anyway)
                    + the option to spend your position on the next target

    The useful output is not a number, it is "he will not clear — claim now"
    versus "he clears, save your priority".
    """
    weeks = weeks if weeks is not None else state.reg_weeks
    if contest is None:
        contest = contest_detail(player, teams, profile, tendencies, state=state,
                                 weeks=weeks, reps=reps, exclude_team_id=me.team_id,
                                 curve=curve, quality_pct=quality_pct,
                                 pool_ids=pool_ids, runs=runs)

    qs = contest.scaled_claims()
    by_id = {t.team_id: t for t in teams}
    ahead, everyone = [], []
    mine = me.waiver_priority
    for rec, q in zip(contest.by_team, qs):
        everyone.append(q)
        theirs = getattr(by_id.get(rec["team_id"]), "waiver_priority", None)
        if mine is None or theirs is None:
            ahead.append(q * 0.5)          # order unknown: half the field is ahead
        elif theirs < mine:
            ahead.append(q)

    # Level from the league's measured base rate, shape from where you sit in the
    # queue. The structural model says WHO would claim him; it is not allowed to
    # say how often a claim happens, because that is the part with no validated
    # skill. `p_win_now` keeps the shape: managers ahead of you in the order are
    # the only ones who can take him first, so a good position raises it above
    # the flat survival rate and last position collapses it onto it.
    p_win_now, p_survives = none_of(ahead), none_of(everyone)
    nxt = (contest.run or {}).get("next_run") if contest.run else None
    surv_rate, surv_src = base_rate(curve, "p_survives_to_free_agency")
    if nxt and nxt.get("p_contested") is not None:
        # the run he would clear is the one that decides whether he clears
        surv_rate = 1.0 - clamp(float(nxt["p_contested"]))
        surv_src = (f"1 - the {str(nxt.get('day') or '').title()} run's measured "
                    f"contested rate over {nxt.get('n_runs', '?')} runs")
    elif surv_rate is None:
        cont_rate, _ = base_rate(curve, "p_contested")
        if cont_rate is not None:
            surv_rate, surv_src = 1.0 - clamp(cont_rate), "1 - the league's contested base rate"
    if surv_rate is not None:
        lift = (p_win_now - p_survives)          # what your queue position is worth
        p_survives = clamp(surv_rate)
        p_win_now = clamp(p_survives + lift)

    edge = max(0.0, p_win_now - p_survives)
    gain_now = marginal * edge

    if profile.acquisition.order_resets:
        # spending your position costs you the next target you would have won with
        # it; how likely such a target appears scales with how much season is left
        season = max(1, int(profile.regular_weeks or len(weeks)))
        option_cost = max(0.0, next_best) * edge * clamp(len(weeks) / season)
    else:
        option_cost = 0.0             # position is not consumed, so nothing is forgone

    return ClaimPlan(recommend=bool(marginal > EPS and gain_now > option_cost),
                     p_survives=p_survives, p_win_now=p_win_now, contest=contest,
                     gain_now=gain_now, option_cost=option_cost, position=mine)


def claim_or_wait(player: Player, me: TeamState, teams, profile: LeagueProfile,
                  **kw) -> dict:
    """Priority-order recommendation -> {"recommend", "p_survives"}."""
    return plan_claim(player, me, teams, profile, **kw).as_claim()


# ── the board ────────────────────────────────────────────────────────────────
def _why(player: Player, state: SeasonState, reg: float, post: float, drop,
         plan, reps, weeks) -> list:
    """Rationale, in the order the value actually accrues.

    Roster fit first: that is the part no national ranking can price, and in-season
    it is the only place a real edge lives — the platform projection is our
    backbone, so we have nothing to out-project it with. Then the contest odds,
    which is what the decision actually turns on. Market signals appear last and
    strictly as evidence about RIVAL DEMAND, never as a claim that the market has
    the player wrong.
    """
    prof = state.profile
    why = []
    slots = [int(s) for s in prof.starting_slots if int(s) in player.eligible_slots]
    best = max(slots, key=lambda s: reps.get(s, 0.0)) if slots else None
    rep = state.replacement_for(player, reps)
    if reg <= EPS:
        why.append(f"{player.points_per_game:.1f} ppg but he does not crack your "
                   f"starting lineup in any remaining week — worth ~0 to you, "
                   f"whatever he costs the room")
    elif best is not None:
        why.append(f"{slot_name(best)}: {player.points_per_game:.1f} ppg vs "
                   f"{rep:.1f} replacement — +{reg:.1f} to your starters over "
                   f"{len(weeks)} wk")
    else:
        why.append(f"+{reg:.1f} to your starters over {len(weeks)} wk")
    if drop is not None:
        why.append(f"costs you {drop.name} ({drop.points_per_game:.1f} ppg) off the roster")
    if reg > EPS and post > reg * 0.5:
        why.append(f"holds up in the playoff weeks (+{post:.1f})")
    elif reg > EPS and post <= EPS:
        why.append("regular-season help only — adds nothing in the playoff weeks")
    mine = {p.bye_week for p in state.my_team.roster if p.bye_week}
    if player.bye_week and player.bye_week not in mine:
        why.append(f"bye wk {player.bye_week} does not clash with your starters")

    c = plan.contest
    # SHAPE, scaled so it adds up to the headline base rate rather than sitting
    # next to it contradicting it: if this claim is contested, these are the
    # managers it is most likely to be contested by.
    scaled = sorted(zip(c.by_team, c.scaled_claims()), key=lambda t: -t[1])[:3]
    hot = [(r, q) for r, q in scaled if q > 0.005]
    if hot:
        why.append("if contested, most likely by: " + ", ".join(
            f"{r['manager'] or ('team ' + str(r['team_id']))} {q:.0%}"
            for r, q in hot))
    else:
        why.append("no rival roster gains a starting point from him")
    bits = [f"{k} {v:,.0f}" if abs(v) >= 100 else f"{k} {v:g}"
            for k, v in sorted(c.demand.items())]
    if bits:
        why.append("rival-demand signals: " + ", ".join(bits))
    if isinstance(plan, BidPlan):
        why.append(f"worth {plan.share:.0%} of the value left on your board -> "
                   f"ceiling ${plan.ceiling:.0f} of ${state.my_team.budget(prof)} left")
        why.append(f"price source: {plan.source}")
    why.extend(_timing_why(plan.contest, isinstance(plan, BidPlan), reg))
    if c.curve_source:
        why.append(f"contest odds: {c.curve_source}")
    return why


def _timing_why(contest: Contest, is_faab: bool, marginal: float) -> list:
    """WHEN to make the claim — which is the advice that actually pays.

    Claims are not spread evenly. In every league here they pile into one
    processing run a week, and that run is two to four times more likely to be
    contested than any other — and costs about three times as much. So the
    recommendation is rarely "bid more". It is "this is the expensive run, bid to
    win or wait for the quiet one", and in a priority league waiting is free.
    """
    run = contest.run or {}
    nxt = run.get("next_run")
    if not nxt:
        return []
    out = []
    day = str(nxt.get("day") or "").title()
    hours = nxt.get("hours_away")
    when = f"{day} {int(run.get('process_hour') or 0):02d}:00"
    if hours is not None:
        when += f", {hours:.0f}h away"
    out.append(f"next run: {when} — {float(nxt.get('p_contested') or 0):.0%} of claims "
               f"in that run are contested (n={nxt.get('n_runs', '?')})")

    quiet = nxt.get("quieter_alternative")
    busiest, hottest = run.get("busiest_day"), run.get("hottest_day")
    by_day = {d["day"]: d for d in run.get("by_day") or []}
    here = by_day.get(nxt.get("day"))
    there = by_day.get(quiet) if quiet else None
    if marginal <= EPS:
        return out
    if here and there:
        ratio = (here["rate"] / there["rate"]) if there["rate"] else None
        cost = ""
        if is_faab and here.get("mean_win") and there.get("mean_win") is not None:
            cost = (f" and costs ${here['mean_win']:.2f} against "
                    f"${there['mean_win']:.2f}")
        out.append(f"this run is {('%.1fx' % ratio) if ratio else 'the most'} more "
                   f"contested than {quiet.title()}'s{cost} — "
                   + ("bid to win here, or wait for the quiet run and take him for "
                      "the minimum" if is_faab else
                      "if he survives it, next run costs you no priority at all"))
    elif nxt.get("day") in (busiest, hottest):
        out.append("this is the busiest run of the week here — everyone else is "
                   "queuing into it too")
    return out


def waiver_board(state: SeasonState, *, tendencies=None, curve=None, runs=None, limit=None,
                 horizon: str = "reg", max_candidates: int = 40,
                 max_priced: int = 15, log=print) -> list:
    """The ranked waiver board, in `season_data.json`'s `waivers` shape.

    `bid` appears only in FAAB leagues and `claim` only in priority leagues, chosen
    off `profile.acquisition.model`. Both horizons are computed for every candidate
    and reported side by side; `horizon` only picks which one sorts the board.

    Every cap is logged rather than silently applied.
    """
    prof = state.profile
    me = state.my_team
    reg_weeks, post_weeks = state.reg_weeks, state.post_weeks
    reps = state.replacement()

    # Pre-screen: points ABOVE the easiest replacement he could displace, then
    # ROUND-ROBIN across positions. Both halves matter. Raw points fills the
    # shortlist with backup quarterbacks, who out-score everyone in absolute terms
    # and start nowhere; points-over-replacement alone then fills it with defenses,
    # because there are thirty-two of them clustered a point above the bar. Taking
    # the best of each position in turn gives every slot the league actually starts
    # a look, and the exact marginal value decides the order afterwards.
    fa = [p for p in state.free_agents if p.points_per_game > 0]
    total_fa = len(state.free_agents)
    by_pos = {}
    for p in fa:
        by_pos.setdefault(p.pos, []).append(p)
    for group in by_pos.values():
        group.sort(key=lambda p: -(p.points_per_game - state.replacement_floor(p, reps)))
    cands, lanes = [], [list(g) for g in by_pos.values()]
    while lanes and len(cands) < max_candidates:
        for lane in list(lanes):
            if not lane:
                lanes.remove(lane)
                continue
            cands.append(lane.pop(0))
            if len(cands) >= max_candidates:
                break
    if log:
        log(f"  waivers: {len(cands)} of {total_fa} free agents scored for marginal "
            f"value (cap max_candidates={max_candidates}; pre-ranked by ppg over the "
            f"easiest replacement they could displace, taken round-robin across "
            f"{len(by_pos)} positions so none crowds the shortlist; "
            f"{total_fa - len(fa)} had no remaining projection)")

    near_weeks = list(reg_weeks)[:NEAR_WEEKS]
    rows = []
    for p in cands:
        reg, drop = marginal_add(me.roster, p, prof, reg_weeks, must_drop=True)
        post, _ = (marginal_add(me.roster, p, prof, post_weeks, must_drop=True)
                   if post_weeks else (0.0, None))
        # What he adds in the weeks you cannot defer past.
        now, _ = (marginal_add(me.roster, p, prof, near_weeks, must_drop=True)
                  if near_weeks else (0.0, None))
        # The first week he actually contributes — i.e. the week the need bites, and
        # therefore the last run you could still wait for. must_drop=False here is a
        # deliberate cheap probe: we only need "does he score in this week", and the
        # drop search would cost a lineup solve per roster slot per week.
        act_by = None
        for w in reg_weeks:
            g, _ = marginal_add(me.roster, p, prof, [w], must_drop=False)
            if g > 0.05:
                act_by = int(w)
                break
        rows.append({"player": p, "reg": max(0.0, reg), "post": max(0.0, post),
                     "now": max(0.0, now), "act_by": act_by, "drop": drop})

    key = "post" if horizon == "post" else "reg"
    weeks = post_weeks if key == "post" else reg_weeks
    # Rank by what you cannot postpone, then by season-long value as the tiebreak.
    # A pure bye-week filler still appears — it just stops outranking players who
    # help on Sunday.
    rows.sort(key=lambda r: (-(r["post"] if key == "post" else r["now"]), -r[key]))
    priced = rows[:max_priced]
    if log and len(rows) > len(priced):
        log(f"  waivers: contest odds + pricing computed for the top {len(priced)} "
            f"of {len(rows)} (cap max_priced={max_priced}); the remainder are "
            f"ranked but unpriced")

    # Rival interest across the WHOLE scored board, not just the priced rows. A
    # manager makes a finite number of claims a week, so his appetite has to be
    # divided across every target he has — divide it over a short list and a
    # popular player reads as a certainty for half the league, which is how a
    # contest model ends up saying everything is contested.
    pool_ids = [r["player"].id for r in rows]
    values = [r[key] for r in priced]
    pcts = percentiles(values) if values else {}
    interest = [rival_interest(r["player"], state.opponents, prof, weeks, state=state,
                               reps=reps, exclude_team_id=me.team_id) for r in rows]

    totals = {}
    for rowset in interest:
        for rec in rowset:
            totals[rec["team_id"]] = totals.get(rec["team_id"], 0.0) + \
                saturating(rec["interest"], HALF_INTEREST)
    shares_by_cand = []
    for rowset in interest:
        s = {}
        for rec in rowset:
            tot = totals.get(rec["team_id"], 0.0)
            s[rec["team_id"]] = (saturating(rec["interest"], HALF_INTEREST) / tot
                                 if tot > EPS else 0.0)
        shares_by_cand.append(s)
    if log:
        log(f"  waivers: rival interest solved for {len(rows)} x "
            f"{len(state.opponents)} roster pairs; free agents below the "
            f"max_candidates cut are assumed to draw no claim (they rank below "
            f"every scored one on projected ppg)")

    contests = [contest_from_interest(r["player"], rowset, state, shares=sh,
                                      teams=state.opponents, curve=curve, runs=runs,
                                      quality_pct=pcts.get(i), pool_ids=pool_ids)
                for i, (r, rowset, sh) in enumerate(zip(priced, interest, shares_by_cand))]

    my_shares = budget_shares(values, [c.p for c in contests])

    out = []
    for i, r in enumerate(priced):
        p, marg, drop, contest = r["player"], r[key], r["drop"], contests[i]
        row = {"player_id": p.id,
               "marginal_now": round(r["now"], 1),
               "near_weeks": len(near_weeks),
               "act_by": r["act_by"],
               "deferrable": bool(r["now"] <= 0.05 and r["reg"] > 0.05),
               "marginal_reg": round(r["reg"], 1),
               "marginal_post": round(r["post"], 1),
               "drop_player_id": drop.id if drop is not None else None}
        if prof.acquisition.model == FAAB:
            # rivals get the same rule applied to their own board, which we have
            # from the interest matrix: their value above their own substitutes
            rival_bids = []
            qs = contest.scaled_claims()
            for j, rec in enumerate(contest.by_team):
                if qs[j] <= 0:
                    continue
                t = next((x for x in state.opponents if x.team_id == rec["team_id"]), None)
                if t is None:
                    continue
                # the same substitution rule applied to HIS board: what this
                # player is worth to him above the best substitute he could get
                # free. Measured over the priced shortlist, which is where the
                # contested money actually is.
                their_vals = [interest[k][j]["gain"] for k in range(len(priced))]
                their_share = budget_shares(their_vals, [c.p for c in contests])[i]
                cap = t.waiver_tendencies.get("max_bid")
                bid = t.budget(prof) * their_share
                try:
                    if cap is not None:
                        bid = min(bid, float(cap))
                except (TypeError, ValueError):
                    pass
                rival_bids.append({"team_id": t.team_id, "manager": t.manager,
                                   "q": qs[j], "bid": max(float(prof.acquisition.min_bid), bid),
                                   "budget": t.budget(prof)})
            plan = plan_bid(p, me, state.opponents, prof, state=state, marginal=marg,
                            weeks=weeks, tendencies=tendencies, curve=curve,
                            quality_pct=pcts.get(i), reps=reps, pool_ids=pool_ids,
                            runs=runs, contest=contest, share=my_shares[i],
                            rival_bids=rival_bids)
            row["bid"] = plan.as_bid()
        else:
            nxt = max([v for j, v in enumerate(values) if j != i] or [0.0])
            plan = plan_claim(p, me, state.opponents, prof, state=state, marginal=marg,
                              weeks=weeks, tendencies=tendencies, curve=curve,
                              quality_pct=pcts.get(i), next_best=nxt, reps=reps,
                              pool_ids=pool_ids, runs=runs, contest=contest)
            row["claim"] = plan.as_claim()
        row["why"] = _why(p, state, r["reg"], r["post"], drop, plan, reps, weeks)
        out.append(row)

    for r in rows[len(priced):]:
        out.append({"player_id": r["player"].id, "marginal_reg": round(r["reg"], 1),
                    "marginal_post": round(r["post"], 1),
                    "drop_player_id": r["drop"].id if r["drop"] is not None else None,
                    "why": ["ranked but not priced — beyond the contest-odds cap"]})
    return out[:limit] if limit else out
