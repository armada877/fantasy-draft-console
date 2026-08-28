#!/usr/bin/env python3
"""Shared loaders for league analysis.

Handles the three things every analysis needs:
  - player id -> (name, position)   [built across all seasons w/ players.json]
  - per-season teamId -> manager identity (stable across seasons via member id)
  - draft picks with the $100 keeper hack normalized to true cost

League-specific identity lives in config/ (local, gitignored), NOT in this code:
  - MANAGER_CANON  <- config/manager_canon.json  (GUID -> canonical name; optional)
  - ME             <- config/league.json "me"    (which manager you are)
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, os.pardir, "scraping", "raw")
CONFIG = os.path.join(HERE, os.pardir, "config")


def _load_config(name):
    """Load a config/<name> JSON, or {} if it isn't there (keeps the code league-agnostic)."""
    p = os.path.join(CONFIG, name)
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {}

AUCTION_SEASONS = list(range(2017, 2026))   # 2017..2025
ALL_SEASONS = list(range(2013, 2026))

# Some leagues record a keeper's price INFLATED by a flat amount (ESPN has no native
# keeper-cost field, so it's a house convention: bid = true cost + N). Where that is the
# rule, N must come back off or keeper spend is overstated. Where it is NOT the rule —
# ESPN records true cost, and raw bids already reconcile to the budget — subtracting
# anything drives keepers to $0 and quietly skews concentration and max-buy.
# League-specific, so it lives in config. Default 0 = trust the recorded bid.
KEEPER_INFLATION = int(_load_config("league.json").get("keeper_inflation", 0) or 0)
_KEEPER_WARNED = set()

# Some leagues run a restricted-free-agent round before the open auction: each manager's
# FIRST nomination must come from their prior roster, and the incumbent may retain at
# whatever price the bidding sets. Those dollars aren't a free-market choice the way an
# open bid is — a retention is matching a price others set — so calibration has to tell
# the two apart. League-specific, so it is opt-in; leagues without the rule must not have
# their first nominations relabelled.
RFA_ROUND = bool(_load_config("league.json").get("rfa_round", False))

# Managers drift. A draft from a decade ago says far less about how someone bids today
# than last season does, yet an unweighted mean treats them identically — and a plain
# max() over all history is worse still, letting one ancient splurge set a ceiling
# forever. Half-life in seasons: a season N years old counts 0.5**(N/HALF_LIFE).
# 0 disables weighting entirely (every season equal, max-buy stays a true maximum).
RECENCY_HALF_LIFE = float(_load_config("league.json").get("recency_half_life", 0) or 0)

# How many recent seasons a max-buy ceiling may be drawn from. Max-buy is deliberately NOT
# recency-weighted (a ceiling is not an average), so it needs its own window — and tying that
# window to the half-life breaks once the half-life is short enough to weight the multipliers
# the way you want: at 0.8 seasons "within one half-life" is the latest season alone, which
# pins every ceiling to whether a manager happened to chase a stud once. 0 keeps the old
# behaviour (one half-life).
MAXBUY_WINDOW = int(_load_config("league.json").get("maxbuy_window", 0) or 0)

# Managers who have LEFT the league. Their drafts are still in the history and would otherwise
# calibrate a profile nobody can use and, worse, pull the league-average multipliers that the
# console reads as "what this room pays" toward someone who will not be bidding. Filtered at
# draft_picks() so every downstream analysis drops them at once. Canonical names.
EXCLUDE_MANAGERS = {str(n) for n in (_load_config("league.json").get("exclude_managers") or [])}

POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DST",
       7: "OP", 9: "DL", 10: "LB", 11: "DB", 12: "DP", 13: "DT", 14: "DE"}


_LOAD_CACHE = {}


def load(season):
    """Return the league_full object (unwrapping the historical list form).

    A season with no scrape yields {} rather than raising: history is routinely partial
    — a league that changed platforms mid-life, or a fresh setup holding only the current
    season. Every caller reads the result with .get(), so the seasons you DO have still
    contribute and build_agents() no longer dies on the first gap in its hardcoded range.
    Use available_seasons() to report what actually got used.
    """
    if season in _LOAD_CACHE:
        return _LOAD_CACHE[season]
    path = os.path.join(RAW, str(season), "league_full.json")
    if not os.path.exists(path):
        _LOAD_CACHE[season] = {}
        return _LOAD_CACHE[season]
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    if isinstance(d, list):
        d = d[0] if d else {}
    _LOAD_CACHE[season] = d
    return d


def available_seasons(seasons=None):
    """Which of `seasons` (default ALL_SEASONS) contribute usable auction history."""
    return [yr for yr in (seasons or ALL_SEASONS) if has_auction_prices(yr)]


_PRICED_CACHE = {}


def has_auction_prices(season):
    """True when the season's draft actually carries bid amounts.

    A draft run OFFLINE — conducted outside ESPN, or on another platform with only the
    resulting rosters typed back in — still emits a full set of pick rows, every one with
    bidAmount 0. Such a season must never reach calibration: concentration divides by
    total spend, so an all-zero season yields a 0% top-3 share for every team and drags
    each manager's mean concentration down toward zero. It looks like data and is not.
    """
    if season not in _PRICED_CACHE:
        _PRICED_CACHE[season] = any(p["cost"] > 0 for p in draft_picks(season))
    return _PRICED_CACHE[season]


def load_players_raw(season):
    p = os.path.join(RAW, str(season), "players.json")
    if not os.path.exists(p):
        return []
    d = json.load(open(p))
    return d.get("players", []) if isinstance(d, dict) else d


def load_transactions(season):
    p = os.path.join(RAW, str(season), "transactions.json")
    if not os.path.exists(p):
        return []
    return json.load(open(p))


def load_playercards(season):
    p = os.path.join(RAW, str(season), "playercards.json")
    if not os.path.exists(p):
        return []
    return json.load(open(p))


_TRADE_CACHE = {}


def executed_trades(season):
    """Unique executed trades for a season, parsed from playercards (the only
    complete source of trade item detail). Each trade dict:
        {sp, date, sides: {teamId: [playerIds]}, managers: {teamId: name},
         pids: [all playerIds]}
    Deduped across the redundant per-player card copies.
    """
    if season in _TRADE_CACHE:
        return _TRADE_CACHE[season]
    seen = {}
    for card in load_playercards(season):
        for t in card.get("transactions", []):
            if t.get("type") != "TRADE_ACCEPT" or not t.get("items"):
                continue
            moves = [(i["playerId"], i["fromTeamId"], i["toTeamId"])
                     for i in t["items"] if i.get("type") == "TRADE"]
            if not moves:
                continue
            key = (t.get("acceptedDate") or t.get("proposedDate"), tuple(sorted(moves)))
            if key in seen:
                continue
            sides = {}
            for pid, ft, tt in moves:
                sides.setdefault(tt, []).append(pid)
            seen[key] = {
                "sp": t.get("scoringPeriodId"),
                "date": t.get("acceptedDate") or t.get("proposedDate"),
                "sides": sides,
                "managers": {tm: manager(season, tm) for tm in sides},
                "pids": [m[0] for m in moves],
            }
    out = list(seen.values())
    _TRADE_CACHE[season] = out
    return out


_PLAYER_MAP = None


def player_map():
    """Global {playerId: {'name':..., 'pos':...}} across every season available."""
    global _PLAYER_MAP
    if _PLAYER_MAP is not None:
        return _PLAYER_MAP
    m = {}
    for yr in ALL_SEASONS:
        for entry in load_players_raw(yr):
            p = entry.get("player", {})
            pid = p.get("id")
            if pid is None:
                continue
            name = p.get("fullName") or f"{p.get('firstName','')} {p.get('lastName','')}".strip()
            pos = POS.get(p.get("defaultPositionId"), str(p.get("defaultPositionId")))
            # prefer the most recent non-empty name
            m[pid] = {"name": name or m.get(pid, {}).get("name", f"#{pid}"), "pos": pos}
    _PLAYER_MAP = m
    return m


def pname(pid):
    return player_map().get(pid, {}).get("name", f"#{pid}")


def ppos(pid):
    return player_map().get(pid, {}).get("pos", "?")


_MEMBER_CACHE = {}
_TEAMOWNER_CACHE = {}


def member_names(season=None):
    """{memberId: 'First Last'} — merged across all seasons if season is None."""
    key = season or "ALL"
    if key in _MEMBER_CACHE:
        return _MEMBER_CACHE[key]
    out = {}
    seasons = [season] if season else ALL_SEASONS
    for yr in seasons:
        d = load(yr)
        for mem in d.get("members", []):
            mid = mem.get("id")
            nm = f"{mem.get('firstName','').strip()} {mem.get('lastName','').strip()}".strip()
            if mid and nm:
                out[mid] = nm
    _MEMBER_CACHE[key] = out
    return out


# Canonical manager identity ({primaryOwnerGUID: name}) and which manager is you.
# Both are LOCAL config (real names / your identity), so the code ships clean.
# MANAGER_CANON is optional: when a GUID isn't in it, manager() falls back to the
# team's scraped owner name — so a fresh league works with no canon file at all.
# (Copy config/manager_canon.example.json to config/manager_canon.json to set one up.)
MANAGER_CANON = _load_config("manager_canon.json")
ME = _load_config("league.json").get("me", "")


def manager(season, team_id):
    """Canonical manager name for a team in a season (via primary owner)."""
    to = team_owner(season)
    prim = to.get(team_id, {}).get("owner")
    return MANAGER_CANON.get(prim, to.get(team_id, {}).get("ownerName", "?"))


def team_owner(season):
    """Per-season {teamId: {'name': teamName, 'owner': memberId, 'ownerName': str,
    'owners': [memberIds]}} using the primary (first) owner as the manager."""
    if season in _TEAMOWNER_CACHE:
        return _TEAMOWNER_CACHE[season]
    d = load(season)
    names = member_names()  # merged, so co-owners resolve even if named oddly
    out = {}
    for t in d.get("teams", []):
        owners = t.get("owners") or []
        primary = owners[0] if owners else None
        tname = t.get("name") or f"{t.get('location','')} {t.get('nickname','')}".strip()
        out[t["id"]] = {
            "name": tname,
            "owner": primary,
            "ownerName": names.get(primary, primary or "?"),
            "owners": owners,
        }
    _TEAMOWNER_CACHE[season] = out
    return out


def auction_budget(season):
    d = load(season)
    ds = d.get("settings", {}).get("draftSettings", {})
    return ds.get("auctionBudget") or 200


def draft_picks(season):
    """List of normalized picks for a season. Each pick:
       {teamId, owner, ownerName, playerId, name, pos, bid_raw, is_keeper, cost}
       cost = true dollars spent (keeper bids have 100 subtracted).
    """
    d = load(season)
    to = team_owner(season)
    picks = d.get("draftDetail", {}).get("picks", []) or []
    out = []
    for p in picks:
        raw = p.get("bidAmount", 0) or 0
        # Keeper is the ESPN `keeper` flag ONLY. A bid>=100 heuristic would corrupt real
        # spend, since in a large budget non-keeper studs legitimately clear $100.
        # Whether the recorded bid needs deflating is a house rule -> KEEPER_INFLATION
        # (config). Leagues that price keepers directly (e.g. "last year's cost + $5")
        # record TRUE cost and must leave it at 0.
        is_keeper = bool(p.get("keeper"))
        cost = raw - KEEPER_INFLATION if is_keeper else raw
        if cost < 0:
            cost = 0
        # A keeper whose whole price vanishes means the configured inflation does not
        # match this league's convention. Silent otherwise: costs just collapse to $0 and
        # skew concentration / max-buy with nothing to show for it.
        if is_keeper and KEEPER_INFLATION and cost == 0 and season not in _KEEPER_WARNED:
            _KEEPER_WARNED.add(season)
            print(f"  ! {season}: keeper bid ${raw} <= keeper_inflation "
                  f"${KEEPER_INFLATION}, so its cost floors at $0. If your league prices "
                  "keepers directly rather than inflating the recorded bid, set "
                  '"keeper_inflation": 0 in config/league.json.')
        pid = p.get("playerId")
        tid = p.get("teamId")
        prim = to.get(tid, {}).get("owner")
        mgr_name = MANAGER_CANON.get(prim, to.get(tid, {}).get("ownerName", "?"))
        if str(mgr_name) in EXCLUDE_MANAGERS:
            continue                      # departed manager — not part of this room any more
        out.append({
            "teamId": tid,
            "owner": prim,
            "manager": mgr_name,
            "ownerName": to.get(tid, {}).get("ownerName", "?"),
            "teamName": to.get(tid, {}).get("name", "?"),
            "playerId": pid,
            "name": pname(pid),
            "pos": ppos(pid),
            "bid_raw": raw,
            "is_keeper": is_keeper,
            "cost": cost,
            "overall": p.get("overallPickNumber"),
            "nominatingTeamId": p.get("nominatingTeamId"),
        })
    _tag_phases(out)
    return out


def _tag_phases(picks):
    """Tag every pick 'keeper' | 'rfa' | 'open' (in place).

    ESPN emits keepers as ordinary pick rows with nominatingTeamId 0 — they are pre-draft
    roster assignments, not auction events, so they lead the board without anyone having
    nominated them. When RFA_ROUND is on, each team's FIRST nomination of the live auction
    is its restricted free agent. Note the tag follows the NOMINATING team (the incumbent),
    while `manager` is whoever actually won the player — the two differ exactly when an
    RFA is poached rather than retained.
    """
    nominated = set()
    for p in sorted(picks, key=lambda x: (x.get("overall") is None, x.get("overall") or 0)):
        if p["is_keeper"]:
            p["phase"] = "keeper"
            continue
        nt = p.get("nominatingTeamId")
        if RFA_ROUND and nt is not None and nt not in nominated:
            nominated.add(nt)
            p["phase"] = "rfa"
        else:
            p["phase"] = "open"


def draft_type(season):
    return load(season).get("settings", {}).get("draftSettings", {}).get("type")
