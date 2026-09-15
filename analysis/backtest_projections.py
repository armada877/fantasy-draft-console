#!/usr/bin/env python3
"""WS-8b — is ESPN's weekly projection any good, and can we beat it?

`ros_points` is the number the whole in-season console is built on: waivers, trades
and the lineup planner are all functions of it, and it is ESPN's projection
recomputed on the league's own scoring. Until this file ran, that backbone had
never been scored against a single realised outcome. Now it is, on the 39,020
paired player-weeks `scraping/backfill_weeks.py` put on disk.

Two questions, and they are NOT the same question:

  1. **How good is the baseline?** Descriptive. MAE, bias and hit rates by
     position, by week, by projection tier, per league. No model, no claim.
  2. **Can any correction beat it out of sample?** Every candidate below is a
     CLAIM that we out-model a vendor with a full-time projections team, and it is
     refused unless it clears `engine.projection_policy`'s bar:
     >= 2 leagues at n >= 500 held-out player-weeks, beats baseline in EVERY
     powered league, harms no underpowered league by more than 2%, whole SEASONS
     held out.

Protocol — leave-one-season-out, because adjacent weeks leak. A player's week-6
role, injury and depth chart are most of what sets his week-7 number, so a random
row split trains on next week's answer and manufactures a pass. Each fold fits on
every OTHER season and predicts the held-out one. A league with a single season
cannot hold one out at all; it is scored with a model fitted on the OTHER LEAGUES'
seasons, which is still honest out-of-sample and is labelled `cross-league`. It can
never confirm a candidate (it is never "powered") — but it can veto one.

Candidates tested (deliberately simple; a shape you cannot write in a line is a
shape 39k rows cannot support):

    positional_bias   x by mean(actual)/mean(projected) for that position
    linear_recal      OLS a + b*projected per position — fixes a compressed or
                      stretched scale, which a single multiplier cannot
    shrink_to_pos     pull each projection toward its positional mean by a fitted
                      factor — the "projections are overconfident" hypothesis
    week_decay        per-week-bin multiplier — "the projection goes stale as roles
                      change and the vendor does not keep up"
    form_blend        w*projection + (1-w)*that player's own trailing 3-week mean

Outputs:
    reports/projection_accuracy.json     full numbers, per league, machine-readable
    stdout                               the human report

Run:
    python3 analysis/backtest_projections.py
    python3 analysis/backtest_projections.py --league 2kdome --verbose
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import leagues                                          # noqa: E402
from engine import projection_policy as pp              # noqa: E402
from engine.profile import BENCH_SLOTS, position_name   # noqa: E402

# Columns of a distilled row, as written by scraping/backfill_weeks.py.
SEASON, WEEK, TEAM, PID, SLOT, POS, PROJ, ACT = range(8)

# A projection of zero is not a forecast, it is "no opinion" (bye, inactive,
# just-signed). Grading those measures ESPN's roster hygiene, not its modelling,
# and they are 30%+ of rostered player-weeks — enough to swamp any real effect.
MIN_PROJ = 0.5

# Projection tiers, in points. The top band is where lineup and waiver decisions
# actually get made, so it is reported separately rather than averaged away.
TIERS = ((0.5, 5, "0.5-5"), (5, 10, "5-10"), (10, 15, "10-15"),
         (15, 20, "15-20"), (20, 999, "20+"))
WEEK_BINS = ((1, 4, "1-4"), (5, 9, "5-9"), (10, 14, "10-14"), (15, 18, "15-18"))

MIN_FIT_N = 30              # below this a fitted cell falls back to the identity


# ── small stats ──────────────────────────────────────────────────────────────
def mae(pairs):
    return statistics.fmean(abs(p - a) for p, a in pairs) if pairs else 0.0


def bias(pairs):
    return statistics.fmean(p - a for p, a in pairs) if pairs else 0.0


def rmse(pairs):
    return (statistics.fmean((p - a) ** 2 for p, a in pairs) ** 0.5) if pairs else 0.0


def corr(pairs):
    if len(pairs) < 3:
        return None
    xs = [p for p, _ in pairs]
    ys = [a for _, a in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    return (num / (dx * dy)) if dx and dy else None


def ols(pairs):
    """(intercept, slope) of actual ~ projected. Identity when undetermined."""
    if len(pairs) < MIN_FIT_N:
        return 0.0, 1.0
    xs = [p for p, _ in pairs]
    ys = [a for _, a in pairs]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0, 1.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def bin_of(value, bins):
    for lo, hi, label in bins:
        if lo <= value < hi:
            return label
    return bins[-1][2]


# ── data ─────────────────────────────────────────────────────────────────────
def load_rows(ctx):
    """Gradeable player-weeks for one league, plus the trailing-form index.

    Returns (rows, form) where `form` maps (season, week, player_id) -> the mean of
    that player's own actuals over his previous 3 played weeks, or None. It is built
    STRICTLY from earlier weeks, so `form_blend` cannot see its own answer.
    """
    path = ctx.raw("player_weeks.json")
    if not os.path.exists(path):
        return [], {}, f"no {os.path.relpath(path, ROOT)} — run scraping/backfill_weeks.py"
    with open(path) as f:
        art = json.load(f)
    rows = [r for r in (art.get("rows") or []) if float(r[PROJ]) >= MIN_PROJ]

    history = defaultdict(list)          # player_id -> [(season, week, actual)]
    for r in sorted(art.get("rows") or [], key=lambda r: (r[SEASON], r[WEEK])):
        history[r[PID]].append((r[SEASON], r[WEEK], float(r[ACT])))
    form = {}
    for pid, seq in history.items():
        for i, (s, w, _) in enumerate(seq):
            prev = [a for (ps, pw, a) in seq[max(0, i - 3):i] if ps == s]
            form[(s, w, pid)] = statistics.fmean(prev) if prev else None
    return rows, form, None


# ── the baseline description (question 1) ────────────────────────────────────
def describe(rows):
    """What the baseline actually does, sliced the ways a decision cares about."""
    def slice_stats(pairs):
        return {"n": len(pairs), "mae": round(mae(pairs), 3), "bias": round(bias(pairs), 3),
                "rmse": round(rmse(pairs), 3),
                "corr": (round(corr(pairs), 3) if corr(pairs) is not None else None),
                "mean_proj": round(statistics.fmean([p for p, _ in pairs]), 2) if pairs else 0,
                "mean_actual": round(statistics.fmean([a for _, a in pairs]), 2) if pairs else 0}

    allp = [(float(r[PROJ]), float(r[ACT])) for r in rows]
    by_pos, by_tier, by_week, by_season = defaultdict(list), defaultdict(list), \
        defaultdict(list), defaultdict(list)
    started = []
    for r in rows:
        pair = (float(r[PROJ]), float(r[ACT]))
        by_pos[position_name(r[POS])].append(pair)
        by_tier[bin_of(float(r[PROJ]), TIERS)].append(pair)
        by_week[bin_of(int(r[WEEK]), WEEK_BINS)].append(pair)
        by_season[str(r[SEASON])].append(pair)
        if int(r[SLOT]) not in BENCH_SLOTS:
            started.append(pair)
    return {
        "overall": slice_stats(allp),
        "started_only": slice_stats(started),
        "by_position": {k: slice_stats(v) for k, v in sorted(by_pos.items())},
        "by_tier": {lab: slice_stats(by_tier[lab]) for _, _, lab in TIERS if by_tier[lab]},
        "by_week_bin": {lab: slice_stats(by_week[lab]) for _, _, lab in WEEK_BINS
                        if by_week[lab]},
        "by_season": {k: slice_stats(v) for k, v in sorted(by_season.items())},
    }


# ── the candidates (question 2) ──────────────────────────────────────────────
# Each is (name, fit, apply, description). `fit(train_rows, form) -> params`;
# `apply(row, params, form) -> adjusted projection`. Fitting sees ONLY training
# seasons; applying sees only the row.

def _fit_positional_bias(rows, form):
    num, den = defaultdict(float), defaultdict(float)
    for r in rows:
        num[r[POS]] += float(r[ACT])
        den[r[POS]] += float(r[PROJ])
    n = defaultdict(int)
    for r in rows:
        n[r[POS]] += 1
    return {p: (num[p] / den[p]) for p in den
            if den[p] > 0 and n[p] >= MIN_FIT_N}


def _apply_positional_bias(r, params, form):
    return float(r[PROJ]) * params.get(r[POS], 1.0)


def _fit_linear_recal(rows, form):
    by = defaultdict(list)
    for r in rows:
        by[r[POS]].append((float(r[PROJ]), float(r[ACT])))
    return {p: ols(v) for p, v in by.items()}


def _apply_linear_recal(r, params, form):
    a, b = params.get(r[POS], (0.0, 1.0))
    return max(0.0, a + b * float(r[PROJ]))


def _fit_shrink_to_pos(rows, form):
    """mean_pos + k*(proj - mean_pos), k from the regression of actual on the
    centred projection — the textbook test of 'this forecast is overconfident'."""
    by = defaultdict(list)
    for r in rows:
        by[r[POS]].append((float(r[PROJ]), float(r[ACT])))
    out = {}
    for p, v in by.items():
        if len(v) < MIN_FIT_N:
            continue
        m = statistics.fmean([x for x, _ in v])
        _, b = ols([(x - m, y) for x, y in v])
        out[p] = (m, b)
    return out


def _apply_shrink_to_pos(r, params, form):
    hit = params.get(r[POS])
    if not hit:
        return float(r[PROJ])
    m, k = hit
    return max(0.0, m + k * (float(r[PROJ]) - m))


def _fit_week_decay(rows, form):
    num, den, n = defaultdict(float), defaultdict(float), defaultdict(int)
    for r in rows:
        b = bin_of(int(r[WEEK]), WEEK_BINS)
        num[b] += float(r[ACT])
        den[b] += float(r[PROJ])
        n[b] += 1
    return {b: num[b] / den[b] for b in den if den[b] > 0 and n[b] >= MIN_FIT_N}


def _apply_week_decay(r, params, form):
    return float(r[PROJ]) * params.get(bin_of(int(r[WEEK]), WEEK_BINS), 1.0)


def _fit_form_blend(rows, form):
    """The blend weight that minimises training MAE, on a coarse grid. A grid beats
    a closed form here because the loss is MAE, not squared error."""
    usable = [(float(r[PROJ]), float(r[ACT]), form.get((r[SEASON], r[WEEK], r[PID])))
              for r in rows]
    usable = [(p, a, f) for p, a, f in usable if f is not None]
    if len(usable) < MIN_FIT_N:
        return {"w": 1.0}
    best, best_mae = 1.0, None
    for i in range(11):
        w = i / 10
        m = statistics.fmean(abs(w * p + (1 - w) * f - a) for p, a, f in usable)
        if best_mae is None or m < best_mae:
            best, best_mae = w, m
    return {"w": best}


def _apply_form_blend(r, params, form):
    f = form.get((r[SEASON], r[WEEK], r[PID]))
    if f is None:
        return float(r[PROJ])
    w = params.get("w", 1.0)
    return w * float(r[PROJ]) + (1 - w) * f


CANDIDATES = [
    ("positional_bias", _fit_positional_bias, _apply_positional_bias,
     "scale each position by its historical actual/projected ratio"),
    ("linear_recal", _fit_linear_recal, _apply_linear_recal,
     "OLS recalibration a + b*projected, fitted per position"),
    ("shrink_to_pos", _fit_shrink_to_pos, _apply_shrink_to_pos,
     "pull each projection toward its positional mean by a fitted factor"),
    ("week_decay", _fit_week_decay, _apply_week_decay,
     "scale by a fitted multiplier for the part of the season the week falls in"),
    ("form_blend", _fit_form_blend, _apply_form_blend,
     "blend the projection with the player's own trailing 3-week mean"),
]


# ── evaluation ───────────────────────────────────────────────────────────────
def evaluate(name, fit, apply_fn, per_league, verbose=False):
    """Leave-one-season-out per league -> {league: (baseline_mae, model_mae, n, folds)}.

    A league with one season cannot hold a season out, so it is fitted on every
    OTHER league's rows. That is labelled and, because such a league is never
    powered, it can only veto.
    """
    out = {}
    for key, (rows, form, _) in per_league.items():
        seasons = sorted({r[SEASON] for r in rows})
        base, model, folds = [], [], []
        for s in seasons:
            test = [r for r in rows if r[SEASON] == s]
            train = [r for r in rows if r[SEASON] != s]
            train_form = form
            source = "within-league"
            if len(train) < MIN_FIT_N * 10:
                # borrow every other league's history; never this league's own season
                train, train_form, source = [], {}, "cross-league"
                for k2, (r2, f2, _) in per_league.items():
                    if k2 == key:
                        continue
                    train.extend(r2)
                    train_form.update(f2)
            if not train:
                continue
            params = fit(train, train_form)
            b = [(float(r[PROJ]), float(r[ACT])) for r in test]
            m = [(apply_fn(r, params, form), float(r[ACT])) for r in test]
            base.extend(b)
            model.extend(m)
            folds.append({"held_out": s, "n": len(test), "fit": source,
                          "baseline_mae": round(mae(b), 4), "model_mae": round(mae(m), 4)})
            if verbose:
                print(f"      {key} hold out {s}: n={len(test):5d} "
                      f"baseline {mae(b):.4f} -> {mae(m):.4f} "
                      f"({(mae(b) - mae(m)) / mae(b):+.2%}) [{source}]")
        if base:
            out[key] = {"baseline_mae": round(mae(base), 4), "model_mae": round(mae(model), 4),
                        "n": len(base), "folds": folds,
                        "seasons_held_out": [f["held_out"] for f in folds]}
    return out


def to_validation(name, description, results) -> pp.Validation:
    """Wrap the measured folds in the object `projection_policy` judges. The verdict
    is the module's, not ours — that is the entire point of routing through it."""
    return pp.Validation(
        method=("leave-one-season-out within league; single-season leagues fitted on "
                "the other leagues (cross-league), never on their own held-out season"),
        fitted_on=tuple(sorted(results)),
        results=tuple(pp.LeagueResult(
            league=k, baseline_mae=v["baseline_mae"], model_mae=v["model_mae"],
            n=v["n"], seasons_held_out=tuple(v["seasons_held_out"]))
            for k, v in sorted(results.items())),
        notes=description)


# ── report ───────────────────────────────────────────────────────────────────
def print_description(key, d):
    o = d["overall"]
    print(f"\n[{key}] baseline: ESPN weekly projection, ESPN's own applied scoring")
    print(f"  n={o['n']:,} rostered player-weeks with a projection >= {MIN_PROJ}")
    print(f"  MAE {o['mae']:.2f} · bias {o['bias']:+.2f} · RMSE {o['rmse']:.2f} · "
          f"corr {o['corr']} · mean proj {o['mean_proj']} vs actual {o['mean_actual']}")
    s = d["started_only"]
    print(f"  started only: n={s['n']:,} MAE {s['mae']:.2f} bias {s['bias']:+.2f}")
    print(f"  {'position':10}{'n':>7}{'MAE':>8}{'bias':>8}{'corr':>7}{'proj':>7}{'actual':>8}")
    for pos, v in d["by_position"].items():
        if v["n"] < 100:
            continue
        print(f"  {pos:10}{v['n']:>7,}{v['mae']:>8.2f}{v['bias']:>+8.2f}"
              f"{(v['corr'] if v['corr'] is not None else 0):>7.2f}"
              f"{v['mean_proj']:>7.1f}{v['mean_actual']:>8.1f}")
    print(f"  {'proj tier':10}{'n':>7}{'MAE':>8}{'bias':>8}{'actual':>8}")
    for tier, v in d["by_tier"].items():
        print(f"  {tier:10}{v['n']:>7,}{v['mae']:>8.2f}{v['bias']:>+8.2f}"
              f"{v['mean_actual']:>8.1f}")
    print(f"  {'weeks':10}{'n':>7}{'MAE':>8}{'bias':>8}")
    for wb, v in d["by_week_bin"].items():
        print(f"  {wb:10}{v['n']:>7,}{v['mae']:>8.2f}{v['bias']:>+8.2f}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 analysis/backtest_projections.py",
        description="WS-8b — score the projection baseline and every candidate correction.")
    ap.add_argument("--league", "-l", action="append", metavar="KEY")
    ap.add_argument("--verbose", "-v", action="store_true", help="print every fold")
    ap.add_argument("--out", default=os.path.join(ROOT, "reports", "projection_accuracy.json"))
    args = ap.parse_args(argv)

    ctxs = ([leagues.resolve(k) for k in args.league] if args.league else leagues.all())
    per_league, missing = {}, []
    for ctx in ctxs:
        rows, form, err = load_rows(ctx)
        if err or not rows:
            missing.append(f"{ctx.key}: {err or 'no gradeable rows'}")
            continue
        per_league[ctx.key] = (rows, form, err)
    if not per_league:
        print("✗ no backfilled player-weeks on disk. Run: python3 scraping/backfill_weeks.py")
        for m in missing:
            print(f"  · {m}")
        return 1

    print("WS-8b — projection accuracy and the corrections that tried to beat it")
    print(f"bar: >= {pp.MIN_POWERED_LEAGUES} leagues at n >= {pp.MIN_N} held-out "
          f"player-weeks, beats baseline in every powered league, harms no "
          f"underpowered league by > {pp.HARM_TOLERANCE:.0%}")
    for m in missing:
        print(f"  ! {m}")

    art = {"generated": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
           "min_projection_graded": MIN_PROJ,
           "population": "rostered player-weeks (ESPN boxscore); see backfill_weeks.py",
           "baseline": "ESPN weekly projection (src1 split1), ESPN's own applied scoring",
           "bar": {"min_n": pp.MIN_N, "min_powered_leagues": pp.MIN_POWERED_LEAGUES,
                   "harm_tolerance": pp.HARM_TOLERANCE},
           "leagues": {}, "candidates": {}}

    for key, (rows, _, _) in per_league.items():
        d = describe(rows)
        art["leagues"][key] = d
        print_description(key, d)

    print("\ncandidate corrections — leave-one-season-out, whole seasons held out")
    for name, fit, apply_fn, desc in CANDIDATES:
        print(f"\n  {name}: {desc}")
        results = evaluate(name, fit, apply_fn, per_league, verbose=args.verbose)
        if not results:
            print("    no evaluable folds")
            continue
        val = to_validation(name, desc, results)
        ships, why = val.verdict()
        for k, v in sorted(results.items()):
            imp = (v["baseline_mae"] - v["model_mae"]) / v["baseline_mae"]
            powered = v["n"] >= pp.MIN_N
            print(f"    {k:20s} n={v['n']:6,}  baseline {v['baseline_mae']:.4f} -> "
                  f"{v['model_mae']:.4f}  {imp:+.2%}"
                  f"{'' if powered else '   [underpowered]'}")
        print(f"    -> {'SHIPS' if ships else 'REFUSED'}: {why}")
        art["candidates"][name] = {
            "description": desc, "ships": bool(ships), "reason": why,
            "results": {k: {kk: vv for kk, vv in v.items()} for k, v in results.items()},
        }

    shipped = [n for n, c in art["candidates"].items() if c["ships"]]
    art["verdict"] = ("baseline ships unmodified — no candidate cleared the bar"
                      if not shipped else
                      "cleared the bar: " + ", ".join(shipped))
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(art, f, indent=1)
    print(f"\n{art['verdict']}")
    print(f"-> {os.path.relpath(args.out, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
