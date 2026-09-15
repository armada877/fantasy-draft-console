#!/usr/bin/env python3
"""nflverse — advanced usage. The source that powers buy-low / sell-high.

Release assets (verified live 2026-09-14) at
`https://github.com/nflverse/nflverse-data/releases/download/{asset}/{file}`:

| asset/file                             | rows    | what we take                        |
|----------------------------------------|---------|-------------------------------------|
| `players/players.csv`                   | 24,821 | the **espn_id crosswalk** (790/794 of this season's fantasy players carry one) |
| `stats_player/stats_player_week_2026.csv` | 1,042 | targets, carries, `target_share`, air-yards share, PPR points |
| `snap_counts/snap_counts_2026.csv`      |  1,398 | `offense_pct` -> snap share, and its week-over-week move |
| `injuries/injuries_2026.csv`            |    183 | latest practice/report status |
| `depth_charts/depth_charts_2026.csv`    | 518,582 | **49 MB** — a full timestamped snapshot series. Opt-in only (`--deep`), never on the default path; players.csv already gives us the crosswalk. |

The separation this exists to make:

    volume steady + TD rate regressed   ->  unlucky   ->  BUY LOW
    snap share collapsed                ->  displaced ->  SELL HIGH

which is why both `snap_share` and `target_share` are carried, plus their deltas.

Advisory only. Nothing here touches `ros_points` / `marginal` — those stay computed
from the league's own scraped ESPN scoring (docs/contracts.md guardrail 6).
"""
from __future__ import annotations

from . import common
from .common import _f, _i

NAME = "nflverse"
TTL = 12 * 3600
BASE = "https://github.com/nflverse/nflverse-data/releases/download/{asset}/{file}"

PLAYERS_TTL = 7 * 24 * 3600      # the crosswalk barely moves


def _url(asset, file):
    return BASE.format(asset=asset, file=file)


def players_csv(refresh=False, ttl=PLAYERS_TTL):
    """The espn_id crosswalk. Cached under scraping/raw/_shared/ because it is
    league-independent — downloading 7 MB once per league would be rude."""
    return common.shared_blob("nflverse_players.csv",
                              _url("players", "players.csv"),
                              ttl=ttl, refresh=refresh)


def week_stats(season, refresh=False, ttl=TTL):
    return common.shared_blob(f"nflverse_stats_player_week_{season}.csv",
                              _url("stats_player", f"stats_player_week_{season}.csv"),
                              ttl=ttl, refresh=refresh)


def snap_counts(season, refresh=False, ttl=TTL):
    return common.shared_blob(f"nflverse_snap_counts_{season}.csv",
                              _url("snap_counts", f"snap_counts_{season}.csv"),
                              ttl=ttl, refresh=refresh)


def injuries(season, refresh=False, ttl=TTL):
    return common.shared_blob(f"nflverse_injuries_{season}.csv",
                              _url("injuries", f"injuries_{season}.csv"),
                              ttl=ttl, refresh=refresh)


def depth_charts(season, refresh=False, ttl=TTL):
    """49 MB of timestamped snapshots — opt-in (`--deep`) only."""
    return common.shared_blob(f"nflverse_depth_charts_{season}.csv",
                              _url("depth_charts", f"depth_charts_{season}.csv"),
                              ttl=ttl, refresh=refresh)


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _collect(ctx, week, profile, refresh=False, deep=False):
    season = ctx.season
    xw = common.crosswalk(refresh=refresh)

    # ── weekly box score -> volume, share, production ────────────────────────
    per = {}
    for r in week_stats(season, refresh=refresh):
        if (r.get("season_type") or "REG") != "REG":
            continue
        gsis = (r.get("player_id") or "").strip()
        name = r.get("player_display_name") or r.get("player_name") or ""
        if not name:
            continue
        key = gsis or common.norm_name(name)
        p = per.setdefault(key, {"gsis": gsis or None, "name": name,
                                 "pos": (r.get("position") or "").upper(),
                                 "team": (r.get("team") or "").upper(),
                                 "weeks": []})
        p["team"] = (r.get("team") or p["team"]).upper()
        p["weeks"].append({
            "week": _i(r.get("week")),
            "targets": _f(r.get("targets")),
            "carries": _f(r.get("carries")),
            "target_share": _f(r.get("target_share"), None),
            "air_yards_share": _f(r.get("air_yards_share"), None),
            "wopr": _f(r.get("wopr"), None),
            "rec_yds": _f(r.get("receiving_yards")),
            "rush_yds": _f(r.get("rushing_yards")),
            "tds": _f(r.get("passing_tds")) + _f(r.get("rushing_tds")) + _f(r.get("receiving_tds")),
            "ppr": _f(r.get("fantasy_points_ppr")),
            "epa": (_f(r.get("rushing_epa"), 0.0) + _f(r.get("receiving_epa"), 0.0)),
        })

    # ── snap share, joined via pfr_player_id ─────────────────────────────────
    snaps = {}
    try:
        for r in snap_counts(season, refresh=refresh):
            if (r.get("game_type") or "REG") != "REG":
                continue
            pfr = (r.get("pfr_player_id") or "").strip()
            name = r.get("player") or ""
            if not name:
                continue
            key = pfr or common.norm_name(name)
            s = snaps.setdefault(key, {"pfr": pfr or None, "name": name, "weeks": []})
            s["weeks"].append({"week": _i(r.get("week")),
                               "off_pct": _f(r.get("offense_pct"), None),
                               "off_snaps": _f(r.get("offense_snaps"))})
    except Exception:
        snaps = {}                      # a missing snaps asset degrades the field, not the source

    snap_by_gsis, snap_by_norm = {}, {}
    for key, s in snaps.items():
        w = sorted([x for x in s["weeks"] if x["week"] is not None], key=lambda x: x["week"])
        if not w:
            continue
        info = {"share": _mean([x["off_pct"] for x in w]),
                "last": w[-1]["off_pct"],
                "prev": w[-2]["off_pct"] if len(w) > 1 else None,
                "snaps": sum(x["off_snaps"] for x in w)}
        rec = xw["by_pfr"].get(s["pfr"] or "")
        if rec and rec.get("gsis_id"):
            snap_by_gsis[rec["gsis_id"]] = info
        snap_by_norm[common.norm_name(s["name"])] = info

    # ── latest injury report ─────────────────────────────────────────────────
    inj = {}
    try:
        for r in injuries(season, refresh=refresh):
            g = (r.get("gsis_id") or "").strip()
            wk = _i(r.get("week"), 0) or 0
            if not g:
                continue
            cur = inj.get(g)
            if not cur or wk >= cur[0]:
                inj[g] = (wk, {
                    "report_status": (r.get("report_status") or "").strip() or None,
                    "practice_status": (r.get("practice_status") or "").strip() or None,
                    "injury": (r.get("report_primary_injury")
                               or r.get("practice_primary_injury") or "").strip() or None,
                })
    except Exception:
        inj = {}

    depth = {}
    if deep:
        try:
            latest = {}
            for r in depth_charts(season, refresh=refresh):
                eid = (r.get("espn_id") or "").strip()
                if not eid:
                    continue
                dt = r.get("dt") or ""
                cur = latest.get(eid)
                if not cur or dt > cur[0]:
                    latest[eid] = (dt, r)
            for eid, (_dt, r) in latest.items():
                depth[eid] = {"depth_pos": r.get("pos_abb"), "depth_rank": _i(r.get("pos_rank"))}
        except Exception:
            depth = {}

    out = []
    for key, p in per.items():
        w = sorted([x for x in p["weeks"] if x["week"] is not None], key=lambda x: x["week"])
        if not w:
            continue
        n = len(w)
        recent = w[-3:]
        snap = (snap_by_gsis.get(p["gsis"] or "")
                or snap_by_norm.get(common.norm_name(p["name"])) or {})
        touches = sum(x["targets"] + x["carries"] for x in w)
        tds = sum(x["tds"] for x in w)
        ppr = sum(x["ppr"] for x in w)
        ts = _mean([x["target_share"] for x in w])
        ts_last = w[-1]["target_share"]
        i = inj.get(p["gsis"] or "")
        eid = common.resolve_espn_id(norm=common.norm_name(p["name"]), gsis=p["gsis"])
        d = depth.get(str(eid)) if eid else None
        fields = {
            "games": n,
            "snap_share": round(snap["share"], 4) if snap.get("share") is not None else None,
            "snap_share_last": round(snap["last"], 4) if snap.get("last") is not None else None,
            "snap_share_delta": (round(snap["last"] - snap["prev"], 4)
                                 if snap.get("last") is not None and snap.get("prev") is not None
                                 else None),
            "target_share": round(ts, 4) if ts is not None else None,
            "target_share_last": round(ts_last, 4) if ts_last is not None else None,
            "targets": round(sum(x["targets"] for x in w), 1),
            "carries": round(sum(x["carries"] for x in w), 1),
            "touches_per_game": round(touches / n, 2),
            "air_yards_share": (round(_mean([x["air_yards_share"] for x in w]), 4)
                                if _mean([x["air_yards_share"] for x in w]) is not None else None),
            "wopr": (round(_mean([x["wopr"] for x in w]), 4)
                     if _mean([x["wopr"] for x in w]) is not None else None),
            "epa_per_game": round(sum(x["epa"] for x in w) / n, 3),
            # the buy-low lever: TD rate per touch regresses hard, volume does not
            "tds": round(tds, 1),
            "td_rate_per_touch": round(tds / touches, 4) if touches else None,
            "ppr_per_game": round(ppr / n, 2),
            "ppr_per_game_last3": round(sum(x["ppr"] for x in recent) / len(recent), 2),
            "injury_status": (i[1]["report_status"] if i else None),
            "injury": (i[1]["injury"] if i else None),
        }
        if d:
            fields.update({k: v for k, v in d.items() if v is not None})
        out.append(common.record(p["name"], pos=p["pos"], team=p["team"],
                                 espn_id=eid, gsis=p["gsis"], **fields))
    return out


def fetch(ctx, week=None, *, refresh=False, ttl=TTL, write=True, profile=None,
          deep=False, **_):
    return common.run_adapter(
        NAME, lambda c, w, p: _collect(c, w, p, refresh=refresh, deep=deep),
        ctx, week, refresh=refresh, ttl=ttl, write=write, profile=profile)
