#!/usr/bin/env python3
"""Extract all ESPN fantasy league (44252) JSON responses captured in the HAR.

The HAR was exported without cookies, but it still contains the full response
bodies for the requests the browser made while logged in. This pulls every
usable JSON body for our league / players out of the HAR and saves them,
deduplicated, into scraping/har_extracted/ so nothing is lost.
"""
import json
import os
from urllib.parse import urlparse, parse_qs

HAR = os.path.join(os.path.dirname(__file__), os.pardir, "fantasy.espn.com.har")
OUT = os.path.join(os.path.dirname(__file__), "har_extracted")
LEAGUE_ID = "44252"


def slug(url):
    p = urlparse(url)
    q = parse_qs(p.query)
    views = "-".join(q.get("view", [])) or "noview"
    # season from path .../seasons/{year}/...
    parts = p.path.strip("/").split("/")
    season = ""
    if "seasons" in parts:
        season = parts[parts.index("seasons") + 1]
    sp = q.get("scoringPeriodId", [""])[0]
    kind = "players" if p.path.endswith("/players") or "kona_player" in views else "league"
    name = f"{season}_{kind}_{views}"
    if sp:
        name += f"_sp{sp}"
    return name


def main():
    with open(HAR) as f:
        har = json.load(f)
    entries = har["log"]["entries"]
    saved = {}
    for e in entries:
        url = e["request"]["url"]
        if "fantasy.espn.com" not in urlparse(url).netloc:
            continue
        if LEAGUE_ID not in url and "/ffl/seasons/" not in url:
            continue
        body = e["response"]["content"].get("text", "")
        if not body:
            continue
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        name = slug(url)
        # keep the largest body for each logical name (most complete / not a 304 stub)
        size = len(body)
        if name not in saved or size > saved[name][1]:
            saved[name] = (data, size)

    os.makedirs(OUT, exist_ok=True)
    manifest = []
    for name, (data, size) in sorted(saved.items()):
        path = os.path.join(OUT, name + ".json")
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        manifest.append((name, size))
        print(f"saved {name}.json ({size:,} bytes)")
    print(f"\n{len(manifest)} files written to {OUT}")


if __name__ == "__main__":
    main()
