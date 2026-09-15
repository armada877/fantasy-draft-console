#!/usr/bin/env python3
"""season-inject — template + data → the served HTML. Multi-league, one league per file.

The golden rule from CLAUDE.md holds here exactly as it does for the draft console:
**the served HTML is GENERATED.** Edit `draft_sheets/season_tool_template.html` or
`draft_sheets/home_template.html`, then re-inject. Never hand-edit anything under
`draft_app/static/`.

    draft_sheets/season_tool_template.html + ctx.out("season_data.json")
        → draft_app/static/l/{key}/season.html
    draft_sheets/home_template.html        + every league's summary
        → draft_app/static/home.html   (+ draft_app/static/leagues.json)

`static/home.html`, NOT `static/index.html`: `pipeline.py inject` owns `index.html`
(it is where the draft console is written) and `tests/draft_regression.py` asserts its
bytes. Writing the launcher there would be clobbered by the very command the regression
test runs. `draft_app/server.py` therefore serves `home.html` at `/`.

Usage:
    python3 draft_sheets/inject_season.py                 # every registered league
    python3 draft_sheets/inject_season.py --league 2kdome # just one
    python3 draft_sheets/inject_season.py --fixtures      # build from tests/fixtures/
    python3 draft_sheets/inject_season.py --home-only     # relist the launcher only

Self-skipping: a league with no `season_data.json` is skipped with a note — the launcher
still lists it, with its in-season tool marked unavailable. So this never fails the
pipeline just because one league has not been built yet.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import leagues  # noqa: E402

SEASON_TEMPLATE = os.path.join(ROOT, "draft_sheets", "season_tool_template.html")
HOME_TEMPLATE = os.path.join(ROOT, "draft_sheets", "home_template.html")
STATIC = os.path.join(ROOT, "draft_app", "static")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
MARKER = "/*DATA*/"


def _render(template_path: str, data_json: str) -> str:
    with open(template_path, encoding="utf-8") as f:
        tpl = f.read()
    if MARKER not in tpl:
        raise SystemExit(f"inject: {os.path.relpath(template_path, ROOT)} is missing the "
                         f"{MARKER} marker.")
    return tpl.replace(MARKER, data_json)


def season_data_path(ctx, fixtures: bool = False) -> str | None:
    """Where this league's cockpit payload lives, or None if it has not been built.

    Default: the real payload only. `--fixtures` deliberately PREFERS
    tests/fixtures/season_data.<key>.json — that is the frozen schema the cockpit is
    developed against, so `--fixtures` must be reproducible even once a real payload
    exists beside it.
    """
    real = ctx.out("season_data.json")
    fx = os.path.join(FIXTURES, f"season_data.{ctx.key}.json")
    order = ([fx, real] if fixtures else [real])
    for p in order:
        if os.path.exists(p):
            return p
    return None


def inject_league(ctx, fixtures: bool = False) -> str | None:
    """Write draft_app/static/l/{key}/season.html for one league.

    Returns the written path, or None when the league has no payload yet (self-skip).
    Each league writes only under its OWN static dir — no league's data can reach
    another's page.
    """
    src = season_data_path(ctx, fixtures)
    if not src:
        return None
    with open(src, encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(raw)            # fail loudly on a corrupt payload, not silently
    key_in = (data.get("league") or {}).get("key")
    if key_in and key_in != ctx.key:
        raise SystemExit(f"inject: {os.path.relpath(src, ROOT)} is league {key_in!r}, "
                         f"not {ctx.key!r} — refusing to cross league contexts.")
    out_dir = os.path.join(STATIC, "l", ctx.key)
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "season.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(_render(SEASON_TEMPLATE, raw))
    # the payload alongside it, mirroring how the draft console ships static/data.json
    with open(os.path.join(out_dir, "season_data.json"), "w", encoding="utf-8") as f:
        f.write(raw)
    return out


def league_card(ctx, fixtures: bool = False) -> dict:
    """One launcher card's worth of facts, drawn from this league only.

    Order of preference for each fact: the league's own season payload, then its
    scraped LeagueProfile, then nothing. Never another league, never a constant.
    """
    card = {
        "key": ctx.key, "name": ctx.name or ctx.key, "season": ctx.season,
        "league_id": ctx.league_id, "team_id": ctx.team_id, "team_name": ctx.team_name,
        "size": None, "scoring_label": None, "draft_type": None, "auction_budget": None,
        "acquisition": None, "lineup_slots": None, "slot_names": None,
        "playoff_teams": None, "regular_weeks": None, "veto_votes": None,
        "week": None, "record": None, "faab_left": None, "waiver_priority": None,
        "playoff_odds": None, "fixture": False, "season_ready": False,
        "tools": {"season": False, "draft": False},
    }

    src = season_data_path(ctx, fixtures)
    if src:
        with open(src, encoding="utf-8") as f:
            d = json.load(f)
        lg = d.get("league") or {}
        card.update({k: lg.get(k) for k in
                     ("size", "scoring_label", "acquisition", "lineup_slots", "slot_names",
                      "playoff_teams", "regular_weeks", "veto_votes")})
        # forward-compatible: season_data.json's league block does not carry draft_type
        # today (see docs/ws5_stage.md), but read it if WS-4 starts sending it.
        for k in ("draft_type", "auction_budget"):
            if lg.get(k):
                card[k] = lg[k]
        card["name"] = lg.get("name") or card["name"]
        card["season"] = lg.get("season") or card["season"]
        card["week"] = d.get("week")
        card["fixture"] = bool(d.get("fixture"))
        card["season_ready"] = True
        me = d.get("me") or {}
        mine = next((t for t in (d.get("teams") or [])
                     if t.get("team_id") == me.get("team_id")), None)
        if mine:
            card["team_name"] = mine.get("name") or card["team_name"]
            for k in ("record", "faab_left", "waiver_priority", "playoff_odds"):
                card[k] = mine.get(k)

    # fall back to the league's own scraped settings for anything still missing
    if card["draft_type"] is None or card["size"] is None:
        try:
            prof = ctx.profile()
        except Exception:
            prof = None
        if prof is not None:
            card["draft_type"] = card["draft_type"] or prof.draft_type or None
            card["auction_budget"] = card["auction_budget"] or prof.auction_budget
            card["size"] = card["size"] or prof.size
            card["scoring_label"] = card["scoring_label"] or prof.scoring_label
            card["playoff_teams"] = card["playoff_teams"] or prof.playoff_teams
            card["regular_weeks"] = card["regular_weeks"] or prof.regular_weeks
            if card["veto_votes"] is None:
                card["veto_votes"] = prof.veto_votes
            if card["acquisition"] is None:
                a = prof.acquisition
                card["acquisition"] = {"model": a.model, "budget": a.budget,
                                       "min_bid": a.min_bid, "continuous": a.continuous}
            if card["lineup_slots"] is None:
                from engine.profile import slot_name
                card["lineup_slots"] = {str(k): v for k, v in prof.starting_slots.items()}
                card["slot_names"] = {str(k): slot_name(k) for k in prof.starting_slots}

    card["tools"]["season"] = card["season_ready"]
    # The draft console is a single generated file today: `pipeline.py inject` writes
    # static/index.html from config/league.json, i.e. for the LEGACY league only. Do not
    # advertise it for the others — it would show the 12-team league's auction board under a snake
    # league's name. A per-league console, once one exists, lives beside the cockpit.
    card["tools"]["draft"] = (
        os.path.exists(os.path.join(STATIC, "l", ctx.key, "draft.html"))
        or (ctx.is_legacy and os.path.exists(os.path.join(STATIC, "index.html"))))
    return card


def inject_home(ctxs, fixtures: bool = False) -> str:
    """Write the launcher (draft_app/static/home.html) + the key list the server validates."""
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "leagues": [league_card(c, fixtures) for c in ctxs],
    }
    raw = json.dumps(payload, separators=(",", ":"))
    os.makedirs(STATIC, exist_ok=True)
    out = os.path.join(STATIC, "home.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(_render(HOME_TEMPLATE, raw))
    with open(os.path.join(STATIC, "leagues.json"), "w", encoding="utf-8") as f:
        f.write(raw)
    return out


def run(keys=None, fixtures: bool = False, home_only: bool = False) -> int:
    """The stage entry point. Returns a process exit code (0 = fine, including skips)."""
    ctxs = leagues.all()
    if keys:
        want = set(keys)
        ctxs = [c for c in ctxs if c.key in want]
        missing = want - {c.key for c in ctxs}
        if missing:
            print(f"✗ season-inject: unknown league key(s) {sorted(missing)}; "
                  f"have {[c.key for c in leagues.all()]}")
            return 1
    if not ctxs:
        print("• season-inject: no leagues registered — run `python3 leagues.py` first.")
        return 0

    built = skipped = 0
    if not home_only:
        for ctx in ctxs:
            out = inject_league(ctx, fixtures)
            if out is None:
                skipped += 1
                print(f"• season-inject: {ctx.key:20s} skipped — no season_data.json "
                      f"({os.path.relpath(ctx.out('season_data.json'), ROOT)})")
                continue
            built += 1
            src = season_data_path(ctx, fixtures)
            tag = " [fixture]" if src and src.startswith(FIXTURES) else ""
            print(f"• season-inject: {ctx.key:20s} → {os.path.relpath(out, ROOT)} "
                  f"({os.path.getsize(out):,}b){tag}")

    home = inject_home(ctxs, fixtures)
    print(f"• season-inject: launcher → {os.path.relpath(home, ROOT)} "
          f"({len(ctxs)} league{'' if len(ctxs) == 1 else 's'}, "
          f"{built} cockpit{'' if built == 1 else 's'}, {skipped} skipped)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--league", action="append", metavar="KEY",
                    help="only this league (repeatable); default = every registered league")
    ap.add_argument("--fixtures", action="store_true",
                    help="fall back to tests/fixtures/season_data.<key>.json when a league "
                         "has no real payload yet")
    ap.add_argument("--home-only", action="store_true",
                    help="rebuild only the launcher")
    a = ap.parse_args(argv)
    return run(keys=a.league, fixtures=a.fixtures, home_only=a.home_only)


if __name__ == "__main__":
    sys.exit(main())
