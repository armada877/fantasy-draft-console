#!/usr/bin/env python3
"""Pull the keepers managers have DECLARED on Sleeper into config/league.json.

Sleeper exposes each roster's designated keepers for the upcoming draft at
`/league/<id>/rosters` -> `keepers`: a list of player ids. That is the same list the draft
room shows, so it is fact rather than the console's biggest-surplus projection, and
`announced_keepers` in config exists precisely to override the projection.

Player ids are resolved from the drafts already in the league chain -- a few hundred players
across the last three seasons, no large download -- and only fall back to the full
`/players/nfl` dump for ids none of them contain (a rookie kept the year after he was
drafted, say). Names are written in the console's own form, so a defence becomes
"Texans D/ST" and matches the board.

Run it before a build; `pipeline.py build` then injects the result:

    python3 scraping/scrape_sleeper_keepers.py
"""
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CFG = os.path.join(ROOT, "config", "league.json")
API = "https://api.sleeper.app/v1"


def get(path):
    with urllib.request.urlopen(API + path, timeout=60) as r:
        return json.load(r)


def console_name(first, last, pos):
    if (pos or "").upper() == "DEF":
        return str(last or first or "").strip().split(" ")[-1] + " D/ST"
    return ((first or "") + " " + (last or "")).strip()


def main():
    cfg = json.load(open(CFG, encoding="utf-8"))
    lid = str(cfg.get("sleeper_league_id") or "").strip()
    if not lid:
        sys.exit("Set sleeper_league_id in config/league.json.")

    rosters = get("/league/%s/rosters" % lid)
    users = {u["user_id"]: (u.get("display_name") or "").strip()
             for u in get("/league/%s/users" % lid)}
    want = {}
    for r in rosters:
        for pid in (r.get("keepers") or []):
            want.setdefault(str(pid), []).append(users.get(r.get("owner_id")) or "?")
    if not want:
        print("No keepers declared on Sleeper yet — config left unchanged.")
        return

    # resolve ids from the drafts already in the chain before reaching for the full dump
    names, chain, seen = {}, lid, set()
    while chain and chain not in seen and set(want) - set(names):
        seen.add(chain)
        lg = get("/league/%s" % chain)
        for d in (get("/league/%s/drafts" % chain) or []):
            for pk in (get("/draft/%s/picks" % d["draft_id"]) or []):
                pid = str(pk.get("player_id"))
                if pid in want and pid not in names:
                    m = pk.get("metadata") or {}
                    names[pid] = console_name(m.get("first_name"), m.get("last_name"),
                                              m.get("position"))
        chain = lg.get("previous_league_id")

    missing = [p for p in want if p not in names]
    if missing:
        print("  %d id(s) not in the league's own drafts — fetching the player index" % len(missing))
        allp = get("/players/nfl")
        for pid in missing:
            p = allp.get(pid) or {}
            nm = p.get("full_name") or ""
            names[pid] = (console_name(p.get("first_name"), p.get("last_name"), p.get("position"))
                          or nm)

    announced = {}
    for pid, mgrs in want.items():
        for m in mgrs:
            if names.get(pid):
                announced[m] = names[pid]

    prev = cfg.get("announced_keepers") or {}
    cfg["announced_keepers"] = announced
    json.dump(cfg, open(CFG, "w", encoding="utf-8"), indent=2)

    print("Declared keepers on Sleeper (%d of %d rosters):" % (len(announced), len(rosters)))
    for m, p in sorted(announced.items()):
        mark = "" if prev.get(m) == p else ("  <-- was %r" % prev[m] if m in prev else "  <-- new")
        print("  %-16s %s%s" % (m, p, mark))
    dropped = [m for m in prev if m not in announced]
    if dropped:
        print("  no longer declared:", ", ".join(dropped))
    print("\nWritten to config/league.json. Run `python3 pipeline.py build inject` to apply.")


if __name__ == "__main__":
    main()
