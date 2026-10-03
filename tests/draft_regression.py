#!/usr/bin/env python3
"""Regression guard for the EXISTING draft console.

The in-season work refactors code the draft console depends on (build_tool_data,
analysis/lib, pipeline). This asserts the console's generated payload does not
change by so much as a byte while that happens.

    python3 tests/draft_regression.py --bless   # record the current output as golden
    python3 tests/draft_regression.py           # rebuild and compare against golden

The golden hashes depend on LOCAL inputs (config/, csg_consensus.json, the .xlsm), so
they are machine-specific by design: bless once on your box before refactoring, then
run after every change. Verified deterministic — two consecutive builds hash equal.
"""
import hashlib
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GOLDEN = os.path.join(ROOT, "tests", "draft_golden.json")
ARTIFACTS = [
    "draft_sheets/tool_data.json",
    "draft_app/static/index.html",
    "draft_app/static/data.json",
]
PY_BIN = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY_BIN):
    PY_BIN = sys.executable


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def build():
    r = subprocess.run([PY_BIN, os.path.join(ROOT, "pipeline.py"), "build", "inject"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        sys.exit("draft pipeline FAILED to run — that is itself a regression")
    return r.stdout


def snapshot():
    out = {}
    for rel in ARTIFACTS:
        p = os.path.join(ROOT, rel)
        out[rel] = {"sha256": sha(p), "bytes": os.path.getsize(p)} if os.path.exists(p) else None
    return out


def main():
    bless = "--bless" in sys.argv
    build()
    current = snapshot()
    if bless:
        with open(GOLDEN, "w") as f:
            json.dump(current, f, indent=2, sort_keys=True)
            f.write("\n")
        print("blessed:")
        for k, v in sorted(current.items()):
            print(f"  {v['sha256'][:16] if v else 'MISSING':16s}  {v['bytes'] if v else 0:>8}  {k}")
        return 0
    if not os.path.exists(GOLDEN):
        sys.exit("no golden yet — run with --bless first")
    with open(GOLDEN) as f:
        golden = json.load(f)
    bad = []
    for rel in ARTIFACTS:
        g, c = golden.get(rel), current.get(rel)
        if g != c:
            bad.append(f"{rel}: golden {g and g['sha256'][:12]} ({g and g['bytes']}b) "
                       f"-> now {c and c['sha256'][:12]} ({c and c['bytes']}b)")
        else:
            print(f"  OK  {rel}")
    if bad:
        print("\nDRAFT CONSOLE REGRESSION:")
        for b in bad:
            print("  " + b)
        return 1
    print("\ndraft console unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
