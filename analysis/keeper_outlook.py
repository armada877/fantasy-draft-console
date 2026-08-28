#!/usr/bin/env python3
"""Project next season's keepers, post-keeper budgets and RFA nominations.

Leagues that keep players at a formula price ("last season's price + N") make the keeper
decision predictable: for each player still on last season's ending roster, compare the
formula cost against this season's projected worth. The biggest surplus is the keep.

That matters more than it sounds. A player with a large keeper surplus never reaches the
auction at all, so the console's "best available" overstates the real board — and the
money a manager sinks into an expensive keeper is gone before the first nomination.

    keeper cost = keeper_bump + (draft price if drafted or traded else keeper_waiver_value)
    surplus     = projected worth - keeper cost

ESPN records last season's price in playerPoolEntry.keeperValueFuture and how the roster
spot was acquired in acquisitionType. For DRAFT and TRADE those agree with what was paid --
a trade passes the original drafter's value to the new team -- and on the 2025 scrape all
73 DRAFT entries match the recorded auction cost exactly. For a waiver ADD, keeperValueFuture
is an ESPN-computed number unrelated to any bid ($8 for a player nobody paid for), so the
house waiver value replaces it before the bump is added. Charging keeperValueFuture there
overprices free pickups badly enough to change which player a manager keeps.

With one keeper per team, the SECOND-best surplus is the natural RFA nomination: a player
the manager would have kept but cannot, and may retain only at market price.

Config (config/league.json):
    keepers_per_team     default 1
    keeper_bump          default 5   (dollars added to last season's value)
    keeper_waiver_value  default 1   (what a waiver pickup counts as, before the bump)

Run:  PYTHONPATH=analysis python3 analysis/keeper_outlook.py [--markdown]
Reads the built draft_sheets/tool_data.json for this season's worth, so run `pipeline.py
build` first. Outputs are a projection, not a confirmed keeper list.
"""
import argparse
import json
import os
import re
import sys

import lib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(n):
    n = re.sub(r"\([^)]*\)", "", str(n))
    n = re.sub(r"[.'`]", "", n.lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def _cfg(key, default):
    path = os.path.join(ROOT, "config", "league.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f).get(key, default)
    return default


def load_worth():
    path = os.path.join(ROOT, "draft_sheets", "tool_data.json")
    if not os.path.exists(path):
        sys.exit("draft_sheets/tool_data.json missing — run `python3 pipeline.py build` first.")
    with open(path, encoding="utf-8") as f:
        td = json.load(f)
    return {norm(p["name"]): p for p in td["players"]}, (td.get("budget") or 200)


def console_names():
    """{canonical manager name: current-season console name}, via manager_canon + scrape."""
    season = int(_cfg("season", 0) or 0)
    canon_path = os.path.join(ROOT, "config", "manager_canon.json")
    raw = os.path.join(ROOT, "scraping", "raw", str(season), "league_full.json")
    if not (os.path.exists(canon_path) and os.path.exists(raw)):
        return {}
    with open(canon_path, encoding="utf-8") as f:
        canon = json.load(f)
    with open(raw, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        data = data[0] if data else {}
    out = {}
    for m in data.get("members", []):
        c = canon.get(m.get("id"))
        if c:
            out[c] = (m.get("displayName") or "").strip() or c
    return out


def build(prior_season):
    worth, budget = load_worth()
    bump = int(_cfg("keeper_bump", 5) or 0)
    waiver_value = int(_cfg("keeper_waiver_value", 1) or 0)
    per_team = int(_cfg("keepers_per_team", 1) or 1)
    c2c = console_names()
    d = lib.load(prior_season)
    if not d.get("teams"):
        sys.exit(f"No scraped roster data for {prior_season} "
                 f"(need scraping/raw/{prior_season}/league_full.json with mRoster).")
    teams = []
    for t in d["teams"]:
        mgr = lib.manager(prior_season, t.get("id"))
        rows = []
        for e in ((t.get("roster") or {}).get("entries") or []):
            pe = e.get("playerPoolEntry") or {}
            pl = pe.get("player") or {}
            kvf = pe.get("keeperValueFuture")
            w = worth.get(norm(pl.get("fullName") or ""))
            if kvf is None or not w:
                continue        # not rostered with a price, or not in this year's pool
            acq = e.get("acquisitionType") or "DRAFT"
            cost = (waiver_value if acq == "ADD" else kvf) + bump
            rows.append({"name": pl.get("fullName"), "pos": w["pos"], "cost": cost,
                         "worth": w["worth"], "surplus": w["worth"] - cost})
        rows.sort(key=lambda r: -r["surplus"])
        teams.append({"manager": c2c.get(mgr, mgr), "rows": rows,
                      "keepers": rows[:per_team],
                      "rfa": rows[per_team] if len(rows) > per_team else None})
    teams.sort(key=lambda t: -(t["keepers"][0]["surplus"] if t["keepers"] else -999))
    return teams, budget, bump, per_team


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prior-season", type=int,
                    default=int(_cfg("season", 2026) or 2026) - 1)
    ap.add_argument("--markdown", action="store_true", help="emit a briefing-ready table")
    args = ap.parse_args()

    teams, budget, bump, per_team = build(args.prior_season)
    waiver_value = int(_cfg("keeper_waiver_value", 1) or 0)
    print(f"Keeper outlook from {args.prior_season} ending rosters — "
          f"{per_team} keeper(s)/team, cost = ${bump} + prior value "
          f"(waiver pickups valued at ${waiver_value})\n")
    if args.markdown:
        print("| Manager | Likely keeper | Cost | Surplus | Budget left | Likely RFA |")
        print("|---|---|---|---|---|---|")
        for t in teams:
            k = t["keepers"][0] if t["keepers"] else None
            spent = sum(x["cost"] for x in t["keepers"])
            print("| %s | %s | %s | %s | $%d | %s |" % (
                t["manager"],
                f"{k['name']} ({k['pos']})" if k else "—",
                f"${k['cost']}" if k else "",
                f"{k['surplus']:+d}" if k else "",
                budget - spent,
                f"{t['rfa']['name']} ({t['rfa']['pos']})" if t["rfa"] else "—"))
        return
    for t in teams:
        spent = sum(x["cost"] for x in t["keepers"])
        print(f"{t['manager']}   (budget left ${budget - spent})")
        for k in t["keepers"]:
            print(f"   KEEP {k['name']:<24} {k['pos']:<3} ${k['cost']:<4} "
                  f"worth ${k['worth']:<4} surplus {k['surplus']:+d}")
        if t["rfa"]:
            r = t["rfa"]
            print(f"   RFA? {r['name']:<24} {r['pos']:<3} ${r['cost']:<4} "
                  f"worth ${r['worth']:<4} surplus {r['surplus']:+d}")
        print()
    print("Players projected OFF the board (kept):")
    for t in teams:
        for k in t["keepers"]:
            print(f"   {k['name']:<24} {k['pos']:<3} — {t['manager']}")


if __name__ == "__main__":
    main()
