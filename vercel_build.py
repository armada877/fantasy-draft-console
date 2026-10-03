#!/usr/bin/env python3
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config")
BUILD_REQUIREMENTS = os.path.join(ROOT, "requirements-build.txt")
BOARD_REQUIREMENTS = os.path.join(ROOT, "requirements-board.txt")
BUILD_DEPS = os.path.join(tempfile.gettempdir(), "draft-console-build-deps")
BUILD_MODULES = ("openpyxl",)
BOARD_MODULES = ("sklearn", "yaml")
UV_PATHS = (os.path.expanduser("~/.local/bin/uv"), "/usr/local/bin/uv")
TOOL_DATA = os.path.join(ROOT, "draft_sheets", "tool_data.json")
CONSOLE = os.path.join(ROOT, "draft_app", "static", "index.html")
BOARD = os.path.join(ROOT, "draft_app", "static", "board.html")
BOARDS_CONFIG = os.path.join(CONFIG, "boards.json")
REPORTERS = os.path.join(ROOT, "fftiers", "data", "bsky_reporters.json")
APP_REPORTERS = os.path.join(ROOT, "draft_app", "fftiers", "data", "bsky_reporters.json")
BOARD_STAGES = ("pull", "vbd-boards", "csg-boards", "board")


class BoardSkipped(Exception):
    pass


def fail(message):
    sys.exit(f"✗ vercel build: {message}")


def require_password_gate():
    if os.environ.get("CONSOLE_PASSWORD", "").strip():
        return
    if os.environ.get("ALLOW_PUBLIC_CONSOLE", "").strip() == "1":
        print("• CONSOLE_PASSWORD not set and ALLOW_PUBLIC_CONSOLE=1: the console is public.")
        return
    fail("CONSOLE_PASSWORD is not set, so the deploy would serve your league data and "
         "/api/advise to anyone. Set CONSOLE_PASSWORD, or set ALLOW_PUBLIC_CONSOLE=1 "
         "to deploy a public console.")


def write_league_config():
    raw = os.environ.get("LEAGUE_CONFIG_JSON", "").strip()
    if not raw:
        fail("LEAGUE_CONFIG_JSON is not set. Set it to the content of config/league.json "
             "(see config/league.example.json).")
    try:
        cfg = json.loads(raw)
    except ValueError as e:
        fail(f"LEAGUE_CONFIG_JSON is not valid JSON: {e}")
    if not isinstance(cfg, dict) or not cfg.get("sleeper_league_id"):
        fail("LEAGUE_CONFIG_JSON must be a JSON object with sleeper_league_id.")
    if not cfg.get("roster"):
        fail("LEAGUE_CONFIG_JSON must set roster: scrape_sleeper.py drops the K and DEF slots.")
    os.makedirs(CONFIG, exist_ok=True)
    with open(os.path.join(CONFIG, "league.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print("• wrote config/league.json from LEAGUE_CONFIG_JSON")
    return cfg


def missing_modules(modules):
    return [m for m in modules if importlib.util.find_spec(m) is None]


def install_commands(requirements):
    uv = shutil.which("uv") or next((p for p in UV_PATHS if os.path.exists(p)), None)
    if uv:
        yield [uv, "pip", "install", "--quiet", "--python", sys.executable,
               "--target", BUILD_DEPS, "-r", requirements]
    for py in dict.fromkeys([sys.executable, getattr(sys, "_base_executable", sys.executable)]):
        yield [py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
               "--target", BUILD_DEPS, "-r", requirements]


def install(requirements, modules, env):
    if not missing_modules(modules):
        return True
    for cmd in install_commands(requirements):
        print(f"$ {' '.join(cmd)}", flush=True)
        if subprocess.run(cmd).returncode == 0:
            paths = [BUILD_DEPS, *env.get("PYTHONPATH", "").split(os.pathsep)]
            env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(p for p in paths if p))
            return True
    return False


def build_env():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if not install(BUILD_REQUIREMENTS, BUILD_MODULES, env):
        fail("could not install requirements-build.txt (no uv, and no pip).")
    return env


def run_step(args, env):
    print(f"\n$ python {' '.join(args)}", flush=True)
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env).returncode == 0


def run(script, *args, env):
    if not run_step([os.path.join(ROOT, script), *args], env):
        fail(f"{script} failed")


def report_console():
    if not os.path.exists(CONSOLE):
        fail("draft_app/static/index.html is missing after inject.")
    with open(TOOL_DATA, encoding="utf-8") as f:
        data = json.load(f)
    print(f"\n✓ console built: {len(data.get('managers', []))} managers, "
          f"{len(data.get('players', []))} players, starters {data.get('starters')}")


def board_config(league_cfg):
    raw = os.environ.get("BOARDS_CONFIG_JSON", "").strip()
    if raw:
        try:
            cfg = json.loads(raw)
        except ValueError as e:
            raise BoardSkipped(f"BOARDS_CONFIG_JSON is not valid JSON: {e}")
        if not isinstance(cfg, dict) or not isinstance(cfg.get("leagues"), dict):
            raise BoardSkipped("BOARDS_CONFIG_JSON must be a JSON object with leagues.")
    else:
        user = league_cfg.get("sleeper_username") or league_cfg.get("me")
        if not user:
            raise BoardSkipped("LEAGUE_CONFIG_JSON has no sleeper_username, so the board "
                               "cannot find your team. Set it, or set BOARDS_CONFIG_JSON.")
        cfg = {"season": league_cfg.get("season"),
               "leagues": {"league": {"platform": "sleeper",
                                      "league_id": str(league_cfg["sleeper_league_id"]),
                                      "user": user, "yaml": "leagues/league.yaml"}}}
    leagues = {}
    for key, lg in cfg["leagues"].items():
        if lg.get("platform") == "sleeper":
            leagues[key] = lg
        else:
            print(f"• board: skipped league {key!r}: only Sleeper boards build on Vercel "
                  "(ESPN needs private cookies)")
    if not leagues:
        raise BoardSkipped("no Sleeper league in the board config.")
    cfg["leagues"] = leagues
    with open(BOARDS_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"• wrote config/boards.json: {', '.join(leagues)}")
    return leagues


def write_league_yamls(leagues, env):
    for key, lg in leagues.items():
        dest = os.path.join(ROOT, lg["yaml"])
        if os.path.exists(dest):
            continue
        if not run_step(["-m", "fftiers.sleeper_cli", "sync-league", str(lg["league_id"]),
                         "--dest", dest], env):
            raise BoardSkipped(f"could not write {lg['yaml']} from the Sleeper settings.")


def copy_reporters():
    os.makedirs(os.path.dirname(APP_REPORTERS), exist_ok=True)
    shutil.copyfile(REPORTERS, APP_REPORTERS)
    print("• copied the Wire's reporter list into draft_app/")


def build_board(league_cfg, env):
    leagues = board_config(league_cfg)
    if not install(BOARD_REQUIREMENTS, BOARD_MODULES, env):
        raise BoardSkipped("could not install requirements-board.txt.")
    write_league_yamls(leagues, env)
    if not run_step([os.path.join(ROOT, "pipeline.py"), *BOARD_STAGES], env):
        raise BoardSkipped("pipeline.py " + " ".join(BOARD_STAGES) + " failed.")
    if not os.path.exists(BOARD):
        raise BoardSkipped("draft_app/static/board.html is missing after the board stage.")
    copy_reporters()
    print(f"\n✓ manage board built: {os.path.getsize(BOARD):,} bytes")


def try_build_board(league_cfg, env):
    try:
        build_board(league_cfg, env)
    except BoardSkipped as e:
        print(f"\n✗ MANAGE BOARD NOT BUILT: {e}\n"
              "  The draft console still deploys. /manage returns 404 until a build "
              "makes the board.", flush=True)


def main():
    require_password_gate()
    league_cfg = write_league_config()
    env = build_env()
    run("scraping/scrape_sleeper.py", env=env)
    run("scraping/scrape_sleeper_history.py", env=env)
    run("scraping/scrape_sleeper_keepers.py", env=env)
    run("pipeline.py", "calibrate", "build", env=env)
    run("scraping/scrape_sleeper_status.py", env=env)
    run("pipeline.py", "build", "inject", env=env)
    report_console()
    try_build_board(league_cfg, env)


if __name__ == "__main__":
    main()
