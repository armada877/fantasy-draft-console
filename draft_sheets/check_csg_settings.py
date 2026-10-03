#!/usr/bin/env python3
"""Diff the CSG sheet's league settings against your ACTUAL scraped ESPN league.

Why this exists instead of a writer: the CSG workbook is a ~3.4MB macro sheet whose
every value is a formula. openpyxl cannot recalculate formulas and drops this file's
conditional-formatting and data-validation extensions on save (it says so on load), so
writing settings programmatically would leave a sheet that LOOKS updated while every
VBD/price below stayed cached at the old settings — the worst possible failure, because
it is silent. So: this reports what to change, you change it in Excel (where the formulas
recalc natively), then re-run to confirm. Re-run after any CSG version bump too.

    config/league.json + scraping/raw/<season>/league_full.json   (truth)
                          vs
    CSG*-auction.xlsm  "League Info" + "Overall"                  (the sheet)

Run:   python3 draft_sheets/check_csg_settings.py
"""
import glob
import json
import os
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import build_tool_data as btd  # noqa: E402  (reuse the scrape reader — one source of truth)

# ── where each setting lives in the CSG "League Info" tab ──
# label is what the sheet prints next to the input, kept so a version bump that moves a
# row is caught by the label mismatch rather than silently comparing the wrong cell.
ROSTER = [   # (cell, sheet label, our key)
    ("C4",  "QB",           "QB"), ("C5",  "RB",  "RB"), ("C6",  "WR", "WR"),
    ("C7",  "TE",           "TE"),
    ("C8",  "WR/TE",        None), ("C9",  "WR/RB", None),
    ("C10", "WR/RB/TE",     "FLEX"),
    ("C11", "WR/RB/TE/QB",  None),
    ("C12", "DST",          "DST"), ("C13", "K", "K"),
    ("C18", "Bench",        "BENCH"),
]
SCORING = [  # (cell, sheet label, our scoring key from ESPN statIds)
    ("F4",  "Point Per Yard",     "passYds"),
    ("F5",  "TD",                 "passTD"),
    ("F6",  "INT",                "passInt"),
    ("F10", "Point Per Yard",     "rushYds"),
    ("F11", "TD",                 "rushTD"),
    ("F14", "Point Per Yard",     "recYds"),
    ("F15", "TD",                 "recTD"),
    ("F16", "Reception",          "rec"),
    ("F19", "Fumble",             "fumbleLost"),
]
LEAGUE = [("I3", "# Teams"), ("I15", "Auction Budget"), ("I18", "Min Bid")]
# ranking-format selectors on the "Overall" tab (dropdowns; vocabulary is in column AX)
FORMAT_CELLS = [("P8", "ECR ranking format"), ("AF9", "Boris Chen tier format")]


def espn_truth():
    """Roster + scoring from the scrape, i.e. the same numbers the console values with."""
    cfg = btd.load_config()
    season = cfg.get("season")
    league = btd.read_scraped_league(season)
    if not league:
        raise SystemExit(f"No scrape for {season} — run `python3 pipeline.py scrape` first.")
    raw = json.load(open(os.path.join(ROOT, "scraping", "raw", str(season), "league_full.json")))
    slots = ((raw.get("settings") or {}).get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    want = dict(league["starters"])
    want["FLEX"] = league["flex"]
    want["BENCH"] = league["bench"]
    want["DST"] = int(slots.get("16") or 0)
    want["K"] = int(slots.get("17") or 0)
    # use the same display labels as the console board, so the CSG team list reads the way
    # you'll hear names called at the table (config/league.json "manager_labels")
    labels = cfg.get("manager_labels") or {}
    if labels:
        league["managers"] = [labels.get(n, n) for n in league["managers"]]
    return season, want, league


def fmt(v):
    if v is None:
        return "(blank)"
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def main():
    season, want, league = espn_truth()
    scoring = league["scoring"] or {}
    paths = sorted(glob.glob(os.path.join(HERE, f"CSG*{season}*auction.xlsm"))) \
        or sorted(glob.glob(os.path.join(HERE, "CSG*auction.xlsm")))
    if not paths:
        raise SystemExit("No CSG*auction.xlsm in draft_sheets/.")
    path = paths[-1]
    wb = openpyxl.load_workbook(path, data_only=True)
    li, ov = wb["League Info"], wb["Overall"]

    print(f"CSG sheet:  {os.path.basename(path)}")
    print(f"Your league: {league.get('league_name')} {season} — "
          f"{len(league['managers'])} teams, ${league['budget']}, "
          f"starters {league['starters']} +{league['flex']}FLX +{league['bench']}BN\n")

    todo = []

    def row(cell, label, have, need, ok):
        mark = "ok " if ok else "→  "
        print(f"  {mark}{cell:<5} {label:<22} sheet={fmt(have):<8} yours={fmt(need)}")
        if not ok:
            todo.append((cell, label, fmt(have), fmt(need)))

    print("ROSTER  (League Info tab, column C)")
    for cell, label, key in ROSTER:
        have = li[cell].value
        sheet_label = li["B" + cell[1:]].value
        if sheet_label and str(sheet_label).strip() != label:
            print(f"  ⚠ {cell}: expected label {label!r}, sheet says {sheet_label!r} "
                  f"— CSG may have moved this row; verify by eye.")
        need = want.get(key, 0) if key else 0
        row(cell, label, have, need, (have or 0) == need)

    print("\nSCORING  (League Info tab, column F)")
    for cell, label, key in SCORING:
        have = li[cell].value
        need = scoring.get(key)
        if need is None:
            print(f"  -- {cell:<5} {label:<22} sheet={fmt(have):<8} (not in ESPN scoring)")
            continue
        row(cell, label, have, need, abs((have or 0) - need) < 1e-9)

    print("\nLEAGUE  (League Info tab)")
    truth = {"# Teams": len(league["managers"]), "Auction Budget": league["budget"], "Min Bid": 1}
    for cell, label in LEAGUE:
        row(cell, label, li[cell].value, truth[label], (li[cell].value or 0) == truth[label])

    # half-PPR leagues must also flip the ranking-format dropdowns, or the ECR/Boris
    # columns keep showing full-PPR consensus while the valuation is half-PPR.
    rec = scoring.get("rec")
    fmt_want = "0.5PPR" if rec == 0.5 else ("PPR" if rec == 1 else None)
    print(f"\nRANKING FORMAT  (Overall tab dropdowns) — your reception value is {fmt(rec)}")
    for cell, label in FORMAT_CELLS:
        have = ov[cell].value
        ok = (fmt_want is None) or (str(have).strip() == fmt_want)
        row(cell, label, have, fmt_want or "(n/a)", ok)

    me = li["I12"].value
    cfg_me = btd.load_config().get("me")
    print(f"\nIDENTITY\n  {'ok ' if me == cfg_me else '→  '}I12   Enter Your Name       "
          f"sheet={fmt(me):<8} yours={cfg_me}")
    if me != cfg_me:
        todo.append(("I12", "Enter Your Name", fmt(me), str(cfg_me)))
    names = [li[f"L{r}"].value for r in range(3, 3 + len(league["managers"]))]
    if [n for n in names if n] != league["managers"]:
        print(f"  →  L3:L{2+len(league['managers'])}  Team names            "
              f"sheet={fmt(names[0])}… yours={league['managers'][0]}…")
        todo.append((f"L3:L{2+len(league['managers'])}", "Team names",
                     fmt(names[0]), "your 12 managers"))
    wb.close()

    print("\n" + "─" * 72)
    if not todo:
        print("✓ CSG settings already match your league.")
        return 0
    print(f"{len(todo)} setting(s) to change — edit in Excel, then re-run this script:\n")
    for cell, label, have, need in todo:
        print(f"   {cell:<9} {label:<22} {have:>8}  ->  {need}")
    if any(c.startswith("L3:") for c, *_ in todo):
        print("\n   L3:L%d — paste these 12 names in order:" % (2 + len(league["managers"])))
        for i, n in enumerate(league["managers"], 3):
            print(f"       L{i:<3} {n}")
    print("\nAfter editing, save the workbook so Excel recalculates, then:")
    print("   python3 pipeline.py csg build inject")
    print("Until then the sheet's own VBD / Proj Price columns reflect the OLD settings.")
    print("(Rankings, ADP and market prices are external data — unaffected by these.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
