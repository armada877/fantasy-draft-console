#!/usr/bin/env python3
"""Extract every tracked Elboberto projection workbook into one JSON, keyed by year.

The checked-in `*_elboberto.xlsm` baselines (2022-2026) all use the same layout, so
this reuses build_tool_data.py's CheatSheet reader across every year. Output is the
universal projection history used by the (local) analysis pipeline for projected-vs-
realized value work. These are the workbook's OWN computed values (not adjusted to a
particular league) — fine as a historical baseline.

Output: draft_sheets/elboberto_projections.json
  { "2026": [{name, pos, tier, proj_value, start_vbd, fpts}], "2025": [...], ... }

Field names match what the analysis pipeline reads (a18_agent_auction / calibrate /
research a7-a16 key on `proj_value` and `start_vbd`): read_cheatsheet's `worth`/`vbd`
are the workbook's projected auction-$ and value-over-replacement, so they are emitted
here as `proj_value`/`start_vbd` respectively.

For a single current-year, league-adjusted console build you don't need this — run
build_tool_data.py (which recomputes valuation from your scraped league).
"""
import glob
import json
import os
import re

from build_tool_data import _wb, read_cheatsheet

HERE = os.path.dirname(os.path.abspath(__file__))


def year_of(path):
    m = re.search(r"(20\d{2})", os.path.basename(path))
    return m.group(1) if m else os.path.basename(path)


def main():
    files = sorted(glob.glob(os.path.join(HERE, "*_elboberto.xlsm")))
    if not files:
        raise SystemExit("No *_elboberto.xlsm workbooks found in draft_sheets/.")
    out = {}
    for path in files:
        # read_cheatsheet yields worth/vbd/fpts; rename to the keys the analysis
        # pipeline consumes (proj_value/start_vbd) so this is its single source of truth.
        players = [{"name": p["name"], "pos": p["pos"], "tier": p["tier"],
                    "proj_value": p["worth"], "start_vbd": p["vbd"], "fpts": p["fpts"]}
                   for p in read_cheatsheet(_wb(path))]
        y = year_of(path)
        out[y] = players
        counts = {}
        for p in players:
            counts[p["pos"]] = counts.get(p["pos"], 0) + 1
        print(f"{y}: {len(players)} players {counts}  ({os.path.basename(path)})")
    dst = os.path.join(HERE, "elboberto_projections.json")
    json.dump(out, open(dst, "w"), indent=1)
    print("wrote", dst)


if __name__ == "__main__":
    main()
