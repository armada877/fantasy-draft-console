#!/usr/bin/env python3
"""Live auction draft console — FastAPI backend.

Serves the static console and a thin LLM advisor (/api/advise) powered by
Claude Haiku 4.5 (fast, for live-draft latency). The advisor's system prompt is
a strategy briefing distilled from the league analysis, so it predicts opponent
behavior with full context; the frontend posts the live draft state each call.

Set CONSOLE_PASSWORD to put HTTP Basic in front of everything but /healthz — required
when deploying publicly, since /api/advise spends real Anthropic credit per call.
"""
import base64
import json
import os
import re
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
import anthropic

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")
DEFAULT_MODEL = "claude-haiku-4-5"  # fast, for live-draft latency
# models the advisor dropdown may select (allowlist — anything else falls back to default)
ALLOWED_MODELS = {"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-4-8"}

def _load_briefing():
    """The advisor's system prompt. Kept in the local (gitignored) config/ directory so
    league-specific content (opponent names, your plan, your league's tendencies) stays
    out of the public repo. Override with STRATEGY_BRIEFING_PATH. Falls back to a generic,
    still-grounded briefing if none is present. See config/briefing.example.md."""
    path = os.environ.get(
        "STRATEGY_BRIEFING_PATH",
        os.path.join(HERE, os.pardir, "config", "briefing.md"),
    )
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    except OSError:
        pass
    return (
        "You are a draft-night strategist for an auction fantasy football league. Be concise, "
        "concrete, and decisive. Use ONLY the players, budgets, needs, and rosters in the provided "
        "live state — never invent players; TARGET must be a name in `best_available`. Size a bid "
        "off the player's `worth`/`est_price`, not the user's budget (budget is a ceiling, not a "
        "target). Check `teams[me].needs` before recommending a position. Copy "
        "config/briefing.example.md to config/briefing.md and customize it with your league's "
        "tendencies to make the advisor sharp."
    )


STRATEGY_BRIEFING = _load_briefing()


app = FastAPI(title="Live Auction Draft Console")

# ── Access gate ──────────────────────────────────────────────────────────────
# Deployed publicly (Railway), the console is a findable URL and /api/advise spends
# real Anthropic credit on every call. Set CONSOLE_PASSWORD to require HTTP Basic on
# everything except /healthz (Railway's health check must stay open). Unset => open,
# so local development and `python3 draft_app/eval_advisor.py` are unaffected.
CONSOLE_PASSWORD = os.environ.get("CONSOLE_PASSWORD", "").strip()
CONSOLE_USER = os.environ.get("CONSOLE_USER", "draft").strip() or "draft"
OPEN_PATHS = {"/healthz"}


def _authorized(header: str) -> bool:
    """Constant-time check of an HTTP Basic header against the configured password."""
    if not header.startswith("Basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:], validate=True).decode().partition(":")
    except Exception:
        return False
    # compare both halves in constant time so neither is a timing oracle
    return (secrets.compare_digest(user, CONSOLE_USER)
            and secrets.compare_digest(pw, CONSOLE_PASSWORD))


@app.middleware("http")
async def require_password(request: Request, call_next):
    if CONSOLE_PASSWORD and request.url.path not in OPEN_PATHS:
        if not _authorized(request.headers.get("authorization", "")):
            return Response(
                "Authentication required.", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Draft console", charset="UTF-8"'},
            )
    return await call_next(request)


@app.post("/api/advise")
async def advise(req: Request):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return JSONResponse({"error": "ANTHROPIC_API_KEY not set on server"}, status_code=503)
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    question = (body.get("question") or "").strip()
    state = body.get("state") or {}
    model = body.get("model") if body.get("model") in ALLOWED_MODELS else DEFAULT_MODEL
    if not question:
        return JSONResponse({"error": "empty question"}, status_code=400)

    client = anthropic.Anthropic(api_key=key)
    user_msg = (
        "Live draft state (JSON):\n" + json.dumps(state, separators=(",", ":")) +
        "\n\nQuestion: " + question
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=2048,  # ceiling only — model stops at natural end, so no latency cost for short answers
            system=STRATEGY_BRIEFING,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIStatusError as e:
        return JSONResponse({"error": f"model error {e.status_code}"}, status_code=502)
    except Exception as e:
        return JSONResponse({"error": str(e)[:200]}, status_code=502)

    answer = "".join(b.text for b in resp.content if b.type == "text").strip()
    return {"answer": answer, "model": resp.model, "truncated": resp.stop_reason == "max_tokens"}


@app.get("/healthz")
async def healthz():
    return {"ok": True, "advisor": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "gated": bool(CONSOLE_PASSWORD)}


# ── Surface: launcher at /, per-league tools under /l/{key}/ ─────────────────
# Everything served here is GENERATED (draft_sheets/inject_season.py for the launcher
# and the in-season cockpit; pipeline.py's `inject` stage for the draft console).
# Nothing under static/ is hand-written, and this module never builds a payload.
#
#   /                      launcher            static/home.html
#   /l/{key}/draft         auction console     static/l/{key}/draft.html, else static/index.html
#   /l/{key}/season        in-season cockpit   static/l/{key}/season.html
#
# The draft console MOVED here from /. Its bytes are untouched — the same generated
# file, served at a new path (tests/draft_regression.py still guards it).
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MISSING = ("Not built yet. Generate it with:\n\n"
            "    python3 draft_sheets/inject_season.py\n")


def _registry():
    """The generated launcher payload: {key: card}.

    Read per request (never cached) so a re-inject is picked up without a restart —
    the server has no --reload. Falls back to the directories inject_season wrote, so
    a stripped deploy bundle still routes.
    """
    try:
        with open(os.path.join(STATIC_DIR, "leagues.json"), encoding="utf-8") as f:
            reg = {lg.get("key"): lg for lg in (json.load(f).get("leagues") or [])
                   if lg.get("key") and KEY_RE.match(lg["key"])}
        if reg:
            return reg
    except (OSError, ValueError):
        pass
    try:
        return {d: {} for d in os.listdir(os.path.join(STATIC_DIR, "l"))
                if KEY_RE.match(d) and os.path.isdir(os.path.join(STATIC_DIR, "l", d))}
    except OSError:
        return {}


def _page(path: str, what: str):
    if os.path.exists(path):
        return FileResponse(path, media_type="text/html")
    return Response(f"{what} not built.\n\n{_MISSING}", status_code=404,
                    media_type="text/plain")


def _league(league_key: str):
    """(static dir, launcher card) for a known league, or (None, None).

    Validated against the registry, so no user-supplied string ever reaches a path.
    """
    if not KEY_RE.match(league_key or ""):
        return None, None
    reg = _registry()
    if league_key not in reg:
        return None, None
    return os.path.join(STATIC_DIR, "l", league_key), reg[league_key]


def _unknown(league_key: str):
    return Response(f"unknown league {league_key!r}\n", status_code=404,
                    media_type="text/plain")


@app.get("/")
async def launcher():
    """The app's front door: every league, every tool."""
    return _page(os.path.join(STATIC_DIR, "home.html"), "Launcher")


@app.get("/l/{league_key}")
async def league_root(league_key: str):
    return RedirectResponse(f"/l/{league_key}/season", status_code=307)


@app.get("/l/{league_key}/season")
async def season_console(league_key: str):
    d, _ = _league(league_key)
    if d is None:
        return _unknown(league_key)
    return _page(os.path.join(d, "season.html"), f"In-season cockpit for {league_key}")


@app.get("/l/{league_key}/draft")
async def draft_console(league_key: str):
    """The existing auction console, unchanged — only its route moved.

    A per-league generated console is served when one exists. Otherwise this falls back
    to the single console `pipeline.py inject` writes at static/index.html, but ONLY for
    the league that console was built for — the launcher card says which (`tools.draft`).
    Serving it under another league's key would show that league one league's board.
    """
    d, card = _league(league_key)
    if d is None:
        return _unknown(league_key)
    per_league = os.path.join(d, "draft.html")
    if os.path.exists(per_league):
        return FileResponse(per_league, media_type="text/html")
    if (card or {}).get("tools", {}).get("draft") is False:
        return Response(
            f"No draft console for {league_key!r}.\n\n"
            "static/index.html is generated by `pipeline.py inject` for the league in "
            "config/league.json only.\n", status_code=404, media_type="text/plain")
    return _page(os.path.join(STATIC_DIR, "index.html"), f"Draft console for {league_key}")


# Static assets (mounted last so the routes above and /api/* take precedence)
from data_api import router as data_router  # noqa: E402
app.include_router(data_router)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
