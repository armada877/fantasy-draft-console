# analysis/research/ — archived one-off analyses

These are the exploratory scripts that produced **`reports/league_analysis.md`**. They are
provenance, not part of the repeatable console pipeline (that's `../calibrate.py` →
`config/tendencies.json`, plus `../a18_agent_auction.py` for simulation). Kept here so the
report can be regenerated or adjusted; all stdout-only.

## Running them

They import the shared library and engines that stay one level up (`lib`, `a5_draft_value`),
so put `analysis/` on the path:

```bash
PYTHONPATH=analysis python3 analysis/research/a11_manager_vs_model.py
# (use the venv python if openpyxl/etc. aren't on your system python:
#  PYTHONPATH=analysis .venv/bin/python analysis/research/a11_manager_vs_model.py)
```

They read `draft_sheets/elboberto_projections.json` (regenerate it with
`python3 pipeline.py calibrate`, or `draft_sheets/extract_elboberto_master.py`) and the raw
scrape under `scraping/raw/`.

## What each produced (→ section of `reports/league_analysis.md`)

| Script | Topic |
|--------|-------|
| `a1_league_spending.py` | §1 league auction spending tendencies |
| `a2_manager_tendencies.py` | §2 per-manager draft tendencies (positional lean, stars-vs-scrubs) |
| `a3_transactions.py` | §3 per-manager transaction / waiver / offer behavior |
| `a3b_trades.py` | §3b executed trade history (from playercards) |
| `a3c_trade_offer_tendencies.py` | §3c trade & offer archetypes |
| `a4_success.py` | §4 behaviors that separate success vs failure (exposes `build()`) |
| `a6_nominations.py` | §6 nomination strategy |
| `a7_elboberto.py` | §7 Elboberto projections vs ESPN actuals |
| `a8_projected_vs_realized.py` | projected vs realized VORP (2025) |
| `a9_model_validation.py` | §9 model validation — baseline vs actuals |
| `a10_tier_conversion.py` | §10 where projected value converts (position × price tier) |
| `a11_manager_vs_model.py` | §11 manager vs the model — price-prediction + value pockets |
| `a12_framework_backtest.py` | §12 framework backtest (honest negative result) |
| `a13_framework_v2.py` | framework v2 allocation-rules backtest |
| `a14_auction_flow.py` | auction flow — when positions dry up |
| `a15_opponent_briefs.py` | per-opponent draft-day briefs |
| `a16_tier_cliffs.py` | 2026 tier cliffs (anti-panic map) |
| `a17_draft_sim.py` | early single-agent Monte-Carlo draft sim (superseded by `../a18_agent_auction.py`) |

Dependencies: `a7–a13` import `../a5_draft_value.py` (the shared VORP engine); `a11`/`a15`
import `a4_success.py`. Those resolve automatically with the `PYTHONPATH=analysis` prefix above.
