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
    python3 pipeline.py all                 # local refresh: calibrate -> build -> inject
    python3 pipeline.py build inject        # rebuild the console from current data only
    python3 pipeline.py calibrate           # opponents -> config/tendencies.json
    python3 pipeline.py simulate            # agent-auction strategy test (stdout)
    python3 pipeline.py scrape calibrate build inject   # full refresh from ESPN

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
import json
import os
import subprocess
import sys

# Windows consoles default to a legacy codepage (cp1252) that cannot encode the status
# glyphs this pipeline and its stage scripts print (✓ ✗ • × →). Force UTF-8 on our own
# streams, and via the environment on every child stage, so output is not locale-dependent.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
STAGES = ("scrape", "calibrate", "simulate", "build", "inject", "all")

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
    """True when the (local) calibration pipeline can run: its script + scraped auction
    history — meaning at least one scraped season that actually contains draft picks.

    The mere existence of a league_full.json is NOT enough: every fresh-setup scraper
    (scrape_league.py, scrape_sleeper.py) writes settings + managers for the CURRENT
    season with no draftDetail, while build_agents() reads PRIOR seasons' picks. Checking
    only for the file made `all` and `calibrate` die with FileNotFoundError on a
    brand-new league, instead of skipping calibration the way the README promises.
    """
    if not os.path.exists(os.path.join(ROOT, "analysis", "calibrate.py")):
        return False
    for path in glob.glob(os.path.join(ROOT, "scraping", "raw", "*", "league_full.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(d, list):
            d = d[0] if d else {}
        if (d.get("draftDetail") or {}).get("picks"):
            return True
    return False


# ───────────────────────────────── stages ─────────────────────────────────
def scrape(args):
    run(PY, os.path.join(ROOT, "scraping", "scrape_league.py"))
    if args.deep:
        run(PY, os.path.join(ROOT, "scraping", "scrape.py"))


def calibrate(args):
    if not have_calibration():
        print("• calibrate: skipped — no local analysis/ pipeline, or no scraped season "
              "with draft picks (scraping/raw/*/league_full.json → draftDetail.picks). "
              "Opponents stay neutral.")
        return
    # projections cache the calibration reads (regenerated from the tracked workbooks)
    run(PY, os.path.join(ROOT, "draft_sheets", "extract_elboberto_master.py"))
    run(PY, os.path.join(ROOT, "analysis", "calibrate.py"))


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
    # encoding is explicit: the template carries non-ASCII glyphs (◎ ☾ ⊘, ·) and Python
    # defaults to the locale codec on Windows (cp1252), which cannot read or write them.
    tpl = open(TEMPLATE, encoding="utf-8").read()
    data = open(TOOL_DATA, encoding="utf-8").read()
    if "/*DATA*/" not in tpl:
        sys.exit("inject: template is missing the /*DATA*/ marker.")
    os.makedirs(STATIC, exist_ok=True)
    with open(os.path.join(STATIC, "index.html"), "w", encoding="utf-8") as f:
        f.write(tpl.replace("/*DATA*/", data))
    with open(os.path.join(STATIC, "data.json"), "w", encoding="utf-8") as f:
        f.write(data)
    print(f"• inject: wrote {os.path.relpath(os.path.join(STATIC, 'index.html'), ROOT)} "
          f"and static/data.json ({len(data):,} bytes of data)")


def do_all(args):
    calibrate(args)   # self-skips for a fresh league
    build(args)
    inject(args)


DISPATCH = {"scrape": scrape, "calibrate": calibrate, "simulate": simulate,
            "build": build, "inject": inject, "all": do_all}


def main():
    ap = argparse.ArgumentParser(
        description="Run the draft pipeline end-to-end.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="stages: " + " ".join(STAGES))
    ap.add_argument("stages", nargs="+", choices=STAGES, metavar="STAGE",
                    help="one or more of: " + ", ".join(STAGES))
    ap.add_argument("--deep", action="store_true", help="with scrape: also pull full history")
    ap.add_argument("--stress", action="store_true", help="with simulate: also run a19 stress test")
    args = ap.parse_args()

    print(f"pipeline: {' -> '.join(args.stages)}")
    for stage in args.stages:
        DISPATCH[stage](args)
    print("\n✓ done.")


if __name__ == "__main__":
    main()
