# research_agent — fantasy-football research agent (Claude Agent SDK)

An agent whose tools wrap the [r/fantasyfootball wiki resource directory](https://www.reddit.com/r/fantasyfootball/wiki/resources/):
expert-consensus rankings, Boris Chen tiers, strength of schedule, Vegas lines,
kicker/D-ST streaming stats, depth charts, weather, news, and the wiki's
expert/beat-writer Twitter lists. Built-in agent tools (Bash, file access,
generic web) are disabled — it can only reach hosts the directory names.

## Run

```bash
# CLI (needs ANTHROPIC_API_KEY; 1Password: op read 'op://HMD LOCAL/Claude - API Key/credential')
.venv/bin/python -m research_agent "which kicker should I stream this week?"
.venv/bin/python -m research_agent -q --model claude-opus-5 "..."   # -q = final answer only

# Launcher UI (research_app/, port 8010). Login + keys come from config/.env
# (gitignored): set -a && . config/.env && set +a
# RESEARCH_USER/RESEARCH_PASSWORD gate everything but /healthz (unset pw = open).
ANTHROPIC_API_KEY=... .venv/bin/uvicorn server:app --app-dir research_app --host 127.0.0.1 --port 8010

# Same tools inside Claude Code sessions: the ff-research plugin (claude_plugin/)
# serves research_agent/mcp_server.py over stdio — no API key needed there.
```

## Tools

**Public web (MCP server `ffresearch`)**

| tool | what it does |
|---|---|
| `list_resources` | Browse/search the directory (category or free-text filter) |
| `fetch_resource` | Fetch a directory entry by id, HTML → readable text; url templates take `params` (e.g. `fantasypros-rankings` + `{"pos": "rb"}`) |
| `fetch_url` | Fetch any URL on an allowlisted (directory) host — for following links |
| `fantasypros_rankings` | Structured ECR from the embedded `ecrData` JSON (the pages are JS-rendered, so raw fetch gets nav only): rank, avg ± std, best/worst, start/sit grade, matchup, ownership |
| `fp_league_value` | THE league-adapted FantasyPros calculation: value over replacement from FP's own consensus points (`r2p_pts`), scoring flavor from the league's label, replacement baseline from the league's size + lineup (every starting seat filled, FLEX/superflex pooled). Cross-position comparable |
| `league_tiers` | Boris Chen's tier method (1-D GMM over ranks, ported from github.com/borisachen/fftiers with his per-position k and depth cutoffs) run live and adapted per league: auto-picks the scoring variant, tags players [MINE]/[FA]/owner; `source='projections'` tiers the league's own recomputed ros_ppg — covers scoring his published std/PPR/half CSVs can't |
| `boris_chen_tiers` | His published weekly tier CSVs (std/PPR/half only) — corroborated with FantasyPros, per the system prompt's two-source rule |
| `player_news` | The live news wire (Rotowire): full report text with beat-writer attribution and source-tweet links; filter by player/team/position |
| `reporter_feed` | One reporter's recent reporting via Google News RSS (handle or real name; alias map for @RapSheet-style handles) — the readable substitute for their timeline |
| `twitter_handles` | The wiki's expert list and per-team beat-writer handles (~2019 vintage, flagged as such) — who to follow; content comes from the two tools above, since X blocks anonymous reads (syndication endpoint 429s, nitter dead — probed 2026-09-15) |

**My leagues (MCP server `myleague`)** — reads the repo's own generated data,
never the network: `config/leagues.json` (registry, via `leagues.resolve`),
`draft_sheets/out/<key>/season_data.json` (the in-season payload), and
`ctx.config("tendencies.json")` (calibrated auction tendencies, where the
league has them). Implemented in `leaguetools.py`; every output carries the
payload's generated timestamp and flags it STALE past 48h.

| tool | what it does |
|---|---|
| `list_leagues` | The registered leagues + how fresh each payload is |
| `league_details` | Rules (size, scoring, lineup, FAAB/priority, playoffs) + standings with records, FAAB left, playoff odds |
| `team_details` | Any team (default: mine): record, needs, full roster with ros/last-3 ppg + ECR, and that manager's in-season tendencies |
| `opponent_tendencies` | The cached opponent model per manager: waiver aggression / contested win rate / max bid, trade rate / accept rate / position flow, drop latency; plus auction mult/conc/maxbuy where calibrated |
| `console_boards` | The console's precomputed `waivers` / `trades` / `buy_low` / `sell_high` boards, with the "why" lines |

League-identifying content stays in the local (gitignored) files these tools
read; the code itself is generic and trackable. If a payload is missing or
stale, the tools say to re-run `python3 pipeline.py week` rather than guess.

## Layout

- `catalog.py` — the wiki directory as data: id, category, url (templates), purpose,
  and a `status` from a liveness probe (2026-09-15). Dead sites (KFFL, Fantasy
  Football Metrics, MSN Fox, Nerdball, StatMilk, …) are kept as `defunct` and
  refused at fetch time. Moved sites carry their current URL (fftoolbox →
  fulltimefantasy.com, Rotoworld → nbcsports.com, CBS deep links → rankings hub).
- `webtools.py` — SDK-free tool implementations: polite allowlisted HTTP
  (1s/host delay, retries), stdlib HTML→text extraction, truncation caps.
- `agent.py` — `@tool` wrappers, `create_sdk_mcp_server`, `ClaudeAgentOptions`,
  and the CLI. Tool errors return `is_error` results, never crash the loop.
- `test_webtools.py` — network smoke test, no API key:
  `.venv/bin/python -m research_agent.test_webtools`

## Dependency note (read before touching the venv)

`claude-agent-sdk` depends on `mcp`, which on Python ≥3.14 declares
`starlette>=0.48` — but the draft console pins `fastapi==0.115.6`
(`starlette<0.42`). The venv deliberately holds **starlette 0.41.3**: the
console server is the thing that must not regress, and the Agent SDK's
in-process MCP server never touches starlette's HTTP paths (verified: full
agent run works). `pip check` will report the mcp/sse-starlette mismatch —
that is expected. If you ever need mcp's own HTTP transports, upgrade fastapi
instead of bumping starlette.
