#!/usr/bin/env python3
"""Calibrate opponent bid tendencies from real auction history -> config/tendencies.json.

This is the seam that connects the (local) analysis pipeline to the shipped console.
It reuses a18_agent_auction.build_agents() VERBATIM — the same per-manager model the
agent-auction simulator bids with — and writes it to the file build_tool_data.py already
merges (config/tendencies.json). No re-derivation of the model here; this is purely the
export half that the retired a20_export_tool_data.py used to do, but writing the schema
the console actually reads (dropping the console-irrelevant isHarry/leagueMult fields).

  scraping/raw/<yr>/ + elboberto_projections.json
        └─ lib + a18.build_agents() ─► {mgr: {mult{QB,RB,WR,TE}, conc, maxbuy}}
                                        └─► config/tendencies.json  ─►  build_tool_data.py

Run:   python3 analysis/calibrate.py        (or: python3 pipeline.py calibrate)

mult = $-weighted paid/projected by position (>1 = overpays), conc = stars-and-scrubs
top-3 spend share, maxbuy = historical single-buy ceiling +15%. See build_agents() for the
exact derivation. Managers with too little history fall back to the league positional mean.
"""
import json
import os
import sys

import a18_agent_auction as a18

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "config", "tendencies.json")
POS = ("QB", "RB", "WR", "TE")

# reuse the console builder's own scraped-league name derivation so tendencies keys
# match exactly what build_tool_data.py will call each manager (no divergence).
sys.path.insert(0, os.path.join(ROOT, "draft_sheets"))
import build_tool_data as btd  # noqa: E402


def config_season(default=2026):
    path = os.path.join(ROOT, "config", "league.json")
    if os.path.exists(path):
        with open(path) as f:
            return int(json.load(f).get("season", default))
    return default


def current_league_aliases(season):
    """{console_name: canonical_name} for the CURRENT league.

    build_agents() keys managers by lib.MANAGER_CANON identity (e.g. a short/nickname
    form), but the console names managers from the live scrape (e.g. their full ESPN
    display name). Both come from the same ESPN member GUID, so we bridge by GUID:
    read_scraped_league gives
    the exact console names in team order; the raw file gives the GUID in the same order.
    Returns {} when there's no scrape (fresh league — names already align generically)."""
    league = btd.read_scraped_league(season)
    if not league:
        return {}
    raw = os.path.join(ROOT, "scraping", "raw", str(season), "league_full.json")
    with open(raw) as f:
        data = json.load(f)
    if isinstance(data, list):
        data = data[0] if data else {}
    guids = [t.get("primaryOwner") for t in (data.get("teams") or [])]
    aliases = {}
    for console_name, guid in zip(league["managers"], guids):
        canon = a18.lib.MANAGER_CANON.get(guid)
        if canon and canon != console_name:
            aliases[console_name] = canon
    return aliases


def main():
    seasons = a18.lib.available_seasons()
    agents = a18.build_agents()
    if not agents:
        raise SystemExit(
            "build_agents() returned nothing — is scraping/raw/ history + "
            "draft_sheets/elboberto_projections.json present? "
            "Run `python3 pipeline.py calibrate` (it regenerates the projections first)."
        )

    tendencies = {
        name: {
            "mult": {p: round(a["mult"].get(p, 1.0), 2) for p in POS},
            "conc": round(a["conc"]),
            "maxbuy": round(a["maxbuy"]),
        }
        for name, a in agents.items()
    }

    # Emit under the current league's console names too, so returning managers whose
    # scraped display name differs from their calibration identity still match.
    season = config_season()
    aliases = current_league_aliases(season)
    for console_name, canon in aliases.items():
        if canon in tendencies and console_name not in tendencies:
            tendencies[console_name] = tendencies[canon]

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(tendencies, f, indent=2, sort_keys=True)

    # audit table — same view a18 prints, so a calibrate run is self-documenting
    print(f"Calibrated {len(tendencies)} managers from auction history "
          f"(league fallback mult {a18.LEAGUE_MULT}).")
    # Say which seasons actually contributed. build_agents() reads fixed season ranges and
    # load() now tolerates gaps, so a partial scrape calibrates quietly off less than you
    # might assume — and a per-position multiplier needs >=3 priced picks or it falls back
    # to the league mean. Without this line a one-season model looks like a nine-season one.
    print(f"   seasons used: {', '.join(map(str, seasons)) or 'NONE'}")
    fallbacks = sum(1 for t in tendencies.values() for p in POS
                    if abs(t["mult"][p] - round(a18.LEAGUE_MULT[p], 2)) < 0.005)
    total = len(tendencies) * len(POS)
    if fallbacks:
        print(f"   {fallbacks}/{total} positional multipliers fell back to the league mean "
              "(too few priced picks for that manager+position).")
    if len(seasons) < 3:
        print("   THIN HISTORY: with fewer than 3 seasons these tendencies are noisy — "
              "treat conc/maxbuy as indicative and expect most mults to be league-mean.")
    print(f"   {'manager':20}{'QB':>6}{'RB':>6}{'WR':>6}{'TE':>6}{'conc%':>7}{'maxbuy':>8}")
    for name in sorted(tendencies, key=lambda m: -tendencies[m]["mult"]["RB"]):
        t = tendencies[name]
        m = t["mult"]
        print(f"   {name:20}{m['QB']:>6.2f}{m['RB']:>6.2f}{m['WR']:>6.2f}{m['TE']:>6.2f}"
              f"{t['conc']:>7}{t['maxbuy']:>8}")
    if aliases:
        print("\n   aliased to current-league scrape names: "
              + ", ".join(f"{c} = {k}" for k, c in aliases.items()))
    print(f"\nWrote {OUT}")
    print("  build_tool_data.py merges these per manager (by name); unmatched managers "
          "stay neutral. Next: python3 pipeline.py build inject")


if __name__ == "__main__":
    main()
