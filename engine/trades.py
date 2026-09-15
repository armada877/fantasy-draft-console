#!/usr/bin/env python3
"""The trade engine — finder, offer evaluator, buy-low/sell-high.

Every question here is the same question the waiver board asks, run twice: what
does this do to MY optimal starting lineup, and what does it do to THEIRS. Both
sides come off `engine.lineup.trade_delta`, so a deal is only proposed when it is
positive for both — which is also the only kind of deal that gets accepted.

Three league facts that are never assumed and always read off the profile:

  * starting requirements. One league on this account starts two quarterbacks, so
    "surplus at QB" means something different there. Surplus and deficit are
    measured against each team's OWN requirement, slot by slot.
  * league size, which sets the replacement level a surplus is measured above.
  * veto votes. A lopsided-but-mutually-beneficial deal is unremarkable in a league
    with no veto mechanism and genuinely reversible in one that needs a handful of
    votes, so veto risk is a first-class output, not a footnote.

On P(accept): the disagreement that pays in a trade is with THAT MANAGER, not with
national consensus. Calibrated tendencies — his positional bias, his trade rate,
whether he answers at all — are preferred wherever WS-3 has them. Market trade
values are a fallback for a counterparty we cannot model, plus the anchor people
actually cite in the negotiation, and every rationale says which one it used.

Combinatorics are capped, and the cap is printed rather than silently applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from .lineup import Player, trade_delta
from .profile import LeagueProfile, slot_name
from .state import (EPS, SeasonState, TeamState, clamp, horizon_points, percentiles,
                    saturating)

# ── model priors (not league facts) ──────────────────────────────────────────
K_SEND = 4
K_RECEIVE = 4
"""How many of each side's most tradeable assets enter the enumeration. The full
space is every subset of both rosters; this is the cap, and `find_trades` logs
what it covered."""

MAX_SEND = 2
MAX_RECEIVE = 2
"""1-for-1, 2-for-1, 1-for-2 and 2-for-2."""

HALF_TRADE = 0.10
"""A gain worth a tenth of a replacement starter's remaining output is a coin-flip
'yes' for an averagely-engaged manager. Trades clear a lower bar than waivers: a
manager who has already agreed to negotiate is buying an edge, not a starter."""

NEUTRAL_ACCEPT = 0.35
"""P(accept) for a manager with no calibration at all. Deliberately middling: an
uncalibrated counterparty is an unknown, not an easy mark."""

NO_RESPONSE = 0.15
"""Multiplier for a manager the history says does not answer offers."""

MEASURED_FLOOR = 0.05
"""Floor for a MEASURED zero. A manager with real history who has never accepted
a trade is unlikely, not impossible, and a hard zero would rank him below a deal
that cannot happen at all."""

FORM_BAND = 0.20
"""How far recent form must diverge from the rest-of-season rate before it is
signal rather than noise."""


# ── positional surplus / deficit, against each team's OWN requirement ────────
@dataclass
class Balance:
    """Per-slot surplus and deficit for one roster over one horizon."""
    by_slot: dict = field(default_factory=dict)

    def deficits(self) -> dict:
        return {s: v["deficit"] for s, v in self.by_slot.items() if v["deficit"] > 0}

    def surpluses(self) -> dict:
        return {s: v["surplus"] for s, v in self.by_slot.items() if v["surplus"] > 0}


def positional_balance(roster, profile: LeagueProfile, weeks, reps: dict) -> Balance:
    """ROS points above/below this roster's own starting requirement, slot by slot.

    A screen, not a valuation: a flex-eligible receiver counts toward both his own
    slot and the flex, so the totals overlap by design. The exact number that ends
    up in a recommendation always comes from `trade_delta`.
    """
    n_weeks = max(1, len(weeks))
    out = {}
    for slot, count in profile.starting_slots.items():
        slot = int(slot)
        rep = reps.get(slot, 0.0) * n_weeks
        elig = sorted((p for p in roster if slot in p.eligible_slots),
                      key=lambda p: -p.points_per_game)
        vals = [p.points_per_game * n_weeks for p in elig]
        need = int(count)
        if len(vals) < need:
            deficit = (need - len(vals)) * rep
        else:
            deficit = max(0.0, rep - vals[need - 1])
        surplus = sum(max(0.0, v - rep) for v in vals[need:])
        out[slot] = {"surplus": surplus, "deficit": deficit,
                     "depth": max(0, len(vals) - need), "replacement": rep}
    return Balance(by_slot=out)


def _tradeable(team: TeamState, profile: LeagueProfile, weeks, k: int) -> list:
    """This roster's most tradeable assets: high value, low cost to lose.

    Cost of losing a player is exactly what the lineup solver says it is — the
    points his removal takes off the optimal lineup — so a third receiver on a
    two-receiver roster scores as nearly free even though he projects well.
    """
    base = horizon_points(team.roster, profile, weeks)
    n = max(1, len(weeks))
    scored = []
    for p in team.roster:
        rest = [q for q in team.roster if q is not p]
        cost = base - horizon_points(rest, profile, weeks)
        scored.append((p.points_per_game * n - cost, cost, p))
    scored.sort(key=lambda t: -t[0])
    return [p for _, _, p in scored[:k]]


def _wanted(me: TeamState, partner: TeamState, profile: LeagueProfile, weeks,
            k: int) -> list:
    """Their players ranked by what they would add to MY starting lineup."""
    base = horizon_points(me.roster, profile, weeks)
    scored = []
    for p in partner.roster:
        gain = horizon_points(list(me.roster) + [p], profile, weeks) - base
        scored.append((gain, p))
    scored.sort(key=lambda t: -t[0])
    return [p for g, p in scored[:k] if g > EPS]


# ── P(accept) — that manager first, the market only as a fallback ────────────
def accept_odds(partner: TeamState, send, receive, their_delta: float,
                profile: LeagueProfile, state: SeasonState, weeks, reps: dict) -> tuple:
    """Odds this manager says yes, plus the reasons that moved the number.

    The one distinction that matters here is UNMEASURED versus MEASURED ZERO. A
    league whose trade history could not be parsed at all writes `rate_per_season:
    null` and then, mechanically, `accept_rate: 0.0` and `responds: false` — which
    are not findings about the manager, they are the absence of any finding. Taking
    them at face value declares every trade in that league impossible and silently
    empties the board. So the trade profile counts as real only when
    `rate_per_season` is present; otherwise the neutral prior is used and the
    rationale says the history is missing.
    """
    why = []
    tt = partner.trade_tendencies
    measured = tt.get("rate_per_season") is not None
    calibrated = measured and not partner.borrowed

    # 1. engagement — does he trade at all, and does he answer?
    base, src = NEUTRAL_ACCEPT, "no trade history for this league — neutral prior"
    if measured:
        try:
            base = clamp(float(tt["accept_rate"])) if tt.get("accept_rate") is not None \
                else clamp(saturating(float(tt["rate_per_season"]), 2.0))
            src = ("his calibrated accept rate" if tt.get("accept_rate") is not None
                   else "his calibrated trade rate")
            # a manager who has never been seen to trade is unlikely, not impossible
            base = max(base, MEASURED_FLOOR)
        except (TypeError, ValueError):
            pass
        if tt.get("responds") is False:
            base *= NO_RESPONSE
            why.append("history says this manager does not answer offers")
    why.append(f"P(accept) base from {src}"
               + (" (borrowed prior — not this manager's own history)"
                  if measured and partner.borrowed else ""))

    # 2. quality — how much it actually helps him, in replacement units
    ref = max((state.replacement_for(p, reps) for p in receive), default=0.0) * max(1, len(weeks))
    quality = saturating(their_delta / ref, HALF_TRADE) if ref > EPS else (
        1.0 if their_delta > 0 else 0.0)

    # 3. positional flow — the side of the board he has historically bought
    flow = tt.get("pos_flow") or {}
    mult = 1.0
    if flow:
        gets = [float(flow.get(p.pos, 0.0)) for p in receive]
        gives = [float(flow.get(p.pos, 0.0)) for p in send]
        tilt = (sum(gets) / len(gets) if gets else 0.0) - (sum(gives) / len(gives) if gives else 0.0)
        mult *= clamp(1.0 + 0.4 * tilt, 0.6, 1.4)
        if tilt > 0.05:
            why.append("he buys " + "/".join(sorted({p.pos for p in receive}))
                       + " and sells " + "/".join(sorted({p.pos for p in send})))
        elif tilt < -0.05:
            why.append("cuts against his usual positional flow")

    # 4. affinity — has he traded with me before?
    partners = tt.get("partners") or {}
    mine = state.my_team.manager
    if partners and mine:
        total = sum(float(v) for v in partners.values()) or 1.0
        share = float(partners.get(mine, 0)) / total
        if share > 0:
            mult *= clamp(1.0 + share, 1.0, 1.3)
            why.append(f"has traded with you before ({share:.0%} of his deals)")

    # 5. fallback anchor: only when we cannot model the manager himself
    if not calibrated:
        got = sum(float(((state.raw.get(p.id) or {}).get("mkt") or {}).get("trade_value") or 0)
                  for p in receive)
        gave = sum(float(((state.raw.get(p.id) or {}).get("mkt") or {}).get("trade_value") or 0)
                   for p in send)
        if got or gave:
            mult *= clamp(0.7 + 0.6 * saturating(got / max(gave, EPS), 1.0), 0.7, 1.3)
            why.append("no calibration for this manager — market trade value used "
                       "as the negotiation anchor")

    return clamp(base * quality * mult, 0.0, 0.95), why


def veto_risk(send, receive, profile: LeagueProfile, state: SeasonState) -> float:
    """How reversible this deal is by the rest of the league.

    Zero when the league has no veto mechanism at all. Otherwise it is how lopsided
    the deal LOOKS from outside — onlookers see projected points and names, not your
    lineup deltas — against how many of the disinterested owners have to agree.
    """
    votes = int(profile.veto_votes or 0)
    if votes <= 0:
        return 0.0
    voters = max(1, int(profile.size) - 2)
    hurdle = clamp(votes / voters)
    got = sum(float((state.raw.get(p.id) or {}).get("ros_points") or 0) for p in receive)
    gave = sum(float((state.raw.get(p.id) or {}).get("ros_points") or 0) for p in send)
    imbalance = abs(got - gave) / max(got, gave, EPS)
    if len(receive) < len(send) and got > gave:
        imbalance = clamp(imbalance + 0.1)      # consolidation reads worse than it is
    return round(clamp(imbalance * (1.0 - hurdle)), 3)


# ── the finder ───────────────────────────────────────────────────────────────
def _subsets(items, max_n):
    for n in range(1, max_n + 1):
        for c in combinations(items, n):
            yield list(c)


def _why_trade(send, receive, my_delta, their_delta, state, weeks, reps, odds_why,
               veto, profile) -> list:
    """Roster fit first, then what it takes to get it done."""
    why = []
    gets = ", ".join(f"{p.name} ({p.pos} {p.points_per_game:.1f})" for p in receive)
    gives = ", ".join(f"{p.name} ({p.pos} {p.points_per_game:.1f})" for p in send)
    why.append(f"+{my_delta:.1f} pts to your starters over {len(weeks)} wk: "
               f"in {gets}, out {gives}")
    bal = positional_balance(state.my_team.roster, profile, weeks, reps)
    short = sorted(bal.deficits().items(), key=lambda kv: -kv[1])[:2]
    if short:
        why.append("fills your thinnest slot: "
                   + ", ".join(f"{slot_name(s)} ({v:.0f} pts under replacement)"
                               for s, v in short))
    why.append(f"+{their_delta:.1f} to his starters too — it is not a fleece, "
               f"which is why it can get signed")
    why.extend(odds_why)
    if veto > 0:
        why.append(f"veto risk {veto:.0%} — this league can reverse a deal "
                   f"({profile.veto_votes} votes)")
    return why


def find_trades(state: SeasonState, *, tendencies=None, weeks=None, limit: int = 10,
                k_send: int = K_SEND, k_receive: int = K_RECEIVE,
                max_send: int = MAX_SEND, max_receive: int = MAX_RECEIVE,
                max_per_partner: int = 3, partner_ids=None, log=print) -> list:
    """Every mutually-positive deal we can find, ranked by my_delta x accept_odds.

    Enumeration is capped at the `k_send` most tradeable of my players against the
    `k_receive` of theirs that most help me. The cap and the size of the space it
    covers are LOGGED, never applied silently.
    """
    prof = state.profile
    weeks = weeks if weeks is not None else state.reg_weeks
    reps = state.replacement()
    me = state.my_team
    partners = [t for t in state.opponents
                if partner_ids is None or t.team_id in set(partner_ids)]

    mine = _tradeable(me, prof, weeks, k_send)
    send_sets = list(_subsets(mine, max_send))

    enumerated = 0
    full_space = 0
    found = []
    for partner in partners:
        theirs = _wanted(me, partner, prof, weeks, k_receive)
        n_mine, n_theirs = len(me.roster), len(partner.roster)
        full_space += (sum(len(list(combinations(range(n_mine), n))) for n in range(1, max_send + 1))
                       * sum(len(list(combinations(range(n_theirs), n)))
                             for n in range(1, max_receive + 1)))
        if not theirs:
            continue
        for recv in _subsets(theirs, max_receive):
            for send in send_sets:
                enumerated += 1
                my_delta = trade_delta(me.roster, send, recv, prof, weeks)
                if my_delta <= EPS:
                    continue
                their_delta = trade_delta(partner.roster, recv, send, prof, weeks)
                if their_delta <= EPS:
                    continue
                odds, odds_why = accept_odds(partner, send, recv, their_delta, prof,
                                             state, weeks, reps)
                veto = veto_risk(send, recv, prof, state)
                found.append({
                    "partner_team_id": partner.team_id,
                    "send": [p.id for p in send], "receive": [p.id for p in recv],
                    "my_delta": round(my_delta, 1), "their_delta": round(their_delta, 1),
                    "accept_odds": round(odds, 2), "veto_risk": veto,
                    "_rank": my_delta * odds,
                    "why": _why_trade(send, recv, my_delta, their_delta, state, weeks,
                                      reps, odds_why, veto, prof)})

    if log:
        log(f"  trades: enumerated {enumerated:,} packages across {len(partners)} partners "
            f"(k_send={k_send} x k_receive={k_receive}, up to {max_send}-for-{max_receive}); "
            f"the uncapped space over full rosters is {full_space:,} — "
            f"{enumerated / full_space:.1%} covered, chosen as the most tradeable "
            f"assets on each side")
        log(f"  trades: {len(found):,} were positive for BOTH sides; "
            f"returning the top {min(limit, len(found))} by my_delta x accept_odds")

    found.sort(key=lambda d: -d["_rank"])
    # One partner's roster often dominates the whole list; a board of five variants
    # of the same deal is one idea, not five.
    kept, per = [], {}
    for d in found:
        pid = d["partner_team_id"]
        if max_per_partner and per.get(pid, 0) >= max_per_partner:
            continue
        per[pid] = per.get(pid, 0) + 1
        kept.append(d)
    if log and len(kept) < len(found):
        log(f"  trades: capped at {max_per_partner} packages per partner "
            f"({len(found) - len(kept)} near-duplicate variants dropped)")
    for d in kept:
        d.pop("_rank", None)
    return kept[:limit] if limit else kept


# ── the evaluator ────────────────────────────────────────────────────────────
def evaluate_offer(state: SeasonState, partner_team_id: int, send, receive, *,
                   tendencies=None, weeks=None, max_counters: int = 200,
                   log=None) -> dict:
    """Verdict on a specific offer, both sides' lineup delta, and a counter.

    `send` / `receive` are player ids (or Players). The counter search perturbs the
    package one asset at a time and keeps the best variant that is still positive
    for him — a counter he will not sign is not a counter.
    """
    prof = state.profile
    weeks = weeks if weeks is not None else state.reg_weeks
    reps = state.replacement()
    me = state.my_team
    partner = state.team(partner_team_id)
    if partner is None:
        return {"verdict": "unknown", "why": [f"no team {partner_team_id} in this league"]}

    def resolve(items, roster):
        out = []
        for it in items:
            if isinstance(it, Player):
                out.append(it)
            else:
                match = next((p for p in roster if p.id == int(it)), None)
                if match is not None:
                    out.append(match)
        return out

    send = resolve(send, me.roster)
    receive = resolve(receive, partner.roster)
    if not send and not receive:
        return {"verdict": "unknown", "why": ["neither side of the offer resolved "
                                              "to players on these rosters"]}

    my_delta = trade_delta(me.roster, send, receive, prof, weeks)
    their_delta = trade_delta(partner.roster, receive, send, prof, weeks)
    post = (trade_delta(me.roster, send, receive, prof, state.post_weeks)
            if state.post_weeks else 0.0)
    odds, odds_why = accept_odds(partner, send, receive, their_delta, prof, state,
                                 weeks, reps)
    veto = veto_risk(send, receive, prof, state)

    # counter search: swap one asset on either side, keep him whole
    best, tried = None, 0
    my_others = [p for p in _tradeable(me, prof, weeks, k=len(me.roster))
                 if p not in send]
    their_others = [p for p in _wanted(me, partner, prof, weeks, k=len(partner.roster))
                    if p not in receive]
    variants = []
    for i in range(len(send)):
        for alt in my_others[:6]:
            variants.append((send[:i] + [alt] + send[i + 1:], list(receive)))
    if len(send) > 1:
        for i in range(len(send)):
            variants.append((send[:i] + send[i + 1:], list(receive)))
    for alt in their_others[:6]:
        variants.append((list(send), list(receive) + [alt]))
    for s2, r2 in variants[:max_counters]:
        tried += 1
        md = trade_delta(me.roster, s2, r2, prof, weeks)
        if md <= my_delta + EPS:
            continue
        td = trade_delta(partner.roster, r2, s2, prof, weeks)
        if td <= EPS:
            continue
        o2, _ = accept_odds(partner, s2, r2, td, prof, state, weeks, reps)
        score = md * o2
        if best is None or score > best["_rank"]:
            best = {"send": [p.id for p in s2], "receive": [p.id for p in r2],
                    "my_delta": round(md, 1), "their_delta": round(td, 1),
                    "accept_odds": round(o2, 2),
                    "veto_risk": veto_risk(s2, r2, prof, state), "_rank": score}
    if best:
        best.pop("_rank", None)
    if log:
        log(f"  counter search: {tried} of {len(variants)} variants evaluated "
            f"(capped at max_counters={max_counters}); single-asset perturbations only")

    if my_delta > EPS and their_delta > EPS:
        verdict = "accept"
    elif my_delta > EPS:
        verdict = "accept"          # good for me; he may not sign, hence accept_odds
    elif best:
        verdict = "counter"
    else:
        verdict = "decline"

    why = [f"your starters: {my_delta:+.1f} pts over {len(weeks)} wk "
           f"({post:+.1f} in the playoff weeks)",
           f"his starters: {their_delta:+.1f} pts"]
    if my_delta <= EPS:
        why.append("this lowers your starting lineup — the projected points you "
                   "receive do not reach it")
    why.extend(odds_why)
    if veto > 0:
        why.append(f"veto risk {veto:.0%} ({prof.veto_votes} votes needed here)")
    if best:
        why.append("a counter exists that is better for you and still positive for him")

    return {"verdict": verdict, "partner_team_id": partner_team_id,
            "send": [p.id for p in send], "receive": [p.id for p in receive],
            "my_delta": round(my_delta, 1), "my_delta_post": round(post, 1),
            "their_delta": round(their_delta, 1), "accept_odds": round(odds, 2),
            "veto_risk": veto, "counter": best, "why": why}


# ── buy low / sell high ──────────────────────────────────────────────────────
def _volume_percentiles(state: SeasonState) -> dict:
    """Snap/target share percentile WITHIN each position, across rostered players.

    Within-position because a 70% snap share means one thing for a back and
    another for a receiver, and the split is per league pool, not a constant.
    """
    by_pos = {}
    for p in state.rostered:
        by_pos.setdefault(p.pos, []).append(p)
    out = {}
    for pos, players in by_pos.items():
        vals, have = [], []
        for p in players:
            mkt = (state.raw.get(p.id) or {}).get("mkt") or {}
            v = mkt.get("snap_share")
            if v is None:
                v = mkt.get("target_share")
            have.append(v is not None)
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                vals.append(0.0)
        if not any(have):
            continue
        pcts = percentiles(vals)
        for i, p in enumerate(players):
            if have[i]:
                out[p.id] = pcts[i]
    return out


def buy_low_sell_high(state: SeasonState, *, weeks=None, limit: int = 5,
                      band: float = FORM_BAND, log=print) -> dict:
    """Recent form against the rest-of-season rate, filtered by usage.

    The filter is the whole point. Production falling while volume holds is an
    unlucky player — touchdowns regressed, the role did not — and he is a buy.
    Production falling because the snaps went to somebody else is a displaced
    player, and he is a sell no matter how good the name is. Without usage data
    the two look identical, so when snap/target share is missing the row still
    appears but says so.
    """
    weeks = weeks if weeks is not None else state.reg_weeks
    vol = _volume_percentiles(state)
    buy, sell = [], []
    no_volume, no_form = 0, 0

    for p in state.rostered:
        raw = state.raw.get(p.id) or {}
        ros = float(raw.get("ros_ppg") or 0.0)
        last3 = raw.get("last3_ppg")
        actual = raw.get("actual_ppg")
        if last3 is None:
            no_form += 1
            continue
        if ros <= EPS:
            continue
        last3 = float(last3)
        form = last3 / ros
        v = vol.get(p.id)
        if v is None:
            no_volume += 1
        mine = p.id in {q.id for q in state.my_team.roster}
        owner = p.owner

        if form < 1.0 - band and not mine and owner is not None:
            steady = v is None or v >= 0.5
            why = [f"{p.pos} {p.name}: {last3:.1f} ppg last 3 vs {ros:.1f} "
                   f"rest-of-season rate"]
            if v is None:
                why.append("no snap/target share — cannot separate unlucky from "
                           "displaced; treat as low confidence")
            elif steady:
                why.append(f"usage holding ({v:.0%} of his position) — the role is "
                           f"intact, the production regressed")
            else:
                why.append(f"usage collapsed ({v:.0%} of his position) — displaced, "
                           f"not unlucky; this is a sell for his owner, not a buy")
            if steady:
                why.append(f"worth asking his owner (team {owner}) before the "
                           f"rate catches back up")
                buy.append({"player_id": p.id, "owner_team_id": owner,
                            "_rank": (1.0 - form) * ros, "why": why})

        if form > 1.0 + band and mine:
            thin = v is not None and v < 0.5
            why = [f"{p.pos} {p.name}: {last3:.1f} ppg last 3 vs {ros:.1f} "
                   f"rest-of-season rate"]
            if v is None:
                why.append("no snap/target share — cannot confirm the run is "
                           "unsupported; low confidence")
            elif thin:
                why.append(f"usage does not support it ({v:.0%} of his position) — "
                           f"sell into the run")
            else:
                why.append(f"usage backs the run ({v:.0%} of his position) — hold "
                           f"unless somebody overpays")
            if actual is not None:
                why.append(f"season rate {float(actual):.1f} ppg")
            if v is None or thin:
                sell.append({"player_id": p.id, "_rank": (form - 1.0) * ros, "why": why})

    if log:
        log(f"  form: {len(buy)} buy-low and {len(sell)} sell-high candidates from "
            f"{len(state.rostered)} rostered ({band:.0%} divergence band); "
            f"{no_volume} had no usage data")
        if no_form:
            log(f"  form: {no_form} players have no recent-form history yet "
                f"(week {state.week} — there is nothing to diverge from this early)")
    for lst in (buy, sell):
        lst.sort(key=lambda d: -d["_rank"])
        for d in lst:
            d.pop("_rank", None)
    return {"buy_low": buy[:limit], "sell_high": sell[:limit]}
