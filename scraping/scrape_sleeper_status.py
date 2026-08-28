#!/usr/bin/env python3
"""Pull each board player's live NFL status from Sleeper into config/player_status.json.

Drafting weeks before kickoff means paying for players whose availability is not yet settled.
Sleeper's public player index carries the designations that matter — IR, PUP, an injury
report with the body part, and whether the player is on an NFL roster at all — and the
reporting is current: on the board this was written against, the median flagged player had
news two days old.

Matching is on NAME AND POSITION, and among duplicates prefers a player who is actually
rostered, then the better search_rank. Name alone is not safe: the index holds a retired
guard called Josh Allen with no team, a receiver called Kenneth Walker, and a cornerback
called DJ Moore, and a name-only join hands you those instead of the quarterback, the KC
running back and the Buffalo receiver. Getting that wrong is worse than no flag at all — it
would have marked the best quarterback on the board as unsigned.

What this CANNOT tell you is trade risk. Nothing in the feed predicts a trade, so nothing
here claims to; the flags are injury designation, roster status, and being unsigned.

    python3 scraping/scrape_sleeper_status.py
    python3 pipeline.py build inject      # to apply
"""
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "config", "player_status.json")
BOARD = os.path.join(ROOT, "draft_sheets", "tool_data.json")
SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
# designations that mean "may not play at all", as opposed to a weekly injury report
HARD = {"IR", "PUP", "NFI", "Sus", "COV"}


def norm(n):
    n = re.sub(r"[.'`]", "", str(n).lower())
    n = re.sub(r"[-/]", " ", n)
    return " ".join(t for t in n.split() if t and t not in SUFFIX)


def main():
    if not os.path.exists(BOARD):
        sys.exit("draft_sheets/tool_data.json missing — run `python3 pipeline.py build` first.")
    with open(BOARD, encoding="utf-8") as f:
        board = json.load(f)["players"]

    print("fetching Sleeper's player index (a few MB, once)…")
    with urllib.request.urlopen("https://api.sleeper.app/v1/players/nfl", timeout=180) as r:
        allp = json.load(r)

    idx = {}
    for p in allp.values():
        nm = p.get("full_name") or ((p.get("first_name") or "") + " "
                                    + (p.get("last_name") or "")).strip()
        pos = (p.get("position") or "").upper()
        if not nm or not pos:
            continue
        key = (norm(nm), "DST" if pos == "DEF" else pos)
        cur = idx.get(key)
        rank = p.get("search_rank") or 9999999
        if cur is None or (1 if p.get("team") else 0, -rank) > cur[1]:
            idx[key] = (p, (1 if p.get("team") else 0, -rank))

    now = time.time()
    out, counts = {}, {"hard": 0, "questionable": 0, "unsigned": 0}
    for b in board:
        hit = idx.get((norm(b["name"]), b["pos"]))
        if not hit:
            continue                      # defences aren't in the index by that name; fine
        p = hit[0]
        inj = (p.get("injury_status") or "").strip()
        status = (p.get("status") or "Active").strip()
        team = p.get("team")
        kind = None
        if not team:
            kind, counts["unsigned"] = "unsigned", counts["unsigned"] + 1
        elif inj in HARD or status not in ("Active", ""):
            kind, counts["hard"] = "out", counts["hard"] + 1
        elif inj:
            kind, counts["questionable"] = "watch", counts["questionable"] + 1
        if not kind:
            continue
        news = p.get("news_updated")
        out[b["name"]] = {
            "kind": kind,
            "label": inj or (status if status != "Active" else "no NFL team"),
            "body": (p.get("injury_body_part") or "").strip() or None,
            "team": team,
            "news_days": int((now - news / 1000.0) / 86400) if news else None,
        }

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"pulled": time.strftime("%Y-%m-%d %H:%M"), "players": out}, f, indent=1)

    print("matched %d of %d board players; flagged %d"
          % (sum(1 for b in board if (norm(b["name"]), b["pos"]) in idx), len(board), len(out)))
    print("  out/PUP/IR %d · unsigned %d · injury report %d"
          % (counts["hard"], counts["unsigned"], counts["questionable"]))
    worth = {b["name"]: b["worth"] for b in board}
    big = sorted((n for n in out if worth.get(n, 0) >= 15), key=lambda n: -worth[n])
    if big:
        print("\n  flagged players worth $15+:")
        for n in big:
            r = out[n]
            print("    %-24s $%-3d %-12s %s%s"
                  % (n[:24], worth[n], r["label"], r["body"] or "",
                     "  (%dd ago)" % r["news_days"] if r["news_days"] is not None else ""))
    print("\nWritten to config/player_status.json. Run `python3 pipeline.py build inject`.")


if __name__ == "__main__":
    main()
