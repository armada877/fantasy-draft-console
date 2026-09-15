"""Research desk — a chat UI over the research_agent (Claude Agent SDK).

Separate from draft_app on purpose: different lifecycle (local research chat vs
the deployed draft console), different port, no shared state. Uses the same
ANTHROPIC_API_KEY env var as draft_app.

    ANTHROPIC_API_KEY=sk-ant-... .venv/bin/uvicorn server:app \
        --app-dir research_app --host 127.0.0.1 --port 8010

Each browser tab holds one agent session (ClaudeSDKClient), so follow-ups keep
context. /api/chat streams SSE events: `tool` (a tool call started), `text`
(an assistant text block), `result` (turn done: cost/duration), `error`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from research_agent import fpvalue, leaguetools, tiers, webtools  # noqa: E402
from research_agent.agent import DEFAULT_MODEL, build_options  # noqa: E402

ALLOWED_MODELS = {"claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"}
SESSION_IDLE_SECONDS = 60 * 60      # drop agent sessions untouched for an hour
MAX_SESSIONS = 20

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(title="research desk")


class Session:
    def __init__(self, model: str):
        self.model = model
        self.client = ClaudeSDKClient(options=build_options(model=model))
        self.lock = asyncio.Lock()
        self.connected = False
        self.touched = time.time()

    async def ensure_connected(self):
        if not self.connected:
            await self.client.connect()
            self.connected = True

    async def close(self):
        if self.connected:
            self.connected = False
            try:
                await self.client.disconnect()
            except Exception:
                pass


SESSIONS: dict[str, Session] = {}


async def _reap():
    now = time.time()
    stale = [k for k, s in SESSIONS.items()
             if now - s.touched > SESSION_IDLE_SECONDS]
    if len(SESSIONS) - len(stale) >= MAX_SESSIONS:
        stale += sorted(SESSIONS, key=lambda k: SESSIONS[k].touched)[:1]
    for k in stale:
        s = SESSIONS.pop(k, None)
        if s:
            await s.close()


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str
    model: str | None = None


def _sse(kind: str, **payload) -> str:
    return "data: " + json.dumps({"type": kind, **payload}) + "\n\n"


def _tool_event(block: ToolUseBlock) -> dict | None:
    # mcp__myleague__team_details -> family "myleague", name "team_details".
    # Harness-internal calls (ToolSearch etc.) are noise — no chip.
    parts = block.name.split("__")
    if len(parts) != 3:
        return None
    family, name = parts[1], parts[2]
    args = {k: v for k, v in (block.input or {}).items() if v not in (None, "")}
    brief = ", ".join(f"{k}={json.dumps(v) if not isinstance(v, str) else v}"
                      for k, v in list(args.items())[:3])
    return {"family": family, "name": name, "args": brief[:120]}


@app.get("/healthz")
async def healthz():
    leagues_ok = os.path.exists(os.path.join(ROOT, "config", "leagues.json"))
    return {"ok": True, "agent": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "leagues": leagues_ok, "sessions": len(SESSIONS)}


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.post("/api/chat")
async def chat(inp: ChatIn):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(503, "ANTHROPIC_API_KEY is not set on the server")
    msg = (inp.message or "").strip()
    if not msg:
        raise HTTPException(400, "empty message")
    model = inp.model or DEFAULT_MODEL
    if model not in ALLOWED_MODELS:
        raise HTTPException(400, f"model must be one of {sorted(ALLOWED_MODELS)}")

    await _reap()
    sid = inp.session_id or uuid.uuid4().hex[:12]
    sess = SESSIONS.get(sid)
    if sess is None or sess.model != model:
        if sess is not None:            # model switch = fresh agent session
            await sess.close()
        sess = SESSIONS[sid] = Session(model)
    sess.touched = time.time()

    async def stream():
        # one query at a time per session; a second tab just waits
        async with sess.lock:
            yield _sse("session", session_id=sid, model=model)
            try:
                await sess.ensure_connected()
                await sess.client.query(msg)
                async for m in sess.client.receive_response():
                    if isinstance(m, AssistantMessage):
                        for block in m.content:
                            if isinstance(block, ToolUseBlock):
                                ev = _tool_event(block)
                                if ev:
                                    yield _sse("tool", **ev)
                            elif isinstance(block, TextBlock) and block.text:
                                yield _sse("text", text=block.text)
                    elif isinstance(m, ResultMessage):
                        yield _sse(
                            "result",
                            is_error=bool(m.is_error),
                            result=m.result or "",
                            turns=m.num_turns,
                            duration_s=round((m.duration_ms or 0) / 1000, 1),
                            cost_usd=round(m.total_cost_usd or 0, 4),
                        )
            except Exception as e:
                # a dead subprocess would poison the session — drop it
                SESSIONS.pop(sid, None)
                await sess.close()
                yield _sse("error", error=f"{type(e).__name__}: {e}")
            finally:
                sess.touched = time.time()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache",
                                      "x-accel-buffering": "no"})


# ── data endpoints for the non-chat sub-apps (no model in the loop) ──────────
# plain `def` so FastAPI runs the blocking file/HTTP work in its threadpool
@app.get("/api/leagues")
def api_leagues():
    import leagues
    return [{"key": c.key, "name": c.name, "team": c.team_name}
            for c in leagues.all()]


@app.get("/api/tiers")
def api_tiers(league: str, position: str = "RB", source: str = "consensus",
              horizon: str | None = None):
    try:
        return tiers.league_tiers_data(league, position, source, horizon)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/api/lineup")
def api_lineup(league: str, team: str | None = None):
    try:
        return leaguetools.weekly_lineup_data(league, team)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/api/fpvalue")
def api_fpvalue(league: str, limit: int = 60, horizon: str = "week"):
    try:
        return fpvalue.fp_league_value(league, limit, horizon)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.get("/api/news")
def api_news(query: str | None = None, limit: int = 25):
    try:
        d = webtools.player_news_data(query or None)
        d["items"] = d["items"][: max(1, min(int(limit), 50))]
        return d
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}")


@app.post("/api/reset")
async def reset(inp: ChatIn):
    sess = SESSIONS.pop(inp.session_id or "", None)
    if sess:
        await sess.close()
    return {"ok": True}
