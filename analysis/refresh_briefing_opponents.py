#!/usr/bin/env python3
"""Rewrite the "## Opponents" section of config/briefing.md from config/tendencies.json.

The advisor reads the briefing as its system prompt, so a hand-maintained opponent table goes
stale the moment calibration is re-run — and stale opponent numbers are worse than none,
because the advisor states them with the same confidence as live data. After reweighting
calibration to 2023-25 the pinned table still said 27jay paid 0.95 at RB when he had moved to
1.49, the highest in the league, and the advisor duly quoted the old figure.

This regenerates the table and the reads that follow it straight from the calibrated file, so
the section cannot drift from what the console is actually pricing with. Run it after every
`pipeline.py calibrate`.

    python3 analysis/refresh_briefing_opponents.py
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BRIEF = os.path.join(ROOT, "config", "briefing.md")
TEND = os.path.join(ROOT, "config", "tendencies.json")
POS = ("QB", "RB", "WR", "TE")
HEADING = "## Opponents"


def current_managers():
    """Console names of the managers actually in this season's league."""
    cfg_p = os.path.join(ROOT, "config", "league.json")
    season = 2026
    if os.path.exists(cfg_p):
        with io.open(cfg_p, encoding="utf-8") as f:
            season = int(json.load(f).get("season", 2026))
    raw = os.path.join(ROOT, "scraping", "raw", str(season), "league_full.json")
    if not os.path.exists(raw):
        return []
    with io.open(raw, encoding="utf-8") as f:
        d = json.load(f)
    d = (d[0] if d else {}) if isinstance(d, list) else d
    return [(m.get("displayName") or "").strip() for m in (d.get("members") or [])]


def main():
    if not os.path.exists(TEND):
        sys.exit("config/tendencies.json missing — run `python3 pipeline.py calibrate` first.")
    with io.open(TEND, encoding="utf-8") as f:
        tend = json.load(f)
    me = ""
    cfg_p = os.path.join(ROOT, "config", "league.json")
    if os.path.exists(cfg_p):
        with io.open(cfg_p, encoding="utf-8") as f:
            me = json.load(f).get("me") or ""

    names = [n for n in current_managers() if n in tend]
    if not names:
        sys.exit("no calibrated managers matched this season's league.")
    rows = sorted(names, key=lambda n: -(tend[n].get("maxbuy") or 0))

    avg = {p: sum(tend[n]["mult"].get(p, 1.0) for n in names) / len(names) for p in POS}
    out = ["%s — calibrated, recency-weighted to the last three seasons" % HEADING, "",
           "`mult` = how much they pay vs projected value by position (>1 overpays). `conc` = "
           "share of", "budget on their top 3. `max` = their realistic single-player ceiling. "
           "`rfa%` = share of", "budget spent in the RFA round, `keep%` = how often they "
           "retained the player they nominated.", "",
           "League average: " + " · ".join("**%s %.2f**" % (p, avg[p]) for p in POS), "",
           "| Manager | QB | RB | WR | TE | conc | max | rfa% | keep% |",
           "|---|---|---|---|---|---|---|---|---|"]
    for n in rows:
        a = tend[n]
        m = a["mult"]
        cells = []
        for p in POS:
            v = m.get(p, 1.0)
            # bold whatever is furthest from the room, in either direction
            cells.append("**%.2f**" % v if abs(v / (avg[p] or 1) - 1) >= 0.25 else "%.2f" % v)
        out.append("| %s | %s | %d | %d | %d | %d |"
                   % (n + (" (you)" if n == me else ""), " | ".join(cells),
                      a.get("conc", 0), a.get("maxbuy", 0),
                      a.get("rfa_share", 0), a.get("rfa_retain", 0)))

    def top(pos, n=2):
        return sorted((x for x in rows if x != me), key=lambda x: -tend[x]["mult"].get(pos, 1))[:n]

    rb, wr = top("RB"), top("WR")
    qb = top("QB", 1)[0]
    deep = sorted((x for x in rows if x != me), key=lambda x: -(tend[x].get("maxbuy") or 0))[:3]
    conc = max((x for x in rows if x != me), key=lambda x: tend[x].get("conc", 0))
    thin = min((x for x in rows if x != me), key=lambda x: tend[x].get("maxbuy") or 999)
    out += ["", "Key reads — derived from the table above, not written by hand:",
            "- **Backs go to %s** (%.2f / %.2f). They set the price on every RB you want."
            % (" and ".join(rb), tend[rb[0]]["mult"]["RB"], tend[rb[1]]["mult"]["RB"]),
            "- **Receivers go to %s** (%.2f / %.2f). Let them have WRs, or nominate one when "
            "you want their money gone." % (" and ".join(wr), tend[wr[0]]["mult"]["WR"],
                                            tend[wr[1]]["mult"]["WR"]),
            "- **%s pays closest to value at QB (%.2f)** — the main competition for the "
            "quarterback edge." % (qb, tend[qb]["mult"]["QB"]),
            "- **Deepest pockets: %s** ($%d-%d ceilings). Do not get into a war with them over "
            "one target." % (", ".join(deep), tend[deep[-1]]["maxbuy"], tend[deep[0]]["maxbuy"]),
            "- **%s is the most top-heavy** (%d%% on three players, %d%% of budget in the RFA "
            "round) — commits early, runs thin late."
            % (conc, tend[conc]["conc"], tend[conc].get("rfa_share", 0)),
            "- **%s has the lowest ceiling ($%d)** — never lose a player to them by a dollar."
            % (thin, tend[thin]["maxbuy"]), ""]

    with io.open(BRIEF, encoding="utf-8") as f:
        s = f.read()
    start = s.index(HEADING)
    nxt = s.index("\n## ", start + 1)
    # keep the blank line markdown needs before the heading that follows
    body = "\n".join(out).rstrip("\n") + "\n\n"
    io.open(BRIEF, "w", encoding="utf-8", newline="").write(s[:start] + body + s[nxt + 1:])
    print("briefing opponents section refreshed from %d calibrated managers" % len(rows))
    print("league average: " + ", ".join("%s %.2f" % (p, avg[p]) for p in POS))
    print("\nRestart the server — the briefing is read at startup.")


if __name__ == "__main__":
    main()
