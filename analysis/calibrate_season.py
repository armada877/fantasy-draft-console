#!/usr/bin/env python3
"""Per-manager IN-SEASON tendencies from transaction history -> season_tendencies.json.

The in-season twin of `analysis/calibrate.py`. That one reads the auction and answers
"what will this manager pay for a player on draft night"; this one reads the transaction
log and answers "how does this manager behave for the next four months" — how much churn,
how hard they bid, whether they trade, whether they even answer an offer, how fast they
cut a player they just added.

    raw/<season>/transactions.json (+ playercards, players)
        -> league_history (league-generic loaders, ctx paths)
        -> shrinkage      (empirical Bayes toward the league mean)
        -> ctx.config("season_tendencies.json")

Three disciplines it inherits, all of them things this repo has already been bitten by:

1. **Never pool across an acquisition-regime switch.** The waiver block is built only
   from seasons in the league's CURRENT regime (`acquisition_regime`). the 12-team league's 2018 was
   priority-order — every bid in it is $0 by construction — and folding it into a FAAB
   aggression number would drag the whole league toward zero with observations that were
   never dollars. Same class of bug as pooling keeper and full-supply auction prices.
   The trade and drop blocks are NOT acquisition-priced, so they pool every season with
   transactions; `_meta` records both season lists separately.

2. **Shrink.** Sample size varies ~8x. See `analysis/shrinkage.py`.

3. **A borrowed number must not look calibrated.** A manager with no history gets the
   league mean outright, flagged `borrowed: true`, `n_seasons: 0`, `shrink: 1.0`.

Identity is the manager, via the ESPN member GUID (`league_history.manager`), so a
display-name drift — "Jon" vs "Jonathan" — still matches, and `manager_labels` renames
apply the same way the draft console applies them. Teams, never the `members` array:
members carries co-owners and departed managers.

Run:  python3 analysis/calibrate_season.py --league 2kdome
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (ROOT, HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import analysis.acquisition_regime as AR            # noqa: E402
import analysis.league_history as H                 # noqa: E402
from analysis.shrinkage import eb_binomial, eb_normal, eb_vector   # noqa: E402

LEAGUE_DEFAULT_KEY = "_league_default"
OUT_NAME = "season_tendencies.json"


# ── season pools ─────────────────────────────────────────────────────────────
def season_pools(ctx, include_current=False):
    """(waiver_seasons, history_seasons) — the regime-filtered pool and the full one.

    The in-progress season is excluded by default: every per-season RATE would be
    divided by a full season's worth of weeks while only one or two have been played,
    which biases an active manager's churn toward zero. `--include-current` opts in.
    """
    hist = [s for s in H.transaction_seasons(ctx)
            if include_current or int(s) < int(ctx.season)]
    return AR.filter_seasons(ctx, hist, AR.current_regime(ctx)), hist


# ── raw per-manager counters ─────────────────────────────────────────────────
def _weeks(ctx):
    prof = H.profile(ctx, ctx.season)
    return max(1, prof.regular_weeks + len(prof.playoff_weeks))


def waiver_counters(ctx, seasons):
    """Everything the waiver block needs, per manager, kept as raw observations so the
    shrinker can see the sample sizes rather than pre-averaged numbers."""
    n_weeks = _weeks(ctx)
    c = collections.defaultdict(lambda: {
        "adds": collections.defaultdict(int),       # season -> successful adds
        "spend": collections.defaultdict(float),    # season -> $ (budget-normalized)
        "claims": collections.defaultdict(int),     # season -> claims entered
        "pace": collections.defaultdict(lambda: [0.0] * n_weeks),
        "maxbid": collections.defaultdict(float),
        "contested_entered": 0, "contested_won": 0,
        "wins": 0, "zero_wins": 0, "seasons": set(),
    })
    cur_budget = AR.budget(ctx, ctx.season)
    for yr in seasons:
        b = AR.budget(ctx, yr)
        # FAAB budgets are flat $100 in every the 12-team league FAAB season, so this is a no-op
        # here; it exists so a league that DID change its budget is not pooled raw.
        scale = (float(cur_budget) / float(b)) if (cur_budget and b) else 1.0
        live = H.active_teams(ctx, yr)
        for tid, name in H.managers(ctx, yr).items():
            if tid in live:
                c[name]["seasons"].add(yr)
        for r in H.acquisitions(ctx, yr):
            if not r.manager:
                continue
            m = c[r.manager]
            m["adds"][yr] += 1
            if r.channel == "waiver":
                bid = r.bid * scale
                m["spend"][yr] += bid
                m["wins"] += 1
                m["zero_wins"] += 1 if r.bid == 0 else 0
                m["maxbid"][yr] = max(m["maxbid"][yr], bid)
                wk = min(max(int(r.week or 1), 1), n_weeks)
                m["pace"][yr][wk - 1] += bid
        for con in H.contests(ctx, yr):
            entrants = ([con["winner"]] if con["winner"] else []) + con["losers"]
            for r in entrants:
                if not r.manager:
                    continue
                c[r.manager]["claims"][yr] += 1
                if con["n_bidders"] > 1:
                    c[r.manager]["contested_entered"] += 1
                    if r.won:
                        c[r.manager]["contested_won"] += 1
    return c, n_weeks


def trade_counters(ctx, seasons):
    """(counters, detail_by_season).

    `detail` is "playercards" when the executed trades' item lists are available and
    "transactions" when only the fact of the trade is. Without item detail there is no
    honest `rate_per_season`, `partners` or `pos_flow` — a TRADE_ACCEPT row names the
    ACCEPTING team only, so counting it per manager would systematically credit
    acceptors and erase proposers. Those fields go null rather than half-right.
    `accept_rate` and `responds` survive, because the acceptor is exactly who they are
    about.
    """
    c = collections.defaultdict(lambda: {
        "trades": collections.defaultdict(int), "partners": collections.Counter(),
        "accepted": 0, "died": 0, "flow": collections.defaultdict(list),
        "moves": 0, "seasons": set()})
    detail = {}
    for yr in seasons:
        mgr = H.managers(ctx, yr)
        pos = H.player_positions(ctx, yr)
        live = H.active_teams(ctx, yr)
        for tid, name in mgr.items():
            if tid in live:
                c[name]["seasons"].add(yr)
        tr = H.trades(ctx, yr)
        detail[int(yr)] = tr["detail"]
        for t in tr["executed"]:
            if t["sides"]:
                names = [mgr.get(tm) for tm in t["sides"]]
                for tm, pids in t["sides"].items():
                    nm = mgr.get(tm)
                    if not nm:
                        continue
                    e = c[nm]
                    e["trades"][yr] += 1
                    e["accepted"] += 1
                    for other in names:
                        if other and other != nm:
                            e["partners"][other] += 1
                    got = set(pids)
                    for pid in t["pids"]:
                        p = pos.get(pid, "?")
                        e["flow"][p].append(1.0 if pid in got else -1.0)
                        e["moves"] += 1
            elif t.get("acceptor_manager"):
                c[t["acceptor_manager"]]["accepted"] += 1
        for off in tr["offers_died"]:
            for tm in off["targets"]:
                nm = mgr.get(tm)
                if nm:
                    c[nm]["died"] += 1
    return c, detail


def drop_counters(ctx, seasons):
    c = collections.defaultdict(list)
    for yr in seasons:
        for d in H.drops(ctx, yr):
            if d["manager"] and d["held_weeks"] is not None and d["held_weeks"] >= 0:
                c[d["manager"]].append(float(d["held_weeks"]))
    return c


# ── the calibration ──────────────────────────────────────────────────────────
def calibrate(ctx, include_current=False):
    """-> (artifact dict, warnings list). Never raises on missing data."""
    warn = []
    model = AR.current_regime(ctx)
    waiver_seasons, hist_seasons = season_pools(ctx, include_current)
    roster = sorted(set(H.managers(ctx, ctx.season).values()))

    if not hist_seasons:
        warn.append(
            f"{ctx.key}: NO usable transaction history "
            f"(seasons with rows: {H.transaction_seasons(ctx) or 'none'}; "
            f"current season excluded unless --include-current). Every manager is "
            f"borrowed and there is nothing to borrow FROM — emitting an empty "
            f"_league_default. Cross-league priors are deliberately NOT used: "
            f"contracts.md guardrail 4 forbids one league's data reaching another.")
    if hist_seasons and not waiver_seasons:
        warn.append(
            f"{ctx.key}: history exists ({hist_seasons}) but none of it is in the "
            f"current {model.upper()} regime — the waiver block is uncalibrated. "
            f"Regime switches: {AR.switch_points(ctx) or 'none'}.")

    identity = {int(y): H.identity_source(ctx, y) for y in hist_seasons}
    assumed = sorted(y for y, src in identity.items() if src == "assumed")
    if assumed:
        warn.append(
            f"{ctx.key}: seasons {assumed} have no league_full.json, so team -> manager "
            f"came from the {ctx.season} franchise map. ESPN team ids are stable per "
            f"franchise, but if a team changed hands its history is now attributed to "
            f"the wrong person. Every manager touched by it carries "
            f"`identity_assumed: true`.")
    # A league that changed size changed what a "normal" number of claims even means.
    sizes = {y: len({t.get("teamId") for t in H.load_transactions(ctx, y)
                     if (t.get("teamId") or 0) > 0}) for y in hist_seasons}
    now = len(roster)
    odd = {y: n for y, n in sizes.items() if n and now and abs(n - now) >= 1}
    if odd:
        warn.append(
            f"{ctx.key}: league size differs between calibration seasons and now "
            f"({odd} active teams then vs {now} now). Per-season COUNTS (adds, claims, "
            f"trades) scale with the number of rivals, so these are not strictly "
            f"comparable — the shrunk means are still the best available estimate, but "
            f"they are not a like-for-like measurement.")

    wc, n_weeks = waiver_counters(ctx, waiver_seasons)
    tc, trade_detail = trade_counters(ctx, hist_seasons)
    dc = drop_counters(ctx, hist_seasons)
    names = sorted(set(wc) | set(tc) | set(dc) | set(roster))

    budget = AR.budget(ctx, ctx.season)

    # ── waiver block ─────────────────────────────────────────────────────────
    adds = {n: [wc[n]["adds"].get(y, 0) for y in sorted(wc[n]["seasons"])]
            for n in names if n in wc and wc[n]["seasons"]}
    v_adds, w_adds, f_adds = eb_normal(adds)

    if model == AR.FAAB and budget:
        # aggression = share of the FAAB budget actually spent in a season.
        agg = {n: [wc[n]["spend"].get(y, 0.0) / budget for y in sorted(wc[n]["seasons"])]
               for n in adds}
    else:
        # priority: there is no money, so aggression is claim volume relative to the
        # league norm (1.0 = an average number of claims entered per season).
        raw = {n: [float(wc[n]["claims"].get(y, 0)) for y in sorted(wc[n]["seasons"])]
               for n in adds}
        flat = [x for v in raw.values() for x in v]
        norm = statistics.fmean(flat) if flat else 1.0
        agg = {n: [x / norm if norm else 0.0 for x in v] for n, v in raw.items()}
    v_agg, w_agg, f_agg = eb_normal(agg)

    cw_s = {n: wc[n]["contested_won"] for n in adds}
    cw_n = {n: wc[n]["contested_entered"] for n in adds}
    v_cw, w_cw, f_cw = eb_binomial(cw_s, cw_n)

    zb_s = {n: wc[n]["zero_wins"] for n in adds}
    zb_n = {n: wc[n]["wins"] for n in adds}
    v_zb, w_zb, f_zb = eb_binomial(zb_s, zb_n)

    # max_bid: the MEAN of per-season maxima, not the all-time max. An all-time max over
    # eight seasons is mechanically larger than one over two — that is sample size, not
    # appetite, and it is exactly the bias shrinkage exists to remove.
    mx = {n: [wc[n]["maxbid"].get(y, 0.0) for y in sorted(wc[n]["seasons"])] for n in adds}
    v_mx, w_mx, f_mx = eb_normal(mx)

    pace = {n: [[x / budget if budget else 0.0 for x in wc[n]["pace"][y]]
                for y in sorted(wc[n]["seasons"])] for n in adds}
    v_pace, w_pace, _ = eb_vector(pace, n_weeks)

    # ── trade block ──────────────────────────────────────────────────────────
    full_trades = all(v == "playercards" for v in trade_detail.values()) and trade_detail
    if not full_trades and hist_seasons:
        warn.append(
            f"{ctx.key}: executed-trade item detail is only in playercards.json, which "
            f"has not been scraped for "
            f"{sorted(y for y, v in trade_detail.items() if v != 'playercards')}. "
            f"`rate_per_season`, `partners` and `pos_flow` are null there rather than 0 "
            f"— a TRADE_ACCEPT row names only the accepting team, so counting it per "
            f"manager would erase every proposer. `accept_rate` and `responds` are still "
            f"measured.")
    trates = {n: [tc[n]["trades"].get(y, 0) for y in sorted(tc[n]["seasons"])]
              for n in names if n in tc and tc[n]["seasons"]}
    v_tr, w_tr, f_tr = eb_normal(trates)

    ac_s = {n: tc[n]["accepted"] for n in trates}
    ac_n = {n: tc[n]["accepted"] + tc[n]["died"] for n in trates}
    v_ac, w_ac, f_ac = eb_binomial(ac_s, ac_n)

    positions = sorted({p for n in trates for p in tc[n]["flow"]})
    v_flow, w_flow = {}, {}
    for p in positions:
        series = {n: tc[n]["flow"].get(p, []) for n in trates}
        vp, wp, _ = eb_normal({k: v for k, v in series.items() if v})
        for n in trates:
            v_flow.setdefault(n, {})[p] = vp.get(n, 0.0)
            w_flow.setdefault(n, []).append(wp.get(n, 0.0))

    # ── drop block ───────────────────────────────────────────────────────────
    v_lat, w_lat, f_lat = eb_normal({n: dc[n] for n in dc if dc[n]})

    responds_default = bool(f_ac.get("mu", 0.0) > 0)

    # With NO history there is no prior to borrow either. Emit null, never 0.0: a
    # rendered "0 adds per season, $0 max bid" reads as a calibrated statement about a
    # passive manager, which is exactly the plausible-looking-wrong-default failure
    # CLAUDE.md warns about. null cannot be mistaken for a measurement.
    empty = not hist_seasons

    def R(v, nd=None):
        if empty or v is None:
            return None
        return round(v, nd) if nd is not None else round(v)
    # the league-average pace curve — also what a borrowed manager is handed
    mean_pace = [round(statistics.fmean([v_pace[n][i] for n in v_pace]), 4)
                 if v_pace else 0.0 for i in range(n_weeks)]

    def build(name):
        in_w, in_t = name in adds, name in trates
        n_seasons = len(set(wc.get(name, {}).get("seasons", set()))
                        | set(tc.get(name, {}).get("seasons", set())))
        weights = []
        for got, w in ((in_w, w_adds), (in_w, w_agg), (in_w, w_cw), (in_w, w_zb),
                       (in_w, w_mx), (in_w, w_pace), (in_t, w_tr), (in_t, w_ac),
                       (name in dc, w_lat)):
            weights.append(w.get(name, 0.0) if got else 0.0)
        if name in w_flow and w_flow[name]:
            weights.append(statistics.fmean(w_flow[name]))
        borrowed = not (in_w or in_t or name in dc)
        shrink = 1.0 if borrowed else round(1.0 - statistics.fmean(weights), 3)
        mine = (set(wc.get(name, {}).get("seasons", set()))
                | set(tc.get(name, {}).get("seasons", set())))
        return {
            "n_seasons": 0 if borrowed else n_seasons,
            "shrink": shrink,
            "borrowed": borrowed,
            "identity_assumed": bool(mine & set(assumed)),
            "seasons": sorted(mine),
            "waiver": {
                "adds_per_season": R(v_adds.get(name, f_adds["mu"]), 1),
                "aggression": R(v_agg.get(name, f_agg["mu"]), 3),
                "contested_win_rate": R(v_cw.get(name, f_cw["mu"]), 3),
                "zero_bid_share": (R(v_zb.get(name, f_zb["mu"]), 3)
                                   if model == AR.FAAB else None),
                "spend_pace": (None if empty else
                               [round(x, 4) for x in v_pace[name]]
                               if name in v_pace else list(mean_pace)),
                "max_bid": (R(v_mx.get(name, f_mx["mu"]))
                            if model == AR.FAAB else None),
            },
            "trade": {
                "rate_per_season": (R(v_tr.get(name, f_tr["mu"]), 2)
                                    if full_trades else None),
                "accept_rate": R(v_ac.get(name, f_ac["mu"]), 3),
                "partners": (dict(tc[name]["partners"].most_common())
                             if in_t and full_trades else {}),
                "pos_flow": ({p: round(x, 3) for p, x in
                              sorted(v_flow.get(name, {}).items())}
                             if full_trades else {}),
                "responds": (None if empty else
                         (tc[name]["accepted"] > 0) if in_t else responds_default),
            },
            "drop": {"latency_weeks": R(v_lat.get(name, f_lat["mu"]), 2)},
        }

    out = {}
    for name in names:
        if name.startswith("_"):
            continue
        out[name] = build(name)

    # the prior itself, as a manager-shaped record — what a newcomer is modelled as
    default = {
        "n_seasons": 0, "shrink": 1.0, "borrowed": True,
        "identity_assumed": bool(assumed), "seasons": [],
        "waiver": {
            "adds_per_season": R(f_adds["mu"], 1),
            "aggression": R(f_agg["mu"], 3),
            "contested_win_rate": R(f_cw["mu"], 3),
            "zero_bid_share": R(f_zb["mu"], 3) if model == AR.FAAB else None,
            "spend_pace": None if empty else mean_pace,
            "max_bid": R(f_mx["mu"]) if model == AR.FAAB else None,
        },
        "trade": {
            "rate_per_season": R(f_tr["mu"], 2) if full_trades else None,
            "accept_rate": R(f_ac["mu"], 3), "partners": {},
            "pos_flow": ({p: round(statistics.fmean(
                [v_flow[n].get(p, 0.0) for n in v_flow]) if v_flow else 0.0, 3)
                for p in positions} if full_trades else {}),
            "responds": None if empty else responds_default,
        },
        "drop": {"latency_weeks": R(f_lat["mu"], 2)},
    }

    for name in roster:
        if out.get(name, {}).get("borrowed", True):
            out[name] = json.loads(json.dumps(default))

    artifact = {
        "_meta": {
            "league": ctx.key, "season": ctx.season, "model": model,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seasons": waiver_seasons,
            "waiver_seasons": waiver_seasons,
            "trade_drop_seasons": hist_seasons,
            "acquisition_regimes": {str(k): v for k, v in
                                    sorted(AR.regime_map(ctx).items())},
            "regime_sources": {str(y): AR.regime_source(ctx, y)
                               for y in sorted(AR.regime_map(ctx))},
            "identity_sources": {str(k): v for k, v in sorted(identity.items())},
            "trade_detail_sources": {str(k): v for k, v in sorted(trade_detail.items())},
            "trade_item_detail_available": bool(full_trades),
            "teams_active_by_season": {str(k): v for k, v in sorted(sizes.items())},
            "regime_switches": [{"season": s, "from": a, "to": b}
                                for s, a, b in AR.switch_points(ctx)],
            "faab_budget": budget,
            "weeks": n_weeks,
            "method": "shrinkage-to-league-mean",
            "shrinkage": {
                "kind": "empirical-Bayes (method of moments)",
                "adds_per_season": dict(f_adds), "aggression": dict(f_agg),
                "contested_win_rate": dict(f_cw), "zero_bid_share": dict(f_zb),
                "max_bid": dict(f_mx), "rate_per_season": dict(f_tr),
                "accept_rate": dict(f_ac), "latency_weeks": dict(f_lat),
            },
            "definitions": {
                "adds_per_season": "won waiver claims + free-agent pickups per season",
                "aggression": ("share of the FAAB budget spent per season"
                               if model == AR.FAAB else
                               "claims entered per season / league mean (1.0 = average)"),
                "contested_win_rate": "claims won / claims entered that had >1 bidder",
                "zero_bid_share": "winning claims at $0 / all winning claims",
                "spend_pace": "share of the FAAB budget spent in week i+1, per season",
                "max_bid": "mean of per-season largest winning bids (not the all-time max)",
                "rate_per_season": "executed trades the manager was a side of",
                "accept_rate": "executed / (executed + offers received that died)",
                "pos_flow": "+1 received / -1 sent, averaged per player moved",
                "latency_weeks": "weeks between acquiring a player and dropping him",
            },
            "caveats": warn,
        },
        LEAGUE_DEFAULT_KEY: default,
    }
    artifact.update(out)
    return artifact, warn


# ── reporting ────────────────────────────────────────────────────────────────
def report(ctx, art):
    meta = art["_meta"]
    model = meta["model"]
    print(f"\n{ctx.key}  season {ctx.season}  model={model.upper()}")
    print(f"  waiver pool     : {meta['waiver_seasons'] or 'EMPTY'}")
    print(f"  trade/drop pool : {meta['trade_drop_seasons'] or 'EMPTY'}")
    for sw in meta["regime_switches"]:
        print(f"  regime switch   : {sw['season']} {sw['from']} -> {sw['to']}  "
              f"(seasons before it are excluded from the waiver block)")
    def f(v, nd=2, w=7):
        return f"{'  n/a':>{w}}" if v is None else f"{v:>{w}.{nd}f}"

    def line(name, t, tail=""):
        wv, tr = t["waiver"], t["trade"]
        print(f"  {name:22}{t['n_seasons']:>3}{t['shrink']:>8.2f}"
              f"{f(wv['adds_per_season'], 1)}{f(wv['aggression'])}"
              f"{f(wv['contested_win_rate'])}{f(wv['zero_bid_share'])}"
              f"{f(wv['max_bid'], 0, 8)}{f(tr['rate_per_season'])}"
              f"{f(tr['accept_rate'], 2, 6)}{f(t['drop']['latency_weeks'], 2, 6)}"
              f"  {tail}")

    roster = sorted(set(H.managers(ctx, ctx.season).values()))
    print(f"\n  {'manager':22}{'n':>3}{'shrink':>8}{'adds':>7}{'aggr':>7}"
          f"{'cwin':>7}{'zero':>7}{'maxbid':>8}{'trade':>7}{'acc':>6}{'lat':>6}  flag")
    for name in roster:
        t = art.get(name)
        if t:
            flags = " ".join(f for f in (
                "BORROWED" if t["borrowed"] else "",
                "id-assumed" if t.get("identity_assumed") else "") if f)
            line(name, t, flags)
    line("_league_default", art[LEAGUE_DEFAULT_KEY],
         "<- what a manager with no history gets")


def run(ctx, include_current=False, quiet=False):
    """Calibrate ONE league and write its artifact. -> the artifact dict, or None when
    the league has not been scraped yet. Never raises on missing data."""
    if not H.load_league(ctx, ctx.season):
        print(f"! {ctx.key}: no league_full.json for {ctx.season} — skipped "
              f"(run the season scrape first).")
        return None
    art, warn = calibrate(ctx, include_current)
    ctx.ensure_dirs()
    path = ctx.config(OUT_NAME)
    with open(path, "w") as f:
        json.dump(art, f, indent=2, sort_keys=False)
        f.write("\n")
    for w in warn:
        print(f"! {w}")
    if not quiet:
        report(ctx, art)
    n_cal = sum(1 for k, v in art.items()
                if not k.startswith("_") and not v["borrowed"])
    n_bor = sum(1 for k, v in art.items()
                if not k.startswith("_") and v["borrowed"])
    print(f"\n  wrote {os.path.relpath(path, ROOT)} "
          f"({n_cal} calibrated, {n_bor} borrowed)\n")
    return art


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--league", help="league key; default: every registered league")
    ap.add_argument("--include-current", action="store_true",
                    help="also calibrate on the in-progress season (biases rates low)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    import leagues
    ctxs = [leagues.resolve(args.league)] if args.league else leagues.all()
    for ctx in ctxs:
        run(ctx, include_current=args.include_current, quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
