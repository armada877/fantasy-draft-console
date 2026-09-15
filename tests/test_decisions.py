#!/usr/bin/env python3
"""Unit tests for the waiver and trade decision logic (WS-4).

    python3 tests/test_decisions.py          # plain runner, no pytest needed
    python3 -m pytest tests/test_decisions.py

Every league fact in here is built into a synthetic profile and then varied, so a
test that only passes for one league shape fails. Where a claim is about the real
leagues, the checked-in fixtures are used — all three of them, never one.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from engine.lineup import Player, marginal_add, trade_delta          # noqa: E402
from engine.profile import Acquisition, FAAB, LeagueProfile, PRIORITY  # noqa: E402
from engine.state import SeasonState, TeamState                      # noqa: E402
from engine.trades import (accept_odds, buy_low_sell_high, evaluate_offer,  # noqa: E402
                           find_trades, positional_balance, veto_risk)
from engine.waiver_runs import (DAYS, MIN_DAY_N, build_run_calendar,  # noqa: E402
                                summary as run_summary)
from engine.waivers import (budget_shares, claim_or_wait, contest_detail,  # noqa: E402
                            p_contested, plan_bid, plan_claim, suggest_bid,
                            waiver_board)

FIXTURES = os.path.join(ROOT, "tests", "fixtures")
ELIG = {"QB": frozenset({0, 7, 20, 21}), "RB": frozenset({2, 3, 23, 7, 20, 21}),
        "WR": frozenset({4, 3, 5, 23, 7, 20, 21}), "TE": frozenset({6, 5, 23, 7, 20, 21}),
        "K": frozenset({17, 20, 21}), "DST": frozenset({16, 20, 21})}

FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)
    return ok


# ── synthetic league construction ────────────────────────────────────────────
def profile(model=FAAB, slots=None, size=12, bench=5, budget=100, min_bid=0,
            veto=0, regular_weeks=14, order_resets=True):
    slots = slots or {0: 1, 2: 2, 4: 2, 6: 1, 16: 1, 23: 2}
    return LeagueProfile(
        league_id=1, season=2026, name="synthetic", size=size, scoring={53: 0.5},
        lineup_slots={**slots, 20: bench, 21: 1}, bench=bench, ir=1,
        acquisition=Acquisition(model, budget if model == FAAB else None, min_bid,
                                True, 24, order_resets, -1),
        draft_type="SNAKE", auction_budget=None, keeper_count=0,
        regular_weeks=regular_weeks, playoff_teams=6, trade_deadline_ms=None,
        veto_votes=veto)


def roster(prefix, ppg_by_pos, start_id=1):
    out, pid = [], start_id
    for pos, ppgs in ppg_by_pos.items():
        for v in ppgs:
            out.append(Player(pid, f"{prefix}{pos}{pid}", pos, ELIG[pos], float(v)))
            pid += 1
    return out


def state_of(prof, rosters, me_id=1, week=1, raw_extra=None):
    """A SeasonState from explicit rosters: {team_id: [Player]}."""
    players, teams = {}, []
    for tid, rs in rosters.items():
        for p in rs:
            p.owner = tid
            players[p.id] = p
        teams.append(TeamState(team_id=tid, name=f"Team {tid}", manager=f"M{tid}",
                               roster=list(rs),
                               faab_left=prof.acquisition.budget if prof.acquisition.is_faab else None,
                               waiver_priority=None if prof.acquisition.is_faab else tid,
                               tendencies={}, borrowed=True))
    raw = {p.id: {"id": p.id, "name": p.name, "pos": p.pos, "ros_ppg": p.points_per_game,
                  "ros_points": p.points_per_game * 14, "mkt": {}}
           for p in players.values()}
    for pid, extra in (raw_extra or {}).items():
        raw.setdefault(pid, {}).update(extra)
    return SeasonState(profile=prof, week=week, teams=teams, players=players, raw=raw,
                       me_id=me_id)


def add_free_agents(st, players, raw_extra=None):
    for p in players:
        p.owner = None
        st.players[p.id] = p
        st.raw[p.id] = {"id": p.id, "name": p.name, "pos": p.pos,
                        "ros_ppg": p.points_per_game,
                        "ros_points": p.points_per_game * 14, "mkt": {}}
        if raw_extra and p.id in raw_extra:
            st.raw[p.id].update(raw_extra[p.id])
    return st


# ── 1. a player who cannot crack the lineup is worth ~0 ──────────────────────
def test_uncrackable_player_is_worth_zero():
    print("\n[1] a player who cannot crack your lineup is worth ~0 marginal")
    prof = profile()
    mine = roster("me", {"QB": [22], "RB": [20, 19, 18], "WR": [21, 20, 19],
                         "TE": [15], "DST": [9]})
    st = state_of(prof, {1: mine, 2: roster("o", {"QB": [5], "RB": [4]}, 200)})

    scrub = Player(900, "Scrub", "RB", ELIG["RB"], 1.0)
    gain, _ = marginal_add(mine, scrub, prof, st.reg_weeks, must_drop=False)
    check("a 1.0-ppg back behind three better ones adds nothing",
          abs(gain) < 1e-6, f"{gain:.4f} pts over {len(st.reg_weeks)} wk")

    # ...and the board agrees: he ranks last and prices at the league minimum
    add_free_agents(st, [scrub, Player(901, "Useful", "TE", ELIG["TE"], 16.0)])
    board = waiver_board(st, log=None, max_candidates=10, max_priced=10)
    row = next(r for r in board if r["player_id"] == 900)
    check("the board gives him marginal_reg 0.0", row["marginal_reg"] == 0.0,
          str(row["marginal_reg"]))
    check("...and prices him at the league minimum bid",
          row["bid"]["suggested"] == prof.acquisition.min_bid,
          f"${row['bid']['suggested']}")
    check("...and his rationale says so, not just the number",
          any("does not crack" in w for w in row["why"]), row["why"][0][:60])

    # the SAME player is worth real points to a roster with a hole at his slot
    thin = roster("thin", {"QB": [22], "WR": [21, 20], "TE": [15], "DST": [9]}, 500)
    gain2, _ = marginal_add(thin, scrub, prof, st.reg_weeks, must_drop=False)
    check("the same scrub IS worth something to a roster with the hole",
          gain2 > gain, f"{gain2:.1f} vs {gain:.1f} — marginal is roster-specific")


# ── 2. a $0 bid when nothing is contested ────────────────────────────────────
def test_zero_bid_when_uncontested():
    print("\n[2] a minimum bid is recommended when p_contested is ~0")
    prof = profile(min_bid=0)
    # every rival is stacked, so nobody gains anything from our target
    # full rosters: every starting seat already filled by somebody better, so a
    # 14-ppg back genuinely cannot improve their lineup
    stacked = lambda n: roster(f"s{n}", {"QB": [25, 24], "RB": [24, 23, 22, 21],
                                         "WR": [25, 24, 23, 22], "TE": [20, 19],
                                         "DST": [12, 11], "K": [9]}, 100 * n)
    mine = roster("me", {"QB": [10], "RB": [9, 8], "WR": [9, 8], "TE": [5], "DST": [4]})
    st = state_of(prof, {1: mine, 2: stacked(2), 3: stacked(3), 4: stacked(4)})
    target = Player(900, "Target", "RB", ELIG["RB"], 14.0)
    add_free_agents(st, [target])

    c = contest_detail(target, st.opponents, prof, state=st, exclude_team_id=1)
    check("no rival gains a starting point from him",
          all(r["gain"] <= 1e-9 for r in c.by_team), str(c.components))
    check("p_contested is near zero", c.p < 0.05, f"{c.p:.3f}")

    marg, _ = marginal_add(mine, target, prof, st.reg_weeks)
    plan = plan_bid(target, st.my_team, st.opponents, prof, state=st, marginal=marg,
                    alternatives=[], contest=c)
    check("he is genuinely worth something to us", marg > 0, f"+{marg:.1f} pts")
    check("suggested bid is the league minimum ($0 here)",
          plan.suggested == 0, f"${plan.suggested}")
    check("p80 is also the minimum — nothing to outbid",
          plan.p80 == 0, f"${plan.p80}")

    # the same board in a league whose minimum bid is $1 must say $1, not $0
    prof1 = profile(min_bid=1)
    st1 = state_of(prof1, {1: mine, 2: stacked(2), 3: stacked(3), 4: stacked(4)})
    add_free_agents(st1, [Player(900, "Target", "RB", ELIG["RB"], 14.0)])
    bid = suggest_bid(st1.players[900], st1.my_team, st1.opponents, prof1, state=st1,
                      marginal=marg, alternatives=[])
    check("a $1-minimum league recommends $1, not $0", bid["suggested"] == 1,
          f"${bid['suggested']}")


def test_contested_player_costs_money():
    print("\n[2b] a contested, uniquely valuable player does cost money")
    prof = profile(min_bid=0)
    # every rival has a hole at RB, so they all want him, and there is no substitute
    thin = lambda n: roster(f"t{n}", {"QB": [22], "WR": [21, 20], "TE": [15],
                                      "DST": [9]}, 100 * n)
    mine = roster("me", {"QB": [22], "WR": [21, 20], "TE": [15], "DST": [9]})
    st = state_of(prof, {1: mine, 2: thin(2), 3: thin(3), 4: thin(4), 5: thin(5)})
    star = Player(900, "Star", "RB", ELIG["RB"], 20.0)
    add_free_agents(st, [star])
    board = waiver_board(st, log=None, max_candidates=5, max_priced=5)
    row = board[0]
    check("the only real target on the board is contested",
          row["bid"]["p_contested"] > 0.3, f"{row['bid']['p_contested']:.2f}")
    check("...and with no substitute, he is worth real money",
          row["bid"]["suggested"] > 0, f"${row['bid']['suggested']}")
    check("p80 is at least the suggested bid", row["bid"]["p80"] >= row["bid"]["suggested"],
          f"${row['bid']['p80']} vs ${row['bid']['suggested']}")


def test_budget_shares_reward_scarcity():
    print("\n[2c] the budget rule prices scarcity, not raw value")
    lone = budget_shares([30.0, 1.0, 1.0], [0.0, 0.0, 0.0])
    flat = budget_shares([30.0, 29.0, 28.0], [0.0, 0.0, 0.0])
    check("a unique target takes most of the budget", lone[0] > 0.8, f"{lone[0]:.2f}")
    check("an interchangeable one takes little", flat[0] < 0.5, f"{flat[0]:.2f}")
    check("shares never over-allocate the budget", sum(lone) <= 1.0 + 1e-9,
          f"{sum(lone):.3f}")
    check("a flat board leaves most of the budget unspent for a better week",
          sum(flat) < 0.2, f"{sum(flat):.3f}")


# ── 3. a trade that lowers your starters is never recommended ────────────────
def test_bad_trade_not_recommended():
    print("\n[3] a trade that lowers your starting lineup is never recommended")
    prof = profile(veto=0)
    mine = roster("me", {"QB": [22], "RB": [20, 18], "WR": [21, 19], "TE": [15],
                         "DST": [9]})
    theirs = roster("them", {"QB": [8], "RB": [6, 5], "WR": [7, 6], "TE": [4],
                             "DST": [3]}, 100)
    st = state_of(prof, {1: mine, 2: theirs})

    best_mine = max(mine, key=lambda p: p.points_per_game)
    worst_theirs = min(theirs, key=lambda p: p.points_per_game)
    delta = trade_delta(mine, [best_mine], [worst_theirs], prof, st.reg_weeks)
    check("giving up your best for their worst is negative for you", delta < 0,
          f"{delta:.1f} pts")

    found = find_trades(st, log=None, limit=50)
    check("the finder proposes nothing with a negative my_delta",
          all(t["my_delta"] > 0 for t in found), f"{len(found)} proposals")
    check("...and nothing with a negative their_delta either",
          all(t["their_delta"] > 0 for t in found))
    check("against a strictly worse roster there is nothing to find",
          len(found) == 0, f"{len(found)} proposals")

    ev = evaluate_offer(st, 2, [best_mine.id], [worst_theirs.id], log=None)
    check("the evaluator declines or counters, never accepts",
          ev["verdict"] != "accept", ev["verdict"])
    check("...and says why in plain terms",
          any("lowers your starting lineup" in w for w in ev["why"]))


def test_evaluator_accepts_and_counters():
    print("\n[3b] the evaluator accepts a good offer and can counter a thin one")
    prof = profile(veto=0)
    mine = roster("me", {"QB": [22], "RB": [20, 4], "WR": [21, 19], "TE": [15],
                         "DST": [9]})
    theirs = roster("them", {"QB": [21], "RB": [19, 18], "WR": [20, 5], "TE": [14],
                             "DST": [8]}, 100)
    st = state_of(prof, {1: mine, 2: theirs})
    my_spare = next(p for p in mine if p.pos == "WR" and p.points_per_game == 19)
    their_rb = next(p for p in theirs if p.points_per_game == 18)
    ev = evaluate_offer(st, 2, [my_spare.id], [their_rb.id], log=None)
    check("a mutually useful swap is judged on both lineups",
          "my_delta" in ev and "their_delta" in ev,
          f"you {ev['my_delta']:+.1f} / them {ev['their_delta']:+.1f}")
    check("a verdict is always produced",
          ev["verdict"] in ("accept", "decline", "counter"), ev["verdict"])
    check("the playoff horizon is reported separately from the regular season",
          "my_delta_post" in ev, f"post {ev.get('my_delta_post')}")


# ── 4. veto risk comes off the league, not a constant ────────────────────────
def test_veto_risk_is_league_driven():
    print("\n[4] veto risk comes off profile.veto_votes, never a constant")
    mine = roster("me", {"RB": [20]})
    theirs = roster("them", {"RB": [5], "WR": [4]}, 100)
    raw = {p.id: {"id": p.id, "name": p.name, "pos": p.pos,
                  "ros_points": p.points_per_game * 14} for p in mine + theirs}

    risks = {}
    for votes in (0, 3, 5):
        prof = profile(veto=votes)
        st = state_of(prof, {1: mine, 2: theirs})
        st.raw.update(raw)
        risks[votes] = veto_risk([mine[0]], theirs, prof, st)
    check("a league with no veto mechanism carries no veto risk",
          risks[0] == 0.0, str(risks[0]))
    check("a league that CAN veto carries some", risks[3] > 0 and risks[5] > 0,
          str(risks))

    # a balanced deal is not veto bait even where vetoes exist
    prof = profile(veto=5)
    even_a = roster("a", {"RB": [15]}, 300)
    even_b = roster("b", {"WR": [15]}, 400)
    st = state_of(prof, {1: even_a, 2: even_b})
    st.raw.update({p.id: {"id": p.id, "name": p.name, "pos": p.pos,
                          "ros_points": 15 * 14} for p in even_a + even_b})
    check("an even deal draws near-zero veto risk even at 5 votes",
          veto_risk(even_a, even_b, prof, st) < 0.05,
          str(veto_risk(even_a, even_b, prof, st)))


# ── 5. priority leagues: claim vs wait, and the cost of the position ─────────
def test_priority_claim_or_wait():
    print("\n[5] priority leagues spend position, not money")
    prof = profile(model=PRIORITY, budget=None, min_bid=1, size=14)
    thin = lambda n: roster(f"t{n}", {"QB": [22], "WR": [21, 20], "TE": [15],
                                      "DST": [9]}, 100 * n)
    mine = roster("me", {"QB": [22], "WR": [21, 20], "TE": [15], "DST": [9]})
    st = state_of(prof, {1: mine, 2: thin(2), 3: thin(3), 4: thin(4)})
    star = Player(900, "Star", "RB", ELIG["RB"], 20.0)
    add_free_agents(st, [star])

    board = waiver_board(st, log=None, max_candidates=5, max_priced=5)
    row = board[0]
    check("a priority league emits `claim`, never `bid`",
          "claim" in row and "bid" not in row, str(list(row)))
    check("a heavily contested must-have says CLAIM", row["claim"]["recommend"] is True,
          str(row["claim"]))
    check("...because he is unlikely to clear", row["claim"]["p_survives"] < 0.5,
          f"{row['claim']['p_survives']:.2f}")

    plan = plan_claim(star, st.my_team, st.opponents, prof, state=st, marginal=100.0,
                      next_best=0.0)
    plan_rival = plan_claim(star, st.my_team, st.opponents, prof, state=st,
                            marginal=100.0, next_best=1000.0)
    check("a far better future target makes you save your priority",
          plan.recommend and not plan_rival.recommend,
          f"alone={plan.recommend} with a better target later={plan_rival.recommend}")

    # when position does NOT reset it is not consumed, so nothing is forgone
    prof2 = profile(model=PRIORITY, budget=None, min_bid=1, order_resets=False)
    st2 = state_of(prof2, {1: mine, 2: thin(2), 3: thin(3), 4: thin(4)})
    add_free_agents(st2, [Player(900, "Star", "RB", ELIG["RB"], 20.0)])
    p2 = plan_claim(st2.players[900], st2.my_team, st2.opponents, prof2, state=st2,
                    marginal=100.0, next_best=1000.0)
    check("a league where the order does not reset never forgoes anything",
          p2.option_cost == 0.0, str(p2.option_cost))


# ── 6. pluggability is by acquisition model, never by league name ────────────
def test_pluggable_by_model_not_name():
    print("\n[6] the model is chosen by profile.acquisition, never by league name")
    mine = roster("me", {"QB": [10], "RB": [9], "WR": [9, 8], "TE": [5], "DST": [4]})
    other = roster("o", {"QB": [10], "RB": [9], "WR": [9, 8], "TE": [5], "DST": [4]}, 100)
    target = [Player(900, "Target", "RB", ELIG["RB"], 15.0)]
    shapes = {}
    for label, prof in (("faab", profile(model=FAAB)),
                        ("priority", profile(model=PRIORITY, budget=None, min_bid=1))):
        st = state_of(prof, {1: list(mine), 2: list(other)})
        add_free_agents(st, [Player(900, "Target", "RB", ELIG["RB"], 15.0)])
        shapes[label] = set(waiver_board(st, log=None, max_candidates=3,
                                         max_priced=3)[0])
    check("FAAB emits bid and not claim",
          "bid" in shapes["faab"] and "claim" not in shapes["faab"])
    check("PRIORITY emits claim and not bid",
          "claim" in shapes["priority"] and "bid" not in shapes["priority"])
    check("both emit the same horizon fields",
          {"marginal_reg", "marginal_post"} <= shapes["faab"] & shapes["priority"])


def test_two_qb_league_values_quarterbacks():
    print("\n[6b] a 2QB league values a second quarterback; a 1QB league does not")
    slots_1qb = {0: 1, 2: 2, 4: 2, 6: 1, 16: 1, 23: 2}
    slots_2qb = {0: 2, 2: 2, 4: 3, 6: 1, 16: 1, 17: 1, 23: 1}
    base = {"QB": [20], "RB": [18, 17], "WR": [19, 18, 17], "TE": [14], "K": [9],
            "DST": [8]}
    gains = {}
    for label, slots, size in (("1QB", slots_1qb, 12), ("2QB", slots_2qb, 8)):
        prof = profile(slots=slots, size=size)
        mine = roster("me", base)
        st = state_of(prof, {1: mine, 2: roster("o", base, 100)})
        qb2 = Player(900, "QB2", "QB", ELIG["QB"], 18.0)
        gains[label], _ = marginal_add(mine, qb2, prof, st.reg_weeks, must_drop=False)
    check("an 18-ppg QB is worth ~nothing behind a 20-ppg starter in a 1QB league",
          gains["1QB"] < 1.0, f"{gains['1QB']:.1f}")
    check("...and worth a lot in a league that starts two",
          gains["2QB"] > 10 * gains["1QB"] + 10,
          f"{gains['2QB']:.1f} vs {gains['1QB']:.1f}")


# ── 7. surplus/deficit is measured against each team's OWN requirement ───────
def test_balance_uses_own_requirement():
    print("\n[7] positional surplus is measured against each team's own lineup")
    base = {"QB": [20, 18], "RB": [18, 17], "WR": [19, 18], "TE": [14], "DST": [8]}
    reps = {0: 12.0, 2: 10.0, 4: 10.0, 6: 8.0, 16: 6.0, 17: 6.0, 23: 9.0}
    weeks = list(range(1, 15))
    p1 = profile(slots={0: 1, 2: 2, 4: 2, 6: 1, 16: 1, 23: 2})
    p2 = profile(slots={0: 2, 2: 2, 4: 3, 6: 1, 16: 1, 17: 1, 23: 1}, size=8)
    r = roster("t", base)
    b1 = positional_balance(r, p1, weeks, reps)
    b2 = positional_balance(r, p2, weeks, reps)
    check("two QBs are a surplus in a 1QB league", b1.by_slot[0]["surplus"] > 0,
          f"{b1.by_slot[0]['surplus']:.0f}")
    check("...and not a surplus in a 2QB league", b2.by_slot[0]["surplus"] == 0,
          f"{b2.by_slot[0]['surplus']:.0f}")
    check("the 2QB league sees a kicker hole the 1QB league does not have",
          17 in b2.by_slot and b2.by_slot[17]["deficit"] > 0 and 17 not in b1.by_slot)


# ── 8. buy-low / sell-high separates unlucky from displaced ──────────────────
def test_form_separates_unlucky_from_displaced():
    print("\n[8] usage separates an unlucky player from a displaced one")
    prof = profile()
    mine = roster("me", {"QB": [20], "RB": [18, 17], "WR": [19, 18], "TE": [14],
                         "DST": [8]})
    theirs = roster("them", {"QB": [20], "RB": [18, 17], "WR": [19, 18], "TE": [14],
                             "DST": [8]}, 100)
    st = state_of(prof, {1: mine, 2: theirs})
    unlucky, displaced = theirs[2], theirs[3]          # two of their backs
    st.raw[unlucky.id].update({"ros_ppg": 18.0, "last3_ppg": 8.0, "actual_ppg": 9.0,
                               "mkt": {"snap_share": 0.85}})
    st.raw[displaced.id].update({"ros_ppg": 17.0, "last3_ppg": 7.0, "actual_ppg": 8.0,
                                 "mkt": {"snap_share": 0.10}})
    for p in mine + theirs:
        st.raw[p.id].setdefault("last3_ppg", st.raw[p.id]["ros_ppg"])
        st.raw[p.id].setdefault("actual_ppg", st.raw[p.id]["ros_ppg"])
        st.raw[p.id]["mkt"].setdefault("snap_share", 0.5)
    st.raw[unlucky.id]["mkt"]["snap_share"] = 0.85
    st.raw[displaced.id]["mkt"]["snap_share"] = 0.10

    out = buy_low_sell_high(st, log=None, limit=10)
    buys = {b["player_id"] for b in out["buy_low"]}
    check("the player whose volume held up is a buy-low", unlucky.id in buys,
          f"{len(buys)} buy candidates")
    check("the player whose snaps collapsed is NOT a buy-low",
          displaced.id not in buys)
    reason = next(b for b in out["buy_low"] if b["player_id"] == unlucky.id)["why"]
    check("...and the rationale names the usage evidence",
          any("usage holding" in w for w in reason), reason[1][:60])


# ── 9. isolation: one league's data never reaches another ────────────────────
def test_leagues_are_isolated():
    print("\n[9] each fixture league produces its own shape, with no cross-talk")
    seen = {}
    for key in ("2kdome", "chi-phi-american", "inlaws-outlaws"):
        path = os.path.join(FIXTURES, f"season_data.{key}.json")
        if not os.path.exists(path):
            check(f"{key} fixture present", False, "missing")
            continue
        with open(path) as f:
            st = SeasonState.from_season_data(json.load(f))
        board = waiver_board(st, log=None, max_candidates=12, max_priced=6)
        model = st.profile.acquisition.model
        key_name = "bid" if model == FAAB else "claim"
        check(f"{key}: every priced row carries `{key_name}`",
              all(key_name in r for r in board[:6]),
              f"{model}, {st.profile.size} teams")
        seen[key] = (st.profile.size, st.profile.starters_per_team, model,
                     st.profile.veto_votes)
    check("the three leagues really do differ on shape",
          len(set(seen.values())) == len(seen), str(seen))



# ── 10. waiver runs: the temporal signal, measured not assumed ───────────────
def _tx(day_hour_ms, player, team, bid=0, status="EXECUTED"):
    return {"type": "WAIVER", "processDate": day_hour_ms, "status": status,
            "bidAmount": bid, "seasonId": 2025, "teamId": -2147483648,
            "items": [{"type": "ADD", "toTeamId": team, "playerId": player}]}


def _at(weekday, hour=16, week=0):
    """Epoch ms for a given weekday. 1970-01-05 was a Monday."""
    import datetime as _dt
    base = _dt.datetime(1970, 1, 5, tzinfo=_dt.timezone.utc)
    d = base + _dt.timedelta(days=weekday + 7 * week, hours=hour)
    return int(d.timestamp() * 1000)


def test_run_calendar_measures_the_busy_day():
    print("\n[10] the contested day is measured per league, never assumed")
    acq = {"waiverProcessDays": ["WEDNESDAY", "THURSDAY"], "waiverProcessHour": 12,
           "isUsingAcquisitionBudget": True}
    tx = []
    pid = 0
    # Wednesday (weekday 2): 30 runs, 20 of them contested, winners pay real money
    for i in range(30):
        pid += 1
        tx.append(_tx(_at(2, week=i), pid, 1, bid=20, status="EXECUTED"))
        if i < 20:
            tx.append(_tx(_at(2, week=i) + 137, pid, 2, bid=15,
                          status="FAILED_INVALIDPLAYERSOURCE"))
    # Thursday (weekday 3): 30 runs, 2 contested, winners pay nothing
    for i in range(30):
        pid += 1
        tx.append(_tx(_at(3, week=i), pid, 3, bid=0, status="EXECUTED"))
        if i < 2:
            tx.append(_tx(_at(3, week=i) + 137, pid, 4, bid=0,
                          status="FAILED_INVALIDPLAYERSOURCE"))
    cal = build_run_calendar(tx, acq)
    check("both days' runs are counted", cal.total_runs == 60, str(cal.total_runs))
    check("the losing bid is grouped into the SAME run as the winner",
          cal.by_day["WEDNESDAY"].contested == 20,
          f"{cal.by_day['WEDNESDAY'].contested} contested of "
          f"{cal.by_day['WEDNESDAY'].runs}")
    check("the hot day is found from the data", cal.hottest == "WEDNESDAY", cal.hottest)
    wed = cal.by_day["WEDNESDAY"].shrunk_rate(cal.overall_rate)
    thu = cal.by_day["THURSDAY"].shrunk_rate(cal.overall_rate)
    check("the busy day is materially more contested", wed > 2 * thu,
          f"{wed:.0%} vs {thu:.0%}")
    check("...and materially more expensive",
          cal.by_day["WEDNESDAY"].mean_win > cal.by_day["THURSDAY"].mean_win,
          f"${cal.by_day['WEDNESDAY'].mean_win:.2f} vs "
          f"${cal.by_day['THURSDAY'].mean_win:.2f}")
    check("the quieter alternative is offered", cal.quieter_alternative("WEDNESDAY") == "THURSDAY",
          str(cal.quieter_alternative("WEDNESDAY")))

    # the SAME code with the busy day moved must follow it — nothing is hardcoded
    moved = []
    for t in tx:
        import datetime as _dt
        d = _dt.datetime.fromtimestamp(t["processDate"] / 1000, _dt.timezone.utc)
        shift = 3 if d.weekday() == 2 else -1          # Wed->Sat, Thu->Wed
        t2 = dict(t)
        t2["processDate"] = t["processDate"] + shift * 86400000
        moved.append(t2)
    cal2 = build_run_calendar(moved, {"waiverProcessDays": ["SATURDAY", "WEDNESDAY"],
                                      "waiverProcessHour": 12,
                                      "isUsingAcquisitionBudget": True})
    check("a league that runs its hot waivers on Saturday reports Saturday",
          cal2.hottest == "SATURDAY", cal2.hottest)


def test_thin_days_are_shrunk_and_flagged():
    print("\n[10b] a day with two runs does not get a confident rate")
    acq = {"waiverProcessDays": ["WEDNESDAY", "FRIDAY"], "waiverProcessHour": 12,
           "isUsingAcquisitionBudget": True}
    tx, pid = [], 0
    for i in range(40):
        pid += 1
        tx.append(_tx(_at(2, week=i), pid, 1, bid=5))
        if i < 16:
            tx.append(_tx(_at(2, week=i) + 91, pid, 2, bid=4,
                          status="FAILED_INVALIDPLAYERSOURCE"))
    for i in range(2):                       # Friday: n=2, both uncontested
        pid += 1
        tx.append(_tx(_at(4, week=i), pid, 3, bid=0))
    cal = build_run_calendar(tx, acq)
    fri = cal.by_day["FRIDAY"]
    check("a 2-run day reports a raw 0%", fri.raw_rate == 0.0)
    check("...but its shipped rate is shrunk toward the league rate",
          fri.shrunk_rate(cal.overall_rate) > 0.1,
          f"{fri.shrunk_rate(cal.overall_rate):.0%} from n={fri.runs}")
    check("...and it is flagged thin", fri.to_dict(cal.overall_rate)["thin"] is True)
    check("a thin day is never offered as the quieter alternative",
          cal.quieter_alternative("WEDNESDAY") is None,
          str(cal.quieter_alternative("WEDNESDAY")))


def test_priority_league_reports_no_prices():
    print("\n[10c] a priority league has no bids, so no prices are quoted")
    acq = {"waiverProcessDays": ["WEDNESDAY"], "waiverProcessHour": 11,
           "isUsingAcquisitionBudget": False}
    tx = [_tx(_at(2, week=i), i + 1, 1, bid=0) for i in range(30)]
    cal = build_run_calendar(tx, acq)
    check("has_bids is off", cal.has_bids is False)
    check("price_for returns nothing rather than a misleading $0",
          cal.price_for("WEDNESDAY") == (None, None, None),
          str(cal.price_for("WEDNESDAY")))
    check("the summary omits the money column",
          "mean win" not in run_summary(cal))


def test_run_drives_contest_level_and_price():
    print("\n[10d] the run sets p_contested and anchors the bid")
    prof = profile(min_bid=0)
    mine = roster("me", {"QB": [10], "RB": [9], "WR": [9, 8], "TE": [5], "DST": [4]})
    other = roster("o", {"QB": [10], "RB": [9], "WR": [9, 8], "TE": [5], "DST": [4]}, 100)
    st = state_of(prof, {1: mine, 2: other})
    add_free_agents(st, [Player(900, "Target", "RB", ELIG["RB"], 15.0)])

    acq = {"waiverProcessDays": ["WEDNESDAY", "THURSDAY"], "waiverProcessHour": 12,
           "isUsingAcquisitionBudget": True}
    tx, pid = [], 0
    for i in range(40):
        pid += 1
        tx.append(_tx(_at(2, week=i), pid, 1, bid=25))
        if i < 24:
            tx.append(_tx(_at(2, week=i) + 77, pid, 2, bid=20,
                          status="FAILED_INVALIDPLAYERSOURCE"))
    for i in range(40):
        pid += 1
        tx.append(_tx(_at(3, week=i), pid, 3, bid=0))
    cal = build_run_calendar(tx, acq)

    import datetime as _dt
    # a Monday "now", so the next run is the hot Wednesday one
    monday = _dt.datetime(2026, 9, 14, 9, tzinfo=_dt.timezone.utc)
    hot = cal.to_dict(monday)
    check("next run resolves to the hot day", hot["next_run"]["day"] == "WEDNESDAY",
          str(hot["next_run"]["day"]))

    board_hot = waiver_board(st, runs=hot, log=None, max_candidates=3, max_priced=3)
    # ...and a Wednesday-evening "now", so the next run is the quiet Thursday one
    wed_pm = _dt.datetime(2026, 9, 16, 20, tzinfo=_dt.timezone.utc)
    quiet = cal.to_dict(wed_pm)
    check("next run rolls on to the quiet day", quiet["next_run"]["day"] == "THURSDAY",
          str(quiet["next_run"]["day"]))
    board_quiet = waiver_board(st, runs=quiet, log=None, max_candidates=3, max_priced=3)

    ph, pq = board_hot[0]["bid"], board_quiet[0]["bid"]
    check("p_contested comes from the run, not the player",
          ph["p_contested"] > 2 * pq["p_contested"],
          f"hot {ph['p_contested']:.0%} vs quiet {pq['p_contested']:.0%}")
    check("the same player costs more into the busy run",
          ph["suggested"] > pq["suggested"], f"${ph['suggested']} vs ${pq['suggested']}")
    check("the quiet run takes him for the minimum",
          pq["suggested"] == prof.acquisition.min_bid, f"${pq['suggested']}")
    why = " ".join(board_hot[0]["why"])
    check("the rationale states when the next run is", "next run:" in why)
    check("...and offers the wait", "wait for the quiet run" in why, why[-120:])


def main():
    print("WS-4 decision-engine tests")
    for fn in (test_uncrackable_player_is_worth_zero,
               test_zero_bid_when_uncontested,
               test_contested_player_costs_money,
               test_budget_shares_reward_scarcity,
               test_bad_trade_not_recommended,
               test_evaluator_accepts_and_counters,
               test_veto_risk_is_league_driven,
               test_priority_claim_or_wait,
               test_pluggable_by_model_not_name,
               test_two_qb_league_values_quarterbacks,
               test_balance_uses_own_requirement,
               test_form_separates_unlucky_from_displaced,
               test_leagues_are_isolated,
               test_run_calendar_measures_the_busy_day,
               test_thin_days_are_shrunk_and_flagged,
               test_priority_league_reports_no_prices,
               test_run_drives_contest_level_and_price):
        fn()
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURE(S): ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
