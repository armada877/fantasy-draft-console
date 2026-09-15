"""Fantasy-football research agent built on the Claude Agent SDK.

Its tools wrap the r/fantasyfootball wiki resource directory (see catalog.py):
browse the directory, fetch any live resource in it, pull Boris Chen tiers as
structured data, and look up the wiki's expert/beat-writer Twitter handles.
Built-in tools (Bash, file access, generic web) are disabled — the agent can
only reach hosts the directory names.

Run:
    .venv/bin/python -m research_agent "which kicker should I stream this week?"
    .venv/bin/python -m research_agent --model claude-opus-5 "..."

Needs ANTHROPIC_API_KEY in the environment and `pip install claude-agent-sdk`.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    create_sdk_mcp_server,
    query,
    tool,
)

from . import fpvalue, leaguetools, tiers, webtools

DEFAULT_MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """\
You are a fantasy-football research assistant with two tool families.

PUBLIC WEB (server `ffresearch`) — the r/fantasyfootball wiki resource
directory: expert-consensus rankings, tier lists, strength of schedule, Vegas
lines, kicker/defense streaming stats, depth charts, weather, and news.

MY LEAGUES (server `myleague`) — the user's own ESPN leagues, read from this
repo's generated data: league rules and standings, every team's roster and
needs, the calibrated opponent-tendency model (waiver aggression, bid sizing,
trade behavior, drop latency, auction-day multipliers), and the in-season
console's precomputed waiver/trade boards.

How to work:
- LINEUPS ARE WEEKLY (rosters lock Tue-Tue). For any start/sit or lineup
  question, use weekly_lineup and each player's THIS-WEEK projection — never
  rank starters by rest-of-season numbers. ros_points/ros_ppg are for VALUE
  decisions (trades, waivers, holds), not weekly lineups.
- Questions about "my team", an opponent, a trade, or a waiver bid start with
  the myleague tools (list_leagues first if the league is ambiguous). Combine
  with public sources when outside context helps (news, tiers, weather).
- For rankings and start/sit calls, corroborate at least TWO independent
  public sources — typically fantasypros_rankings (expert consensus with
  spread) alongside league_tiers (Chen's clustering adapted to the user's
  league, with ownership tags) — and say where they agree and disagree.
  One source is a data point, not an answer. Use
  league_tiers(source='projections') when league scoring quirks matter.
- The directory has more than rankings: strength of schedule and points-against
  (fetch_resource: fftoday-sos, espn-points-against), Vegas totals
  (teamrankings-vegas), kicker/DST streaming stats (teamrankings-*), weather
  (nfl-weather), depth charts (razzball-depth-charts). For news, player_news
  is the full-text wire (with source-tweet links) and reporter_feed reads a
  specific beat writer's recent reporting. Reach for these when they bear
  on the question.
- You also have general WebSearch/WebFetch. Prefer the directory tools (structured, league-aware); reach for web search when the question needs breaking or niche information they do not cover — and still cite what you used.
- The public directory dates from ~2019: entries marked defunct cannot be
  fetched, and even live pages may have moved content. Say so when a source
  comes back thin instead of guessing.
- Waiver-board drop suggestions have two known blind spots: the engine has
  no IR-slot concept (an IR-eligible player should move to an open IR slot,
  not be dropped), and a 0.0 ros projection on an IR player means the vendor
  has not priced a return — cross-check player_news before endorsing such a
  drop. Say so when a recommendation trips either.
- League payloads carry a generated timestamp — if a tool flags them STALE,
  say so and treat the numbers as of that date.
- Cite which source each claim came from; where sources disagree, say that —
  disagreement is signal, not noise. Numbers you report must come from a tool
  result in this conversation, never from memory.
"""


def _ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err(e: Exception) -> dict:
    return {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}],
            "is_error": True}


@tool(
    "list_resources",
    "Browse the fantasy-football resource directory. Call this first when you "
    "are unsure which source covers a question. Optionally filter by category "
    "(rankings, start-sit, matchups, streaming, depth-charts, trade, waivers, "
    "stats, news) or by a free-text query over names and descriptions.",
    {
        "type": "object",
        "properties": {
            "category": {"type": "string",
                         "description": "Filter to one category slug."},
            "query": {"type": "string",
                      "description": "Case-insensitive substring filter."},
        },
        "required": [],
    },
)
async def list_resources(args):
    try:
        return _ok(webtools.list_resources(args.get("category"), args.get("query")))
    except Exception as e:
        return _err(e)


@tool(
    "fetch_resource",
    "Fetch a directory resource by id (from list_resources) as readable text. "
    "Use for strength of schedule (fftoday-sos), points against "
    "(espn-points-against), Vegas lines (teamrankings-vegas), kicker/DST "
    "streaming stats (teamrankings-*), weather (nfl-weather), depth charts "
    "(razzball-depth-charts), and news (rotowire, rotoworld). Templated urls "
    "take params. Refuses defunct resources.",
    {
        "type": "object",
        "properties": {
            "resource_id": {"type": "string"},
            "params": {"type": "object",
                       "description": "Values for the url template, if any."},
            "max_chars": {"type": "integer",
                          "description": "Truncate returned text (default 12000)."},
        },
        "required": ["resource_id"],
    },
)
async def fetch_resource(args):
    try:
        return _ok(webtools.fetch_resource(
            args["resource_id"], args.get("params"),
            args.get("max_chars") or webtools.DEFAULT_MAX_CHARS))
    except Exception as e:
        return _err(e)


@tool(
    "fetch_url",
    "Fetch any URL on a host that appears in the resource directory (for "
    "following links within those sites). Returns readable text. Hosts outside "
    "the directory are refused.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "max_chars": {"type": "integer"},
        },
        "required": ["url"],
    },
)
async def fetch_url(args):
    try:
        return _ok(webtools.fetch_page(
            args["url"], args.get("max_chars") or webtools.DEFAULT_MAX_CHARS))
    except Exception as e:
        return _err(e)


@tool(
    "boris_chen_tiers",
    "Boris Chen's weekly tiered rankings, grouped by tier — shows where the "
    "consensus has a real gap vs rounding noise. One ranking signal among "
    "several: corroborate with fantasypros_rankings. QB/K/DST have no scoring "
    "variants; RB/WR/TE/FLX take ppr, half-ppr, or standard. Weekly only.",
    {
        "type": "object",
        "properties": {
            "position": {"type": "string",
                         "enum": ["QB", "RB", "WR", "TE", "K", "DST", "FLX"]},
            "scoring": {"type": "string",
                        "enum": ["ppr", "half-ppr", "standard"],
                        "description": "Ignored for QB/K/DST. Default half-ppr."},
        },
        "required": ["position"],
    },
)
async def boris_chen_tiers(args):
    try:
        return _ok(webtools.boris_chen_tiers(
            args["position"], args.get("scoring") or "half-ppr"))
    except Exception as e:
        return _err(e)


@tool(
    "fantasypros_rankings",
    "FantasyPros expert-consensus rankings (ECR), structured: rank, consensus "
    "avg ± std with best/worst, start/sit grade, matchup, ownership, and rank "
    "movement since last update. ~40 experts. Call this for any ranking or "
    "start/sit question, alongside boris_chen_tiers — the std and best/worst "
    "spread shows where experts disagree. Weekly view.",
    {
        "type": "object",
        "properties": {
            "position": {"type": "string",
                         "enum": ["QB", "RB", "WR", "TE", "K", "DST", "FLEX"]},
            "scoring": {"type": "string",
                        "enum": ["ppr", "half-ppr", "standard"],
                        "description": "Ignored for QB/K/DST. Default half-ppr."},
            "limit": {"type": "integer",
                      "description": "How many rows (default 40)."},
            "horizon": {"type": "string", "enum": ["week", "ros"],
                        "description": "week (default) or rest-of-season."},
        },
        "required": ["position"],
    },
)
async def fantasypros_rankings(args):
    try:
        return _ok(webtools.fantasypros_rankings(
            args["position"], args.get("scoring") or "half-ppr",
            args.get("limit") or 40, args.get("horizon") or "week"))
    except Exception as e:
        return _err(e)


@tool(
    "league_tiers",
    "Boris Chen's tier method (Gaussian-mixture clustering, ported from his "
    "fftiers repo) run live and ADAPTED TO ONE OF THE USER'S LEAGUES: picks "
    "the right scoring variant automatically and tags every player as [MINE], "
    "[FA], or owned-by-whom. source='consensus' clusters FantasyPros expert "
    "ranks (his exact pipeline, a this-week view); 'projections_week' clusters "
    "this league's own THIS-WEEK projections (start/sit with league scoring); "
    "'projections' clusters rest-of-season league projections (a VALUE view "
    "for trades/waivers). Prefer over boris_chen_tiers for league questions.",
    {
        "type": "object",
        "properties": {
            "league": {"type": "string",
                       "description": "League key (fuzzy ok); omit if only one."},
            "position": {"type": "string",
                         "enum": ["QB", "RB", "WR", "TE", "K", "DST", "FLEX"]},
            "source": {"type": "string",
                       "enum": ["consensus", "projections"],
                       "description": "consensus = FP expert ranks; "
                                      "projections = league-scored points."},
            "horizon": {"type": "string", "enum": ["week", "ros"],
                        "description": "week = start/sit view; ros = "
                                       "rest-of-season value view (waivers/"
                                       "trades). Defaults: consensus->week, "
                                       "projections->ros."},
        },
        "required": ["position"],
    },
)
async def league_tiers(args):
    try:
        return _ok(tiers.league_tiers(args.get("league"), args["position"],
                                      args.get("source") or "consensus",
                                      args.get("horizon")))
    except Exception as e:
        return _err(e)


@tool(
    "twitter_handles",
    "The wiki's Twitter/X handle lists: fantasy experts, and NFL beat writers "
    "by team (~2019 vintage — say so). Tweets themselves cannot be fetched (X "
    "blocks anonymous reads) — use this to find WHO covers a team, then get "
    "their actual content via reporter_feed (their recent reporting) or "
    "player_news (the full-text news wire).",
    {
        "type": "object",
        "properties": {"team": {"type": "string"}},
        "required": [],
    },
)
async def twitter_handles(args):
    try:
        return _ok(webtools.twitter_handles(args.get("team")))
    except Exception as e:
        return _err(e)


@tool(
    "player_news",
    "The live NFL news wire (Rotowire): full report text with beat-writer "
    "attribution and a link to the source tweet — the readable version of the "
    "Twitter firehose. Optional query filters by player, team, or position. "
    "Call this for injuries, role changes, actives/inactives, transactions.",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Player/team/position substring filter."},
            "limit": {"type": "integer", "description": "Max items (default 10)."},
        },
        "required": [],
    },
)
async def player_news(args):
    try:
        return _ok(webtools.player_news(args.get("query"),
                                        args.get("limit") or 10))
    except Exception as e:
        return _err(e)


@tool(
    "reporter_feed",
    "Recent reporting by (or citing) one person from the Twitter lists — the "
    "content of their timeline, via news syndication. Pass a handle "
    "(@AdamSchefter) or a real name; headline-level with dates. Use after "
    "twitter_handles to actually read what a beat writer is reporting.",
    {
        "type": "object",
        "properties": {
            "who": {"type": "string",
                    "description": "Handle or real name of the reporter/expert."},
            "limit": {"type": "integer", "description": "Max items (default 12)."},
        },
        "required": ["who"],
    },
)
async def reporter_feed(args):
    try:
        return _ok(webtools.reporter_feed(args["who"], args.get("limit") or 12))
    except Exception as e:
        return _err(e)


# ── local-league tools (server `myleague`) ───────────────────────────────────
@tool(
    "list_leagues",
    "List the user's fantasy leagues (key, name, season, their team) and how "
    "fresh each league's generated data payload is. Call this first when the "
    "question doesn't name a league.",
    {"type": "object", "properties": {}, "required": []},
)
async def list_leagues(args):
    try:
        return _ok(leaguetools.list_leagues())
    except Exception as e:
        return _err(e)


@tool(
    "league_details",
    "One league's rules and current state: size, scoring, lineup slots, "
    "acquisition model (FAAB/priority), playoff format, and full standings "
    "with records, FAAB left, and playoff odds. `league` is a key from "
    "list_leagues (fuzzy match ok); omit it only if there is a single league.",
    {"type": "object",
     "properties": {"league": {"type": "string"}},
     "required": []},
)
async def league_details(args):
    try:
        return _ok(leaguetools.league_details(args.get("league")))
    except Exception as e:
        return _err(e)


@tool(
    "team_details",
    "A team's full detail in one league: record, FAAB/priority, playoff odds, "
    "starter-slot needs, the complete roster with rest-of-season and last-3 "
    "ppg, and that manager's in-season tendencies. Omit `team` for the user's "
    "own team; otherwise match by team name, manager name, or team id.",
    {"type": "object",
     "properties": {"league": {"type": "string"}, "team": {"type": "string"}},
     "required": []},
)
async def team_details(args):
    try:
        return _ok(leaguetools.team_details(args.get("league"), args.get("team")))
    except Exception as e:
        return _err(e)


@tool(
    "opponent_tendencies",
    "The cached opponent modeling, calibrated from the league's own history: "
    "per manager, waiver aggression / contested-claim win rate / max bid / "
    "spend pace, trade rate / accept rate / position flow, drop latency — plus "
    "auction-day tendencies (positional pay multipliers, stars-and-scrubs "
    "concentration, max buy) where the league drafts by auction. Use this "
    "before sizing a waiver bid or pitching a trade. Optional `manager` "
    "filters to one manager/team.",
    {"type": "object",
     "properties": {"league": {"type": "string"}, "manager": {"type": "string"}},
     "required": []},
)
async def opponent_tendencies(args):
    try:
        return _ok(leaguetools.opponent_tendencies(args.get("league"),
                                                   args.get("manager")))
    except Exception as e:
        return _err(e)


@tool(
    "evaluate_trade",
    "Evaluate a SPECIFIC trade with the league engine: both sides' optimal-"
    "lineup delta over the remaining weeks, the playoff-weeks delta, accept "
    "odds from the partner's calibrated tendencies, veto risk, and a counter-"
    "offer search. send/receive take player names (or ids) — send = players "
    "leaving the user's roster, receive = players from the partner's. Use for "
    "any 'should I take/offer this trade' question; use fp_league_value(ros) "
    "as the independent second opinion.",
    {
        "type": "object",
        "properties": {
            "league": {"type": "string"},
            "partner": {"type": "string",
                        "description": "Partner team/manager name or id."},
            "send": {"type": "array", "items": {"type": "string"}},
            "receive": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["partner", "send", "receive"],
    },
)
async def evaluate_trade(args):
    try:
        return _ok(leaguetools.trade_eval_text(
            args.get("league"), args["partner"], args["send"], args["receive"]))
    except Exception as e:
        return _err(e)


@tool(
    "console_boards",
    "The in-season console's precomputed recommendations from the latest "
    "pipeline run: board='waivers' (add targets with suggested bids and "
    "contest odds), 'trades' (mutually-positive trade finder), 'buy_low', or "
    "'sell_high'. These already account for opponent tendencies — prefer them "
    "over re-deriving.",
    {"type": "object",
     "properties": {"league": {"type": "string"},
                    "board": {"type": "string",
                              "enum": ["waivers", "trades", "buy_low", "sell_high"]},
                    "limit": {"type": "integer"}},
     "required": ["board"]},
)
async def console_boards(args):
    try:
        return _ok(leaguetools.console_boards(args.get("league"), args["board"],
                                              args.get("limit") or 8))
    except Exception as e:
        return _err(e)


@tool(
    "weekly_lineup",
    "The optimal lineup for THIS WEEK in one of the user's leagues, computed "
    "by the repo's engine (2QB/superflex-safe) from this week's projections — "
    "rosters lock weekly (Tue-Tue), so THIS is the start/sit baseline, not "
    "rest-of-season numbers. Shows each starter and bench player with this-week "
    "/ ros / last-3 numbers. Omit team for the user's own team.",
    {
        "type": "object",
        "properties": {"league": {"type": "string"}, "team": {"type": "string"}},
        "required": [],
    },
)
async def weekly_lineup(args):
    try:
        return _ok(leaguetools.weekly_lineup(args.get("league"), args.get("team")))
    except Exception as e:
        return _err(e)


@tool(
    "fp_league_value",
    "THE league-adapted FantasyPros calculation: value over replacement (VOR) "
    "built purely from FantasyPros consensus points, with both adaptations "
    "from the league config — scoring picks the FP flavor, and roster size/"
    "lineup structure (teams x slots, FLEX/superflex pooled) sets the "
    "replacement baseline per position. Cross-position comparable: use it for "
    "'who is actually most valuable IN MY LEAGUE', trade fairness, and how "
    "much positional scarcity my league shape creates. Weekly view.",
    {
        "type": "object",
        "properties": {
            "league": {"type": "string"},
            "limit": {"type": "integer", "description": "Rows (default 40)."},
            "horizon": {"type": "string", "enum": ["week", "ros"],
                        "description": "week = this-week value (start/sit); "
                                       "ros = season-total value (waivers and "
                                       "TRADE fairness — use this for trades)."},
        },
        "required": [],
    },
)
async def fp_league_value(args):
    try:
        return _ok(fpvalue.fp_league_value_text(args.get("league"),
                                                args.get("limit") or 40,
                                                args.get("horizon") or "week"))
    except Exception as e:
        return _err(e)


WEB_TOOLS = [list_resources, fetch_resource, fetch_url, boris_chen_tiers,
             fantasypros_rankings, league_tiers, fp_league_value,
             twitter_handles, player_news,
             reporter_feed]
LEAGUE_TOOLS = [list_leagues, league_details, team_details, opponent_tendencies,
                console_boards, weekly_lineup, evaluate_trade]

server = create_sdk_mcp_server(name="ffresearch", version="1.0.0", tools=WEB_TOOLS)
league_server = create_sdk_mcp_server(name="myleague", version="1.0.0",
                                      tools=LEAGUE_TOOLS)


def build_options(model: str = DEFAULT_MODEL, max_turns: int = 30) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        mcp_servers={"ffresearch": server, "myleague": league_server},
        allowed_tools=[
            "mcp__ffresearch__list_resources",
            "mcp__ffresearch__fetch_resource",
            "mcp__ffresearch__fetch_url",
            "mcp__ffresearch__boris_chen_tiers",
            "mcp__ffresearch__fantasypros_rankings",
            "mcp__ffresearch__league_tiers",
            "mcp__ffresearch__fp_league_value",
            "mcp__ffresearch__twitter_handles",
            "mcp__ffresearch__player_news",
            "mcp__ffresearch__reporter_feed",
            "WebSearch",
            "WebFetch",
            "mcp__myleague__list_leagues",
            "mcp__myleague__league_details",
            "mcp__myleague__team_details",
            "mcp__myleague__opponent_tendencies",
            "mcp__myleague__console_boards",
            "mcp__myleague__weekly_lineup",
            "mcp__myleague__evaluate_trade",
        ],
        # WebSearch/WebFetch are ON (user request): breaking news and pages
        # beyond the directory. Filesystem/shell stay off.
        disallowed_tools=["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        system_prompt=SYSTEM_PROMPT,
        model=model,
        max_turns=max_turns,
    )


async def run(prompt: str, model: str = DEFAULT_MODEL, verbose: bool = False) -> str:
    """One research question in, the agent's final answer out."""
    final = ""
    async for message in query(prompt=prompt, options=build_options(model)):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text") and block.text:
                    if verbose:
                        print(block.text, flush=True)
                elif verbose and hasattr(block, "name"):
                    print(f"  [tool: {block.name}]", file=sys.stderr, flush=True)
        elif isinstance(message, ResultMessage):
            final = message.result or ""
    return final


def main() -> None:
    ap = argparse.ArgumentParser(description="Fantasy-football research agent")
    ap.add_argument("prompt", help="the research question")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="print only the final answer")
    args = ap.parse_args()
    answer = asyncio.run(run(args.prompt, args.model, verbose=not args.quiet))
    if args.quiet:
        print(answer)


if __name__ == "__main__":
    main()
