#!/usr/bin/env python3
"""Live auction draft console — FastAPI backend.

Serves the static console and a thin LLM advisor (/api/advise) powered by
Claude Haiku 4.5 (fast, for live-draft latency). The advisor's system prompt is
a strategy briefing distilled from the league analysis, so it predicts opponent
behavior with full context; the frontend posts the live draft state each call.
"""
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import anthropic

HERE = os.path.dirname(os.path.abspath(__file__))
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
    return {"ok": True, "advisor": bool(os.environ.get("ANTHROPIC_API_KEY"))}


# Serve the console at / (mounted last so /api/* and /healthz take precedence)
app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
