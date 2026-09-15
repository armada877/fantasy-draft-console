#!/usr/bin/env python3
"""Conformance harness — the engine's regression test.

Enforces docs/inseason_plan.md 0b: the engine must contain no league-specific facts,
and the lineup solver must be genuinely optimal (not a positional cascade that happens
to work for 1QB leagues).

    python3 -m engine.conformance            # solver + synthetic profiles + grep gate
    python3 -m engine.conformance --live     # also run every league on the ESPN account
"""
from __future__ import annotations

import itertools
import os
import random
import re
import sys

from .lineup import Player, lineup_points, marginal_add, optimal_lineup
from . import projection_policy as pp
from .profile import LeagueProfile, Acquisition, FAAB, PRIORITY, slot_name

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)
    return ok


# ── synthetic profiles: the shapes not present in the real account ───────────
def _profile(**kw):
    base = dict(
        league_id=0, season=2026, name="synthetic", size=12, scoring={53: 1.0},
        lineup_slots={}, bench=6, ir=1,
        acquisition=Acquisition(FAAB, 100, 0, True, 24, True, -1),
        draft_type="SNAKE", auction_budget=None, keeper_count=0,
        regular_weeks=14, playoff_teams=6, trade_deadline_ms=None, veto_votes=0,
    )
    base.update(kw)
    return LeagueProfile(**base)


SYNTHETIC = {
    # name: (lineup_slots, size)
    "1QB standard (no K)":   ({0: 1, 2: 2, 4: 2, 6: 1, 16: 1, 23: 2, 20: 6, 21: 2}, 12),
    "2QB + K":               ({0: 2, 2: 2, 4: 3, 6: 1, 16: 1, 17: 1, 23: 1, 20: 7, 21: 1}, 8),
    "superflex (OP)":        ({0: 1, 2: 2, 4: 2, 6: 1, 7: 1, 23: 1, 20: 6, 21: 1}, 10),
    "14-team standard":      ({0: 1, 2: 2, 4: 2, 6: 1, 16: 1, 17: 1, 23: 1, 20: 6, 21: 2}, 14),
    "no DST, 3 flex":        ({0: 1, 2: 1, 4: 1, 6: 1, 23: 3, 20: 6, 21: 1}, 12),
    "degenerate 2-team":     ({0: 1, 23: 1, 20: 2, 21: 0}, 2),
}

# eligible slots by position, mirroring what ESPN reports per player
ELIG = {
    "QB": frozenset({0, 7, 20, 21}),
    "RB": frozenset({2, 3, 23, 7, 20, 21}),
    "WR": frozenset({4, 3, 5, 23, 7, 20, 21}),
    "TE": frozenset({6, 5, 23, 7, 20, 21}),
    "K":  frozenset({17, 20, 21}),
    "DST": frozenset({16, 20, 21}),
}


def make_roster(rng, n=16):
    out = []
    for i in range(n):
        pos = rng.choice(["QB", "RB", "WR", "WR", "TE", "K", "DST", "RB"])
        out.append(Player(id=i, name=f"P{i}", pos=pos, eligible_slots=ELIG[pos],
                          points_per_game=round(rng.uniform(0, 25), 2)))
    return out


# ── 1. solver optimality vs brute force ──────────────────────────────────────
def brute_force_best(players, profile):
    """Exhaustive best lineup, memoised over (seat index, used-player bitmask).

    Seats may be left EMPTY — a roster with no kicker still fields everyone else.
    This is the independent oracle the Hungarian solver is checked against.
    """
    rows = []
    for slot, count in sorted(profile.starting_slots.items()):
        rows.extend([int(slot)] * int(count))
    pts = [p.points_per_game for p in players]
    elig = [p.eligible_slots for p in players]
    memo = {}

    def rec(i, used):
        if i == len(rows):
            return 0.0
        key = (i, used)
        if key in memo:
            return memo[key]
        best = rec(i + 1, used)                       # leave this seat empty
        slot = rows[i]
        for j in range(len(players)):
            if not (used >> j) & 1 and slot in elig[j]:
                best = max(best, pts[j] + rec(i + 1, used | (1 << j)))
        memo[key] = best
        return best

    return rec(0, 0)


def test_solver_optimal():
    print("\n[1] lineup solver is optimal (brute-force cross-check)")
    rng = random.Random(20260914)
    worst = 0.0
    for label, (slots, size) in SYNTHETIC.items():
        prof = _profile(lineup_slots=slots, size=size)
        bad = 0
        for _ in range(40):
            roster = make_roster(rng, n=10)         # memoised brute force keeps this cheap
            got = lineup_points(roster, prof)
            exp = brute_force_best(roster, prof)
            if abs(got - exp) > 1e-6:
                bad += 1
                worst = max(worst, exp - got)
        check(f"{label}", bad == 0, f"{bad}/40 mismatched" if bad else "40/40 optimal")
    if worst:
        print(f"      worst shortfall: {worst:.2f} pts")


# ── 2. genericity: profile drives behaviour, nothing hardcoded ───────────────
def test_generic_over_profiles():
    print("\n[2] engine adapts to the profile (no hardcoded league shape)")
    rng = random.Random(7)
    roster = make_roster(rng, n=18)

    p1 = _profile(lineup_slots=SYNTHETIC["1QB standard (no K)"][0], size=12)
    p2 = _profile(lineup_slots=SYNTHETIC["2QB + K"][0], size=8)
    _, l1, _ = optimal_lineup(roster, p1)
    _, l2, _ = optimal_lineup(roster, p2)
    check("1QB league starts exactly 1 QB", len(l1.get(0, [])) == 1, f"got {len(l1.get(0, []))}")
    check("2QB league starts exactly 2 QBs", len(l2.get(0, [])) == 2, f"got {len(l2.get(0, []))}")
    check("no-K league starts no kicker", 17 not in p1.starting_slots)
    check("K league fills the kicker slot", len(l2.get(17, [])) == 1)

    sflex = _profile(lineup_slots=SYNTHETIC["superflex (OP)"][0], size=10)
    _, ls, _ = optimal_lineup(roster, sflex)
    op = ls.get(7, [])
    check("superflex OP slot is filled", len(op) == 1, f"by {op[0].pos}" if op else "empty")

    tiny = _profile(lineup_slots=SYNTHETIC["degenerate 2-team"][0], size=2)
    check("degenerate league does not crash", lineup_points(roster, tiny) > 0)

    # every starting slot that can be filled, is
    for label, (slots, size) in SYNTHETIC.items():
        prof = _profile(lineup_slots=slots, size=size)
        _, lu, _ = optimal_lineup(roster, prof)
        unfilled = [slot_name(s) for s, n in prof.starting_slots.items() if len(lu.get(s, [])) < n
                    and any(s in pl.eligible_slots for pl in roster)]
        check(f"{label}: all fillable seats filled", not unfilled, ",".join(unfilled))


# ── 3. bye weeks and marginal value ──────────────────────────────────────────
def test_bye_and_marginal():
    print("\n[3] bye-aware marginal value")
    prof = _profile(lineup_slots={0: 1, 2: 2, 4: 2, 6: 1, 23: 1, 20: 5, 21: 1}, size=12)
    weeks = list(range(2, 15))
    # two RBs, one of whom is on bye in week 9
    roster = [
        Player(1, "QB1", "QB", ELIG["QB"], 18.0),
        Player(2, "RB1", "RB", ELIG["RB"], 15.0),
        Player(3, "RB2", "RB", ELIG["RB"], 12.0, bye_week=9),
        Player(4, "WR1", "WR", ELIG["WR"], 14.0),
        Player(5, "WR2", "WR", ELIG["WR"], 11.0),
        Player(6, "TE1", "TE", ELIG["TE"], 9.0),
        Player(7, "FLX", "WR", ELIG["WR"], 8.0),
    ]
    filler = Player(99, "ByeFiller", "RB", ELIG["RB"], 7.0, bye_week=5)
    dup = Player(98, "Dup", "RB", ELIG["RB"], 7.0, bye_week=9)
    g_fill, _ = marginal_add(roster, filler, prof, weeks, must_drop=False)
    g_dup, _ = marginal_add(roster, dup, prof, weeks, must_drop=False)
    check("a bye-week filler beats an identical player who shares the bye",
          g_fill > g_dup, f"{g_fill:.1f} vs {g_dup:.1f} pts")
    star = Player(97, "Star", "RB", ELIG["RB"], 22.0)
    g_star, _ = marginal_add(roster, star, prof, weeks, must_drop=False)
    check("a better player yields a bigger gain", g_star > g_fill,
          f"{g_star:.1f} vs {g_fill:.1f}")
    # NB: a scrub who is available in week 9 legitimately gains points, because RB2
    # is on bye then. To test "never starts", he must share the hole.
    scrub = Player(96, "Scrub", "RB", ELIG["RB"], 1.0, bye_week=9)
    g_scrub, _ = marginal_add(roster, scrub, prof, weeks, must_drop=False)
    check("a player who never starts adds 0", abs(g_scrub) < 1e-6, f"{g_scrub:.3f}")
    plugger = Player(95, "Plugger", "RB", ELIG["RB"], 1.0)
    g_plug, _ = marginal_add(roster, plugger, prof, weeks, must_drop=False)
    check("...but the same scrub who covers the bye gains exactly the hole",
          abs(g_plug - 1.0) < 1e-6, f"{g_plug:.3f}")


# ── 4. grep gate: no hardcoded league constants in the engine ────────────────
BANNED = [
    (r'\["QB",\s*"RB",\s*"WR",\s*"TE"\]', 'hardcoded position list'),
    (r'\brange\(1,\s*15\)', 'hardcoded 14-week season'),
    (r'(?<![\w.])12(?![\w.])\s*#?\s*teams?', 'hardcoded team count'),
]


def test_no_hardcoded_constants():
    print("\n[4] no hardcoded league constants in engine/")
    hits = []
    for fn in sorted(os.listdir(os.path.join(ROOT, "engine"))):
        if not fn.endswith(".py") or fn == "conformance.py":
            continue
        src = open(os.path.join(ROOT, "engine", fn)).read()
        # strip docstrings/comments so prose about a league is not a violation
        code = re.sub(r'""".*?"""', "", src, flags=re.S)
        code = re.sub(r"#.*", "", code)
        for pat, why in BANNED:
            for m in re.finditer(pat, code):
                hits.append(f"{fn}: {why} ({m.group(0)!r})")
    check("engine modules are league-agnostic", not hits, "; ".join(hits))


# ── 5. projection policy: deviations must be earned ──────────────────────────
def test_projection_policy():
    """The baseline (ESPN projection on league scoring) ships unless a deviation is
    validated out-of-sample ACROSS LEAGUES. Enforced, not advisory."""
    print("\n[5] projection policy refuses unearned deviations")
    saved = dict(pp.REGISTRY)
    try:
        pp.REGISTRY.clear()
        v, applied = pp.adjusted_points(100.0)
        check("empty registry returns the baseline untouched", v == 100.0 and not applied)

        pp.register(pp.Adjustment("hunch", lambda p, pl, pr: p * 1.2))
        v, applied = pp.adjusted_points(100.0)
        check("an UNVALIDATED adjustment is refused", v == 100.0 and not applied)

        pp.REGISTRY.clear()
        pp.register(pp.Adjustment("one_league", lambda p, pl, pr: p * 1.2, pp.Validation(
            "m", (2024,), (pp.LeagueResult("2kdome", 5.0, 4.0, 5000),))))
        v, _ = pp.adjusted_points(100.0)
        check("validated in only ONE league is refused", v == 100.0)

        pp.REGISTRY.clear()
        pp.register(pp.Adjustment("flips", lambda p, pl, pr: p * 1.2, pp.Validation(
            "m", (2024,), (pp.LeagueResult("2kdome", 5.0, 4.0, 5000),
                           pp.LeagueResult("chi-phi-american", 5.0, 5.4, 5000)))))
        v, _ = pp.adjusted_points(100.0)
        check("an effect that REVERSES across leagues is refused", v == 100.0)

        pp.REGISTRY.clear()
        pp.register(pp.Adjustment("harms_thin", lambda p, pl, pr: p * 1.2, pp.Validation(
            "m", (2024,), (pp.LeagueResult("2kdome", 5.0, 4.0, 5000),
                           pp.LeagueResult("chi-phi-american", 5.0, 4.6, 5000),
                           pp.LeagueResult("inlaws-outlaws", 5.0, 6.0, 100)))))
        v, _ = pp.adjusted_points(100.0)
        check("an adjustment that HARMS an underpowered league is refused", v == 100.0)

        # The half of the bar WS-8 had to add. Four real candidates beat the
        # baseline's MAE in all three leagues and then lost realized points, so
        # accuracy evidence alone must not be able to promote anything.
        pp.REGISTRY.clear()
        pp.register(pp.Adjustment("mae_only", lambda p, pl, pr: p * 1.2, pp.Validation(
            "m", (2024,), (pp.LeagueResult("2kdome", 5.0, 4.0, 5000),
                           pp.LeagueResult("chi-phi-american", 5.0, 4.6, 5000)))))
        v, _ = pp.adjusted_points(100.0)
        check("MAE evidence with NO lineup replay is refused", v == 100.0)

        MAE_OK = (pp.LeagueResult("2kdome", 5.0, 4.0, 5000),
                  pp.LeagueResult("chi-phi-american", 5.0, 4.6, 5000))
        pp.REGISTRY.clear()
        pp.register(pp.Adjustment("mae_up_points_down", lambda p, pl, pr: p * 1.2,
                                  pp.Validation("m", (2024,), MAE_OK, decision=(
                                      pp.DecisionResult("2kdome", 1500, +0.21, 3.0, 12.0),
                                      pp.DecisionResult("chi-phi-american", 500, -0.12, -2.3, 3.0)))))
        v, _ = pp.adjusted_points(100.0)
        check("better MAE but FEWER realized points is refused", v == 100.0)

        pp.REGISTRY.clear()
        pp.register(pp.Adjustment("points_noise", lambda p, pl, pr: p * 1.2,
                                  pp.Validation("m", (2024,), MAE_OK, decision=(
                                      pp.DecisionResult("2kdome", 1500, +0.02, 0.4, 4.0),
                                      pp.DecisionResult("chi-phi-american", 500, +0.01, 0.2, 3.0)))))
        v, _ = pp.adjusted_points(100.0)
        check("a points gain indistinguishable from noise is refused", v == 100.0)

        pp.REGISTRY.clear()
        pp.register(pp.Adjustment("earned", lambda p, pl, pr: p * 0.9,
                                  pp.Validation("m", (2024,), MAE_OK, decision=(
                                      pp.DecisionResult("2kdome", 1500, +0.9, 4.1, 18.0),
                                      pp.DecisionResult("chi-phi-american", 500, +0.7, 2.6, 16.0)))))
        v, applied = pp.adjusted_points(100.0)
        check("an adjustment that improves MAE AND adds points applies",
              abs(v - 90.0) < 1e-9 and applied == ["earned"])
    finally:
        pp.REGISTRY.clear()
        pp.REGISTRY.update(saved)


# ── 6. real leagues on this ESPN account ─────────────────────────────────────
def test_live():
    print("\n[6] every league on the account builds a profile")
    sys.path.insert(0, os.path.join(ROOT, "scraping"))
    try:
        from scrape_league import load_auth, discover_leagues
    except Exception as e:
        check("scraping helpers import", False, str(e))
        return
    import json, urllib.request
    swid, s2 = load_auth()
    leagues = discover_leagues(swid, s2)
    check("discovered at least one league", bool(leagues), f"{len(leagues)} found")
    H = {"accept": "application/json", "x-fantasy-platform": "kona",
         "x-fantasy-source": "kona", "cookie": f"SWID={swid}; espn_s2={s2}",
         "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
    for lg in leagues:
        url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/"
               f"{lg['season']}/segments/0/leagues/{lg['league_id']}?view=mSettings")
        try:
            d = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=40))
            prof = LeagueProfile.from_espn(d)
            ok = prof.size > 0 and prof.starters_per_team > 0 and prof.regular_weeks > 0
            check(f"{prof.name}", ok)
            for line in prof.summary().splitlines()[1:]:
                print(f"        {line.strip()}")
        except Exception as e:
            check(f"league {lg['league_id']}", False, str(e)[:80])


def main():
    print("engine conformance")
    test_solver_optimal()
    test_generic_over_profiles()
    test_bye_and_marginal()
    test_no_hardcoded_constants()
    test_projection_policy()
    if "--live" in sys.argv:
        test_live()
    print(f"\n{'ALL PASS' if not FAILURES else str(len(FAILURES)) + ' FAILURE(S): ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
