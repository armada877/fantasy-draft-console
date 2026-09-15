#!/usr/bin/env python3
"""Shared fetch cache — pull once, reuse forever where the data can never change.

ONE cache for every fetcher in the repo (ESPN scrape, source adapters, backtest
backfill) so there is a single policy instead of three. Backtests and repeated
pipeline runs must never re-pull data they already have.

The key idea is TIERS, because most of what we fetch is IMMUTABLE:

  IMMUTABLE  a completed historical week or season. Week 5 of 2023 will never change.
             Fetched exactly once, ever. Never revalidated, never expires.
  DAILY      slow-moving reference data (nflverse players, depth charts).
  LIVE       current-week projections, free-agent pool, ownership, trending adds.
             Short TTL, and revalidated with If-None-Match when the server gave an
             ETag — ESPN does, so a refresh usually costs a 304 instead of ~1MB.

Measured on this league (2026-09-14): one weekly boxscore is 966 KB of JSON but
85 KB gzipped (11x), so the cache stores gzip. A full historical backfill of all
three leagues is 221 requests ~ 214 MB raw, ~19 MB on disk.

    from cache import Cache, IMMUTABLE, LIVE
    cache = Cache(ctx.raw("cache"))
    data, meta = cache.get_json(url, headers=H, key="weeks/2025_wk05", tier=IMMUTABLE)

OFFLINE MODE (use this for backtests): Cache(..., offline=True) or FF_CACHE_OFFLINE=1
never touches the network and raises CacheMiss instead — so a backtest can never
silently start scraping, and a missing input fails loudly.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

IMMUTABLE = "immutable"     # historical; fetch once, never again
DAILY = "daily"             # slow-moving reference data
LIVE = "live"               # current week; short TTL + ETag revalidation

DEFAULT_TTL = {IMMUTABLE: None, DAILY: 24 * 3600, LIVE: 3600}


class CacheMiss(Exception):
    """Offline and the entry is not cached."""


class Cache:
    def __init__(self, root: str, offline: bool | None = None, refresh: bool = False,
                 user_agent: str | None = None):
        self.root = root
        self.offline = (os.environ.get("FF_CACHE_OFFLINE") == "1"
                        if offline is None else offline)
        self.refresh = refresh
        self.user_agent = user_agent or (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
        self._manifest_path = os.path.join(root, "_manifest.json")
        self._manifest = None
        self.stats = {"hit": 0, "miss": 0, "revalidated": 0, "fetched": 0, "bytes": 0}

    # ── manifest ─────────────────────────────────────────────────────────────
    @property
    def manifest(self) -> dict:
        if self._manifest is None:
            try:
                with open(self._manifest_path) as f:
                    self._manifest = json.load(f)
            except (OSError, ValueError):
                self._manifest = {}
        return self._manifest

    def _save_manifest(self):
        os.makedirs(self.root, exist_ok=True)
        tmp = self._manifest_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._manifest, f, indent=1, sort_keys=True)
        os.replace(tmp, self._manifest_path)     # atomic; never a half-written manifest

    # ── paths ────────────────────────────────────────────────────────────────
    def _key_for(self, url: str, key: str | None) -> str:
        if key:
            return key.strip("/")
        return "u/" + hashlib.sha1(url.encode()).hexdigest()[:20]

    def path(self, key: str) -> str:
        return os.path.join(self.root, key + ".json.gz")

    def has(self, key: str) -> bool:
        return os.path.exists(self.path(key))

    def _read(self, key: str):
        with gzip.open(self.path(key), "rt", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, key: str, data):
        p = self.path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, p)
        return os.path.getsize(p)

    # ── the one entry point ──────────────────────────────────────────────────
    def get_json(self, url: str, headers: dict | None = None, key: str | None = None,
                 tier: str = LIVE, ttl: int | None = None, timeout: int = 45,
                 unwrap_list: bool = False):
        """Return (data, meta). Fetches only when the cache cannot answer.

        unwrap_list: ESPN's leagueHistory endpoint wraps the payload in a
        single-element list; set True to store the unwrapped object.
        """
        key = self._key_for(url, key)
        entry = self.manifest.get(key)
        cached = self.has(key)
        ttl = DEFAULT_TTL.get(tier, 3600) if ttl is None else ttl

        # IMMUTABLE: if we have it, it is correct by definition. Never revalidate.
        if cached and entry and not self.refresh:
            age = time.time() - entry.get("fetched_at", 0)
            if tier == IMMUTABLE or ttl is None or age < ttl:
                self.stats["hit"] += 1
                return self._read(key), {**entry, "cache": "hit", "age_s": age}

        if self.offline:
            if cached:
                self.stats["hit"] += 1
                return self._read(key), {**(entry or {}), "cache": "stale-offline"}
            raise CacheMiss(f"offline and not cached: {key} ({url})")

        req_headers = dict(headers or {})
        req_headers.setdefault("user-agent", self.user_agent)
        req_headers.setdefault("accept-encoding", "gzip")
        # conditional revalidation — a 304 costs bytes-of-headers, not megabytes
        if cached and entry and entry.get("etag") and tier != IMMUTABLE:
            req_headers["if-none-match"] = entry["etag"]

        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=req_headers), timeout=timeout) as r:
                raw = r.read()
                if r.headers.get("content-encoding") == "gzip":
                    raw = gzip.decompress(raw)
                etag = r.headers.get("etag")
        except urllib.error.HTTPError as e:
            if e.code == 304 and cached:
                self.stats["revalidated"] += 1
                entry["fetched_at"] = time.time()
                self._save_manifest()
                return self._read(key), {**entry, "cache": "revalidated"}
            if cached:      # serve stale rather than fail — a source outage must not break a run
                self.stats["hit"] += 1
                return self._read(key), {**(entry or {}), "cache": "stale-error",
                                         "error": f"HTTP {e.code}"}
            raise

        data = json.loads(raw)
        if unwrap_list and isinstance(data, list):
            data = data[0] if data else {}
        size = self._write(key, data)
        self.stats["fetched"] += 1
        self.stats["miss"] += 1
        self.stats["bytes"] += size
        self.manifest[key] = {"url": url, "tier": tier, "fetched_at": time.time(),
                              "etag": etag, "bytes": size, "raw_bytes": len(raw),
                              "sha256": hashlib.sha256(raw).hexdigest()[:16]}
        self._save_manifest()
        return data, {**self.manifest[key], "cache": "fetched"}

    def get_text(self, url: str, headers=None, key=None, tier=LIVE, ttl=None, timeout=45):
        """Same policy, for CSV/text sources (Boris Chen, nflverse)."""
        data, meta = self.get_json.__wrapped__(self, url, headers, key, tier, ttl, timeout) \
            if hasattr(self.get_json, "__wrapped__") else (None, None)
        raise NotImplementedError  # replaced below

    def summary(self) -> str:
        s = self.stats
        return (f"cache: {s['hit']} hit, {s['fetched']} fetched, "
                f"{s['revalidated']} revalidated (304), {s['bytes'] / 1e6:.1f} MB written")


def _get_text(self, url, headers=None, key=None, tier=LIVE, ttl=None, timeout=45):
    """Text/CSV variant — stores {'text': ...} so one cache serves both shapes."""
    key = self._key_for(url, key)
    entry = self.manifest.get(key)
    cached = self.has(key)
    ttl = DEFAULT_TTL.get(tier, 3600) if ttl is None else ttl
    if cached and entry and not self.refresh:
        age = time.time() - entry.get("fetched_at", 0)
        if tier == IMMUTABLE or ttl is None or age < ttl:
            self.stats["hit"] += 1
            return self._read(key)["text"], {**entry, "cache": "hit"}
    if self.offline:
        if cached:
            return self._read(key)["text"], {**(entry or {}), "cache": "stale-offline"}
        raise CacheMiss(f"offline and not cached: {key} ({url})")
    h = dict(headers or {})
    h.setdefault("user-agent", self.user_agent)
    h.setdefault("accept-encoding", "gzip")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("content-encoding") == "gzip":
                raw = gzip.decompress(raw)
            etag = r.headers.get("etag")
    except urllib.error.HTTPError as e:
        if cached:
            return self._read(key)["text"], {**(entry or {}), "cache": "stale-error",
                                             "error": f"HTTP {e.code}"}
        raise
    text = raw.decode("utf-8", errors="replace")
    size = self._write(key, {"text": text})
    self.stats["fetched"] += 1
    self.stats["bytes"] += size
    self.manifest[key] = {"url": url, "tier": tier, "fetched_at": time.time(),
                          "etag": etag, "bytes": size, "raw_bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()[:16]}
    self._save_manifest()
    return text, {**self.manifest[key], "cache": "fetched"}


Cache.get_text = _get_text
