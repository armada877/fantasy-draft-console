# Fantasy Console — draft + manage

One app, two modes:

- **Draft (pre-season)** — a dynamic command console for a **fantasy football auction
  draft**, with a thin LLM advisor. It re-prices every remaining player in real time from
  the calibrated bidding tendencies of each opponent, tracks your build against a target
  roster, surfaces value/scarcity as the board moves, and (optionally) calls Claude for a
  live read of the room after every pick.
- **Manage (in-season)** — the **fftiers board**: one page, every league you play in,
  each player valued three independent ways — Boris Chen-style expert-consensus tiers
  (GMM clustering), an elboberto-workbook VBD port, and a CSG games-based VBD port — at
  two horizons (this week / rest of season), with your roster and free agents flagged.
  Rebuilt from live ESPN or Sleeper data + FantasyPros ranks with one command every week.

Built for specific leagues, but the framework is **bring-your-own-league**: the code and
the universal projection baseline are tracked here; your league configs, data, secrets,
and advisor briefing stay local (see [What's ignored](#whats-ignored)).

## What it does

**Draft mode** (`/draft`):

- **On the block** — type the nominated player → **Worth**, live **Will go ≈** (predicted
  sale price from opponents' calibrated bids × market inflation), and a **dynamic Max bid**
  that adjusts all draft long for value, scarcity, market trend, your budget and roster.
- **Value board / Projections** — best-available ranked by worth, or raw projections
  (FPTS / VORP / tiers) filterable by position **and tier**, with live scarcity/cliffs.
- **Your build vs target** — roster (starters + bench) tracked against a target split.
- **Advisor (LLM)** — ask anything, or let it auto-post a read after each pick.

**Manage mode** (`/manage`):

- **Three methods, side by side** — consensus-rank tiers vs. two VBD engines run on your
  league's exact scoring and roster shape; when they disagree about a player, that's the
  signal.
- **Two horizons** — start/sit reads (this week) and trade/waiver reads (rest of season).
- **Multi-league tabs** — every league in `config/boards.json`, each with your roster,
  lineup shape, and free-agent flags.
- **Weekly one-liner** — `python3 pipeline.py week` pulls ESPN projections + rosters and
  FantasyPros ranks, rebuilds every board, and re-renders the page.

## Architecture

```
DRAFT                                              MANAGE
projections .xlsm ─┐                               ESPN (fftiers-espn) ─┐
ESPN league scrape ┼► build_tool_data.py           FantasyPros ranks ───┼► pipeline.py week
tendencies.json ───┘        │                      leagues/*.yaml ──────┘   (pull→tiers→vbd→csg→board)
                            ▼                                                    │
draft_tool_template.html + tool_data.json          board_template.html + viz-data.json
        └────► static/index.html                           └────► static/board.html
                     │                                                │
                 draft_app/server.py (FastAPI): /draft · /manage · /api/advise
```

`pipeline.py` drives both — draft stages (`scrape calibrate csg simulate build inject`,
or `all`) and manage stages (`pull tiers vbd-boards csg-boards board`, or `week`).

- **League-accurate valuation, both modes:** the draft builder **recomputes** FPTS/VBD/
  auction-$ from your league's scoring and roster; the manage boards run the ported
  elboberto and CSG engines on the same league-exact settings (verified against their
  source workbooks).
- **Sleeper leagues (draft mode)** use `scraping/scrape_sleeper.py` instead of the ESPN
  scraper (no auth). It adapts Sleeper's API into the same ESPN-shaped `league_full.json`,
  so everything downstream is platform-agnostic. See [`scraping/README.md`](scraping/README.md).
- **fftiers** (`fftiers/`) is also usable standalone: `fftiers --league leagues/my.yaml`
  (tier charts), `fftiers-vbd`, `fftiers-csg`, `fftiers-espn discover|sync-league|pull|roster`.
  Manage mode reads ESPN only.

### Opponent tendencies (draft)
The per-manager bid model (`mult`/`conc`/`maxbuy`) is calibrated from auction history via
`python3 pipeline.py calibrate` (writes `config/tendencies.json`); with no history every
opponent stays neutral, or hand-write the file from what you know about your leaguemates.

## Quickstart

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r draft_app/requirements.txt   # server + draft build
pip install -e .                            # fftiers (manage mode)

# 1) Configure
cp config/league.example.json config/league.json    # draft: league_id, season, your team ("me")
cp config/boards.example.json config/boards.json    # manage: leagues, team ids
cp config/env.example config/.env                   # ESPN_SWID + ESPN_S2 (+ ANTHROPIC_API_KEY,
set -a && . config/.env && set +a                   #  FANTASYPROS_API_KEY, optional)

# 2) Draft mode
python3 pipeline.py scrape build inject             # ESPN; or `all` when you have auction history
#    Sleeper: no cookies needed — set "sleeper_league_id" in config/league.json, then:
python3 scraping/scrape_sleeper.py && python3 pipeline.py build inject

# 3) Manage mode
python3 -m fftiers.espn_cli sync-league <league_id> # writes leagues/<key>.yaml from ESPN
#    Sleeper: python3 -m fftiers.sleeper_cli sync-league <league_id> --dest leagues/<key>.yaml
python3 pipeline.py week                            # pull + boards + render

# 4) (Optional) enable the advisor — see Configuration below
cp config/briefing.example.md config/briefing.md     # then customize it for your league
# Put ANTHROPIC_API_KEY in config/.env — the server loads that file itself at startup,
# so no shell sourcing is needed (and it works the same on Windows). Restart to pick up edits.

# 5) Run
cd draft_app && uvicorn server:app --host 127.0.0.1 --port 8000
# open http://127.0.0.1:8000  (/ → manage; /draft for the auction console)
```

Run `pipeline.py` with the venv active (or `.venv/bin/python pipeline.py …`) — the draft
build needs `openpyxl`, the manage stages need `scikit-learn`/`matplotlib`.

Without `ANTHROPIC_API_KEY` everything still works; only the Advisor panel is disabled.
Without `FANTASYPROS_API_KEY`, manage `pull` reads the consensus ranks from the public
FantasyPros ranking pages. Those pages give the current week and rest of season, which
is all that the board uses. If a rank fetch fails, the board still builds and says that
the tier columns are empty.

### Sleeper leagues on the manage board

A `config/boards.json` entry with `"platform": "sleeper"` reads Sleeper instead of ESPN.
It needs no cookies. Set `league_id` (the number in the sleeper.com league URL) and
`user` (your Sleeper username), or `team_id` (your `roster_id`). See
`config/boards.example.json`. Make the league YAML once, and name it after the key:

```bash
python3 -m fftiers.sleeper_cli sync-league <league_id> --dest leagues/<key>.yaml
```

It writes the team count, the roster slots (FLEX, K, DEF as DST, bench, IR), the scoring
keys that the tiers read, `week_one_tuesday`, and the last fantasy week. It lists any
offense scoring (for example first downs) that consensus ranks do not reflect.

`pull` and `sync` (`fftiers/sleeper.py`) write the same `dat/espn/<key>-*` files as for
ESPN, so the vbd, csg, and board stages do not change. They read these public endpoints:

| Data | Endpoint |
|---|---|
| League settings, `scoring_settings`, `roster_positions` | `https://api.sleeper.app/v1/league/<id>` |
| Managers and team names | `https://api.sleeper.app/v1/league/<id>/users` |
| Rosters, set starters, IR (`reserve`) | `https://api.sleeper.app/v1/league/<id>/rosters` |
| Current week and season | `https://api.sleeper.app/v1/state/nfl` |
| A username's `user_id` | `https://api.sleeper.app/v1/user/<username>` |
| Weekly projections, with player name, position, and injury status | `https://api.sleeper.app/projections/nfl/<season>/<week>?season_type=regular&position[]=QB&…&position[]=DEF` |
| All players (only if a rostered player has no projection row) | `https://api.sleeper.app/v1/players/nfl` |

The projections endpoint is not documented. It returns a list of
`{player_id, week, stats: {pass_yd, rush_fd, rec, …}, player: {first_name, last_name,
position, injury_status}}`. The stat keys are the same as the `scoring_settings` keys, so
a player's points are the sum of each stat times the league's value for it. That
includes K and DEF scoring. Rest of season is the sum of each week from the current week
to the last playoff week.

## Configuration

**All of your custom, league-specific setup lives in one un-pushed directory: `config/`**
(plus per-league `leagues/*.yaml`). `.gitignore` keeps everything local except the
`*.example` templates and README.

```bash
cp config/league.example.json  config/league.json   # draft league: id, season, "me"
cp config/boards.example.json  config/boards.json   # manage leagues: ids, team ids, yamls
cp config/briefing.example.md  config/briefing.md   # advisor prompt: your opponents + plan
cp config/env.example          config/.env          # secrets: ANTHROPIC_API_KEY (+ ESPN cookies)
set -a && . config/.env && set +a                   # only needed for the scrapers' env vars
```

See [`config/README.md`](config/README.md) for the full table. Generated data
(`tool_data.json`, `static/*.html`, `dat/`, `out/`, `reports/`, scrapes) is built by the
pipeline, not committed.

**Second machine?** Because all of that is gitignored, a fresh clone has no league in it.
`sync_league_data.py` gathers those paths into one folder you can put in iCloud/Dropbox:

```bash
python3 sync_league_data.py export ~/Dropbox/my-league   # repo   -> folder
python3 sync_league_data.py import ~/Dropbox/my-league   # folder -> repo
```
Secrets (`config/.env`, `scraping/.espn_auth.json`) are skipped unless you pass
`--with-secrets`; only do that if the destination is private to you.

## The advisor

`POST /api/advise` sends `{question, state, model}` to Claude. The **system prompt** is
loaded from `config/briefing.md` (gitignored; start from `config/briefing.example.md`).
The **live state** — every team's budget, needs and roster, best-available, inflation,
and the player on the block — is posted on every call. Model is chosen from the dropdown
(allow-listed in `server.py`).

**Eval:** `python3 draft_app/eval_advisor.py` runs a tendency-driven mock auction and
probes the advisor at checkpoints, checking it stays grounded and on-strategy.

## Deploy on Railway

`./deploy_railway.sh` stages only the runtime surface (server, both generated pages, the
briefing) into a temp bundle and runs `railway up` — nothing league-private touches git.
Set `ANTHROPIC_API_KEY`, `CONSOLE_PASSWORD` (HTTP Basic on everything but `/healthz`),
and optionally `DEFAULT_MODE=draft|manage`. See `draft_app/README.md`.

## Deploy on Vercel

Vercel builds the draft console and the manage board from git on each push. The build
scrapes a public Sleeper league, so no league data goes into git. This works for Sleeper
leagues only: the ESPN scrape needs private cookies.

How it works:

- `draft_app/vercel.json` sets the FastAPI preset and the build command
  `cd .. && python vercel_build.py`.
- Vercel installs the runtime packages from `draft_app/pyproject.toml` (fastapi,
  uvicorn, anthropic). Those are the function's packages. `draft_app/requirements.txt`
  stays the full list for local runs and Railway.
- `vercel_build.py` writes `config/league.json` from `LEAGUE_CONFIG_JSON`. It installs
  `requirements-build.txt` (openpyxl) into a temp directory outside the function. Then it
  runs `scrape_sleeper.py`, `scrape_sleeper_history.py`, `scrape_sleeper_keepers.py`,
  `pipeline.py calibrate build`, `scrape_sleeper_status.py` and `pipeline.py build inject`.
  The build fails if a step fails, and Vercel keeps the last good deploy.
- Then `vercel_build.py` builds the manage board. It writes `config/boards.json` from
  `BOARDS_CONFIG_JSON`, or, when that is not set, from `sleeper_league_id` and
  `sleeper_username` (or `me`) in `LEAGUE_CONFIG_JSON`. It writes each league YAML from
  the Sleeper settings, and installs `requirements-board.txt` (scikit-learn, PyYAML) into
  the same temp directory. Then it runs `pipeline.py pull vbd-boards csg-boards board`
  and copies the Wire's reporter list into `draft_app/`. ESPN entries are skipped. If a
  board step fails, the log shows `✗ MANAGE BOARD NOT BUILT: <reason>`, the draft console
  still deploys, and `/manage` returns 404.
- The function holds only `draft_app/` (about 25 MB). Static files stay in the function
  (`cdn = false`), so `CONSOLE_PASSWORD` also guards `/data.json`.

Project settings (Settings → Build and Deployment):

| Setting | Value |
|---|---|
| Root Directory | `draft_app` |
| Include files outside the root directory in the Build Step | Enabled |
| Framework Preset | FastAPI (`draft_app/vercel.json` also sets it) |
| Build Command | no override (`draft_app/vercel.json` sets it) |
| Install Command | no override |
| Output Directory | no override |

Environment variables (Production; the build and the function both read them):

| Name | Value | Used by |
|---|---|---|
| `LEAGUE_CONFIG_JSON` | the content of `config/league.json`: `sleeper_league_id`, `season`, `sleeper_username` or `me`, `roster` with K and DST | build (required) |
| `CONSOLE_PASSWORD` | a long random password | build (required) and function: HTTP Basic, user `draft` |
| `DEFAULT_MODE` | `manage` or `draft` | function: `/` goes to `/manage` or `/draft` |
| `BOARDS_CONFIG_JSON` | the content of `config/boards.json`, Sleeper entries only | build (optional): the board leagues; the default is the `LEAGUE_CONFIG_JSON` league |
| `ANTHROPIC_API_KEY` | your API key | function: the advisor |
| `STRATEGY_BRIEFING_MD` | the content of `config/briefing.md` | function: the advisor prompt (optional) |
| `CONSOLE_USER` | the HTTP Basic user name | function (optional, default `draft`) |
| `ALLOW_PUBLIC_CONSOLE` | `1` | build (optional): permits a deploy without `CONSOLE_PASSWORD` |

All env vars together must be smaller than 64 KB. Without `CONSOLE_PASSWORD`, the
console is public and `/api/advise` spends your API credit. So the build fails when
`CONSOLE_PASSWORD` is empty. To deploy a public console on purpose, set
`ALLOW_PUBLIC_CONSOLE=1`. The build also fails without `LEAGUE_CONFIG_JSON` or without
`roster`, because `scrape_sleeper.py` drops the K and DEF slots. Vercel reads env vars at
deploy time, so redeploy after you change one.

To test the build on your machine, run it in a temporary clone, because it overwrites
`config/league.json`:

```bash
CONSOLE_PASSWORD=test LEAGUE_CONFIG_JSON="$(cat config/league.json)" python3 vercel_build.py
```

The board on Vercel is a snapshot from the build. The page's Sync and Full refresh buttons
need the pipeline in the deploy, so on Vercel they return 501. A new deploy rebuilds the
board with fresh rosters, projections, and ranks. To refresh it on a schedule:

1. In the Vercel project, open Settings → Git → Deploy Hooks. Make a hook for the branch
   `main`. Keep the URL secret: anyone with it can start a deploy.
2. Call the hook from a scheduler, for example a GitHub Actions workflow with
   `on: schedule: - cron: "0 13 * * 2,4,0"` (Tuesday, Thursday, and Sunday) and one step:
   `curl -fsS -X POST "$DEPLOY_HOOK_URL"`, with the URL in a repository secret. The
   repository is public, so never put the URL in the workflow file.

To refresh one time, run `vercel deploy --prod`, use Redeploy in the dashboard, or
`curl -X POST` the hook URL.

## Refresh data

- **Weekly (manage):** `python3 pipeline.py week` — new ESPN or Sleeper projections +
  rosters, fresh FantasyPros ranks, all boards, re-rendered page. On Vercel, a deploy
  does this (see [Deploy on Vercel](#deploy-on-vercel)).
- **New season (draft):** drop the new Elboberto `.xlsm` into `draft_sheets/`, point
  `config/league.json` at it, then `python3 pipeline.py scrape build inject` (or `all` to
  also refresh opponent calibration).

## What's ignored

`.gitignore` keeps league-specific and private files **local** (never pushed): scraped
data (`scraping/raw/`), fetched caches (`dat/`), generated boards and payloads (`out/`,
`tool_data.json`, `static/index.html`, `static/data.json`, `static/board.html`), real
league configs (`leagues/*.yaml` except the example, `config/boards.json`,
`config/league.json`), analysis outputs (`reports/`), private notes (`league/`,
`docs/local/`), the advisor `briefing.md`, and every secret (`config/.env`,
`scraping/.espn_auth.json`, `api_key.txt`). The reusable app, scrapers, templates,
pipeline, fftiers package, and the projection baseline are tracked.
