#!/usr/bin/env python3
"""Generate the installable `ff-research` Claude Code plugin.

The plugin's MCP config must carry absolute paths (this repo's venv python and
PYTHONPATH), which don't belong in a public repo — so the installable tree is
GENERATED into claude_plugin/local/ (gitignored) from this tracked script.

    python3 claude_plugin/build.py
    # then, inside any Claude Code session:
    /plugin marketplace add <repo>/claude_plugin/local
    /plugin install ff-research@fantasy-local

Re-run after moving the repo or rebuilding the venv, then
`/plugin marketplace update fantasy-local`.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOCAL = os.path.join(HERE, "local")
PLUGIN = os.path.join(LOCAL, "ff-research")
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")

SKILL = """\
---
name: ff-research
description: >-
  Fantasy-football research over the user's own ESPN leagues and the public
  resource directory. Use when the user asks about their fantasy team, roster,
  waivers, FAAB bids, trades, start/sit, opponent managers, league standings,
  player tiers/rankings, NFL schedules/weather/Vegas lines for fantasy purposes.
---

# ff-research

The `ff-research` MCP server (tools `mcp__ff-research__*`) gives you:

**The user's leagues (local, generated data — no network):**
- `list_leagues` — their leagues + payload freshness. Call first if the league
  is ambiguous.
- `league_details` — rules, scoring, lineup, FAAB/priority, standings with
  playoff odds.
- `team_details` — a roster with ros/last-3 ppg, needs, and the manager's
  tendencies (omit `team` for the user's own team).
- `opponent_tendencies` — the calibrated opponent model: waiver aggression,
  contested-claim win rate, max bid, trade rate/accept/position flow, drop
  latency; auction multipliers where the league auctions. Consult before
  sizing a bid or pitching a trade.
- `evaluate_trade` — evaluate a specific trade with the league engine:
  both sides' lineup deltas, accept odds from calibrated tendencies,
  veto risk, and a counter search. Use for any trade question.
- `weekly_lineup` — the optimal THIS-WEEK lineup (rosters lock Tue-Tue;
  use this for start/sit, ros numbers for value decisions).
- `console_boards` — the in-season console's precomputed waivers / trades /
  buy_low / sell_high boards. These already price in opponent tendencies —
  prefer them over re-deriving.

**Public web (allowlisted directory hosts only):**
- `fantasypros_rankings` — structured expert-consensus (ECR): avg ± std,
  best/worst, start/sit grades. ~40 experts.
- `fp_league_value` — FantasyPros VOR adapted to the league's scoring
  flavor and roster shape (replacement from every starting seat,
  FLEX/superflex pooled). Cross-position value: trades, scarcity.
  horizon='ros' gives season-total VOR — the trade-value number.
- `league_tiers` — Chen's clustering run live, adapted to one of the
  user's leagues: right scoring variant, [MINE]/[FA]/owner tags;
  source='projections' tiers the league's OWN recomputed projections.
  Prefer this over boris_chen_tiers for league-specific questions.
- `boris_chen_tiers` — his published weekly tiers (std/PPR/half only).
  For rankings/start-sit, corroborate two sources and note disagreement.
- `list_resources` / `fetch_resource` / `fetch_url` — rankings, strength of
  schedule, Vegas lines, kicker/DST streaming stats, depth charts, weather,
  news.
- `player_news` — the live news wire: full report text, beat-writer
  attribution, source-tweet links. Filter by player/team.
- `reporter_feed` — one reporter's recent reporting (handle or name).
- `twitter_handles` — expert/beat-writer lists (~2019 vintage; say so).
  Tweets can't be fetched — read content via the two tools above.

Ground rules:
- Numbers come from tool results, never memory. Cite the tool/source.
- League payloads carry a freshness note; if flagged STALE, say so and suggest
  `python3 pipeline.py week` in the fantasy repo.
- Where sources disagree (console vs public consensus), surface the
  disagreement — it is signal.
"""


def main() -> None:
    if not os.path.exists(PYTHON):
        sys.exit(f"venv python not found at {PYTHON} — create the venv first")

    shutil.rmtree(LOCAL, ignore_errors=True)
    os.makedirs(os.path.join(LOCAL, ".claude-plugin"))
    os.makedirs(os.path.join(PLUGIN, ".claude-plugin"))
    os.makedirs(os.path.join(PLUGIN, "skills", "ff-research"))

    with open(os.path.join(LOCAL, ".claude-plugin", "marketplace.json"), "w") as f:
        json.dump({
            "name": "fantasy-local",
            "owner": {"name": "local"},
            "plugins": [{
                "name": "ff-research",
                "source": "./ff-research",
                "description": "Fantasy-football research: your ESPN leagues, "
                               "opponent tendency model, and the public "
                               "resource directory.",
            }],
        }, f, indent=2)

    with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"), "w") as f:
        json.dump({
            "name": "ff-research",
            "displayName": "Fantasy Football Research",
            "version": "1.7.0",
            "description": "Your ESPN leagues (rosters, standings, opponent "
                           "tendency model, console boards) + the public "
                           "fantasy resource directory, as MCP tools.",
            "mcpServers": {
                "ff-research": {
                    "command": PYTHON,
                    "args": ["-m", "research_agent.mcp_server"],
                    "env": {
                        "PYTHONPATH": ROOT,
                        "PYTHONUNBUFFERED": "1",
                    },
                },
            },
        }, f, indent=2)

    with open(os.path.join(PLUGIN, "skills", "ff-research", "SKILL.md"), "w") as f:
        f.write(SKILL)

    print(f"built {PLUGIN}")
    print("install with:")
    print(f"  /plugin marketplace add {LOCAL}")
    print("  /plugin install ff-research@fantasy-local")


if __name__ == "__main__":
    main()
