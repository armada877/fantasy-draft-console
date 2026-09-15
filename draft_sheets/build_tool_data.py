#!/usr/bin/env python3
"""Build draft_sheets/tool_data.json — the data payload the console runs on.

This is the "glue" of the pipeline: it combines
  1. the universal Elboberto projections workbook  (checked-in .xlsm), and
  2. your league's settings + managers            (from scraping/scrape_league.py,
     or the workbook's own defaults if you haven't scraped yet)
into the single JSON the console template expects.

    projections .xlsm ─┐
                       ├─►  tool_data.json  ─►  (inject step)  ─►  console
    scraped league  ───┘

VALUATION REFLECTS YOUR LEAGUE. Rather than trusting the workbook's pre-computed
values (which were baked for whatever settings the author used), this recomputes
per player from YOUR scraped league:
  FPTS  = raw stat projections × your ESPN scoring (statId → points)
  VBD   = FPTS − replacement-level FPTS, where "replacement" accounts for your
          roster: teams × starters[pos], plus your FLEX slots pooled over RB/WR/TE
  worth = $1 + a share of the discretionary auction pool proportional to VBD
So changing FLEX count, scoring (e.g. −1 vs −2 per INT), teams or budget moves the
numbers, as it should. If the workbook lacks raw stat sheets, it falls back to the
sheet's own computed CheatSheet values.

Run:   python3 draft_sheets/build_tool_data.py
Config: config/league.json  (copy config/league.example.json)

────────────────────────────────────────────────────────────────────────────
tool_data.json SCHEMA (what the console template consumes)
────────────────────────────────────────────────────────────────────────────
{
  "me":       "Your Name",              # which manager is you (must be in managers[])
  "budget":   200,                      # auction $ per team
  "starters": {"QB":1,"RB":2,"WR":2,"TE":1},   # required starters by position
  "flex":     2,                        # RB/WR/TE flex slots
  "bench":    6,                        # bench slots
  "my_mult":  {"QB":1,"RB":1,"WR":1,"TE":1},   # your positional value tilt (1 = neutral)
  "players": [                          # the draftable pool
    {"name":"Josh Allen","pos":"QB","tier":"QB1","worth":32,"vbd":89,"fpts":361}
  ],
  "managers": [                         # every team + its bidding tendencies
    {"name":"Your Name","mult":{"QB":1,"RB":1,"WR":1,"TE":1},"conc":50,"maxbuy":200}
  ]
}

Player fields: worth = projected auction $, vbd = value over replacement (VORP),
fpts = projected season points, tier = e.g. "RB1" (kept from the sheet).

OPPONENT TENDENCIES (mult / conc / maxbuy) drive the bid model in the template:
mult = per-position aggressiveness, conc >72 = stars-and-scrubs tilt, maxbuy = hard
cap on any single bid. A brand-new league has no auction history to calibrate from,
so everyone starts NEUTRAL. **Known gap:** the modeling/simulation pipeline that
calibrates these from years of ESPN auction history is not yet in this repo. Seam:
if config/tendencies.json exists ({"Manager Name": {"mult": {...}, "conc": N,
"maxbuy": N}}), those values are used per manager — that's where calibrated output
plugs in later.

A manager in the league with NO entry in tendencies.json (a mid-season substitution,
an expansion team) gets neutral_profile() — the reserved "_league_default" entry
calibrate.py writes, i.e. the LEAGUE AVERAGE, not a flat 1.0. See neutral_profile()
for why 1.0 is the wrong default. config/league.json "manager_labels" optionally
renames a manager on the board; tendencies merge on the relabelled name.
"""
import json
import os
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)          # so the sibling extractor imports when we are imported
from extract_csg import norm_name as _csg_norm  # noqa: E402  (one shared join key)
ROOT = os.path.dirname(HERE)
POSITIONS = ["QB", "RB", "WR", "TE"]  # skill positions the console drafts

# ESPN lineup-slot ids -> our positions (skill only; K/DST/IR ignored)
ESPN_SLOT = {"0": "QB", "2": "RB", "4": "WR", "6": "TE"}
ESPN_FLEX_SLOTS = {"23"}   # RB/WR/TE flex
ESPN_BENCH_SLOT = "20"

# ESPN scoring statId -> the stat keys we read off the raw projection sheets
ESPN_STAT = {3: "passYds", 4: "passTD", 20: "passInt", 24: "rushYds", 25: "rushTD",
             42: "recYds", 43: "recTD", 53: "rec", 72: "fumbleLost"}
# used when we can't read ESPN scoring (matches the Elboberto workbook's own scoring)
DEFAULT_SCORING = {"passYds": 0.04, "passTD": 4, "passInt": -1, "rushYds": 0.1,
                   "rushTD": 6, "recYds": 0.1, "recTD": 6, "rec": 0.5, "fumbleLost": -2}
# (group, header) in the raw position sheets -> stat key
STAT_COLS = {("passing", "yds"): "passYds", ("passing", "tds"): "passTD",
             ("rushing", "yds"): "rushYds", ("rushing", "tds"): "rushTD",
             ("receiving", "rec"): "rec", ("receiving", "yds"): "recYds",
             ("receiving", "tds"): "recTD"}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_config():
    path = os.path.join(ROOT, "config", "league.json")
    if not os.path.exists(path):
        sys.exit("Missing config/league.json. Copy config/league.example.json and edit it.")
    with open(path) as f:
        return json.load(f)


# reserved key in tendencies.json: the profile to use for a manager with no auction
# history. calibrate.py writes it as the league positional average.
LEAGUE_DEFAULT_KEY = "_league_default"


def load_tendencies():
    """Optional calibrated opponent profiles ({name: {mult, conc, maxbuy}}); {} if absent.
    Keys starting with "_" are reserved (see LEAGUE_DEFAULT_KEY), not manager names."""
    path = os.path.join(ROOT, "config", "tendencies.json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}


def neutral_profile(tendencies, budget):
    """The profile for a manager with NO auction history — a substitution, an expansion
    team, or any league with no calibration at all.

    "Neutral" must mean *league-average*, not a flat 1.0 multiplier: 1.0 would model a
    newcomer as willing to pay full projected value at every position (~2.4x the league
    norm at QB in a typical league) with a max-buy equal to the entire budget, which
    inflates the console's predicted competition for the players you're actually bidding
    on. Prefers calibrate.py's league-average entry; else averages the calibrated
    managers present; else falls back to 1.0 (no calibration to average).
    """
    default = tendencies.get(LEAGUE_DEFAULT_KEY)
    if default and default.get("mult"):
        return ({p: float(default["mult"].get(p, 1.0)) for p in POSITIONS},
                default.get("conc", 50), default.get("maxbuy", budget))
    real = [t for k, t in tendencies.items() if not k.startswith("_") and t.get("mult")]
    if not real:
        return {p: 1.0 for p in POSITIONS}, 50, budget
    mult = {p: round(sum(float(t["mult"].get(p, 1.0)) for t in real) / len(real), 2)
            for p in POSITIONS}
    conc = round(sum(t.get("conc", 50) for t in real) / len(real))
    maxbuy = round(sum(t.get("maxbuy", budget) for t in real) / len(real))
    return mult, conc, maxbuy


def load_csg(season):
    """Optional COMPLEMENTARY market/consensus data from the CSG sheet; {} if absent.

    Produced by extract_csg.py. This is the outside view (where the wider market ranks
    and prices a player) and is advisory ONLY — it never feeds worth/vbd, which stay
    recomputed from your scraped ESPN scoring. Its value is precisely the disagreement:
    a player our league-accurate model prices well above the market is either a real
    edge or a projection we should not trust, and the console shows both numbers so the
    call is yours.
    """
    path = os.path.join(HERE, "csg_consensus.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f).get(str(season)) or {}
    except (OSError, ValueError):
        return {}


def merge_csg(players, csg):
    """Attach p["mkt"] (market/consensus fields) to every player CSG knows.

    Joined on a punctuation/suffix-insensitive name key, because ESPN and CSG disagree
    on "James Cook III" vs "James Cook" and "D.J. Moore" vs "DJ Moore". Only non-null
    fields are emitted, so the injected payload stays small and the console can test for
    presence. Returns (matched, missed_names).
    """
    # exported as short keys: this dict is inlined into index.html on every build
    KEEP = {"mkt_price": "price", "espn_rank": "espn", "ecr": "ecr", "boris_tier": "boris",
            "gold": "gold", "csg_adj_vbd": "advbd", "bs_val": "bs", "status": "status"}
    matched, missed = 0, []
    for p in players:
        rec = csg.get(_csg_norm(p["name"]))
        if not rec:
            missed.append(p["name"])
            continue
        mkt = {out: rec[src] for src, out in KEEP.items() if rec.get(src) is not None}
        if not mkt:
            missed.append(p["name"])
            continue
        # our league-accurate price minus the market's: >0 = we like him more than the room
        if "price" in mkt:
            mkt["edge"] = round(p["worth"] - mkt["price"], 1)
        p["mkt"] = mkt
        matched += 1
    return matched, missed


def load_curve():
    """config/price_curve.json — what each position/tier ACTUALLY cleared at in the
    comparable (full-supply, $200-wallet) seasons. None if absent.

    The console needs this because a pure opponent-bid model has a structural blind spot:
    it prices every player as if all 11 rivals still had a full budget and every need open,
    which is true only for the first nomination. Validated against 2025 it is accurate at
    tier 1 and at the $1 tail but predicts $70 for an RB4 that really went for $17.
    """
    path = os.path.join(ROOT, "config", "price_curve.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    # ship only what the template needs: pos -> tier -> cleared price
    by_tier = {}
    for pos, tiers in (c.get("by_tier") or {}).items():
        for tier, rec in tiers.items():
            if rec.get("price"):
                by_tier.setdefault(pos, {})[tier] = rec["price"]
    if not by_tier:
        return None
    return {"tier": by_tier,
            "rank": [r["p50"] for r in (c.get("by_rank") or [])],
            "seasons": c.get("comparable_seasons") or []}


# Measured on 2025 — the only season sharing 2026's regime ($200 wallet, full player
# supply). Each entry is (min elboberto proj_value, paid/proj ratio actually observed).
# Monotone by construction except the very top step, which genuinely turns down: nobody
# can pay 1.36x for an $84 projection ($114) when the most ever paid is ~$110, so budgets
# cap the top. Top band uses the top-10 aggregate ratio (1.28), which reproduces 2025's
# actual top prices; the rest are that band's median.
PRICE_PREMIUM = [(50, 1.28), (35, 1.36), (25, 1.15), (15, 0.75), (8, 0.35), (0, 0.34)]


def load_proj_values(season):
    """{normalized name: elboberto proj_value} for the season, or {}.

    The console's own `worth` is recomputed from YOUR scoring and runs ~1.11x these
    values at the top, so the two are NOT interchangeable. The league's price history is
    measured against proj_value, so pricing must use proj_value to stay on that scale.
    """
    path = os.path.join(HERE, "elboberto_projections.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for rec in data.get(str(season)) or []:
        v = rec.get("proj_value")
        if v:
            out[_csg_norm(rec["name"])] = float(v)
    return out


def load_plan():
    """Optional backtest-supported budget plan ({slotKey: $ ceiling}); None if absent.
    The console seeds its per-slot bid ceilings from it. Not a validated-optimal allocation —
    a disciplined default (see analysis/research/strategy_search.py). Absent => neutral frame."""
    path = os.path.join(ROOT, "config", "plan.json")
    if os.path.exists(path):
        with open(path) as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    return None


# ───────────────────── projections: raw stats → FPTS → VBD → $ ─────────────────────
def _wb(xlsm_path):
    if not os.path.exists(xlsm_path):
        sys.exit(f"Projections workbook not found: {xlsm_path}")
    return openpyxl.load_workbook(xlsm_path, data_only=True, read_only=True)


def read_raw_projections(wb):
    """Per player from the QB/RB/WR/TE stat sheets: {name, pos, tier, stored_fpts, stats}.
    Returns None if the raw sheets aren't present (older/other workbook layout)."""
    out = []
    for pos in POSITIONS:
        if pos not in wb.sheetnames:
            continue
        ws = wb[pos]
        ncol = ws.max_column
        groups, headers, cur = [], [], ""
        for c in range(1, ncol + 1):
            g = str(ws.cell(row=1, column=c).value or "").strip().lower()
            cur = g or cur                       # forward-fill merged group labels
            groups.append(cur)
            headers.append(str(ws.cell(row=2, column=c).value or "").strip().lower())
        by_header = {h: i for i, h in enumerate(headers) if h}
        if "player" not in by_header or "fpts" not in by_header:
            continue
        stat_col = {key: i for i, (g, h) in enumerate(zip(groups, headers))
                    if (g, h) in STAT_COLS for key in [STAT_COLS[(g, h)]]}
        stat_col["passInt"] = by_header.get("ints", -1)
        stat_col["fumbleLost"] = by_header.get("fl", -1)
        for r in range(3, ws.max_row + 1):
            name = ws.cell(row=r, column=by_header["player"] + 1).value
            if name is None or str(name).strip() == "":
                continue
            stats = {}
            for key, ci in stat_col.items():
                stats[key] = (_num(ws.cell(row=r, column=ci + 1).value) or 0.0) if ci >= 0 else 0.0
            tier = ws.cell(row=r, column=by_header["tier"] + 1).value if "tier" in by_header else None
            out.append({
                "name": str(name).strip(),
                "pos": pos,
                "tier": str(tier).strip() if tier else pos,
                "stored_fpts": _num(ws.cell(row=r, column=by_header["fpts"] + 1).value) or 0.0,
                "stats": stats,
            })
    return out or None


def read_scoring(settings):
    """ESPN statId->points mapped to our stat keys; None if unavailable."""
    items = (settings.get("scoringSettings") or {}).get("scoringItems") or []
    sc = {}
    for it in items:
        key = ESPN_STAT.get(it.get("statId"))
        if key is not None:
            sc[key] = it.get("points", 0) or 0
    return sc or None


def compute_fpts(stats, scoring):
    return sum(stats.get(k, 0.0) * pts for k, pts in scoring.items())


def compute_values(players, starters, flex, bench, teams, budget):
    """Set vbd and worth on each player from the league's roster/teams/budget."""
    by = {p: sorted([x for x in players if x["pos"] == p], key=lambda x: -x["fpts"])
          for p in POSITIONS}
    taken = {p: min(teams * int(starters.get(p, 0)), len(by[p])) for p in POSITIONS}
    # FLEX: pool the best remaining RB/WR/TE and promote them to starters
    pool = sorted([x for p in ("RB", "WR", "TE") for x in by[p][taken[p]:]],
                  key=lambda x: -x["fpts"])
    for x in pool[:teams * int(flex)]:
        taken[x["pos"]] += 1
    baseline = {}                       # replacement = best NON-starter at the position
    for p in POSITIONS:
        rest = by[p][taken[p]:]
        baseline[p] = rest[0]["fpts"] if rest else (by[p][-1]["fpts"] if by[p] else 0.0)
    for x in players:
        x["vbd"] = x["fpts"] - baseline[x["pos"]]
    # auction $: reserve $1 for every roster slot league-wide, split the rest by VBD
    roster_slots = sum(int(v) for v in starters.values()) + int(flex) + int(bench)
    discretionary = max(1.0, teams * budget - teams * roster_slots)
    total_vbd = sum(x["vbd"] for x in players if x["vbd"] > 0) or 1.0
    for x in players:
        share = x["vbd"] / total_vbd * discretionary if x["vbd"] > 0 else 0.0
        x["worth"] = 1.0 + share
    return baseline


def read_cheatsheet(wb):
    """Fallback: the workbook's own pre-computed CheatSheet values (NOT league-adjusted)."""
    if "CheatSheet" not in wb.sheetnames:
        return None
    ws = wb["CheatSheet"]
    grid = [[ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)]
            for r in range(1, ws.max_row + 1)]
    ls = wb["LeagueInfo"] if "LeagueInfo" in wb.sheetnames else None
    base = {}
    if ls:
        for r in range(1, ls.max_row + 1):
            pos, val = str(ls.cell(row=r, column=1).value or "").strip(), _num(ls.cell(row=r, column=2).value)
            if pos in POSITIONS and val is not None:
                base[pos] = val
    players = []
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            text = str(cell or "")
            pos = text.split(" - ")[0].strip() if "Positional Scarcity" in text else None
            if pos not in POSITIONS:
                continue
            hdr = grid[r + 1]
            col = {str(hdr[c + k] or "").strip().upper(): c + k for k in range(8) if c + k < len(hdr)}
            if "NAME" not in col or "$" not in col:
                continue
            for rr in range(r + 2, len(grid)):
                name = grid[rr][col["NAME"]]
                if name is None or str(name).strip() == "":
                    break
                worth = _num(grid[rr][col["$"]])
                vbd = _num(grid[rr][col.get("VBD", -1)]) if "VBD" in col else 0.0
                tier = grid[rr][col["TIER"]] if "TIER" in col else None
                players.append({"name": str(name).strip(), "pos": pos,
                                "tier": str(tier).strip() if tier else pos,
                                "worth": max(0, round(worth)) if worth is not None else 0,
                                "vbd": round(vbd or 0.0), "fpts": round((vbd or 0.0) + base.get(pos, 0))})
    return players or None


def workbook_defaults(wb):
    """Roster/budget fallback from the workbook's LeagueInfo sheet (used before you scrape)."""
    ws = wb["LeagueInfo"]
    grid = [[ws.cell(row=r, column=c).value for c in range(1, 7)] for r in range(1, ws.max_row + 1)]
    starters, flex, teams, budget, bench = {}, 0, 12, 200, 6
    for row in grid:
        label, cnt = str(row[2] or "").strip(), _num(row[3])
        if label in POSITIONS and cnt is not None:
            starters[label] = int(cnt)
        elif label.lower().startswith("flex") and cnt is not None:
            flex += int(cnt)
        key, val = str(row[4] or "").strip().lower(), _num(row[5])
        if key == "teams" and val:
            teams = int(val)
        elif key == "budget" and val:
            budget = int(val)
        elif key == "bench size" and val is not None:
            bench = int(val)
    return {"starters": starters or {"QB": 1, "RB": 2, "WR": 2, "TE": 1},
            "flex": flex, "bench": bench, "teams": teams, "budget": budget, "scoring": None}


# ───────────────────────────── league (ESPN scrape) ─────────────────────────────
def read_scraped_league(season):
    path = os.path.join(ROOT, "scraping", "raw", str(season), "league_full.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        data = data[0] if data else {}
    settings = data.get("settings") or {}
    roster = (settings.get("rosterSettings") or {}).get("lineupSlotCounts") or {}
    draft = settings.get("draftSettings") or {}

    starters = {p: 0 for p in POSITIONS}
    flex = bench = 0
    for slot, cnt in roster.items():
        cnt = int(cnt or 0)
        if slot in ESPN_SLOT:
            starters[ESPN_SLOT[slot]] += cnt
        elif slot in ESPN_FLEX_SLOTS:
            flex += cnt
        elif slot == ESPN_BENCH_SLOT:
            bench += cnt
    starters = {p: n for p, n in starters.items() if n > 0} or {"QB": 1, "RB": 2, "WR": 2, "TE": 1}

    members = {m.get("id"): m for m in (data.get("members") or [])}
    names = []
    for t in (data.get("teams") or []):
        m = members.get(t.get("primaryOwner"))
        if m and (m.get("firstName") or m.get("lastName")):
            name = " ".join(f"{m.get('firstName','')} {m.get('lastName','')}".split())
        elif m and m.get("displayName"):
            name = m["displayName"]
        else:
            name = " ".join((t.get("name") or f"{t.get('location','')} {t.get('nickname','')}").split())
        names.append(name or f"Team {t.get('id')}")

    return {
        "starters": starters,
        "flex": flex or 1,
        "bench": bench or 6,
        "budget": int(draft.get("auctionBudget") or 200),
        "managers": names,
        "league_name": settings.get("name"),
        "scoring": read_scoring(settings),
    }


def main():
    cfg = load_config()
    me = cfg.get("me") or "Me"
    season = int(cfg.get("season", 2026))
    my_mult = cfg.get("my_mult") or {p: 1.0 for p in POSITIONS}
    wb = _wb(os.path.join(ROOT, cfg.get("projections_xlsm", "")))

    league = read_scraped_league(season)
    if league:
        manager_names = league["managers"]
        print(f"Using scraped league (season {season}): "
              f"{len(manager_names)} managers, ${league['budget']} budget.")
        if me not in manager_names:
            print(f"  WARNING: config 'me' = {me!r} is not among the scraped managers: {manager_names}")
    else:
        league = workbook_defaults(wb)
        n = league["teams"]
        manager_names = [me] + [f"Opponent {i}" for i in range(2, n + 1)]
        print(f"No scrape found (scraping/raw/{season}/league_full.json). "
              f"Using workbook defaults: {n} teams, ${league['budget']} budget, generic opponents.")
        print("  Run scraping/scrape_league.py to pull your real league + managers.")

    teams = len(manager_names)
    budget = league["budget"]

    # players + valuation
    raw = read_raw_projections(wb)
    if raw is not None:
        scoring = league.get("scoring") or DEFAULT_SCORING
        src = ("your ESPN scoring" if league.get("scoring")
               else "workbook default scoring (no scrape)")
        for x in raw:
            x["fpts"] = compute_fpts(x["stats"], scoring)
        compute_values(raw, league["starters"], league["flex"], league["bench"], teams, budget)
        # trim the full stat sheets (deep waiver fodder) to a sane draftable pool per
        # position — starters + flex + ~2 bench rounds — AFTER valuation so the
        # replacement baseline still sees the whole pool.
        cap = {p: teams * (int(league["starters"].get(p, 0)) + int(league["flex"]) + 2)
               for p in POSITIONS}
        raw = [x for p in POSITIONS
               for x in sorted([y for y in raw if y["pos"] == p], key=lambda y: -y["fpts"])[:cap[p]]]
        players = [{"name": x["name"], "pos": x["pos"], "tier": x["tier"],
                    "worth": max(0, round(x["worth"])), "vbd": round(x["vbd"]),
                    "fpts": round(x["fpts"])} for x in raw]
        print(f"  Valuation recomputed from raw stats × {src}; "
              f"VBD/$ from roster {league['starters']} +{league['flex']}FLX.")
    else:
        players = read_cheatsheet(wb)
        if not players:
            sys.exit("Could not read raw stat sheets or CheatSheet from the workbook.")
        print("  Raw stat sheets absent — using the workbook's pre-computed CheatSheet "
              "values (NOT adjusted to your league).")

    # Optional display relabelling (config/league.json "manager_labels"): ESPN gives the
    # owner's real name, but at the table you know some teams by their team name — a
    # mid-season substitution especially. Tendencies are merged on the LABELLED name, so a
    # relabelled newcomer correctly falls through to the league-average profile.
    labels = cfg.get("manager_labels") or {}
    if labels:
        relabelled = [(n, labels[n]) for n in manager_names if n in labels]
        manager_names = [labels.get(n, n) for n in manager_names]
        if me in labels:
            me = labels[me]
        if relabelled:
            print("  Manager labels from config/league.json: "
                  + ", ".join(f"{a} -> {b}" for a, b in relabelled))
        unused = sorted(set(labels) - {a for a, _ in relabelled})
        if unused:
            print(f"  ⚠ manager_labels keys matched no scraped manager: {', '.join(unused)}")

    tendencies = load_tendencies()
    if tendencies:
        print(f"  Applying calibrated tendencies from config/tendencies.json "
              f"({len([k for k in tendencies if not k.startswith('_')])} managers).")
    n_mult, n_conc, n_maxbuy = neutral_profile(tendencies, budget)
    managers, uncalibrated = [], []
    for name in manager_names:
        t = tendencies.get(name) or {}
        if not t.get("mult"):
            uncalibrated.append(name)
        managers.append({
            "name": name,
            "mult": t.get("mult") or dict(n_mult),
            "conc": t.get("conc", n_conc),   # <72 => no stars-and-scrubs tilt
            "maxbuy": t.get("maxbuy", n_maxbuy),
        })
    if uncalibrated and tendencies:
        print(f"  No auction history for {', '.join(uncalibrated)} — league-average "
              f"profile (mult {n_mult}, conc {n_conc}, maxbuy ${n_maxbuy}).")

    # Complementary market view (advisory; worth/vbd are already final at this point).
    csg = load_csg(season)
    if csg:
        matched, missed = merge_csg(players, csg)
        pool_worth = sum(p["worth"] for p in players) or 1
        cov_worth = sum(p["worth"] for p in players if p.get("mkt")) / pool_worth
        print(f"  Market view from csg_consensus.json ({season}): matched {matched}/"
              f"{len(players)} players = {cov_worth:.0%} of the $ pool.")
        rich = [p for p in players if p.get("mkt", {}).get("edge") is not None]
        if rich:
            hot = sorted(rich, key=lambda x: -x["mkt"]["edge"])[:3]
            cold = sorted(rich, key=lambda x: x["mkt"]["edge"])[:3]
            print("    we're highest vs market: "
                  + ", ".join(f"{p['name']} ${p['worth']} vs ${p['mkt']['price']:.0f}" for p in hot))
            print("    market's highest vs us:  "
                  + ", ".join(f"{p['name']} ${p['worth']} vs ${p['mkt']['price']:.0f}" for p in cold))

    # projection values on the scale the league's price history was measured against
    pvs = load_proj_values(season)
    if pvs:
        hit = 0
        for pl in players:
            v = pvs.get(_csg_norm(pl["name"]))
            if v:
                pl["pv"] = round(v, 1)
                hit += 1
        print(f"  Projection values (elboberto proj_value) attached to {hit}/{len(players)} "
              f"players — the scale the price premium is calibrated on.")

    curve = load_curve()
    if curve:
        n = sum(len(v) for v in curve["tier"].values())
        print(f"  Price curve from config/price_curve.json: {n} position/tier prices "
              f"from seasons {curve['seasons']} — anchors the console's will-go estimate.")

    plan = load_plan()
    if plan:
        print(f"  Budget plan from config/plan.json (bid ceilings): "
              f"RB1 ${plan.get('RB1','?')} / WR1 ${plan.get('WR1','?')} / … (backtest-supported).")

    out = {
        "me": me,
        "league_name": league.get("league_name") or cfg.get("league_name"),
        "season": season,
        "budget": budget,
        "starters": league["starters"],
        "flex": league["flex"],
        "bench": league["bench"],
        "my_mult": {p: float(my_mult.get(p, 1.0)) for p in POSITIONS},
        "plan": plan,          # per-slot bid ceilings (backtest-supported plan); None => neutral frame
        "curve": curve,        # historical clearing prices by position/tier; None => pure model
        "premium": PRICE_PREMIUM,   # (proj_value floor, observed paid/proj) — prices will-go
        "players": players,
        "managers": managers,
    }
    path = os.path.join(HERE, "tool_data.json")
    with open(path, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    counts = {}
    for p in players:
        counts[p["pos"]] = counts.get(p["pos"], 0) + 1
    top = sorted(players, key=lambda p: -p["worth"])[:5]
    print(f"Wrote {path}")
    print(f"  {len(players)} players {counts} | {len(managers)} managers | "
          f"roster {out['starters']} +{out['flex']}FLX +{out['bench']}BN | ${budget} | me={me!r}")
    print("  priciest:", ", ".join(f"{p['name']} ${p['worth']}({p['pos']})" for p in top))
    print("\nNext: inject into the console template (see README 'Quickstart' step 2).")


if __name__ == "__main__":
    main()
