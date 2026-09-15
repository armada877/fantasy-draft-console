"""Tool implementations for the research agent — plain functions, no SDK.

Everything here returns strings (what the model reads) and raises on failure;
the @tool wrappers in agent.py catch and convert to is_error results. Kept
SDK-free so the smoke test can exercise fetching/parsing without an API key.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

from . import catalog

UA = ("fantasy-research-agent/1.0 (personal fantasy-football tool; "
      "low-volume, non-commercial)")
TIMEOUT = 30
MIN_HOST_DELAY = 1.0
DEFAULT_MAX_CHARS = 12_000
HARD_MAX_CHARS = 40_000

_last_hit: dict[str, float] = {}


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].lower()


def _check_allowed(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"not an http(s) URL: {url}")
    if _host(url) not in catalog.allowed_hosts():
        raise ValueError(
            f"host {_host(url)!r} is not in the resource directory's allowlist; "
            "use list_resources to see what this agent can reach")


def http_get(url: str, *, retries: int = 2) -> bytes:
    """One polite GET against an allowlisted host. Raises RuntimeError on failure."""
    _check_allowed(url)
    host = _host(url)
    headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
    last_err = None
    for attempt in range(retries + 1):
        wait = _last_hit.get(host, 0.0) + MIN_HOST_DELAY - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} for {url}"
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(last_err) from None
        except Exception as e:  # URLError, timeout, DNS, TLS…
            last_err = f"{type(e).__name__}: {e} for {url}"
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(last_err) from None
    raise RuntimeError(last_err or f"unreachable: {url}")


# ── HTML → readable text ─────────────────────────────────────────────────────
_SKIP = {"script", "style", "noscript", "svg", "iframe", "head"}
# Boilerplate on every directory site. Skipped SOFTLY: real pages leave these
# unclosed (razzball's <nav> wraps the whole document), so a content signal —
# a table, <main>, or <article> opening — force-exits the skip.
_CHROME = {"nav", "header", "footer", "aside"}
_CONTENT = {"table", "main", "article"}
_BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
          "table", "section", "article", "ul", "ol"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._chrome_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self._skip_depth += 1
            return
        if tag in _CONTENT:
            self._chrome_depth = 0
        elif tag in _CHROME:
            self._chrome_depth += 1
        if tag in _BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in _CHROME and self._chrome_depth:
            self._chrome_depth -= 1
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth and not self._chrome_depth and data.strip():
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    lines = [" ".join(seg.split()) for seg in "".join(p.parts).split("\n")]
    out, blank = [], 0
    for ln in lines:
        blank = blank + 1 if not ln else 0
        if blank <= 1:
            out.append(ln)
    return "\n".join(out).strip()


def fetch_page(url: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Fetch an allowlisted URL and return readable text, truncated."""
    max_chars = min(int(max_chars or DEFAULT_MAX_CHARS), HARD_MAX_CHARS)
    raw = http_get(url)
    text = raw.decode("utf-8", "replace")
    stripped = text.lstrip()[:200].lower()
    if stripped.startswith(("<!doctype", "<html")) or "<body" in text[:3000].lower():
        text = html_to_text(text)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[truncated at {max_chars} chars of {len(text)}]"
    return text


# ── catalog browsing ─────────────────────────────────────────────────────────
def list_resources(category: str | None = None, query: str | None = None) -> str:
    rows = catalog.RESOURCES
    if category:
        rows = [r for r in rows if r["category"] == category.lower().strip()]
    if query:
        q = query.lower()
        rows = [r for r in rows
                if q in r["id"] or q in r["name"].lower()
                or q in r["site"].lower() or q in r["use"].lower()]
    if not rows:
        return (f"No resources matched. Categories: {', '.join(catalog.CATEGORIES)}")
    lines = [f"{len(rows)} resources (categories: {', '.join(catalog.CATEGORIES)})\n"]
    for r in rows:
        lines.append(f"- id={r['id']} [{r['category']}] {r['site']} — {r['name']}"
                     f" ({r['status']})")
        lines.append(f"    {r['use']}")
        if r.get("params"):
            lines.append(f"    url template: {r['url']}  params: "
                         + json.dumps(r["params"]))
        if r.get("notes"):
            lines.append(f"    note: {r['notes']}")
    return "\n".join(lines)


def fetch_resource(resource_id: str, params: dict | None = None,
                   max_chars: int = DEFAULT_MAX_CHARS) -> str:
    r = catalog.get(resource_id)
    if not r:
        raise ValueError(f"unknown resource id {resource_id!r}; use list_resources")
    if r["status"] != "ok":
        raise ValueError(
            f"{resource_id} is marked {r['status']} — the wiki listed it but it "
            f"no longer serves content. {r.get('notes', '')}".strip())
    url = r["url"]
    if "{" in url:
        try:
            url = url.format(**{k: str(v) for k, v in (params or {}).items()})
        except KeyError as e:
            raise ValueError(
                f"{resource_id} needs params {sorted((r.get('params') or {}))}; "
                f"missing {e}") from None
    return f"[{r['site']} — {r['name']}]\n{url}\n\n" + fetch_page(url, max_chars)


# ── Boris Chen tiers, structured ─────────────────────────────────────────────
_BC_BASE = "https://s3-us-west-1.amazonaws.com/fftiers/out/weekly-{pos}{suffix}.csv"
_BC_SUFFIX = {"ppr": "-PPR", "half-ppr": "-HALF", "half": "-HALF", "standard": ""}
_BC_SCORING_FREE = {"QB", "K", "DST"}
_BC_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DST", "FLX"}


def boris_chen_tiers(position: str, scoring: str = "half-ppr") -> str:
    pos = position.upper().replace("D/ST", "DST").strip()
    if pos == "FLEX":
        pos = "FLX"
    if pos not in _BC_POSITIONS:
        raise ValueError(f"position must be one of {sorted(_BC_POSITIONS)}")
    suffix = "" if pos in _BC_SCORING_FREE else _BC_SUFFIX.get(scoring.lower(), "-HALF")
    url = _BC_BASE.format(pos=pos, suffix=suffix)
    rows = list(csv.DictReader(io.StringIO(http_get(url).decode("utf-8", "replace"))))
    if not rows:
        raise RuntimeError(f"no rows in {url}")
    tiers: dict[str, list[str]] = {}
    for row in rows:
        tiers.setdefault(row.get("Tier", "?"), []).append(
            f"{row.get('Player.Name', '?')} ({row.get('Matchup', '-')}, "
            f"avg {row.get('Avg.Rank', '?')}, sd {row.get('Std.Dev', '?')})")
    label = "n/a (no scoring variants)" if pos in _BC_SCORING_FREE else scoring
    lines = [f"Boris Chen weekly tiers — {pos}, scoring: {label}", url, ""]
    for tier, players in tiers.items():
        lines.append(f"Tier {tier}: " + "; ".join(players))
    lines.append("\nHigh sd = experts disagree. Start/sit by tier gap, not rank.")
    return "\n".join(lines)


# ── FantasyPros expert-consensus rankings, structured ────────────────────────
# The rankings pages render via JS, but the full dataset is embedded in the
# page source as `var ecrData = {...}` — parse that instead of the DOM.
_FP_SCORING_FREE = {"QB", "K", "DST"}
_FP_PREFIX = {"ppr": "ppr-", "half-ppr": "half-point-ppr-", "half": "half-point-ppr-",
              "standard": ""}
_FP_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DST", "FLEX"}


def fp_ecr(position: str, scoring: str = "half-ppr",
           horizon: str = "week") -> dict:
    """Raw FantasyPros ECR data (parsed ecrData JSON) — meta + players list.
    horizon 'week' = this week's pages (r2p_pts = points this week);
    horizon 'ros' = rest-of-season pages (deeper pools, r2p_pts = season-total
    points — verified live 2026-09-15). Shared by fantasypros_rankings,
    tiers.py, and fpvalue.py."""
    pos = position.upper().replace("D/ST", "DST").replace("FLX", "FLEX").strip()
    if pos not in _FP_POSITIONS:
        raise ValueError(f"position must be one of {sorted(_FP_POSITIONS)}")
    if horizon not in ("week", "ros"):
        raise ValueError("horizon must be 'week' or 'ros'")
    prefix = "" if pos in _FP_SCORING_FREE else _FP_PREFIX.get(scoring.lower(), "half-point-ppr-")
    ros = "ros-" if horizon == "ros" else ""
    url = f"https://www.fantasypros.com/nfl/rankings/{ros}{prefix}{pos.lower()}.php"
    raw = http_get(url).decode("utf-8", "replace")
    m = re.search(r"var ecrData = (\{.*?\});\n", raw, re.S) or \
        re.search(r"var ecrData = (\{.*?\});", raw, re.S)
    if not m:
        raise RuntimeError(f"no ecrData found on {url} — page layout changed")
    data = json.loads(m.group(1))
    if not data.get("players"):
        raise RuntimeError(f"ecrData has no players on {url}")
    data["_url"], data["_pos"] = url, pos
    return data


def fantasypros_rankings(position: str, scoring: str = "half-ppr",
                         limit: int = 40, horizon: str = "week") -> str:
    data = fp_ecr(position, scoring, horizon)
    pos, url = data["_pos"], data["_url"]
    players = data["players"]
    span = ("rest of season" if horizon == "ros"
            else f"week {data.get('week')}")
    lines = [
        f"FantasyPros expert-consensus rankings — {pos}, {span}, "
        f"scoring {data.get('scoring')} "
        f"{data.get('year')}, {data.get('total_experts')} experts, "
        f"updated {data.get('last_updated')}",
        url, "",
        "rank(pos)  player  opp  |  avg ± std [best–worst]  grade  own%",
    ]
    for p in players[: max(1, int(limit))]:
        grade = p.get("start_sit_grade") or "-"
        delta = p.get("player_ecr_delta")
        moved = f"  Δ{delta:+d}" if isinstance(delta, int) and delta else ""
        lines.append(
            f"{p.get('rank_ecr'):>3} ({p.get('pos_rank', '-')})  "
            f"{p.get('player_name')} ({p.get('player_team_id')}) "
            f"{p.get('player_opponent') or '-'}  |  "
            f"{p.get('rank_ave')} ± {p.get('rank_std')} "
            f"[{p.get('rank_min')}–{p.get('rank_max')}]  {grade}  "
            f"{p.get('player_owned_avg', '-')}%{moved}")
    if len(players) > limit:
        lines.append(f"… {len(players) - limit} more (raise limit to see them)")
    lines.append("\nHigh std = experts disagree; Δ = rank change since last update.")
    return "\n".join(lines)


# ── Player news (the beat-writer wire, syndicated) ───────────────────────────
# X blocks anonymous reads (syndication endpoint 429s, nitter mirrors dead —
# probed 2026-09-15), so tweets can't be fetched directly. Rotowire's news feed
# carries the same reports with attribution AND a link to the source tweet.
_NEWS_URL = "https://www.rotowire.com/football/news.php"
_NEWS_ITEM = re.compile(
    r'news-update__player-link"[^>]*>(?P<player>[^<]+)</a>'
    r'<a[^>]*news-update__headline"[^>]*>(?P<headline>[^<]*)</a>.*?'
    r'news-update__pos">(?P<pos>[^<]*)</b>(?P<team>[^<]*)<.*?'
    r'(?:news-update__inj">(?P<inj>[^<]*)<.*?)?'
    r'news-update__timestamp">(?P<date>[^<]*)<.*?'
    r'news-update__news">(?P<body>.*?)</div>',
    re.S)
_X_LINK = re.compile(r'href="(https://(?:x|twitter)\.com/[^"]+/status/[^"]+)"')
_TAGS = re.compile(r"<[^>]+>")


def player_news_data(query: str | None = None) -> dict:
    """Structured news items — used by player_news (text) and the UI's /api/news."""
    raw = http_get(_NEWS_URL).decode("utf-8", "replace")
    items = []
    for m in _NEWS_ITEM.finditer(raw):
        body_html = m.group("body")
        tweet = _X_LINK.search(body_html)
        items.append({
            "player": m.group("player").strip(),
            "headline": m.group("headline").strip(),
            "pos": m.group("pos").strip(),
            "team": m.group("team").strip(),
            "inj": (m.group("inj") or "").strip(),
            "date": m.group("date").strip(),
            "body": " ".join(_TAGS.sub("", body_html).split()),
            "tweet": tweet.group(1) if tweet else "",
        })
    if not items:
        raise RuntimeError(f"no news items parsed from {_NEWS_URL} — layout changed")
    total = len(items)
    if query:
        q = query.lower()
        items = [i for i in items
                 if q in i["player"].lower() or q in i["team"].lower()
                 or q in i["body"].lower() or q in i["pos"].lower()]
    return {"total": total, "items": items, "url": _NEWS_URL}


def player_news(query: str | None = None, limit: int = 10) -> str:
    d = player_news_data(query)
    total, items = d["total"], d["items"]
    if query:
        if not items:
            return (f"No items matching {query!r} among the {total} most recent "
                    f"reports on {_NEWS_URL}. Older news isn't served on this "
                    "page — the player may simply have no recent report.")
    lines = [f"Latest NFL player news (Rotowire wire — beat-writer reports with "
             f"source links), {len(items)} of {total} recent items:", ""]
    for i in items[: max(1, int(limit))]:
        inj = f" [{i['inj']}]" if i["inj"] else ""
        lines.append(f"- {i['player']} ({i['pos']}, {i['team']}){inj} — "
                     f"{i['headline']} ({i['date']})")
        lines.append(f"    {i['body']}")
        if i["tweet"]:
            lines.append(f"    source tweet: {i['tweet']}")
    return "\n".join(lines)


# ── Per-reporter content via Google News RSS ─────────────────────────────────
# The wiki's Twitter lists can't be read directly (X blocks anonymous access),
# but each person's *reporting* is syndicated — Google News RSS serves it
# anonymously (probed 2026-09-15: 200, 100 items).
_GN_URL = ("https://news.google.com/rss/search?q=%22{q}%22%20NFL"
           "&hl=en-US&gl=US&ceid=US:en")


# handles whose camel-split is not the person's name
_HANDLE_ALIASES = {
    "rapsheet": "Ian Rapoport", "mortreport": "Chris Mortensen",
    "matthewberrytmr": "Matthew Berry", "yahoonoise": "Dalton Del Don",
    "rotopat": "Patrick Daugherty", "adbrandt": "Andrew Brandt",
    "charrisespn": "Chris Harris", "4for4_john": "John Paulsen",
}


def _handle_to_name(s: str) -> str:
    s = s.strip().lstrip("@")
    alias = _HANDLE_ALIASES.get(s.lower())
    if alias:
        return alias
    # camelCase handle -> spaced name: AdamSchefter -> Adam Schefter
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s.replace("_", " ")).strip()


def reporter_feed(who: str, limit: int = 12) -> str:
    import urllib.parse
    import xml.etree.ElementTree as ET
    name = _handle_to_name(who)
    url = _GN_URL.format(q=urllib.parse.quote(name))
    root = ET.fromstring(http_get(url).decode("utf-8", "replace"))
    items = root.findall(".//item")
    if not items:
        return (f"No recent coverage found for {name!r}. If this was a Twitter "
                "handle, try the person's real name — or the account may be "
                "inactive (the wiki's lists date from ~2019).")
    lines = [f"Latest reporting by/citing {name} (Google News, "
             f"{len(items)} items found):", ""]
    for it in items[: max(1, int(limit))]:
        title = (it.findtext("title") or "").strip()
        date = (it.findtext("pubDate") or "").strip()
        lines.append(f"- {title}  ({date})")
    lines.append("\nHeadline-level only — cross-check details against "
                 "player_news, which carries full report text.")
    return "\n".join(lines)


# ── Twitter handle lookup (data, not fetch) ──────────────────────────────────
def twitter_handles(team: str | None = None) -> str:
    note = ("Handle lists date from the ~2019 wiki revision — expect dead or "
            "renamed accounts, and franchise moves (OAK→LV, SD→LAC, STL→LA, "
            "WAS→Commanders). X can't be read anonymously: for the actual "
            "content, use reporter_feed(handle) or player_news(team).")
    if not team:
        return ("Fantasy experts: " + " ".join(catalog.TWITTER_EXPERTS)
                + "\n\nTeams available: "
                + "; ".join(catalog.TWITTER_BEAT_WRITERS) + f"\n\n{note}")
    t = team.lower()
    for name, handles in catalog.TWITTER_BEAT_WRITERS.items():
        if t in name.lower():
            return f"{name}: " + " ".join(handles) + f"\n\n{note}"
    return ("No team matched. Teams: " + "; ".join(catalog.TWITTER_BEAT_WRITERS))
