#!/usr/bin/env python3
"""Data & research console API — a mountable FastAPI `APIRouter`.

WS-5 owns `server.py`, so this ships as a router the coordinating session mounts
with one line (see docs/ws7_stage.md):

    from data_api import router as data_router
    app.include_router(data_router)

Everything here is READ-ONLY by default. The one write surface — running a
pipeline stage from the browser — is remote code execution, so it is gated three
ways (contracts.md, "Run-trigger API"):

  1. `PIPELINE_RUN_ENABLED=1` in the server environment. Unset => every run
     request is refused with an explanation. The Railway deployment is read-only
     unless someone deliberately turns this on.
  2. The stage name is looked up in a FIXED allow-list of argv templates. An
     unknown stage is a 400; the request string never reaches a command.
  3. The command is a LIST passed to `subprocess.Popen(..., shell=False)`. No
     shell, no interpolation, no user-supplied arguments — the league key is
     validated against the league registry, not echoed through.

`server.py`'s existing HTTP Basic middleware wraps every route on this router
automatically (it is app-level), so mounting this does not weaken the gate.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import leagues                                    # noqa: E402
from analysis import pipeline_status as ps        # noqa: E402

router = APIRouter(prefix="/api", tags=["data-console"])

# Prefer the project venv (openpyxl / the pipeline's deps live there), else this
# interpreter. Never a shell string.
PY_BIN = os.path.join(ROOT, ".venv", "bin", "python")
if not os.path.exists(PY_BIN):
    PY_BIN = sys.executable

# ── the allow-list. Adding a stage here is a deliberate code change. ─────────
# {stage: [argv...]} — "{league}" is the ONLY placeholder, and it is replaced with
# a key that has already been resolved against the league registry.
STAGE_COMMANDS: dict[str, list[str]] = {
    "season-scrape":    [PY_BIN, "pipeline.py", "season-scrape", "--league", "{league}"],
    "sources":          [PY_BIN, "pipeline.py", "sources", "--league", "{league}"],
    "season-calibrate": [PY_BIN, "pipeline.py", "season-calibrate", "--league", "{league}"],
    "season-build":     [PY_BIN, "pipeline.py", "season-build", "--league", "{league}"],
    "season-inject":    [PY_BIN, "pipeline.py", "season-inject", "--league", "{league}"],
    # This workstream's own stage. Deliberately NOT --league scoped: the served page
    # carries every league, so a single-league rebuild would shrink it.
    "data-console":     [PY_BIN, "draft_sheets/inject_data_console.py"],
}

RUN_FLAG = "PIPELINE_RUN_ENABLED"
MAX_LOG_LINES = 4000
JOB_TIMEOUT_S = 60 * 30


def run_enabled() -> bool:
    return os.environ.get(RUN_FLAG, "").strip().lower() in ("1", "true", "yes", "on")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ctx(key):
    """Resolve a league key against the registry. Returns None for anything else —
    this is also what keeps an arbitrary string out of the command line."""
    if not key:
        return None
    try:
        return leagues.resolve(str(key))
    except Exception:
        return None


def _err(msg, code=400, **extra):
    return JSONResponse({"ok": False, "error": msg, **extra}, status_code=code)


# ── status ───────────────────────────────────────────────────────────────────
@router.get("/pipeline/leagues")
def api_leagues():
    """Every league in scope, and whether the run-trigger is armed."""
    try:
        ctxs = leagues.all()
    except Exception as e:
        return _err(f"league registry unavailable: {e}", 503)
    return {"ok": True, "generated": _now(), "run_enabled": run_enabled(),
            "leagues": [c.to_dict() for c in ctxs]}


@router.get("/pipeline/status")
def api_status(league: str | None = None, write: bool = False):
    """Live `pipeline_status.json` — collected from disk at request time."""
    try:
        ctxs = [leagues.resolve(league)] if league else leagues.all()
    except Exception as e:
        return _err(f"unknown league {league!r}: {e}", 404)
    out = []
    for c in ctxs:
        st = ps.collect(c)
        if write:
            try:
                ps.write(c, st)
            except Exception as e:
                st.setdefault("warnings", []).append(f"could not write status file: {e}")
        out.append(st)
    return {"ok": True, "generated": _now(), "run_enabled": run_enabled(),
            "stages_allowed": sorted(STAGE_COMMANDS),
            "statuses": out}


# ── run trigger (gated) ──────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_job_seq = itertools.count(1)


def _refusal():
    return JSONResponse({
        "ok": False,
        "enabled": False,
        "error": f"pipeline runs are disabled on this server ({RUN_FLAG} is not set)",
        "detail": ("Running a pipeline stage from a browser is remote code execution, so it "
                   "ships OFF. Start the server with "
                   f"{RUN_FLAG}=1 to arm it, or run the stage from a shell."),
        "stages_allowed": sorted(STAGE_COMMANDS),
    }, status_code=403)


def _pump(job_id, proc, ctx, stage, started):
    job = _jobs[job_id]
    try:
        for line in iter(proc.stdout.readline, ""):
            with _jobs_lock:
                if len(job["lines"]) < MAX_LOG_LINES:
                    job["lines"].append(line.rstrip("\n"))
                elif len(job["lines"]) == MAX_LOG_LINES:
                    job["lines"].append(f"… log truncated at {MAX_LOG_LINES} lines")
        proc.wait(timeout=JOB_TIMEOUT_S)
    except Exception as e:                       # timeout, killed process, decode error
        with _jobs_lock:
            job["lines"].append(f"[runner] {type(e).__name__}: {e}")
        try:
            proc.kill()
        except Exception:
            pass
    finished = time.time()
    code = proc.returncode
    with _jobs_lock:
        job.update(state="done" if code == 0 else "failed", exit_code=code,
                   finished=_now(), duration_s=round(finished - started, 1))
    try:                                          # feed duration/error back to the status board
        ps.record_run(ctx, stage, started=started, finished=finished, ok=(code == 0),
                      error=None if code == 0 else f"exit {code}: " + " / ".join(job["lines"][-3:]),
                      exit_code=code)
    except Exception:
        pass


@router.post("/pipeline/run")
async def api_run(req: Request):
    """Run ONE allow-listed stage for ONE registered league. Off unless armed."""
    if not run_enabled():
        return _refusal()
    try:
        body = await req.json()
    except Exception:
        return _err("invalid JSON body")
    stage = str(body.get("stage") or "")
    key = str(body.get("league") or "")

    if stage not in STAGE_COMMANDS:               # allow-list, not sanitisation
        return _err(f"unknown stage {stage!r}", 400, stages_allowed=sorted(STAGE_COMMANDS))
    ctx = _ctx(key)
    if ctx is None:
        return _err(f"unknown league {key!r}", 404)

    with _jobs_lock:
        for j in _jobs.values():
            if j["state"] == "running" and j["league"] == ctx.key and j["stage"] == stage:
                return _err(f"`{stage}` is already running for {ctx.key}", 409, job_id=j["job_id"])

    argv = [part.replace("{league}", ctx.key) for part in STAGE_COMMANDS[stage]]
    job_id = f"{ctx.key}.{stage}.{next(_job_seq)}"
    started = time.time()
    try:
        proc = subprocess.Popen(
            argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, shell=False,      # explicit: never a shell
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
    except Exception as e:
        return _err(f"could not start `{stage}`: {type(e).__name__}: {e}", 500)

    with _jobs_lock:
        _jobs[job_id] = {"job_id": job_id, "league": ctx.key, "stage": stage,
                         "state": "running", "started": _now(), "finished": None,
                         "argv": argv, "lines": [f"$ {' '.join(argv)}"],
                         "exit_code": None, "duration_s": None}
    threading.Thread(target=_pump, args=(job_id, proc, ctx, stage, started),
                     daemon=True).start()
    return {"ok": True, "job_id": job_id, "league": ctx.key, "stage": stage,
            "poll": f"/api/pipeline/job/{job_id}"}


@router.get("/pipeline/job/{job_id}")
def api_job(job_id: str, after: int = 0):
    """Poll a job's log. `after` is the number of lines already displayed."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return _err(f"unknown job {job_id!r}", 404)
        lines = job["lines"][max(after, 0):]
        return {"ok": True, **{k: v for k, v in job.items() if k != "lines"},
                "lines": lines, "next": len(job["lines"])}


@router.get("/pipeline/jobs")
def api_jobs():
    with _jobs_lock:
        return {"ok": True, "run_enabled": run_enabled(),
                "jobs": [{k: v for k, v in j.items() if k != "lines"}
                         for j in sorted(_jobs.values(), key=lambda j: j["started"], reverse=True)]}


# ── research: Tier-B allow-list + test fetch ─────────────────────────────────
def _tierb_path(ctx):
    """Per-league override first, then the shared list. WS-2 owns the file; it
    may not exist yet, which is not an error."""
    if ctx is not None:
        p = ctx.config("research_sources.json")
        if os.path.exists(p):
            return p
    p = os.path.join(ROOT, "config", "research_sources.json")
    return p if os.path.exists(p) else None


def _tierb_entries(ctx):
    path = _tierb_path(ctx)
    if not path:
        return [], None, []
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception:
        return [], path, []
    # WS-2's file is {_meta, allow, dead}. Only `allow` is fetchable — `dead` is a
    # record of what NOT to re-add, and is returned separately for display.
    if isinstance(raw, dict):
        items = raw.get("allow") or raw.get("sources") or []
        dead = raw.get("dead") or []
    else:
        items, dead = raw, []
    out = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        url = str(it.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        out.append({"id": str(it.get("id") or it.get("name") or f"src{i}"),
                    "name": it.get("name") or it.get("id") or url,
                    "url": url,
                    "category": it.get("category") or "uncategorised",
                    "good_for": (it.get("what_its_good_for") or it.get("good_for")
                                 or it.get("notes") or ""),
                    "verified": it.get("verified") or it.get("checked") or "",
                    "tier": it.get("tier") or "B"})
    return out, path, dead


@router.get("/research/tierb")
def api_tierb(league: str | None = None):
    ctx = _ctx(league)
    entries, path, dead = _tierb_entries(ctx)
    return {"ok": True, "path": os.path.relpath(path, ROOT) if path else None,
            "present": bool(path), "count": len(entries), "sources": entries, "dead": dead,
            "note": None if path else
                    ("config/research_sources.json does not exist yet (WS-2 owns it). "
                     "Expected: [{id,name,url,category,good_for}].")}


def _cached_get(ctx, url):
    """Probe through the repo's shared fetch cache (scraping/cache.py).

    One policy for every fetcher: a cached page answers instantly and the probe
    costs nobody a request, and `meta["cache"]` tells the user whether they just
    hit the network or the disk. Falls back to a plain request only if the cache
    module is unavailable.
    """
    try:
        sys.path.insert(0, os.path.join(ROOT, "scraping"))
        from cache import Cache, LIVE
        root = ctx.raw("cache") if ctx is not None else os.path.join(
            ROOT, "scraping", "raw", "_shared", "cache")
        return Cache(root).get_text(url, tier=LIVE, timeout=15)
    except ImportError:
        req = urllib.request.Request(url, headers={
            "user-agent": "fantasy-data-console/1.0 (allow-list probe; single request)",
            "accept": "text/html,application/json;q=0.9,*/*;q=0.8"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read(262_144).decode("utf-8", "replace"), {"cache": "uncached"}


@router.post("/research/test_fetch")
async def api_test_fetch(req: Request):
    """Probe ONE allow-listed Tier-B page.

    The request names a source **id**; the URL is read from the config file. A URL
    in the request body is ignored — so this cannot be pointed at an arbitrary host.
    """
    try:
        body = await req.json()
    except Exception:
        return _err("invalid JSON body")
    ctx = _ctx(body.get("league"))
    entries, path, _dead = _tierb_entries(ctx)
    if not entries:
        return _err("no Tier-B allow-list on this server", 404)
    want = str(body.get("id") or body.get("name") or "")
    entry = next((e for e in entries if e["id"] == want or e["name"] == want), None)
    if entry is None:
        return _err(f"{want!r} is not in the allow-list", 400,
                    allowed=[e["id"] for e in entries][:50])

    t0 = time.time()
    try:
        text, meta = _cached_get(ctx, entry["url"])
        return {"ok": True, "id": entry["id"], "url": entry["url"],
                "status": 200, "cache": meta.get("cache"),
                "bytes": len(text.encode("utf-8", "replace")),
                "fetched_at": meta.get("fetched_at"),
                "elapsed_ms": int((time.time() - t0) * 1000),
                "preview": text[:400]}
    except Exception as e:
        return {"ok": False, "id": entry["id"], "url": entry["url"],
                "status": getattr(e, "code", None), "error": f"{type(e).__name__}: {e}"[:300],
                "elapsed_ms": int((time.time() - t0) * 1000)}


# ── research: the advisor's live-research trace ──────────────────────────────
TRACE_NAME = "research_trace.json"


@router.get("/research/trace")
def api_trace(league: str | None = None, limit: int = 25):
    """What the advisor fetched, what it cited, and what it cost in latency.

    Read-only. The producer is the advisor (WS-6): it appends one entry per call
    to `ctx.out("research_trace.json")`. Absent file => empty, not an error.
    """
    ctx = _ctx(league)
    if ctx is None:
        return _err(f"unknown league {league!r}", 404)
    path = ctx.out(TRACE_NAME)
    if not os.path.exists(path):
        return {"ok": True, "present": False, "entries": [],
                "path": os.path.relpath(path, ROOT),
                "note": ("no research trace yet — the advisor writes one entry per live-research "
                         "call to this path")}
    try:
        with open(path) as f:
            entries = json.load(f)
    except Exception as e:
        return _err(f"unreadable trace: {e}", 500)
    if isinstance(entries, dict):
        entries = entries.get("entries") or []
    return {"ok": True, "present": True, "path": os.path.relpath(path, ROOT),
            "entries": list(entries)[-max(limit, 1):][::-1]}


@router.get("/data-console/healthz")
def api_health():
    """Cheap probe for the console itself (separate from server.py's /healthz)."""
    try:
        keys = [c.key for c in leagues.all()]
    except Exception:
        keys = []
    return {"ok": True, "run_enabled": run_enabled(), "leagues": keys,
            "stages_allowed": sorted(STAGE_COMMANDS)}
