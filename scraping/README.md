# 2KDOME — ESPN Fantasy Football League Data

League: **2KDOME**, ESPN league id **44252**, 12 teams, active since **2013**.
Manager (you): **Harry Davis** — member `{953F79FB-8F07-4722-BF79-FB8F07472225}`,
team 11 "David Jango Unchained" (team id / name varies by season).

## League history at a glance

| Season | Draft   | Keepers | Notes |
|--------|---------|---------|-------|
| 2013–2016 | SNAKE  | no  | |
| 2017    | AUCTION | no  | switched to auction |
| 2018–2019 | AUCTION | no | |
| 2020    | AUCTION | yes | keepers introduced (1/team) |
| 2021–2024 | AUCTION | yes | |
| 2025    | AUCTION | (see note) | keeper flag/inflation not present in draft record — investigate |
| 2026    | upcoming | | not yet drafted |

### The $100 keeper hack
Keepers are encoded on the draft board by **inflating the auction `bidAmount` by
$100**. A pick with `bidAmount >= 100` is a keeper; real keeper cost =
`bidAmount - 100`. The `keeper: true` boolean is also set 2020–2024. This was a
workaround for the league's custom keeper budgeting rules. Any budget/spend
analysis MUST subtract 100 from keeper bids to get true dollars spent.

## Directory layout

```
scraping/
  fantasy.espn.com.har        # original browser capture (in repo root)
  extract_har.py              # pulls JSON bodies out of the HAR
  scrape.py                   # authenticated scraper for all seasons
  .espn_auth.json             # SWID + espn_s2 cookies (GITIGNORED, do not commit)
  har_extracted/              # JSON bodies recovered from the HAR (2022 + 2026 bits)
  raw/{season}/
    league_full.json          # draftDetail, settings, teams, members, schedule,
                              #   status, standings, rosters (mTeam/mRoster/mMatchup...)
    players.json              # kona_player_info: player universe + stats/values
                              #   (2018+ only; pre-2018 uses a different endpoint)
    transactions.json         # all adds/drops/waivers/trade OFFERS, deduped by id
                              #   (2018+). NOTE: TRADE_ACCEPT rows here have empty
                              #   items — use playercards.json for trade detail.
    playercards.json          # kona_playercard for every league-involved player
                              #   (2018-2025). The ONLY complete source of executed
                              #   TRADE detail: each card's `transactions` array has
                              #   full trade items (both sides). 2018 items not recorded.
```

### Executed trades
`mTransactions2` does NOT expose executed-trade detail: `TRADE_PROPOSAL` = offers only,
`TRADE_ACCEPT` rows have empty `items`. Real trades (who moved which players) come from
each player's `kona_playercard` `transactions` array. `scrape_playercards.py` bulk-pulls
these (batched ~120 ids/request via x-fantasy-filter). Dedupe trades across the redundant
per-player copies. 172 executed trades recovered 2019–2025 (2018 items unavailable).
`analysis/lib.py: executed_trades(season)` parses them.

## Data coverage notes
- **2018–2025**: full league + players + transactions (~1000–1300 txns/season,
  including `TRADE_PROPOSAL` = offers).
- **2013–2017**: league data only, fetched from the historical endpoint
  (`/apis/v3/games/ffl/leagueHistory/44252?seasonId=YYYY`). `players.json` and
  `transactions.json` are **not** available for these years via the API
  (kona_player_info 404s; transactions not served). Draft, matchups, standings
  and teams ARE present.
- `league_full.json` for 2013–2017 is a **single-element list** (historical
  endpoint wraps the object in a list); 2018+ is a bare object.

## Re-running the scrape
The ESPN session cookies expire. To refresh:
1. Update `scraping/.espn_auth.json` with fresh `SWID` and `espn_s2` cookies
   (fantasy.espn.com → DevTools → Application → Cookies).
2. `python3 scraping/scrape.py`            # all seasons
   `python3 scraping/scrape.py 2025 2026`  # specific seasons

## Key ESPN API reference (league 44252)
- Modern read base: `https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{year}/segments/0/leagues/44252`
- Historical (≤2017): `.../apis/v3/games/ffl/leagueHistory/44252?seasonId={year}`
- Useful `view=` params: `mDraftDetail mSettings mTeam mRoster mMatchup mMatchupScore mStandings mStatus mSchedule mBoxscore mTransactions2`
- Transactions require header `x-fantasy-filter` and are returned **per
  scoringPeriodId** — iterate weeks 1–18 and dedupe by transaction `id`.
  Valid `filterType` values: `WAIVER WAIVER_ERROR FREEAGENT TRADE_PROPOSAL ROSTER DRAFT FUTURE_ROSTER`.
