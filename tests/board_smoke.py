#!/usr/bin/env python3
"""Smoke test for the manage board (fftiers.board build + inject).

Runs an offline build against the synthetic fixtures in tests/fixtures_board/
(fake players, a fake league) and asserts the viz-data schema and the injected
HTML. No network, no real league data.

    .venv/bin/python tests/board_smoke.py
"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures_board")
PY_BIN = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY_BIN):
    PY_BIN = sys.executable


def ok(msg):
    print(f"  OK  {msg}")


def main():
    tmp = tempfile.mkdtemp(prefix="board_smoke_")
    viz_path = os.path.join(tmp, "viz-data.json")
    dest = os.path.join(tmp, "board.html")
    r = subprocess.run(
        [PY_BIN, "-m", "fftiers.board", "build",
         "--config", os.path.join(FIX, "boards.json"),
         "--data-dir", os.path.join(FIX, "dat"),
         "--out-dir", os.path.join(FIX, "out"),
         "--viz-out", viz_path, "--dest", dest, "--no-push",
         "--template", os.path.join(ROOT, "draft_sheets", "board_template.html")],
        cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        sys.exit("board build FAILED against the fixtures")
    ok("build ran against fixtures")

    with open(viz_path) as f:
        data = json.load(f)

    asof = data["asof"]
    for k in ("week", "built", "weeks_left", "final_week", "sources"):
        assert k in asof, f"asof missing {k}: {asof}"
    assert asof["week"] == 2 and asof["weeks_left"] == 16, asof
    ok(f"asof: week {asof['week']}, weeks_left {asof['weeks_left']}, built {asof['built']}")

    L = data["leagues"]["example"]
    for k in ("label", "size", "scoring", "lineup", "team", "slots", "roster", "players"):
        assert k in L, f"league missing {k}"
    assert L["label"] == "Example League" and L["size"] == 12, (L["label"], L["size"])
    assert L["scoring"] == "standard" and "FLEX" in L["lineup"], (L["scoring"], L["lineup"])
    assert ["QB", 1] in L["slots"] and not any(s[0] in ("BENCH", "IR") for s in L["slots"]), L["slots"]
    assert L["roster"] and len(L["roster"][0]) == 5, L["roster"][:1]
    ok(f"league shape: {L['label']} / {L['size']} tm / {L['scoring']} / {L['lineup']}")

    assert L["players"], "no players built"
    for p in L["players"]:
        for k in ("name", "pos", "mine", "fa", "week", "ros"):
            assert k in p, f"player missing {k}: {p}"
        for hz in ("week", "ros"):
            assert set(p[hz]) == {"boris", "elb", "csg"}, p[hz]
    by_name = {p["name"]: p for p in L["players"]}
    aq = by_name["Alpha Quarterback"]
    assert aq["mine"] and not aq["fa"], aq
    assert aq["ros"]["boris"] and aq["ros"]["elb"] and aq["ros"]["csg"], aq["ros"]
    assert by_name["Golf Wideout"]["fa"], by_name["Golf Wideout"]
    ok(f"{len(L['players'])} players; mine/fa flags and both horizons present")

    with open(dest) as f:
        html = f.read()
    assert "Alpha Quarterback" in html, "fixture player missing from injected HTML"
    assert "/*DATA*/" not in html, "injection left the /*DATA*/ marker behind"
    assert "window.claude" not in html, "window.claude survived in the template"
    ok("injected HTML: fixture player present, no /*DATA*/ marker, no window.claude")

    print("\nboard smoke: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
