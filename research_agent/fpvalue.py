"""One calculation: FantasyPros source data, adapted to a league's configuration.

Value over replacement (VOR), computed entirely from FantasyPros' consensus
points (`r2p_pts` in the weekly ECR data), where BOTH adaptation knobs come
from the league:

  scoring      → picks the FP flavor (standard / half-ppr / ppr) matching the
                 league's scoring_label. Honest limit: FP publishes only those
                 three flavors, so this is the nearest match, not the exact
                 recompute the console does with the league's scraped rules.
  roster size  → the replacement baseline. Starter seats per position =
                 league size × lineup slots; FLEX/OP/RB-WR/WR-TE seats are
                 filled greedily from the best remaining eligible players
                 (the same pooled-replacement idea the draft console uses).
                 Replacement level at a position = the best player left on
                 the bench/wire once every starting seat league-wide is full.

VOR = player's FP points − replacement points at his position. That single
number is "how much better than the guy freely available at his spot, in a
league shaped exactly like yours, per FantasyPros' consensus" — it is what
makes a 12-team 2QB league value QBs differently from a 10-team 1QB league
even though both read the same FantasyPros page.
"""
from __future__ import annotations

from . import leaguetools, webtools
from .tiers import _norm, _ownership, _scoring_for

# which FP pages a flex-type slot may draw from
FLEX_ELIGIBLE = {
    "FLEX": ("RB", "WR", "TE"),
    "RB/WR": ("RB", "WR"),
    "WR/TE": ("WR", "TE"),
    "OP": ("QB", "RB", "WR", "TE"),          # superflex
    "SUPERFLEX": ("QB", "RB", "WR", "TE"),
}
DIRECT = ("QB", "RB", "WR", "TE", "K", "DST")


def fp_league_value(league: str | None = None, limit: int = 60,
                    horizon: str = "week") -> dict:
    """horizon 'week': FP points this week (start/sit value). 'ros': FP
    season-total points (waiver/trade value — deeper FP pools back it)."""
    if horizon not in ("week", "ros"):
        raise ValueError("horizon must be 'week' or 'ros'")
    ctx = leaguetools._resolve(league)
    payload = leaguetools._payload(ctx)
    lg = payload["league"]
    size = int(lg.get("size") or 0)
    scoring = _scoring_for(payload)
    own = _ownership(payload)

    # ── seats per position from the league's lineup ──────────────────────────
    slot_names = lg.get("slot_names") or {}
    lineup = lg.get("lineup_slots") or {}
    direct_seats: dict[str, int] = {}
    flex_seats: list[tuple[str, int]] = []
    for sid, count in lineup.items():
        name = (slot_names.get(str(sid)) or "").upper().replace("D/ST", "DST")
        n = int(count) * size
        if name in DIRECT:
            direct_seats[name] = direct_seats.get(name, 0) + n
        elif name in FLEX_ELIGIBLE:
            flex_seats.append((name, n))
    positions = sorted(set(direct_seats)
                       | {p for nm, _ in flex_seats for p in FLEX_ELIGIBLE[nm]})
    if not positions or not size:
        raise ValueError(f"{ctx.key}: no lineup structure in the payload")

    # ── FantasyPros consensus points, league's scoring flavor ────────────────
    players: list[dict] = []
    fp_week = fp_experts = None
    for pos in positions:
        data = webtools.fp_ecr(pos, scoring, horizon)
        fp_week, fp_experts = data.get("week"), data.get("total_experts")
        for p in data["players"]:
            try:
                pts = float(p.get("r2p_pts") or 0)
            except ValueError:
                continue
            if pts <= 0:
                continue
            players.append({"name": p["player_name"], "pos": pos,
                            "team": p.get("player_team_id") or "",
                            "opp": p.get("player_opponent") or "",
                            "ecr": p.get("rank_ecr"), "pts": pts})

    # ── fill every starting seat league-wide (greedy = optimal here) ─────────
    by_pos: dict[str, list[dict]] = {}
    for p in sorted(players, key=lambda x: -x["pts"]):
        by_pos.setdefault(p["pos"], []).append(p)
    started = set()
    for pos, seats in direct_seats.items():
        for p in by_pos.get(pos, [])[:seats]:
            started.add(id(p))
    remaining = sorted((p for p in players if id(p) not in started),
                       key=lambda x: -x["pts"])
    for slot_nm, seats in flex_seats:
        elig = [p for p in remaining if p["pos"] in FLEX_ELIGIBLE[slot_nm]
                and id(p) not in started]
        for p in elig[:seats]:
            started.add(id(p))

    # ── replacement = best player NOT in any starting seat, per position ─────
    replacement: dict[str, float] = {}
    for pos in positions:
        bench = [p for p in by_pos.get(pos, []) if id(p) not in started]
        replacement[pos] = bench[0]["pts"] if bench else 0.0

    rows = []
    for p in players:
        vor = p["pts"] - replacement[p["pos"]]
        rows.append({**p, "vor": round(vor, 1), "pts": round(p["pts"], 1),
                     "starter": id(p) in started,
                     "tag": own.get(_norm(p["name"]), "")})
    rows.sort(key=lambda r: -r["vor"])

    seats_desc = ", ".join(f"{k}×{v // size}" for k, v in direct_seats.items())
    if flex_seats:
        seats_desc += ", " + ", ".join(f"{nm}×{n // size}" for nm, n in flex_seats)
    return {
        "league": ctx.key, "scoring": scoring, "horizon": horizon,
        "unit": "season-total pts" if horizon == "ros" else "pts this week",
        "week": fp_week,
        "experts": fp_experts, "size": size, "lineup": seats_desc,
        "replacement": {k: round(v, 1) for k, v in replacement.items()},
        "rows": rows[: max(1, int(limit))],
        "note": (f"FP flavor '{scoring}' is the nearest match to this league's "
                 "scoring — exact league-rule points live in the projections "
                 "tools; the roster-size adaptation here is exact."),
    }


def fp_league_value_text(league: str | None = None, limit: int = 40,
                         horizon: str = "week") -> str:
    d = fp_league_value(league, limit, horizon)
    span = ("rest of season (season-total points)" if horizon == "ros"
            else f"this week (week {d['week']})")
    lines = [
        f"FantasyPros value board, adapted to {d['league']} — {span}, "
        f"{d['experts']} experts, FP scoring flavor {d['scoring']}",
        f"league shape: {d['size']} teams, lineup {d['lineup']}",
        "replacement level (best freely-available points once every starting "
        "seat in the league is filled): "
        + ", ".join(f"{k} {v}" for k, v in sorted(d["replacement"].items())),
        "",
        "VOR = FP consensus points − replacement at position "
        "(cross-position comparable):",
    ]
    for r in d["rows"]:
        tag = f" [{r['tag']}]" if r["tag"] else ""
        lines.append(f"  {r['vor']:+6.1f}  {r['pos']:3s} {r['name']} "
                     f"({r['team']} {r['opp']})  {r['pts']} pts"
                     f"{'' if r['starter'] else '  <replacement-level>'}{tag}")
    lines.append(f"\n{d['note']}")
    return "\n".join(lines)
