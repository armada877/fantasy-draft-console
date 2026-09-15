"""The r/fantasyfootball resources wiki, as a structured catalog.

Source: https://www.reddit.com/r/fantasyfootball/wiki/resources/ (last revised
~2019). Every entry keeps the wiki's stated purpose; `status` records what a
liveness probe found on 2026-09-15:

    ok        HTTP 200 on the listed URL (content itself may still have moved)
    defunct   dead domain, 404, or parked page — kept for the record, not fetchable
    social    a Twitter/X handle list — data to surface, not a fetchable page

Old `http://` links are upgraded to the current working URL where one exists.
"""
from __future__ import annotations

RESOURCES: list[dict] = [
    # ── FantasyPros ──────────────────────────────────────────────────────────
    {"id": "fantasypros-rankings", "site": "FantasyPros",
     "name": "Expert Consensus Rankings",
     "category": "rankings",
     "url": "https://www.fantasypros.com/nfl/rankings/{pos}.php",
     "params": {"pos": ["qb", "rb", "wr", "te", "k", "dst", "flex",
                        "ppr-rb", "ppr-wr", "ppr-te", "ppr-flex",
                        "half-point-ppr-rb", "half-point-ppr-wr"]},
     "use": "Weekly expert-consensus positional rankings for start/sit calls.",
     "status": "ok",
     "notes": "Prefer the dedicated fantasypros_rankings tool — these pages are "
              "JS-rendered, so raw fetch returns only navigation. Full-coverage "
              "ECR is a ranking, not projections; the projections pages serve "
              "only ~10 rows per position (measured in this repo)."},
    {"id": "fantasypros-start-sit", "site": "FantasyPros",
     "name": "Who Do I Start?",
     "category": "start-sit",
     "url": "https://www.fantasypros.com/nfl/start/",
     "use": "Head-to-head player comparison from expert consensus.",
     "status": "ok"},
    {"id": "fantasypros-matchups", "site": "FantasyPros",
     "name": "Matchup Calendar",
     "category": "matchups",
     "url": "https://www.fantasypros.com/nfl/matchups/{pos}.php",
     "params": {"pos": ["qb", "rb", "wr", "te"]},
     "use": "Upcoming-schedule difficulty — buy low before favorable stretches.",
     "status": "ok"},

    # ── Boris Chen ───────────────────────────────────────────────────────────
    {"id": "borischen-tiers", "site": "Boris Chen",
     "name": "Visualized Tiers & Ranks",
     "category": "rankings",
     "url": "https://s3-us-west-1.amazonaws.com/fftiers/out/weekly-{pos}{suffix}.csv",
     "params": {"pos": ["QB", "RB", "WR", "TE", "K", "DST", "FLX"],
                "suffix": ["", "-PPR", "-HALF"]},
     "use": "Tiered weekly rankings — start/sit by tier gap, not rank noise. "
            "Std.Dev column shows where experts disagree.",
     "status": "ok",
     "notes": "QB/K/DST publish no scoring variants (suffix must be empty). "
              "No rest-of-season files — weekly only. Verified 2026-09-14. "
              "Prefer boris_chen_tiers over raw fetch; for other league "
              "types, league_tiers reruns his method adapted per league."},

    # ── Fantasy Sharks ───────────────────────────────────────────────────────
    {"id": "fantasysharks-lineup-coach", "site": "Fantasy Sharks",
     "name": "Lineup Coach",
     "category": "start-sit",
     "url": "https://www.fantasysharks.com/apps/Remora/lineupcoach_fs.php",
     "use": "Expert consensus start/sit plus recent news and player analysis.",
     "status": "ok"},

    # ── FF Toolbox ───────────────────────────────────────────────────────────
    {"id": "fftoolbox-sos", "site": "FF Toolbox",
     "name": "Strength of Schedule",
     "category": "matchups",
     "url": "https://fftoolbox.fulltimefantasy.com/football/strength_of_schedule.cfm",
     "use": "Schedule difficulty by team/position for buy-low and start/sit.",
     "status": "ok",
     "notes": "Site moved under fulltimefantasy.com; old fftoolbox.com URLs redirect."},

    # ── Razzball ─────────────────────────────────────────────────────────────
    {"id": "razzball-depth-charts", "site": "Razzball",
     "name": "Fantasy Depth Charts (per team)",
     "category": "depth-charts",
     "url": "https://football.razzball.com/nfldepthcharts/{team}",
     "params": {"team": ["full team name, lowercase, hyphenated — e.g. "
                         "detroit-lions, kansas-city-chiefs, san-francisco-49ers"]},
     "use": "Fantasy-oriented depth chart for one team: role (Starter/Backup/"
            "Out), projected snap/rush/target shares, YTD usage.",
     "status": "ok",
     "notes": "The /depthcharts index page is only a link hub — always fetch "
              "the per-team URL."},
    {"id": "razzball-trade-analyzer", "site": "Razzball",
     "name": "Trade Analyzer",
     "category": "trade",
     "url": "https://football.razzball.com/trade-analyzer-fantasy-football",
     "use": "Compare the value of multiple players in a trade.",
     "status": "ok"},

    # ── TeamRankings (kicker/defense streaming + Vegas) ──────────────────────
    {"id": "teamrankings-opp-rz-td-pct", "site": "TeamRankings",
     "name": "Opponent Red Zone Scoring % (TD only)",
     "category": "streaming",
     "url": "https://www.teamrankings.com/nfl/stat/opponent-red-zone-scoring-pct",
     "use": "Target kickers facing teams with a low red-zone TD rate (drives "
            "stall into field goals).",
     "status": "ok"},
    {"id": "teamrankings-opp-fga", "site": "TeamRankings",
     "name": "Opponent FG Attempts per Game",
     "category": "streaming",
     "url": "https://www.teamrankings.com/nfl/stat/opponent-field-goal-attempts-per-game",
     "use": "Target kickers facing teams that yield many FG attempts.",
     "status": "ok"},
    {"id": "teamrankings-fga", "site": "TeamRankings",
     "name": "Team FG Attempts per Game",
     "category": "streaming",
     "url": "https://www.teamrankings.com/nfl/stat/field-goal-attempts-per-game",
     "use": "Target kickers on teams that attempt a lot of field goals.",
     "status": "ok"},
    {"id": "teamrankings-vegas", "site": "TeamRankings",
     "name": "Vegas Lines",
     "category": "matchups",
     "url": "https://www.teamrankings.com/nfl/odds/",
     "use": "Totals and spreads — kickers on high-scoring teams; avoid D/ST in "
            "projected shootouts.",
     "status": "ok"},

    # ── FFToday ──────────────────────────────────────────────────────────────
    {"id": "fftoday-sos", "site": "FFToday",
     "name": "Fantasy Strength of Schedule",
     "category": "matchups",
     "url": "https://www.fftoday.com/stats/fantasystats.php",
     "use": "Fantasy points allowed by position — schedule strength.",
     "status": "ok"},
    {"id": "fftoday-ros", "site": "FFToday",
     "name": "Rest-of-the-Way Rankings",
     "category": "rankings",
     "url": "https://www.fftoday.com/rankings/rotw.php",
     "use": "Rest-of-season rankings for all positions (one page).",
     "status": "ok"},
    {"id": "fftoday-consistency", "site": "FFToday",
     "name": "Consistency Calculator",
     "category": "stats",
     "url": "https://www.fftoday.com/tools/crank.php",
     "use": "Week-to-week consistency ratings per player.",
     "status": "ok"},
    {"id": "fftoday-player-history", "site": "FFToday",
     "name": "Player History",
     "category": "stats",
     "url": "https://www.fftoday.com/stats/playerhistory.php",
     "use": "Historical per-player stat lines.",
     "status": "ok"},

    # ── CBS Sports ───────────────────────────────────────────────────────────
    {"id": "cbs-rankings", "site": "CBS Sports",
     "name": "Fantasy Rankings (weekly + season)",
     "category": "rankings",
     "url": "https://www.cbssports.com/fantasy/football/rankings/",
     "use": "CBS staff positional rankings; PPR and standard variants linked "
            "from this hub.",
     "status": "ok",
     "notes": "The wiki's fantasynews.cbssports.com deep links are dead; this "
              "hub replaces them."},

    # ── ESPN / Yahoo ─────────────────────────────────────────────────────────
    {"id": "espn-points-against", "site": "ESPN",
     "name": "Fantasy Points Against",
     "category": "matchups",
     "url": "https://fantasy.espn.com/football/pointsagainst",
     "use": "Fantasy points allowed per position per team.",
     "status": "ok",
     "notes": "Old games.espn.go.com link is dead; page now lives under "
              "fantasy.espn.com and is JS-rendered — text extraction may be thin."},
    {"id": "yahoo-transaction-trends", "site": "Yahoo",
     "name": "Transaction Trends",
     "category": "waivers",
     "url": "https://football.fantasysports.yahoo.com/f1/buzzindex",
     "use": "Most added/dropped players — the waiver-wire buzz index.",
     "status": "ok",
     "notes": "May require login for full detail."},
    {"id": "yahoo-points-against", "site": "Yahoo",
     "name": "Fantasy Points Against",
     "category": "matchups",
     "url": "https://football.fantasysports.yahoo.com/f1/pointsagainst",
     "use": "Fantasy points allowed per position per team.",
     "status": "ok"},

    # ── Weather / misc tools ─────────────────────────────────────────────────
    {"id": "nfl-weather", "site": "NFL Weather",
     "name": "NFL Weather",
     "category": "matchups",
     "url": "https://www.nflweather.com/",
     "use": "Game-day forecasts — kickers in domes/low wind; fade D/ST-facing "
            "offenses in bad weather.",
     "status": "ok"},
    {"id": "rosterwatch-tools", "site": "RosterWatch",
     "name": "RosterWatch Tools",
     "category": "waivers",
     "url": "https://rosterwatch.com/tools",
     "use": "Matchup, TD-dependence, and waiver-wire tools.",
     "status": "ok"},
    {"id": "walterfootball-weekly", "site": "Walter Football",
     "name": "Weekly Rankings",
     "category": "rankings",
     "url": "https://walterfootball.com/fantasyweeklyrankings.php",
     "use": "Weekly positional rankings.",
     "status": "ok"},

    # ── News ─────────────────────────────────────────────────────────────────
    {"id": "rotowire", "site": "Rotowire",
     "name": "Rotowire News",
     "category": "news",
     "url": "https://www.rotowire.com/football/",
     "use": "Player news and last-minute active/inactive updates.",
     "status": "ok"},
    {"id": "rotoworld", "site": "Rotoworld / NBC Sports",
     "name": "Rotoworld Player News",
     "category": "news",
     "url": "https://www.nbcsports.com/fantasy/football/player-news",
     "use": "Player news feed (Rotoworld's successor).",
     "status": "ok",
     "notes": "rotoworld.com and nbcsportsedge.com are both gone; news lives "
              "under nbcsports.com now."},
    {"id": "google-news-reporter", "site": "Google News",
     "name": "Reporter/expert coverage search",
     "category": "news",
     "url": "https://news.google.com/rss/search?q=%22{name}%22%20NFL&hl=en-US&gl=US&ceid=US:en",
     "params": {"name": ["a reporter or player name, URL-encoded"]},
     "use": "Syndicated coverage by or citing any reporter from the Twitter "
            "lists — the readable substitute for their timeline (X blocks "
            "anonymous reads). Prefer the reporter_feed tool.",
     "status": "ok"},
    {"id": "r-fantasyfootball", "site": "Reddit",
     "name": "r/fantasyfootball",
     "category": "news",
     "url": "https://www.reddit.com/r/fantasyfootball/.rss",
     "use": "Community news and discussion threads (RSS feed fetches cleanly).",
     "status": "ok"},

    # ── Defunct (kept for the record; fetch will refuse) ─────────────────────
    {"id": "kffl", "site": "KFFL",
     "name": "KFFL tools (targets, stats analyzer, trade analyzer, start/sit)",
     "category": "stats",
     "url": "http://www.kffl.com/",
     "use": "Player targets/utilization and trade tools.",
     "status": "defunct",
     "notes": "Domain answers but the fantasy tools are long gone (site folded "
              "into USA Today ~2015)."},
    {"id": "ff-metrics", "site": "Fantasy Football Metrics",
     "name": "Weekly Sit/Start Rankings",
     "category": "rankings",
     "url": "http://www.fantasyfootballmetrics.com/",
     "use": "Weekly sit/start rankings by position.",
     "status": "defunct"},
    {"id": "fox-research", "site": "FOX Sports (MSN)",
     "name": "Points Against / Projections / Rankings",
     "category": "stats",
     "url": "http://msn.foxsports.com/fantasy/football/commissioner/Research/",
     "use": "Points against, projections, rankings.",
     "status": "defunct"},
    {"id": "nerdball", "site": "Nerdball",
     "name": "Defense Wins Championships",
     "category": "streaming",
     "url": "http://nerdballmagazine.com/",
     "use": "Team-defense streaming picks.",
     "status": "defunct"},
    {"id": "footballguys-depth", "site": "Footballguys",
     "name": "Depth Charts (subscriber)",
     "category": "depth-charts",
     "url": "https://subscribers.footballguys.com/apps/depthchart.php",
     "use": "Real NFL depth charts.",
     "status": "defunct",
     "notes": "Old app URL 404s; content is behind the current subscriber site."},
    {"id": "statmilk", "site": "StatMilk",
     "name": "Customizable statistics",
     "category": "stats",
     "url": "http://www.statmilk.com/NFL/0/",
     "use": "Customizable NFL statistics.",
     "status": "defunct"},
    {"id": "fflibrarian", "site": "Fantasy Football Librarian",
     "name": "Fantasy Football Librarian",
     "category": "news",
     "url": "http://www.fflibrarian.com/",
     "use": "Aggregated fantasy news and accuracy tracking.",
     "status": "defunct"},
]

# ── Twitter/X handles from the wiki (data to surface, not fetchable) ─────────
# The wiki flags the beat-writer section as out of date (~2019): expect dead or
# renamed accounts, and note franchise moves (OAK→LV, SD→LAC, STL→LA, WAS name).
TWITTER_EXPERTS = [
    "@mortreport", "@ClaytonESPN", "@MichaelFabiano", "@NathanZegura",
    "@daverichard", "@MatthewBerryTMR", "@CHarrisESPN", "@AdamSchefter",
    "@YahooNoise", "@evansilva", "@andybehrens", "@scott_pianowski",
    "@ChrisWesseling", "@MikeClayNFL", "@gregcosell", "@4for4_John",
    "@FantasyPros_NFL", "@adamlevitan", "@PFF_Fantasy", "@CBSfantasynews",
    "@JodySmithNFL", "@fftoolbox", "@tedschuster", "@rotopat",
    "@siriusxmfantasy", "@adbrandt", "@Rotoworld_FB", "@jameyeisenberg",
    "@AlbertBreer",
]

TWITTER_BEAT_WRITERS = {
    "Arizona Cardinals": ["@AZCardinals", "@kentsomers", "@Cardschatter", "@azcsports", "@revengeofbirds", "@CardsGameday", "@CardsMarkD", "@The_SportsPaige", "@joshweinfuss", "@CardsFBTalk", "@LisaCharisseB"],
    "Atlanta Falcons": ["@AtlantaFalcons", "@DOrlandoAJC", "@vxmcclure23", "@FalconsJAdams", "@KnoxonFox", "@FalconsRFerrin", "@TheFalcoholic", "@JohnMichaels929", "@CharlesOdum", "@FalconsCR", "@CraigSagerJr"],
    "Baltimore Ravens": ["@Ravens", "@jamisonhensley", "@RavensInsider", "@jeffzrebiecsun", "@ryanmink", "@moniquenjones", "@BMoreBeatdown", "@RavensViews", "@Ravens_Examiner", "@ravensbeat", "@RavensNation"],
    "Buffalo Bills": ["@buffalobills", "@ChrisTrapasso", "@salmaiorana", "@MatthewFairburn", "@billsdaily", "@TheBillsMafia", "@BuffRumblings", "@mikerodak", "@SalSports", "@ChrisBrownBills", "@viccarucci", "@JoeBuscaglia", "@TyDunne"],
    "Carolina Panthers": ["@Panthers", "@DNewtonespn", "@josephperson", "@jjones9", "@SteveReedAP", "@tomsorensen", "@CarPanthersNews", "@BlackBlueReview", "@CatScratchReadr", "@PanthersMax"],
    "Chicago Bears": ["@ChicagoBears", "@BradBiggs", "@ZachZaidman", "@Rich_Campbell", "@mikecwright", "@adamjahns", "@BobLeGere", "@bears_insider", "@ESPNChiBears", "@CSNMoonMullin", "@DickersonESPN"],
    "Cincinnati Bengals": ["@Bengals", "@pauldehnerjr", "@ColeyHarvey", "@GeoffHobsonCin", "@CincyJungle", "@JustBeWarned", "@nkyskinner", "@StripeHype", "@BengalsViews", "@JimOwczarski", "@BengalsTalk"],
    "Cleveland Browns": ["@Browns", "@TonyGrossi", "@MaryKayCabot", "@NateUlrichABJ", "@jsbrownsinsider", "@ScottPetrak", "@RuiterWrongFAN", "@Mr_KevinJones", "@FredGreethamOBR", "@sdoerschukREP", "@ESPNCleveland"],
    "Dallas Cowboys": ["@dallascowboys", "@DavidMooreDMN", "@robphillips3", "@toddarcher", "@clarencehilljr", "@BryanBroaddus", "@DMN_George", "@NFLCharean", "@jonmachota", "@BloggingTheBoys"],
    "Denver Broncos": ["@Broncos", "@MikeKlis", "@PostBroncos", "@TroyRenck", "@Jeff_Legwold", "@cecillammey", "@MileHighReport", "@MaseDenver", "@MileHighHuddle", "@markkiszla", "@NickiJhabvala"],
    "Detroit Lions": ["@Lions", "@davebirkett", "@ttwentyman", "@paulapasche", "@PrideOfDetroit", "@kmeinke", "@mikerothstein", "@SideLionReport"],
    "Green Bay Packers": ["@packers", "@ByRyanWood", "@RobDemovsky", "@PackerReport", "@WesHod", "@TomSilverstein", "@PeteDougherty", "@jasonjwilde", "@cheeseheadtv", "@packeverywhere", "@Michael_Cohen13"],
    "Houston Texans": ["@HoustonTexans", "@StephStradley", "@ChronBrianSmith", "@jharrisfootball", "@McClain_on_NFL", "@ChronicleTexans", "@battleredblog", "@DeepSlant", "@awexler", "@DoughertyDrew", "@AaronWilson_NFL"],
    "Indianapolis Colts": ["@Colts", "@mchappell51", "@TribStarTJames", "@HolderStephen", "@MikeWellsNFL", "@gmbremer", "@KBowenColts", "@coltspass", "@TheBlueMare", "@ColtsReporter", "@Coltsfanwilson", "@GreggDoyelStar"],
    "Jacksonville Jaguars": ["@Jaguars", "@ryanohalloran", "@JohnOehser", "@BigCatCountry", "@HaysCarlyon", "@ESPNdirocco", "@jpshadrick", "@vitostellino", "@APMarkLong", "@Amanda1010XL", "@md_1010xl"],
    "Kansas City Chiefs": ["@KCChiefs", "@TerezPaylor", "@adamteicher", "@Jacobs71", "@ChiefsReporter", "@ArrowheadPride", "@ArrowheadAddict", "@HerbieTeope", "@mellinger", "@bobgretzcom"],
    "Miami Dolphins": ["@MiamiDolphins", "@AdamHBeasley", "@OmarKelly", "@ArmandoSalguero", "@chrisperk", "@TheMattyI", "@AbramsonPBP", "@SSMiamiDolphins", "@thephinsider", "@JamesWalkerNFL", "@flasportsbuzz", "@davehydesports"],
    "Minnesota Vikings": ["@Vikings", "@markcraignfl", "@chipscoggins", "@ArifHasanNFL", "@GoesslingESPN", "@christomasson", "@VikingUpdate", "@mattvensel", "@DailyNorseman", "@VikingsNow", "@VikingsCorner", "@Luke_Spinman"],
    "New England Patriots": ["@Patriots", "@BenVolin", "@jeffphowe", "@shalisemyoung", "@MikeReiss", "@FieldYates", "@patspulpit", "@NFL_PatriotsFan", "@PatsFans", "@ScottZolak", "@pfwpaul"],
    "New Orleans Saints": ["@Saints", "@JeffDuncan_", "@TheSaintsBeat", "@LarryHolder", "@MikeTriplett", "@Kat_Terrell", "@garlandgillen", "@TheSaints", "@KristianGarica", "@SeanKelleyLive", "@JohnDeShazier"],
    "New York Giants": ["@Giants", "@TomRock_Newsday", "@RVacchianoNYDN", "@Patricia_Traina", "@NYPost_Schwartz", "@JordanRaanan", "@ebenezersamuel", "@bigblueview", "@art_stapleton", "@BigBlueInteract", "@JamesKratch", "@Giantswfan", "@DanGrazianoESPN"],
    "New York Jets": ["@nyjets", "@BrianCoz", "@MMehtaNYDN", "@RichCimini", "@SethWalderNYDN", "@KMart_LI", "@KristianRDyer", "@jetsNYjetsNYJet", "@LTJ81", "@JetsNation", "@Brian_Bassett"],
    "Las Vegas Raiders (wiki: Oakland)": ["@Raiders", "@CorkOnTheNFL", "@BairCSN", "@Jerrymcd", "@VicTafur", "@PGutierrezESPN", "@silverandblackp", "@raiderfans", "@FallonSmithCSN", "@sbreport", "@BWilliamsonESPN"],
    "Philadelphia Eagles": ["@Eagles", "@Jeff_McLane", "@davespadaro", "@Tim_McManus", "@LesBowen", "@ZBerm", "@RoobCSN", "@GeoffMosherCSN", "@TheRealDGunnCSN", "@JimmyKempski"],
    "Pittsburgh Steelers": ["@steelers", "@EdBouchette", "@MarkKaboly_Trib", "@dlolleyor", "@jimwexell", "@gerrydulac", "@Steelersdepot", "@btsteelcurtain", "@BCTBradford", "@C_AdamskiTrib", "@Ken_Laird"],
    "Los Angeles Chargers (wiki: San Diego)": ["@Chargers", "@UTKevinAcee", "@UTkrasovic", "@UTgehlken", "@BFTB_Chargers", "@Mart_Caswell", "@BB_Chargers", "@eric_d_williams", "@Chargersthunder", "@Bolts709"],
    "San Francisco 49ers": ["@49ers", "@Eric_Branch", "@MaioccoCSN", "@mattbarrows", "@CamInman", "@klynch49", "@NinersNation", "@SF49ers_report", "@TaylorPrice", "@Joe_Fann", "@MBachCSN"],
    "Seattle Seahawks": ["@Seahawks", "@Liz_Mathews", "@JaysonJenks", "@bcondotta", "@TerryBlountESPN", "@johnpboyle", "@hawkblogger", "@FieldGulls", "@gbellseattle", "@DaveBoling"],
    "Los Angeles Rams (wiki: St. Louis)": ["@STLouisRams", "@jthom1", "@nwagoner", "@TurfShowTimes", "@miklasz", "@caseyreporting", "@RamsHerd", "@MylesASimmons", "@RamblinFan", "@ramspress"],
    "Tampa Bay Buccaneers": ["@TBBuccaneers", "@NFLSTROUD", "@JennaLaineBucs", "@gregauman", "@IKaufmanTBO", "@Bucs_Nation", "@pewterreport", "@RCummingsTBO", "@caseyreporting", "@MerissaFox13", "@PatYazESPN"],
    "Tennessee Titans": ["@Titans", "@jwyattsports", "@glennonsports", "@PaulKuharskyNFL", "@ThomasGower", "@TitansMCM", "@terrymc13", "@titanspress", "@TeresaMWalker"],
    "Washington Commanders (wiki: Redskins)": ["@Redskins", "@CindyBoren", "@MarkMaske", "@john_keim", "@MikeJonesWaPo", "@Rich_TandlerCSN", "@HogsHaven", "@Russellmania621", "@lizclarketweet", "@TarikCSN"],
}

CATEGORIES = sorted({r["category"] for r in RESOURCES})


def get(resource_id: str) -> dict | None:
    for r in RESOURCES:
        if r["id"] == resource_id:
            return r
    return None


def allowed_hosts() -> set[str]:
    """Hosts the fetch tool may reach: every live catalog URL's host, and the
    same host with/without a leading www."""
    hosts: set[str] = set()
    for r in RESOURCES:
        if r["status"] != "ok":
            continue
        host = r["url"].split("//", 1)[-1].split("/", 1)[0].lower()
        hosts.add(host)
        hosts.add(host[4:] if host.startswith("www.") else "www." + host)
    return hosts
