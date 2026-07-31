#!/usr/bin/env python3
"""List every ESPN fantasy-football league your cookies belong to — no league_id needed.

Uses the same auth as scrape_league.py (ESPN_SWID/ESPN_S2 env vars or
scraping/.espn_auth.json). Handy for finding your league_id before scraping.

    python3 scraping/discover_leagues.py
"""
from scrape_league import load_auth, discover_leagues


def main():
    swid, s2 = load_auth()
    leagues = discover_leagues(swid, s2)
    if not leagues:
        print("No football leagues found for these cookies (check they're current).")
        return
    print(f"{len(leagues)} football league(s):\n")
    for lg in leagues:
        print(f"  season {lg['season']}  |  league_id {lg['league_id']}  |  "
              f"{lg['league_name']}  (your team: {lg['team_name']})")
    print("\nPut the league_id + season you want in config/league.json "
          "(or leave league_id 0 and let scrape_league.py auto-pick).")


if __name__ == "__main__":
    main()
