#!/usr/bin/env python3
"""Analysis 16: tier cliffs for the 2026 pool — the anti-panic tool.

The "wait then overpay out of desperation" trap comes from not seeing (a) how many
acceptable players remain in your value tier (runway) and (b) how big the drop to
the next tier actually is. Most tier breaks are gentle — there's a fine alternative
just below — so panic is rarely justified. This maps every position's tiers, sizes,
price range, and the POINTS cliff to the next tier, flagging the few real cliffs.
"""
import json
import os
import statistics
from collections import OrderedDict

P = json.load(open(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir,
              "draft_sheets", "elboberto_projections.json")))["2026"]


def tier_num(t):
    try:
        return int("".join(c for c in str(t) if c.isdigit()))
    except ValueError:
        return 99


def main():
    print("=" * 82)
    print("ANALYSIS 16 — 2026 TIER CLIFFS (anti-panic map): runway & drop-off by position")
    print("=" * 82)
    print("cliff = avg projected-points drop to the next tier. Big cliff => worth securing")
    print("one before it. Small cliff => never panic, just take the next player.\n")

    for pos in ["RB", "WR", "TE", "QB"]:
        players = [p for p in P if p["pos"] == pos and p.get("fpts") is not None]
        # group by tier label, ordered by tier number
        tiers = OrderedDict()
        for p in sorted(players, key=lambda p: (tier_num(p["tier"]), -(p["proj_value"] or 0))):
            tiers.setdefault(p["tier"], []).append(p)
        # keep tiers with a sensible number, drop junk
        order = [t for t in tiers if tier_num(t) <= 12]
        print(f"### {pos}")
        print(f"   {'tier':6}{'n':>3}{'proj$ range':>14}{'avg pts':>9}{'cliff→next':>12}   note")
        prev_avg = None
        for i, t in enumerate(order):
            g = tiers[t]
            vals = [x["proj_value"] or 0 for x in g]
            avgpts = statistics.mean(x["fpts"] for x in g)
            cliff = ""
            note = ""
            if i + 1 < len(order):
                nxt = tiers[order[i+1]]
                drop = avgpts - statistics.mean(x["fpts"] for x in nxt)
                cliff = f"{drop:.0f}"
                if drop >= 25:
                    note = "TRUE CLIFF — secure one here"
                elif drop >= 12:
                    note = "moderate step"
                else:
                    note = "gentle — safe to wait"
            rng = f"${min(vals):.0f}-{max(vals):.0f}" if vals else "-"
            star = " <" if len(g) <= 3 else ""
            print(f"   {t:6}{len(g):>3}{rng:>14}{avgpts:>9.0f}{cliff:>12}   {note}{star}")
        print()

    print("── Discipline rule (kills the panic overpay) ──")
    print("  Your MAX BID = worth (the projection). Reaching the end of a tier does NOT change worth.")
    print("  Before panic-bidding the last player in a tier above worth, check the cliff:")
    print("   • cliff < ~12 pts  -> DON'T pay up; the top of the next tier is ~as good. Let him go.")
    print("   • cliff >= ~25 pts -> a real drop; paying a few $ over worth to secure is defensible,")
    print("                         but cap it — the premium should be < the cliff's $-value, never")
    print("                         'whatever it takes'. Count players left in-tier vs teams needing")
    print("                         the spot: if players_left > needy_teams, you can still wait.")


if __name__ == "__main__":
    main()
