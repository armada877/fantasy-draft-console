# Live Auction Draft Console (Railway webapp)

A live auction command console for your draft, deployable on Railway. It
re-prices every remaining player in real time from the bidding tendencies of each
opponent (who still has budget + a need), tracks your build against your roster, and
includes a **thin LLM advisor** (Claude Haiku 4.5) that predicts what the room will
do — grounded in a strategy briefing you write for your league.

## What's here
```
draft_app/
  server.py            FastAPI: serves the console + POST /api/advise (Anthropic Haiku)
  static/index.html    the console (generated: template + injected data)
  static/data.json     your projections + opponent tendencies (copy of tool_data.json)
  requirements.txt     fastapi, uvicorn, anthropic (+ openpyxl for the pipeline)
  Procfile             web: uvicorn server:app --host 0.0.0.0 --port $PORT
  railway.json         Railway build/deploy config
  .env.example         ANTHROPIC_API_KEY=... (local runs use config/.env instead)
```

## Run locally
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...      # advisor works only with this set
uvicorn server:app --reload --port 8000
# open http://localhost:8000
```
Without `ANTHROPIC_API_KEY` the whole console works; only the Advisor panel returns
"unavailable" (everything else — pricing, board, build tracker — is client-side).

## Deploy on Railway
1. Push this `draft_app/` folder to a GitHub repo (or use `railway up` from the CLI).
2. In Railway: **New Project → Deploy from GitHub repo** (pick the repo/root that
   contains `server.py`). Nixpacks auto-detects Python and installs `requirements.txt`.
3. **Variables → New Variable:** `ANTHROPIC_API_KEY = sk-ant-...`
4. Railway sets `$PORT`; the Procfile/`railway.json` start command already binds it.
5. Open the generated URL. Done.

CLI alternative:
```bash
npm i -g @railway/cli
railway login
railway init
railway variables set ANTHROPIC_API_KEY=sk-ant-...
railway up
```

## Using it on draft night
- **On the block:** type the nominated player → see Worth, live "Will go for ≈",
  your Max bid, and a TARGET / FAIR / LET IT GO verdict. Log the sale (price + team).
- **Value board:** best available, ranked by Worth (the top studs lead). Each row also
  shows the live "Will go ≈" price and your Max; the Edge column (worth − predicted
  price) is muted until a genuine bargain opens, then it glows. Prices re-compute as
  opponents spend.
- **Your build vs target:** tracks your roster against your target split (positional
  tilt comes from `my_mult` in `config/league.json`).
- **Advisor (LLM):** ask "predict the room", "where's the value?", or a bid check — it
  reads the live board + budgets + each manager's tendencies. It also **auto-refreshes
  after every logged pick** (debounced), posting a "↻ Auto-read after last pick" with the
  single most important next move. (The formulaic *Read & Recommend* panel is separate and
  always updates instantly on every pick — no model call.)
- **Undo:** per-pick ✕ in the draft log, or "Undo last".

## Refreshing the data for a future year
Re-run `python3 draft_sheets/build_tool_data.py` to regenerate
`draft_sheets/tool_data.json`, then re-inject into `static/index.html` and copy to
`static/data.json` (see the root README Quickstart). Update the advisor briefing in
`config/briefing.md` if your opponents/tendencies change.

## Model
The advisor uses `claude-haiku-4-5` for low latency during a live draft (you asked
for a quick/lighter model). To change it, edit `MODEL` in `server.py`.
