#!/usr/bin/env python3
"""One entry point for the whole draft pipeline: parse -> model -> calibrate -> simulate
-> build -> inject. Runs the existing stage scripts in order; stops on the first failure.

    parse            model + calibrate                 build           serve
    scrape_league ─┐                       ┌ tendencies ┐
                   ├ *_elboberto.xlsm ─────┼─ build ────┴ tool_data.json ─ inject ─ console
    scrape (deep)  ┘  extract_elboberto ───┘   ▲
                      → elboberto_projections   config/league.json
                            └ calibrate ─ config/tendencies.json  [local]

Usage:
    python3 pipeline.py all                 # local refresh: calibrate -> csg -> build -> inject
    python3 pipeline.py build inject        # rebuild the console from current data only
    python3 pipeline.py calibrate           # opponents -> config/tendencies.json
    python3 pipeline.py csg                 # CSG sheet -> csg_consensus.json (market view)
    python3 pipeline.py simulate            # agent-auction strategy test (stdout)
    python3 pipeline.py scrape calibrate build inject   # full refresh from ESPN
    python3 pipeline.py backfill validate   # in-season: re-score the projection baseline

Stages run in the order you list them. Flags:
    --deep      with `scrape`: also pull full multi-season history (scraping/scrape.py)
    --stress    with `simulate`: also run the strategy stress test (a19)

"start fresh" vs "refresh": `all` runs opponent calibration only when the local analysis
pipeline AND scraped history are present; otherwise it skips calibration (every opponent
stays neutral) and still builds a working console. So a brand-new league with no history
runs `all` fine — you just get neutral opponents until you have auction history to calibrate.
"""
import argparse
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
DRAFT_STAGES = ("scrape", "calibrate", "csg", "simulate", "build", "inject", "all")
# In-season stages. Each is league-scoped and runs for every league on the ESPN
# account (leagues.all()) unless --league narrows it.
SEASON_STAGES = ("season-scrape", "sources", "season-calibrate", "season-build",
                 "season-inject", "data-console", "backfill", "validate", "week")
STAGES = DRAFT_STAGES + SEASON_STAGES

TEMPLATE = os.path.join(ROOT, "draft_sheets", "draft_tool_template.html")
TOOL_DATA = os.path.join(ROOT, "draft_sheets", "tool_data.json")
STATIC = os.path.join(ROOT, "draft_app", "static")
PROJECTIONS = os.path.join(ROOT, "draft_sheets", "elboberto_projections.json")


def run(*cmd):
    """Run a stage script from the repo root; abort the pipeline if it fails."""
    print(f"\n\033[1m$ {' '.join(os.path.relpath(c, ROOT) if os.path.isabs(c) else c for c in cmd)}\033[0m",
          flush=True)  # flush so the banner prints before the child's own output
    if subprocess.run(cmd, cwd=ROOT).returncode != 0:
        sys.exit(f"\n✗ stage failed: {' '.join(cmd)}")


def have_calibration():
    """True when the (local) calibration pipeline can run: its script + scraped history."""
    if not os.path.exists(os.path.join(ROOT, "analysis", "calibrate.py")):
        return False
    return bool(glob.glob(os.path.join(ROOT, "scraping", "raw", "*", "league_full.json")))


# ───────────────────────────────── stages ─────────────────────────────────
def scrape(args):
    run(PY, os.path.join(ROOT, "scraping", "scrape_league.py"))
    if args.deep:
        run(PY, os.path.join(ROOT, "scraping", "scrape.py"))


def calibrate(args):
    if not have_calibration():
        print("• calibrate: skipped — no local analysis/ pipeline or no scraped history "
              "(scraping/raw/*/league_full.json). Opponents stay neutral.")
        return
    # projections cache the calibration reads (regenerated from the tracked workbooks)
    run(PY, os.path.join(ROOT, "draft_sheets", "extract_elboberto_master.py"))
    run(PY, os.path.join(ROOT, "analysis", "calibrate.py"))


def csg(args):
    """Complementary market view: the CSG sheet's consensus columns -> csg_consensus.json.

    Self-skips when no CSG workbook is present, so `all` stays valid for anyone who
    doesn't use the sheet. Advisory only — it never feeds the console's worth/vbd.
    """
    if not glob.glob(os.path.join(ROOT, "draft_sheets", "CSG*auction.xlsm")):
        print("• csg: skipped — no draft_sheets/CSG*auction.xlsm (market view is optional).")
        return
    run(PY, os.path.join(ROOT, "draft_sheets", "extract_csg.py"))


def simulate(args):
    if not have_calibration():
        sys.exit("simulate needs the local analysis/ pipeline and scraped history.")
    run(PY, os.path.join(ROOT, "analysis", "a18_agent_auction.py"))
    if args.stress:
        run(PY, os.path.join(ROOT, "analysis", "a19_stress_test.py"))


def build(args):
    run(PY, os.path.join(ROOT, "draft_sheets", "build_tool_data.py"))


def inject(args):
    """Golden rule: the served console is generated — template + injected data, never hand-edited."""
    if not os.path.exists(TOOL_DATA):
        sys.exit(f"inject: {os.path.relpath(TOOL_DATA, ROOT)} missing — run `build` first.")
    tpl = open(TEMPLATE).read()
    data = open(TOOL_DATA).read()
    if "/*DATA*/" not in tpl:
        sys.exit("inject: template is missing the /*DATA*/ marker.")
    os.makedirs(STATIC, exist_ok=True)
    with open(os.path.join(STATIC, "index.html"), "w") as f:
        f.write(tpl.replace("/*DATA*/", data))
    with open(os.path.join(STATIC, "data.json"), "w") as f:
        f.write(data)
    print(f"• inject: wrote {os.path.relpath(os.path.join(STATIC, 'index.html'), ROOT)} "
          f"and static/data.json ({len(data):,} bytes of data)")


def do_all(args):
    calibrate(args)   # self-skips for a fresh league
    csg(args)         # self-skips when the CSG sheet isn't present
    build(args)
    inject(args)


# ──────────────────────────── in-season stages ────────────────────────────
# The in-season tool is multi-league: `leagues.all()` is discovered from your ESPN
# cookies, so these stages fan out over every team you manage. --league narrows them.

def _league_args(args):
    return ["--league", args.league] if getattr(args, "league", None) else []


def season_scrape(args):
    """ESPN -> rosters, free-agent pool, transactions, bye weeks, per-week projections.

    Completed seasons and completed weeks are cached IMMUTABLE — fetched once, ever —
    so a weekly refresh only pays for what can still change.
    """
    run(PY, os.path.join(ROOT, "scraping", "scrape_season.py"), *_league_args(args))
    # Per-week projections. Without these every player collapses to one flat season
    # rate and the lineup planner cannot tell week 3 from week 11.
    run(PY, os.path.join(ROOT, "scraping", "scrape_weekly_proj.py"), *_league_args(args))


def sources(args):
    """External research adapters -> raw/{league}/{season}/sources/*.json."""
    cmd = [PY, "-m", "scraping.sources", *_league_args(args)]
    if getattr(args, "refresh", False):
        cmd.append("--refresh")
    run(*cmd)


def season_calibrate(args):
    """Transaction history -> season_tendencies.json + faab/priority curve."""
    run(PY, os.path.join(ROOT, "analysis", "calibrate_season.py"), *_league_args(args))
    run(PY, os.path.join(ROOT, "analysis", "faab_curve.py"), *_league_args(args))


def season_build(args):
    """Valuation + tendencies + sources -> out/{league}/season_data.json."""
    # build_season_data requires an explicit --all rather than defaulting to every league
    scope = _league_args(args) or ["--all"]
    run(PY, os.path.join(ROOT, "draft_sheets", "build_season_data.py"), *scope)


def season_inject(args):
    """Templates + season_data -> static/home.html and static/l/{league}/season.html."""
    run(PY, os.path.join(ROOT, "draft_sheets", "inject_season.py"), *_league_args(args))


def data_console(args):
    """Pipeline status + research coverage -> the data/research console."""
    run(PY, os.path.join(ROOT, "analysis", "pipeline_status.py"), *_league_args(args))
    run(PY, os.path.join(ROOT, "draft_sheets", "inject_data_console.py"), *_league_args(args))


def backfill(args):
    """WS-8a — historical weekly boxscores: the ground truth every backtest needs.

    Completed weeks are cached IMMUTABLE, so this is free after the first run and
    only pays for the weeks that have been played since.
    """
    run(PY, os.path.join(ROOT, "scraping", "backfill_weeks.py"), *_league_args(args))


def validate(args):
    """WS-8b/c/e — score the projection baseline, every candidate correction, and
    every external source against what actually happened.

    Run it AFTER `backfill`, and expect null results: as of 2026-09-15 no correction
    survives, so the baseline ships unmodified. That is the system working.
    """
    backfill(args)
    run(PY, os.path.join(ROOT, "analysis", "backtest_projections.py"), *_league_args(args))
    run(PY, os.path.join(ROOT, "analysis", "backtest_lineups.py"), *_league_args(args))
    run(PY, os.path.join(ROOT, "analysis", "backtest_sources.py"), *_league_args(args))


def week(args):
    """The weekly refresh: everything needed before a waiver run.

    Calibration is seasonal, not weekly, so it is skipped unless --calibrate is
    passed — it reads years of transactions and does not change between Tuesdays.
    """
    season_scrape(args)
    sources(args)
    if getattr(args, "calibrate", False):
        season_calibrate(args)
    # One more played week of ground truth, every week. Cheap (the completed weeks
    # are already cached) and it is what keeps the projection policy's evidence
    # current instead of frozen at whatever the last manual run measured.
    backfill(args)
    season_build(args)
    season_inject(args)
    data_console(args)


DISPATCH = {"scrape": scrape, "calibrate": calibrate, "csg": csg, "simulate": simulate,
            "build": build, "inject": inject, "all": do_all,
            "season-scrape": season_scrape, "sources": sources,
            "season-calibrate": season_calibrate, "season-build": season_build,
            "season-inject": season_inject, "data-console": data_console,
            "backfill": backfill, "validate": validate, "week": week}


def main():
    ap = argparse.ArgumentParser(
        description="Run the draft pipeline end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="stages: " + " ".join(STAGES))
    ap.add_argument("stages", nargs="+", choices=STAGES, metavar="STAGE",
                    help="one or more of: " + ", ".join(STAGES))
    ap.add_argument("--deep", action="store_true", help="with scrape: also pull full history")
    ap.add_argument("--stress", action="store_true", help="with simulate: also run a19 stress test")
    ap.add_argument("--league", metavar="KEY",
                    help="in-season stages: limit to one league (default: all discovered)")
    ap.add_argument("--refresh", action="store_true",
                    help="with sources: bypass the cache TTL and re-fetch")
    ap.add_argument("--calibrate", action="store_true",
                    help="with week: also re-run season-calibrate (seasonal, not weekly)")
    args = ap.parse_args()

    print(f"pipeline: {' -> '.join(args.stages)}")
    for stage in args.stages:
        DISPATCH[stage](args)
    print("\n✓ done.")


if __name__ == "__main__":
    main()
