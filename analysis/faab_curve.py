#!/usr/bin/env python3
"""What an in-season acquisition actually costs -> faab_curve.json / priority_curve.json.

The data here is unusually good, because **ESPN records the losing bids**. In a
`type=WAIVER, executionType=PROCESS` row:

    EXECUTED                    won the claim
    FAILED_INVALIDPLAYERSOURCE  outbid — this IS the losing bid, and its amount
    FAILED_ROSTERLIMIT / FAILED_PLAYERALREADYDROPPED / FAILED_IRSLOT / ...
                                mechanical failure, excluded (never a contest)

So we can see the whole auction for each player, not just the clearing price. Two
outputs, chosen by the league's CURRENT acquisition regime, never by its name:

    FAAB      median winning bid, p80 winning bid, P(contested), by add quality and week
    PRIORITY  P(survives to free agency) by quality — there the currency is waiver
              position, so "what did it cost" is the wrong question; "will he still be
              there tomorrow" is the right one

"Quality" is measured from what the added player actually DID (season fantasy-point
percentile within his position, scored with this league's own settings), not from his
name or his ADP. See `league_history.quality` for the one thing that measure cannot do.

**Validation is the point, not a footnote.** CLAUDE.md records a 56% "validated" reading
that turned out to be an artifact of the wrong denominator, so every number here is
scored leave-one-season-out against a naive baseline (predict the pooled constant), with
the correct loss for each target: MAE for the median, pinball@0.8 for the p80 quantile,
Brier/log-loss/AUC for the probability. If the curve does not beat the baseline the JSON
says so in `validation.beats_baseline` / `validation.verdict` and nothing is promoted.

Run:  python3 analysis/faab_curve.py --league 2kdome
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import analysis.acquisition_regime as AR        # noqa: E402
import analysis.league_history as H             # noqa: E402

N_QUALITY_BINS = 5
MIN_CELL = 20                   # below this a cell falls back to the pooled prior
# A curve is PROMOTED — i.e. downstream may prefer it to the pooled constant — only when
# it beats the naive baseline out of sample by a margin big enough to survive a different
# seven seasons. 1% on 2,000 claims is a rounding error dressed as a model; the repo has
# already published one "validated" number that was an artifact, and once is enough.
PROMOTE_THRESHOLD_PCT = 5.0
OUT_NAME = {AR.FAAB: "faab_curve.json", AR.PRIORITY: "priority_curve.json"}


# ── small stats helpers ──────────────────────────────────────────────────────
def quantile(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0.0
    return float(xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))])


def pinball(y, f, tau):
    return tau * (y - f) if y >= f else (1 - tau) * (f - y)


def auc(pairs):
    pos = [p for p, y in pairs if y]
    neg = [p for p, y in pairs if not y]
    if not pos or not neg:
        return None
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def logloss(p, y, eps=1e-3):
    p = min(max(p, eps), 1 - eps)
    return -(math.log(p) if y else math.log(1 - p))


QUALITY_UNKNOWN = -1


def qbin(q):
    """Quality bin, or QUALITY_UNKNOWN for a season with no player-outcome file.

    Those rows are not discarded — they still carry a price and a week, and for
    chi-phi-american and inlaws-outlaws they are ALL the history there is. They simply
    form their own cells, so an unmeasured add is never averaged in with a measured one.
    """
    if q is None:
        return QUALITY_UNKNOWN
    return min(N_QUALITY_BINS - 1, int(q * N_QUALITY_BINS))


def wbin(week, n_weeks):
    """Four phases: opening claim, early season, mid, run-in. Boundaries are fractions
    of the league's own season length, not calendar weeks, so a league with a different
    schedule bins the same way."""
    if week <= 1:
        return 0
    f = week / max(1, n_weeks)
    return 1 if f <= 0.35 else 2 if f <= 0.65 else 3


# ── observations ─────────────────────────────────────────────────────────────
def faab_rows(ctx, seasons):
    """One row per waiver contest that had a single winner."""
    rows = []
    for yr in seasons:
        scored = bool(H.load_players_raw(ctx, yr))
        q = H.quality(ctx, yr) if scored else {}
        pos = H.player_positions(ctx, yr) if scored else {}
        b = AR.budget(ctx, yr)
        cur = AR.budget(ctx, ctx.season)
        scale = (float(cur) / float(b)) if (cur and b) else 1.0
        for c in H.contests(ctx, yr):
            w = c["winner"]
            if not w:
                continue
            rows.append({
                "season": yr, "week": c["week"], "player": c["player"],
                "pos": pos.get(c["player"], "?"),
                "q": q.get(c["player"], 0.0) if scored else None,
                "bid": w.bid * scale, "contested": c["n_bidders"] > 1,
                "n_bidders": c["n_bidders"],
                "loser_bids": [r.bid * scale for r in c["losers"]],
                "manager": w.manager,
            })
    return rows


def priority_rows(ctx, seasons):
    """One row per successful add: did the player have to be CLAIMED off waivers, or had
    he already cleared to free agency?

    Note precisely what this conditions on: players somebody wanted. It is not the
    unconditional survival rate of every dropped player — that population is not
    observable, because a player nobody ever adds leaves no transaction at all.
    """
    rows = []
    for yr in seasons:
        scored = bool(H.load_players_raw(ctx, yr))
        q = H.quality(ctx, yr) if scored else {}
        pos = H.player_positions(ctx, yr) if scored else {}
        contested = {}
        for c in H.contests(ctx, yr):
            contested[(c["player"], c["week"])] = c["n_bidders"]
        for r in H.acquisitions(ctx, yr):
            rows.append({
                "season": yr, "week": r.week, "player": r.player,
                "pos": pos.get(r.player, "?"),
                "q": q.get(r.player, 0.0) if scored else None,
                "survived": r.channel == "freeagent",
                "contested": contested.get((r.player, r.week), 1) > 1,
                "manager": r.manager,
            })
    return rows


# ── fitting ──────────────────────────────────────────────────────────────────
def cell_stats(rows, model):
    if model == AR.FAAB:
        bids = [r["bid"] for r in rows]
        return {"n": len(rows),
                "median_win": round(quantile(bids, 0.5), 1),
                "p80_win": round(quantile(bids, 0.8), 1),
                "p95_win": round(quantile(bids, 0.95), 1),
                "mean_win": round(statistics.fmean(bids), 2) if bids else 0.0,
                "p_contested": round(statistics.fmean(
                    [1.0 if r["contested"] else 0.0 for r in rows]), 3) if rows else 0.0}
    return {"n": len(rows),
            "p_survives_to_free_agency": round(statistics.fmean(
                [1.0 if r["survived"] else 0.0 for r in rows]), 3) if rows else 0.0,
            "p_contested": round(statistics.fmean(
                [1.0 if r["contested"] else 0.0 for r in rows]), 3) if rows else 0.0}


def by_quality(rows, model):
    out = []
    for b in range(N_QUALITY_BINS):
        sub = [r for r in rows if qbin(r["q"]) == b]
        lo, hi = b / N_QUALITY_BINS, (b + 1) / N_QUALITY_BINS
        out.append({"pct": round((lo + hi) / 2, 3), "pct_lo": round(lo, 3),
                    "pct_hi": round(hi, 3), **cell_stats(sub, model)})
    unknown = [r for r in rows if r["q"] is None]
    if unknown:
        out.append({"pct": None, "pct_lo": None, "pct_hi": None,
                    "note": "season had no player-outcome file; quality unmeasured",
                    **cell_stats(unknown, model)})
    return out


def by_week(rows, model, budget, n_teams):
    out = []
    for wk in sorted({r["week"] for r in rows}):
        sub = [r for r in rows if r["week"] == wk]
        e = {"week": wk, **cell_stats(sub, model)}
        if model == AR.FAAB and budget and n_teams:
            seasons = len({r["season"] for r in sub}) or 1
            e["share_of_budget_spent"] = round(
                sum(r["bid"] for r in sub) / (budget * n_teams * seasons), 4)
        out.append(e)
    return out


def joint_grid(rows, model, n_weeks):
    out = []
    for b in list(range(N_QUALITY_BINS)) + [QUALITY_UNKNOWN]:
        for w in range(4):
            sub = [r for r in rows if qbin(r["q"]) == b and wbin(r["week"], n_weeks) == w]
            if not sub:
                continue
            out.append({"q_bin": b, "week_bin": w, **cell_stats(sub, model)})
    return out


# ── validation ───────────────────────────────────────────────────────────────
def validate(rows, model, n_weeks):
    """Leave-one-season-out. Fit the (quality x week) grid on the other seasons, score
    the held-out one, compare with a predictor that ignores both features."""
    seasons = sorted({r["season"] for r in rows})
    if len(seasons) < 2:
        return {"ran": False, "reason": f"need >=2 seasons to hold one out, have "
                                        f"{len(seasons)}", "n_holdout": 0}
    acc = collections.defaultdict(list)
    probs_m, probs_b = [], []
    for ho in seasons:
        tr = [r for r in rows if r["season"] != ho]
        te = [r for r in rows if r["season"] == ho]
        cells = collections.defaultdict(list)
        for r in tr:
            cells[(qbin(r["q"]), wbin(r["week"], n_weeks))].append(r)
        if model == AR.FAAB:
            g_med = quantile([r["bid"] for r in tr], 0.5)
            g_p80 = quantile([r["bid"] for r in tr], 0.8)
            g_con = statistics.fmean([1.0 if r["contested"] else 0.0 for r in tr])
            for r in te:
                c = cells.get((qbin(r["q"]), wbin(r["week"], n_weeks)), [])
                if len(c) < MIN_CELL:
                    med, p80, pc = g_med, g_p80, g_con
                else:
                    bs = [x["bid"] for x in c]
                    med, p80 = quantile(bs, 0.5), quantile(bs, 0.8)
                    pc = statistics.fmean([1.0 if x["contested"] else 0.0 for x in c])
                acc["mae_model"].append(abs(r["bid"] - med))
                acc["mae_base"].append(abs(r["bid"] - g_med))
                acc["pin_model"].append(pinball(r["bid"], p80, 0.8))
                acc["pin_base"].append(pinball(r["bid"], g_p80, 0.8))
                acc["brier_model"].append((float(r["contested"]) - pc) ** 2)
                acc["brier_base"].append((float(r["contested"]) - g_con) ** 2)
                acc["ll_model"].append(logloss(pc, r["contested"]))
                acc["ll_base"].append(logloss(g_con, r["contested"]))
                probs_m.append((pc, r["contested"]))
                probs_b.append((g_con, r["contested"]))
        else:
            g_sur = statistics.fmean([1.0 if r["survived"] else 0.0 for r in tr])
            for r in te:
                c = cells.get((qbin(r["q"]), wbin(r["week"], n_weeks)), [])
                ps = (g_sur if len(c) < MIN_CELL
                      else statistics.fmean([1.0 if x["survived"] else 0.0 for x in c]))
                acc["mae_model"].append(abs(float(r["survived"]) - ps))
                acc["mae_base"].append(abs(float(r["survived"]) - g_sur))
                acc["brier_model"].append((float(r["survived"]) - ps) ** 2)
                acc["brier_base"].append((float(r["survived"]) - g_sur) ** 2)
                acc["ll_model"].append(logloss(ps, r["survived"]))
                acc["ll_base"].append(logloss(g_sur, r["survived"]))
                probs_m.append((ps, r["survived"]))
                probs_b.append((g_sur, r["survived"]))

    def m(k):
        return round(statistics.fmean(acc[k]), 4) if acc[k] else None

    out = {"ran": True, "scheme": "leave-one-season-out",
           "holdout_seasons": seasons, "n_holdout": len(acc["mae_model"]),
           "holdout_mae": m("mae_model"), "baseline_mae": m("mae_base"),
           "contested_brier": m("brier_model"), "contested_brier_baseline": m("brier_base"),
           "contested_logloss": m("ll_model"), "contested_logloss_baseline": m("ll_base"),
           "contested_auc": round(auc(probs_m), 3) if auc(probs_m) is not None else None,
           "baseline": "pooled constant from the training seasons (ignores quality and week)"}
    if model == AR.FAAB:
        out["p80_pinball"] = m("pin_model")
        out["p80_pinball_baseline"] = m("pin_base")

    def gain(a, b):
        return None if (a is None or not b) else round(100.0 * (b - a) / b, 1)

    out["gain_pct"] = {
        "median_mae": gain(out["holdout_mae"], out["baseline_mae"]),
        "p80_pinball": gain(out.get("p80_pinball"), out.get("p80_pinball_baseline")),
        "contested_brier": gain(out["contested_brier"], out["contested_brier_baseline"]),
    }
    out["beats_baseline"] = {k: (v is not None and v > 0.0)
                             for k, v in out["gain_pct"].items()}
    out["promoted"] = {k: (v is not None and v >= PROMOTE_THRESHOLD_PCT)
                       for k, v in out["gain_pct"].items()}
    out["promote_threshold_pct"] = PROMOTE_THRESHOLD_PCT
    return out


def verdict(v, model):
    if not v.get("ran"):
        return f"NOT VALIDATED — {v.get('reason')}. Treat every number as descriptive only."
    g = v["gain_pct"]
    bits = []
    if g.get("median_mae") is not None:
        bits.append(f"median bid MAE {v['holdout_mae']} vs baseline {v['baseline_mae']} "
                    f"({g['median_mae']:+.1f}%)")
    if g.get("p80_pinball") is not None:
        bits.append(f"p80 pinball {v['p80_pinball']} vs {v['p80_pinball_baseline']} "
                    f"({g['p80_pinball']:+.1f}%)")
    if g.get("contested_brier") is not None:
        bits.append(f"contested Brier {v['contested_brier']} vs "
                    f"{v['contested_brier_baseline']} ({g['contested_brier']:+.1f}%), "
                    f"AUC {v['contested_auc']}")
    promoted = [k for k, b in v["promoted"].items() if b]
    demoted = [k for k, b in v["promoted"].items()
               if not b and v["gain_pct"].get(k) is not None]
    head = (f"PROMOTED: {', '.join(promoted)} "
            f"(beat the baseline by >={v['promote_threshold_pct']:.0f}% out of sample)"
            if promoted else
            f"NOTHING PROMOTED — no target beat the baseline by "
            f"{v['promote_threshold_pct']:.0f}% out of sample")
    tail = (f" NOT promoted: {', '.join(demoted)} — for those, use the pooled constant, "
            f"not the curve." if demoted else "")
    return head + ". " + "; ".join(bits) + "." + tail


# ── the descriptive findings (counted, not modelled) ─────────────────────────
def concentration(ctx, seasons, rows):
    """The shape of FAAB spending, which is a matter of counting and therefore does not
    need validating: the budget always clears, almost every winning claim is free, and
    the money lands on a handful of contested claims."""
    budget = AR.budget(ctx, ctx.season)
    per_season = []
    for yr in seasons:
        sub = [r for r in rows if r["season"] == yr]
        spend = collections.Counter()
        for r in sub:
            if r["manager"]:
                spend[r["manager"]] += r["bid"]
        b = AR.budget(ctx, yr) or budget
        prof, _exact = H.profile_or_current(ctx, yr)
        teams = len(H.managers(ctx, yr)) or (prof.size if prof else 0) or 1
        con = [r for r in sub if r["contested"]]
        per_season.append({
            "season": yr, "n_claims": len(sub), "teams": teams,
            "teams_at_full_budget": sum(1 for v in spend.values() if b and v >= b),
            "total_spent": round(sum(spend.values())),
            "pct_of_pool_spent": round(sum(spend.values()) / (b * teams), 3) if b else None,
            "zero_bid_wins": sum(1 for r in sub if r["bid"] == 0),
            "contested_claims": len(con),
            "contested_per_team": round(len(con) / teams, 1),
            "pct_of_spend_on_contested": round(
                sum(r["bid"] for r in con) / sum(r["bid"] for r in sub), 3)
            if sum(r["bid"] for r in sub) else None,
            "losing_dollars": round(sum(sum(r["loser_bids"]) for r in sub)),
            # The nominal budget is not always the real denominator: the 12-team league 2019 has a
            # team spending $200 against a $100 setting and 2021 has two over $100, so
            # ESPN's acquisitionBudget is an end-of-season snapshot, not a hard cap the
            # whole season. Surfaced rather than smoothed over — same discipline as the
            # auction side's effective_wallet.
            "effective_wallet": round(sum(spend.values()) / teams, 1),
            "max_team_spend": round(max(spend.values())) if spend else 0,
            "over_nominal_budget": sum(1 for v in spend.values() if b and v > b),
        })
    con = [r for r in rows if r["contested"]]
    unc = [r for r in rows if not r["contested"]]
    return {
        "per_season": per_season,
        "pooled": {
            "n_claims": len(rows),
            "zero_bid_share": round(sum(1 for r in rows if r["bid"] == 0) / len(rows), 3)
            if rows else None,
            "p_contested": round(len(con) / len(rows), 3) if rows else None,
            "median_win_contested": round(quantile([r["bid"] for r in con], 0.5), 1),
            "median_win_uncontested": round(quantile([r["bid"] for r in unc], 0.5), 1),
            "p80_win_contested": round(quantile([r["bid"] for r in con], 0.8), 1),
            "mean_teams_at_full_budget": round(statistics.fmean(
                [s["teams_at_full_budget"] for s in per_season]), 1) if per_season else None,
            "mean_contested_per_team_per_season": round(statistics.fmean(
                [s["contested_per_team"] for s in per_season]), 1) if per_season else None,
            "mean_effective_wallet": round(statistics.fmean(
                [s["effective_wallet"] for s in per_season]), 1) if per_season else None,
            "nominal_budget": budget,
            "team_seasons_over_nominal_budget": sum(
                s["over_nominal_budget"] for s in per_season),
        },
    }


# ── build ────────────────────────────────────────────────────────────────────
def build(ctx, regime=None, include_current=False):
    warn = []
    model = regime or AR.current_regime(ctx)
    pool = [s for s in AR.regime_seasons(ctx, model)
            if include_current or int(s) < int(ctx.season)]
    dropped = [s for s in H.transaction_seasons(ctx)
               if s not in pool and (include_current or int(s) < int(ctx.season))]
    # Every in-regime season is usable for prices and weeks. Only the ones with a
    # player-outcome file can also be binned by quality — so they are separated, not
    # dropped: for two of the three leagues the unscored seasons are the whole history.
    usable = list(pool)
    scored_seasons = [s for s in pool if H.load_players_raw(ctx, s)]
    if set(scored_seasons) != set(pool):
        warn.append(f"{ctx.key}: seasons {sorted(set(pool) - set(scored_seasons))} have "
                    f"transactions but no players.json, so add QUALITY cannot be measured "
                    f"for them. Their claims still count toward the week curve and the "
                    f"spend shape; they are pooled separately from measured claims, "
                    f"never averaged in with them.")
    no_settings = [s for s in pool if not H.has_settings(ctx, s)]
    if no_settings:
        warn.append(f"{ctx.key}: seasons {no_settings} have no league_full.json — their "
                    f"regime was INFERRED from transaction evidence and their league "
                    f"shape is assumed to match {ctx.season}.")
    prof = H.profile(ctx, ctx.season)
    n_weeks = max(1, prof.regular_weeks + len(prof.playoff_weeks))
    budget = AR.budget(ctx, ctx.season)
    n_teams = len(H.managers(ctx, ctx.season)) or (prof.size if prof else 0) or 1

    rows = (faab_rows(ctx, usable) if model == AR.FAAB else priority_rows(ctx, usable))
    if not rows:
        warn.append(f"{ctx.key}: no {model} acquisition history "
                    f"(regime pool {pool or 'empty'}) — writing an empty curve. "
                    f"Nothing downstream may treat this as a fitted model.")

    art = {
        "model": model,
        "league": ctx.key,
        "season": ctx.season,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seasons": usable,
        "excluded_seasons": {
            "wrong_regime": sorted(s for s in dropped
                                   if AR.regime_map(ctx).get(s) != model),
            "no_outcome_data": sorted(set(pool) - set(usable)),
            "in_progress": ([ctx.season] if not include_current
                            and ctx.season in H.transaction_seasons(ctx) else []),
        },
        "acquisition_regimes": {str(k): v for k, v in sorted(AR.regime_map(ctx).items())},
        "regime_switches": [{"season": s, "from": a, "to": b}
                            for s, a, b in AR.switch_points(ctx)],
        "faab_budget": budget if model == AR.FAAB else None,
        "teams": n_teams,
        "weeks": n_weeks,
        "n_claims": len(rows),
        "quality": {
            "measure": "season fantasy-point percentile within position, scored with "
                       "this league's own scoring settings",
            "post_hoc": True,
            "measured_seasons": scored_seasons,
            "unmeasured_seasons": sorted(set(usable) - set(scored_seasons)),
            "claims_with_quality": sum(1 for r in rows if r["q"] is not None),
            "limitation": "ESPN's archived player payload carries season totals only "
                          "(no weekly splits), so this cannot be narrowed to production "
                          "AFTER the add date; binning by week absorbs most of it",
            "reference_points_by_decile": H.quality_reference(ctx, scored_seasons),
        },
        "by_quality": by_quality(rows, model),
        "by_week": by_week(rows, model, budget, n_teams),
        "by_quality_week": joint_grid(rows, model, n_weeks),
        "week_bins": {"0": "week 1", "1": "early (<=35% of season)",
                      "2": "mid (35-65%)", "3": "run-in (>65%)"},
        "min_cell": MIN_CELL,
    }
    if model == AR.FAAB:
        art["concentration"] = concentration(ctx, usable, rows)
    v = validate(rows, model, n_weeks) if rows else {
        "ran": False, "reason": "no observations", "n_holdout": 0}
    v["verdict"] = verdict(v, model)
    art["validation"] = v
    art["caveats"] = warn
    return art, warn


# ── reporting ────────────────────────────────────────────────────────────────
def report(ctx, art):
    model = art["model"]
    print(f"\n{ctx.key}  {model.upper()} curve   seasons={art['seasons'] or 'NONE'}   "
          f"n={art['n_claims']}")
    ex = art["excluded_seasons"]
    if ex["wrong_regime"]:
        print(f"  EXCLUDED (wrong regime)  : {ex['wrong_regime']}  "
              f"<- the regime filter working")
    if ex["no_outcome_data"]:
        print(f"  EXCLUDED (no outcomes)   : {ex['no_outcome_data']}")
    if ex["in_progress"]:
        print(f"  EXCLUDED (in progress)   : {ex['in_progress']}")
    if not art["n_claims"]:
        print("  (empty curve — nothing to fit)")
        return
    if model == AR.FAAB:
        print(f"\n  by add quality (post-hoc production percentile within position)")
        print(f"    {'pct range':>12}{'n':>6}{'median':>8}{'p80':>7}{'p95':>7}"
              f"{'mean':>7}{'p_cont':>8}")
        for b in art["by_quality"]:
            if not b["n"]:
                continue
            lbl = ("   unmeasured" if b["pct_lo"] is None
                   else f"{b['pct_lo']:.2f}-{b['pct_hi']:.2f}")
            print(f"    {lbl:>12}{b['n']:>6}"
                  f"{b['median_win']:>8.0f}{b['p80_win']:>7.0f}{b['p95_win']:>7.0f}"
                  f"{b['mean_win']:>7.1f}{b['p_contested']:>8.2f}")
        print(f"\n  by week")
        print(f"    {'wk':>4}{'n':>6}{'median':>8}{'p80':>7}{'mean':>7}{'p_cont':>8}"
              f"{'share of pool':>15}")
        for b in art["by_week"]:
            print(f"    {b['week']:>4}{b['n']:>6}{b['median_win']:>8.0f}"
                  f"{b['p80_win']:>7.0f}{b['mean_win']:>7.1f}{b['p_contested']:>8.2f}"
                  f"{b.get('share_of_budget_spent', 0):>15.3f}")
        c = art["concentration"]["pooled"]
        print(f"\n  the shape of the money (counted, not modelled)")
        print(f"    winning claims at $0        : {c['zero_bid_share']:.0%}")
        print(f"    P(a claim is contested)     : {c['p_contested']:.0%}")
        print(f"    median win  contested/not   : ${c['median_win_contested']:.0f} / "
              f"${c['median_win_uncontested']:.0f}   (p80 contested "
              f"${c['p80_win_contested']:.0f})")
        print(f"    teams ending at full budget : "
              f"{c['mean_teams_at_full_budget']}/{art['teams']} per season")
        print(f"    contested claims per team   : "
              f"{c['mean_contested_per_team_per_season']} per season")
        print(f"    {'season':>8}{'claims':>8}{'spent':>8}{'pool%':>8}{'$0 wins':>9}"
              f"{'contested':>11}{'$ on cont.':>12}{'full-budget teams':>19}")
        for s in art["concentration"]["per_season"]:
            print(f"    {s['season']:>8}{s['n_claims']:>8}{s['total_spent']:>8}"
                  f"{s['pct_of_pool_spent']:>8.2f}{s['zero_bid_wins']:>9}"
                  f"{s['contested_claims']:>11}"
                  f"{(s['pct_of_spend_on_contested'] or 0):>12.2f}"
                  f"{s['teams_at_full_budget']:>12}/{s['teams']:<6}")
        print(f"    effective wallet ${c['mean_effective_wallet']}/team vs nominal "
              f"${c['nominal_budget']}; {c['team_seasons_over_nominal_budget']} team-seasons "
              f"finished OVER the nominal budget (ESPN's acquisitionBudget is an "
              f"end-of-season snapshot, not a season-long cap)")
    else:
        print(f"\n  P(survives to free agency) by add quality")
        print(f"    {'pct range':>12}{'n':>6}{'p_survives':>12}{'p_contested':>13}")
        for b in art["by_quality"]:
            if not b["n"]:
                continue
            lbl = ("   unmeasured" if b["pct_lo"] is None
                   else f"{b['pct_lo']:.2f}-{b['pct_hi']:.2f}")
            print(f"    {lbl:>12}{b['n']:>6}"
                  f"{b['p_survives_to_free_agency']:>12.2f}{b['p_contested']:>13.2f}")
        print(f"\n  by week")
        print(f"    {'wk':>4}{'n':>6}{'p_survives':>12}")
        for b in art["by_week"]:
            print(f"    {b['week']:>4}{b['n']:>6}{b['p_survives_to_free_agency']:>12.2f}")
    v = art["validation"]
    print(f"\n  OUT-OF-SAMPLE VALIDATION ({v.get('scheme','-')}, "
          f"n={v.get('n_holdout',0)})")
    print(f"    {v['verdict']}")


def run(ctx, regime=None, include_current=False, quiet=False):
    """Fit ONE league's acquisition curve and write it. -> the artifact dict, or None.

    The filename follows the league's CURRENT regime: faab_curve.json for a FAAB league,
    priority_curve.json for a priority league. Never raises on missing data.
    """
    if not H.load_league(ctx, ctx.season):
        print(f"! {ctx.key}: no league_full.json for {ctx.season} — skipped.")
        return None
    art, warn = build(ctx, regime, include_current)
    for w in warn:
        print(f"! {w}")
    if not quiet:
        report(ctx, art)
    if art["model"] != AR.current_regime(ctx):
        print(f"\n  audit only (--regime {art['model']} != current "
              f"{AR.current_regime(ctx)}) — nothing written\n")
        return art
    ctx.ensure_dirs()
    path = ctx.config(OUT_NAME[art["model"]])
    with open(path, "w") as f:
        json.dump(art, f, indent=2)
        f.write("\n")
    print(f"\n  wrote {os.path.relpath(path, ROOT)}\n")
    return art


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--league", help="league key; default: every registered league")
    ap.add_argument("--regime", choices=[AR.FAAB, AR.PRIORITY],
                    help="override the regime (for auditing a league's older era)")
    ap.add_argument("--include-current", action="store_true",
                    help="also use the in-progress season")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    import leagues
    ctxs = [leagues.resolve(args.league)] if args.league else leagues.all()
    for ctx in ctxs:
        run(ctx, regime=args.regime, include_current=args.include_current,
            quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
