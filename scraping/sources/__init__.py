#!/usr/bin/env python3
"""The research engine — Tier-A adapters for external fantasy data.

One module per source, one uniform contract:

    fetch(ctx, week=None, *, refresh=False, ttl=..., write=True, profile=None) -> dict

returning the frozen WS-2 envelope from docs/contracts.md:

    {"source","fetched","season","week","scoring","ok","error","coverage","records"}

and cached to `ctx.raw("sources/{name}.json")`.

Registered sources
------------------
| name          | gives                                                          |
|---------------|----------------------------------------------------------------|
| `fantasypros` | rest-of-season expert consensus (ECR) + this week's consensus  |
| `borischen`   | weekly tiers + rank standard deviation                         |
| `sleeper`     | trending adds/drops, league-independent, hours ahead of ESPN   |
| `fantasycalc` | redraft trade values, priced for THIS league's settings        |
| `nflverse`    | snap share, target share, injuries, and the espn_id crosswalk  |

Three rules hold for all of them:

1. **Self-skipping.** A source that is down returns `ok:false` with an error string.
   Nothing in here raises past `common.run_adapter`, so no third-party outage can
   fail a pipeline. Coverage of a failed source is 0 — visibly, not silently.
2. **Parameterised by the league, never by a constant.** Scoring variant, team
   count, QB slots and which positions to pull all come off `LeagueProfile`.
3. **Advisory, never the valuation.** `ros_points` / `marginal` stay computed from
   the league's own scraped ESPN scoring. What these add is the *disagreement*.

CLI
---
    python3 -m scraping.sources                      # all leagues, all sources
    python3 -m scraping.sources --league 2kdome      # one league
    python3 -m scraping.sources --source fantasypros --refresh
    python3 -m scraping.sources --week 3 --list
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from . import borischen, common, fantasycalc, fantasypros, nflverse, sleeper  # noqa: E402

REGISTRY = {
    fantasypros.NAME: fantasypros,
    borischen.NAME: borischen,
    sleeper.NAME: sleeper,
    fantasycalc.NAME: fantasycalc,
    nflverse.NAME: nflverse,
}
# fetch order: nflverse first so the espn_id crosswalk is warm before the
# name-only sources need it.
ORDER = [nflverse.NAME, fantasypros.NAME, borischen.NAME, sleeper.NAME, fantasycalc.NAME]


def fetch(name, ctx, week=None, **kw) -> dict:
    """Run one adapter. Returns an envelope; never raises for a source problem."""
    mod = REGISTRY.get(name)
    if mod is None:
        raise KeyError(f"unknown source {name!r}; have {sorted(REGISTRY)}")
    return mod.fetch(ctx, week, **kw)


def fetch_all(ctx, week=None, names=None, **kw) -> dict:
    """Every registered adapter for one league -> {name: envelope}."""
    prof = kw.pop("profile", None)
    if prof is None:
        prof = common.league_profile(ctx)
    out = {}
    for name in (names or ORDER):
        out[name] = fetch(name, ctx, week, profile=prof, **kw)
    return out


# ── reporting ────────────────────────────────────────────────────────────────
def coverage_line(name, env) -> str:
    if not env.get("ok"):
        return f"  {name:12s} SKIPPED — {env.get('error')}"
    c = env.get("coverage") or {}
    m, t = c.get("matched", 0), c.get("total", 0)
    pct = f"{100 * m / t:.0f}%" if t else "n/a"
    pool = c.get("pct_of_pool")
    pool_s = f", {100 * pool:.0f}% of pool" if pool is not None else ""
    cached = " [cached]" if (env.get("_cache") or {}).get("hit") else ""
    return (f"  {name:12s} {m}/{t} matched ({pct}{pool_s}) "
            f"· {len(env.get('records') or [])} records{cached}")


def report(ctx, envs, profile=None) -> None:
    prof = profile or common.league_profile(ctx)
    head = (f"{ctx.name} [{ctx.key}]  season {ctx.season}"
            + (f"  ·  {prof.size} teams · {prof.scoring_label} · "
               f"{sum(n for s, n in prof.starting_slots.items() if s == 0)}QB" if prof else ""))
    print(f"\n\033[1m{head}\033[0m")
    for name in ORDER:
        if name in envs:
            print(coverage_line(name, envs[name]))
    basis = next((e["coverage"].get("basis") for e in envs.values()
                  if e.get("ok") and e.get("coverage")), None)
    if basis:
        print(f"  {'':12s} pool basis: {basis}")


def variants(ctx, profile=None) -> dict:
    """What THIS league will actually fetch — the proof that nothing is hardcoded."""
    prof = profile or common.league_profile(ctx)
    if not prof:
        return {}
    qbs, teams, ppr = fantasycalc.params_for(prof)
    return {
        "league": ctx.key, "size": prof.size, "scoring": prof.scoring_label,
        "fantasypros_ros": fantasypros.ros_url(prof.scoring_label),
        "borischen": [borischen.url_for(p, prof.scoring_label).rsplit("/", 1)[-1]
                      for p in borischen.positions_for(prof)],
        "fantasycalc": fantasycalc.url_for(prof),
        "fantasycalc_params": {"numQbs": qbs, "numTeams": teams, "ppr": ppr},
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv=None) -> int:
    import leagues

    ap = argparse.ArgumentParser(
        prog="python3 -m scraping.sources",
        description="Fetch external research sources for one or more leagues.")
    ap.add_argument("--league", "-l", action="append", metavar="KEY",
                    help="league key (repeatable). Default: every registered league.")
    ap.add_argument("--source", "-s", action="append", metavar="NAME",
                    choices=sorted(REGISTRY),
                    help="source name (repeatable). Default: all.")
    ap.add_argument("--week", "-w", type=int, default=None,
                    help="scoring period. Default: Sleeper's live NFL week.")
    ap.add_argument("--refresh", action="store_true",
                    help="bypass the cache TTL and re-fetch everything.")
    ap.add_argument("--ttl", type=int, default=None,
                    help="override every source's cache TTL, in seconds.")
    ap.add_argument("--deep", action="store_true",
                    help="nflverse: also pull the 49MB depth-chart snapshot series.")
    ap.add_argument("--list", action="store_true",
                    help="print the per-league source variants and exit (no network fetch).")
    args = ap.parse_args(argv)

    try:
        ctxs = ([leagues.resolve(k) for k in args.league] if args.league
                else leagues.all())
    except KeyError as e:
        print(f"✗ {e}")
        return 2
    if not ctxs:
        print("• sources: skipped — no leagues registered (run `python3 leagues.py`).")
        return 0

    week = args.week if args.week is not None else sleeper.current_week()
    names = args.source or ORDER

    if args.list:
        for ctx in ctxs:
            v = variants(ctx)
            if not v:
                print(f"{ctx.key}: no profile available")
                continue
            print(f"\n\033[1m{ctx.key}\033[0m  {v['size']} teams · {v['scoring']}")
            print(f"  fantasypros  {v['fantasypros_ros']}")
            print(f"  borischen    {' '.join(v['borischen'])}")
            print(f"  fantasycalc  numQbs={v['fantasycalc_params']['numQbs']} "
                  f"numTeams={v['fantasycalc_params']['numTeams']} "
                  f"ppr={v['fantasycalc_params']['ppr']}")
        return 0

    print(f"sources: week {week} · {len(ctxs)} league(s) · {len(names)} source(s)"
          + (" · --refresh" if args.refresh else ""))
    kw = {"refresh": args.refresh, "deep": args.deep}
    if args.ttl is not None:
        kw["ttl"] = args.ttl
    failures = 0
    for ctx in ctxs:
        ctx.ensure_dirs()
        prof = common.league_profile(ctx)
        envs = {}
        for name in names:
            envs[name] = fetch(name, ctx, week, profile=prof, **kw)
            if not envs[name].get("ok"):
                failures += 1
        report(ctx, envs, profile=prof)
        print(f"  {'':12s} -> {os.path.relpath(common.cache_file(ctx, 'NAME'), ROOT)}")
    # Self-skipping by design: a dead source is reported, never fatal.
    print(f"\n✓ sources done ({failures} source-league pair(s) skipped)."
          if failures else "\n✓ sources done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
