"""Smoke test for the research agent's tool layer — network, no API key.

Run:  .venv/bin/python -m research_agent.test_webtools
"""
from __future__ import annotations

from . import catalog, webtools


def local_league_checks() -> None:
    """League-tool smoke — needs the repo's local league data (registry +
    generated payloads). Skips cleanly on a machine without them."""
    import os

    from . import leaguetools

    if not os.path.exists(os.path.join(leaguetools.ROOT, "config", "leagues.json")):
        print("skip local league checks (no registry)")
        return
    out = leaguetools.list_leagues()
    assert "league" in out.lower()
    print(f"ok   list_leagues  ({len(out)} chars)")
    import leagues
    for ctx in leagues.all():
        try:
            leaguetools._payload(ctx)
        except FileNotFoundError:
            print(f"skip {ctx.key} (no payload)")
            continue
        d = leaguetools.league_details(ctx.key)
        assert "Standings" in d
        t = leaguetools.team_details(ctx.key)
        assert "MY TEAM" in t and "roster:" in t
        o = leaguetools.opponent_tendencies(ctx.key)
        assert "waivers:" in o
        from . import fpvalue as fpv
        v = fpv.fp_league_value(ctx.key, limit=10, horizon="ros")
        assert v["rows"] and v["replacement"] and v["rows"][0]["vor"] > 0
        print(f"ok   {ctx.key}: fp_league_value (top: "
              f"{v['rows'][0]['name']} {v['rows'][0]['vor']:+.1f})")
        wl = leaguetools.weekly_lineup(ctx.key)
        assert "Optimal WEEK" in wl and "bench:" in wl
        w = leaguetools.console_boards(ctx.key, "waivers", limit=3)
        b = leaguetools.console_boards(ctx.key, "trades", limit=3)
        print(f"ok   {ctx.key}: league_details/team_details/tendencies/boards "
              f"({len(d)}/{len(t)}/{len(o)}/{len(w) + len(b)} chars)")
    try:
        leaguetools.console_boards(None, "nope")
        raise AssertionError("bad board name not refused")
    except ValueError:
        print("ok   bad board name refused")


def main() -> None:
    failures = []

    def check(name, fn):
        try:
            out = fn()
            assert out and isinstance(out, str), "empty output"
            print(f"ok   {name}  ({len(out)} chars)")
            return out
        except Exception as e:
            failures.append((name, e))
            print(f"FAIL {name}: {e}")

    # catalog integrity
    ids = [r["id"] for r in catalog.RESOURCES]
    assert len(ids) == len(set(ids)), "duplicate resource ids"
    assert len(catalog.TWITTER_BEAT_WRITERS) == 32, "expected 32 teams"

    check("list_resources()", lambda: webtools.list_resources())
    out = check("list_resources(streaming)",
                lambda: webtools.list_resources(category="streaming"))
    assert out and "teamrankings" in out

    check("boris_chen_tiers(RB, half-ppr)",
          lambda: webtools.boris_chen_tiers("RB", "half-ppr"))
    check("boris_chen_tiers(QB)", lambda: webtools.boris_chen_tiers("QB"))

    out = check("fantasypros_rankings(RB, half-ppr)",
                lambda: webtools.fantasypros_rankings("RB", "half-ppr", limit=10))
    assert out and "experts" in out and "±" in out
    from . import tiers as tiersmod
    out = check("league_tiers(2kdome, RB, consensus)",
                lambda: tiersmod.league_tiers("2kdome", "RB", "consensus"))
    if out:
        assert "Tier 1:" in out and "[MINE]" in out or "owned:" in out
        ntiers = sum(1 for ln in out.splitlines() if ln.startswith("Tier "))
        assert 5 <= ntiers <= 9, f"RB consensus produced {ntiers} tiers (k=9)"
    out = check("league_tiers(2kdome, TE, projections)",
                lambda: tiersmod.league_tiers("2kdome", "TE", "projections"))
    if out:
        assert "ppg" in out and "Tier 1:" in out

    out = check("fantasypros_rankings(RB, ros)",
                lambda: webtools.fantasypros_rankings("RB", "half-ppr", 5, "ros"))
    assert out and "rest of season" in out
    out = check("league_tiers(2kdome, RB, consensus, ros)",
                lambda: tiersmod.league_tiers("2kdome", "RB", "consensus", "ros"))
    assert out and "rest-of-season" in out
    out = check("fantasypros_rankings(QB)",
                lambda: webtools.fantasypros_rankings("QB", limit=5))
    assert out and "QB" in out

    check("fetch_resource(teamrankings-vegas)",
          lambda: webtools.fetch_resource("teamrankings-vegas", max_chars=4000))
    check("fetch_resource(fantasypros-rankings, pos=rb)",
          lambda: webtools.fetch_resource("fantasypros-rankings",
                                          {"pos": "rb"}, max_chars=4000))
    out = check("fetch_resource(razzball-depth-charts, detroit-lions)",
                lambda: webtools.fetch_resource("razzball-depth-charts",
                                                {"team": "detroit-lions"},
                                                max_chars=4000))
    assert out and "Starter" in out
    check("twitter_handles(bears)", lambda: webtools.twitter_handles("bears"))
    out = check("player_news()", lambda: webtools.player_news(limit=3))
    assert out and "source" in out.lower()
    out = check("reporter_feed(@RapSheet)",
                lambda: webtools.reporter_feed("@RapSheet", limit=3))
    assert out and "Ian Rapoport" in out

    # guardrails
    try:
        webtools.fetch_page("https://example.com/")
        failures.append(("allowlist", "example.com was not refused"))
    except ValueError:
        print("ok   allowlist refuses off-directory host")
    try:
        webtools.fetch_resource("kffl")
        failures.append(("defunct", "kffl was not refused"))
    except ValueError:
        print("ok   defunct resource refused")

    local_league_checks()

    if failures:
        raise SystemExit(f"{len(failures)} failures: {[n for n, _ in failures]}")
    print("all smoke tests passed")


if __name__ == "__main__":
    main()
