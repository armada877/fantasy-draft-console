"""Stdio MCP server exposing the research agent's tools to any MCP client —
notably the `ff-research` Claude Code plugin (see claude_plugin/).

Same ten tools as agent.py, but served over stdio instead of in-process, so a
Claude Code session gets them natively as mcp__ff-research__*. No API key
needed here — these are deterministic fetch/read tools; the calling session
supplies the intelligence.

Run directly:  .venv/bin/python -m research_agent.mcp_server
"""
from __future__ import annotations

import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import fpvalue, leaguetools, tiers, webtools

server = MCPServer(
    name="ff-research",
    instructions=(
        "Fantasy-football research tools. Public web: the r/fantasyfootball "
        "wiki resource directory (rankings, tiers, schedules, Vegas lines, "
        "weather, news). Local leagues: the user's own ESPN leagues — rules, "
        "rosters, the calibrated opponent-tendency model, and the in-season "
        "console's waiver/trade boards. Local tools read generated payloads "
        "that carry a freshness note; report staleness rather than guessing."),
    version="1.0.0",
)


def _clean_errors(fn):
    """Expected failures (bad league key, off-allowlist host, defunct resource)
    become ToolError so the client gets the message, not a traceback."""
    @functools.wraps(fn)
    def wrapped(*args, **kw):
        try:
            return fn(*args, **kw)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"{type(e).__name__}: {e}") from None
    return wrapped


# ── public web ───────────────────────────────────────────────────────────────
@server.tool(name="list_resources",
             description="Browse the fantasy-football public resource directory. "
             "Optional category (rankings, start-sit, matchups, streaming, "
             "depth-charts, trade, waivers, stats, news) or free-text query.")
@_clean_errors
def list_resources(category: str | None = None, query: str | None = None) -> str:
    return webtools.list_resources(category, query)


@server.tool(name="fetch_resource",
             description="Fetch a directory resource by id (from list_resources) "
             "as readable text. Templated URLs need params, e.g. "
             "fantasypros-rankings with {'pos': 'rb'}. Refuses defunct entries.")
@_clean_errors
def fetch_resource(resource_id: str, params: dict | None = None,
                   max_chars: int = webtools.DEFAULT_MAX_CHARS) -> str:
    return webtools.fetch_resource(resource_id, params, max_chars)


@server.tool(name="fetch_url",
             description="Fetch any URL on a host named by the resource "
             "directory (for following links within those sites). Other hosts "
             "are refused.")
@_clean_errors
def fetch_url(url: str, max_chars: int = webtools.DEFAULT_MAX_CHARS) -> str:
    return webtools.fetch_page(url, max_chars)


@server.tool(name="boris_chen_tiers",
             description="Boris Chen's weekly tiered rankings grouped by tier — "
             "shows where consensus has a real gap vs rounding noise. One "
             "ranking signal among several: corroborate with "
             "fantasypros_rankings. position: QB/RB/WR/TE/K/DST/FLX; scoring: "
             "ppr/half-ppr/standard (ignored for QB/K/DST). Weekly only.")
@_clean_errors
def boris_chen_tiers(position: str, scoring: str = "half-ppr") -> str:
    return webtools.boris_chen_tiers(position, scoring)


@server.tool(name="fantasypros_rankings",
             description="FantasyPros expert-consensus rankings (ECR), "
             "structured: rank, consensus avg ± std with best/worst, start/sit "
             "grade, matchup, ownership, rank movement. ~40 experts. Call for "
             "any ranking or start/sit question, alongside boris_chen_tiers — "
             "the spread shows where experts disagree. position: "
             "QB/RB/WR/TE/K/DST/FLEX; scoring ignored for QB/K/DST.")
@_clean_errors
def fantasypros_rankings(position: str, scoring: str = "half-ppr",
                         limit: int = 40, horizon: str = "week") -> str:
    return webtools.fantasypros_rankings(position, scoring, limit, horizon)


@server.tool(name="league_tiers",
             description="Boris Chen's tier method (GMM clustering, ported "
             "from his fftiers repo) run live and adapted to one of the "
             "user's leagues: auto-picks the scoring variant and tags players "
             "[MINE]/[FA]/owned-by-whom. source='consensus': FantasyPros expert "
             "ranks, this-week view. 'projections_week': the league's own "
             "THIS-WEEK projections (start/sit with league scoring). "
             "'projections': rest-of-season league projections (VALUE view "
             "for trades/waivers). Prefer over boris_chen_tiers for league "
             "questions. position: QB/RB/WR/TE/K/DST/FLEX.")
@_clean_errors
def league_tiers(league: str | None = None, position: str = "RB",
                 source: str = "consensus", horizon: str | None = None) -> str:
    return tiers.league_tiers(league, position, source, horizon)


@server.tool(name="fp_league_value",
             description="League-adapted FantasyPros value over replacement "
             "(VOR): FP consensus points in the league's scoring flavor, minus "
             "a replacement baseline from the league's size and lineup "
             "structure (FLEX/superflex pooled). Cross-position comparable — "
             "'who is most valuable IN MY LEAGUE', trade fairness, positional "
             "scarcity. horizon: week (start/sit) or ros (season-total — use for trades/waivers).")
@_clean_errors
def fp_league_value(league: str | None = None, limit: int = 40,
                    horizon: str = "week") -> str:
    return fpvalue.fp_league_value_text(league, limit, horizon)


@server.tool(name="twitter_handles",
             description="The wiki's Twitter/X lists: fantasy experts (no args) "
             "or a team's beat writers (team name, substring ok). ~2019 vintage "
             "— surface with that caveat. Tweets can't be fetched (X blocks "
             "anonymous reads) — read the content via reporter_feed or "
             "player_news instead.")
@_clean_errors
def twitter_handles(team: str | None = None) -> str:
    return webtools.twitter_handles(team)


@server.tool(name="player_news",
             description="The live NFL news wire (Rotowire): full report text "
             "with beat-writer attribution and source-tweet links — the "
             "readable Twitter firehose. Optional query filters by "
             "player/team/position. Use for injuries, role changes, "
             "actives/inactives, transactions.")
@_clean_errors
def player_news(query: str | None = None, limit: int = 10) -> str:
    return webtools.player_news(query, limit)


@server.tool(name="reporter_feed",
             description="Recent reporting by (or citing) one person from the "
             "Twitter lists, via news syndication — the content of their "
             "timeline. Pass a handle (@AdamSchefter) or real name; "
             "headline-level with dates.")
@_clean_errors
def reporter_feed(who: str, limit: int = 12) -> str:
    return webtools.reporter_feed(who, limit)


# ── local leagues ────────────────────────────────────────────────────────────
@server.tool(name="list_leagues",
             description="The user's fantasy leagues (key, name, season, their "
             "team) and how fresh each league's generated data payload is. Call "
             "first when the league is ambiguous.")
@_clean_errors
def list_leagues() -> str:
    return leaguetools.list_leagues()


@server.tool(name="league_details",
             description="One league's rules and standings: size, scoring, "
             "lineup slots, FAAB/priority model, playoff format, and every "
             "team's record, FAAB left, and playoff odds. league = key from "
             "list_leagues (fuzzy ok).")
@_clean_errors
def league_details(league: str | None = None) -> str:
    return leaguetools.league_details(league)


@server.tool(name="team_details",
             description="A team's detail in one league: record, needs, full "
             "roster with ros/last-3 ppg + ECR, and the manager's in-season "
             "tendencies. Omit team for the user's own; else match by team "
             "name, manager, or id.")
@_clean_errors
def team_details(league: str | None = None, team: str | None = None) -> str:
    return leaguetools.team_details(league, team)


@server.tool(name="opponent_tendencies",
             description="The cached opponent model, calibrated from league "
             "history: per manager — waiver aggression, contested-claim win "
             "rate, max bid, trade rate/accept rate/position flow, drop "
             "latency; plus auction-day multipliers where the league drafts by "
             "auction. Use before sizing a bid or pitching a trade.")
@_clean_errors
def opponent_tendencies(league: str | None = None, manager: str | None = None) -> str:
    return leaguetools.opponent_tendencies(league, manager)


@server.tool(name="weekly_lineup",
             description="The optimal lineup for THIS WEEK in one of the "
             "user's leagues, from the repo's engine (2QB/superflex-safe) on "
             "this week's projections. Rosters lock weekly (Tue-Tue) — this is "
             "the start/sit baseline; ros numbers are for value decisions. "
             "Omit team for the user's own team.")
@_clean_errors
def weekly_lineup(league: str | None = None, team: str | None = None) -> str:
    return leaguetools.weekly_lineup(league, team)


@server.tool(name="evaluate_trade",
             description="Evaluate a specific trade with the league engine: "
             "both sides' lineup delta over remaining weeks, playoff-weeks "
             "delta, accept odds from the partner's calibrated tendencies, "
             "veto risk, and a counter-offer search. send/receive take player "
             "names or ids (send = from the user's roster).")
@_clean_errors
def evaluate_trade(partner: str, send: list[str], receive: list[str],
                   league: str | None = None) -> str:
    return leaguetools.trade_eval_text(league, partner, send, receive)


@server.tool(name="console_boards",
             description="The in-season console's precomputed recommendations: "
             "board = waivers (bid targets with suggested $ and contest odds), "
             "trades (mutually-positive finder), buy_low, or sell_high. These "
             "already account for opponent tendencies.")
@_clean_errors
def console_boards(league: str | None = None, board: str = "waivers",
                   limit: int = 8) -> str:
    return leaguetools.console_boards(league, board, limit)


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
