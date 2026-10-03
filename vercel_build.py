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
BUILD_DEPS = os.path.join(tempfile.gettempdir(), "draft-console-build-deps")
BUILD_MODULES = ("openpyxl",)
UV_PATHS = (os.path.expanduser("~/.local/bin/uv"), "/usr/local/bin/uv")
TOOL_DATA = os.path.join(ROOT, "draft_sheets", "tool_data.json")
CONSOLE = os.path.join(ROOT, "draft_app", "static", "index.html")


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


def missing_build_modules():
    return [m for m in BUILD_MODULES if importlib.util.find_spec(m) is None]


def install_commands():
    uv = shutil.which("uv") or next((p for p in UV_PATHS if os.path.exists(p)), None)
    if uv:
        yield [uv, "pip", "install", "--quiet", "--python", sys.executable,
               "--target", BUILD_DEPS, "-r", BUILD_REQUIREMENTS]
    for py in dict.fromkeys([sys.executable, getattr(sys, "_base_executable", sys.executable)]):
        yield [py, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
               "--target", BUILD_DEPS, "-r", BUILD_REQUIREMENTS]


def build_env():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    if not missing_build_modules():
        return env
    for cmd in install_commands():
        print(f"$ {' '.join(cmd)}", flush=True)
        if subprocess.run(cmd).returncode == 0:
            break
    else:
        fail("could not install requirements-build.txt (no uv, and no pip).")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (BUILD_DEPS, env.get("PYTHONPATH")) if p)
    return env


def run(script, *args, env):
    print(f"\n$ python {script} {' '.join(args)}".rstrip(), flush=True)
    if subprocess.run([sys.executable, os.path.join(ROOT, script), *args],
                      cwd=ROOT, env=env).returncode != 0:
        fail(f"{script} failed")


def report_console():
    if not os.path.exists(CONSOLE):
        fail("draft_app/static/index.html is missing after inject.")
    with open(TOOL_DATA, encoding="utf-8") as f:
        data = json.load(f)
    print(f"\n✓ console built: {len(data.get('managers', []))} managers, "
          f"{len(data.get('players', []))} players, starters {data.get('starters')}")


def main():
    require_password_gate()
    write_league_config()
    env = build_env()
    run("scraping/scrape_sleeper.py", env=env)
    run("scraping/scrape_sleeper_history.py", env=env)
    run("scraping/scrape_sleeper_keepers.py", env=env)
    run("pipeline.py", "calibrate", "build", env=env)
    run("scraping/scrape_sleeper_status.py", env=env)
    run("pipeline.py", "build", "inject", env=env)
    report_console()


if __name__ == "__main__":
    main()
