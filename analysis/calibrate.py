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
exact derivation. Managers with too little history fall back to the league positional mean;
a reserved "_league_default" entry carries that league-average profile for managers with
NO history at all (substitutions), so the console never models them as flat-1.0 bidders.
"""
import json
import os
import statistics
import sys

import a18_agent_auction as a18

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "config", "tendencies.json")
POS = ("QB", "RB", "WR", "TE")
# reserved tendencies.json key: profile for managers with no auction history
LEAGUE_DEFAULT_KEY = "_league_default"

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

    # Reserved entry: the profile build_tool_data.py gives a manager with NO auction
    # history (a mid-season substitution, an expansion team). "Neutral" has to mean
    # league-average, not 1.0 — a flat 1.0 mult would model a newcomer as willing to pay
    # full projected value at every position (2.4x the league norm at QB) with no ceiling,
    # which inflates predicted competition for exactly the players you're bidding on.
    real = [t for n, t in tendencies.items() if not n.startswith("_")]
    tendencies[LEAGUE_DEFAULT_KEY] = {
        "mult": {p: round(a18.LEAGUE_MULT[p], 2) for p in POS},
        "conc": round(statistics.median(t["conc"] for t in real)) if real else 50,
        "maxbuy": round(statistics.median(t["maxbuy"] for t in real)) if real else 100,
    }

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(tendencies, f, indent=2, sort_keys=True)

    # audit table — same view a18 prints, so a calibrate run is self-documenting
    named = [n for n in tendencies if not n.startswith("_")]
    print(f"Calibrated {len(named)} managers from auction history "
          f"(league fallback mult {a18.LEAGUE_MULT}).")
    print(f"   {'manager':20}{'QB':>6}{'RB':>6}{'WR':>6}{'TE':>6}{'conc%':>7}{'maxbuy':>8}")
    for name in sorted(named, key=lambda m: -tendencies[m]["mult"]["RB"]):
        t = tendencies[name]
        m = t["mult"]
        print(f"   {name:20}{m['QB']:>6.2f}{m['RB']:>6.2f}{m['WR']:>6.2f}{m['TE']:>6.2f}"
              f"{t['conc']:>7}{t['maxbuy']:>8}")
    if aliases:
        print("\n   aliased to current-league scrape names: "
              + ", ".join(f"{c} = {k}" for k, c in aliases.items()))
    d = tendencies[LEAGUE_DEFAULT_KEY]
    print(f"\n   {LEAGUE_DEFAULT_KEY:20}{d['mult']['QB']:>6.2f}{d['mult']['RB']:>6.2f}"
          f"{d['mult']['WR']:>6.2f}{d['mult']['TE']:>6.2f}{d['conc']:>7}{d['maxbuy']:>8}"
          "   <- managers with no history")

    print(f"\nWrote {OUT}")
    print("  build_tool_data.py merges these per manager (by name); managers with no "
          f"history get {LEAGUE_DEFAULT_KEY} (league-average).\n"
          "  Next: python3 pipeline.py build inject")


if __name__ == "__main__":
    main()
