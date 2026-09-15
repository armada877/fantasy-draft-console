"""Boris Chen's tier method, run locally and adapted per league.

Port of github.com/borisachen/fftiers (main.R + ff-functions.R) — an
ITERATION on his method, not a new tiering system. What is his, unchanged:
the input for consensus mode (FantasyPros weekly ECR average ranks), the
model (`Mclust(df, G=k)` = 1-D Gaussian mixture, equal- vs varying-variance
chosen by BIC), the per-position (low, high, k) parameters in CHEN_PARAMS
(verbatim from main.R), and the empty-cluster renumbering. What differs:
`gmm_1d` is a plain-EM reimplementation (mclust seeds EM from hierarchical
clustering; we seed from quantiles), so boundary players can land one tier
off his published run — same procedure, not bit-identical. The projections
modes swap only the INPUT (league-scored points instead of FP ranks) while
keeping his machinery; `webtools.boris_chen_tiers` still serves his own
published CSVs untouched for comparison.

Two things Chen's published CSVs can't do, this can:

  * scoring he doesn't publish — the league's `scoring_label` picks the ECR
    variant automatically, and QB/K/DST still work (he publishes those unsuffixed);
  * tiers on the LEAGUE'S OWN projections (`source="projections"`), where
    scoring quirks live — clustered from `season_data.json`'s ros_ppg, which
    is recomputed on the league's actual scraped scoring rules.

Ownership is annotated from the league payload, so a tier chart reads as
"who on this tier can I actually get".
"""
from __future__ import annotations

import math
import re

from . import leaguetools, webtools

# Chen's weekly parameters, verbatim from main.R `draw.tiers(pos, low, high, k, …)`.
# (low, high, k) per scoring variant; QB/K/DST have one unsuffixed variant.
CHEN_PARAMS = {
    "QB":  {"*":        (1, 26, 8)},
    "K":   {"*":        (1, 20, 5)},
    "DST": {"*":        (1, 20, 6)},
    "RB":  {"standard": (1, 40, 9),  "ppr": (1, 40, 10), "half-ppr": (1, 40, 9)},
    "WR":  {"standard": (1, 60, 12), "ppr": (1, 60, 12), "half-ppr": (1, 60, 10)},
    "TE":  {"standard": (1, 24, 8),  "ppr": (1, 25, 8),  "half-ppr": (1, 25, 7)},
    "FLEX": {"standard": (20, 95, 14), "ppr": (20, 95, 14), "half-ppr": (20, 95, 15)},
}


# ── 1-D Gaussian mixture via EM (mclust G=k, models E and V, chosen by BIC) ──
def _em(values, k, equal_var):
    n = len(values)
    vs = sorted(values)
    # quantile init — deterministic, no RNG
    chunks = [vs[i * n // k:(i + 1) * n // k] or [vs[min(i * n // k, n - 1)]]
              for i in range(k)]
    mu = [sum(c) / len(c) for c in chunks]
    var = [max(sum((x - m) ** 2 for x in c) / len(c), 1e-3)
           for c, m in zip(chunks, mu)]
    if equal_var:
        var = [sum(var) / k] * k
    w = [len(c) / n for c in chunks]

    ll = None
    for _ in range(300):
        # E step
        resp, ll_new = [], 0.0
        for x in values:
            dens = [w[j] * math.exp(-((x - mu[j]) ** 2) / (2 * var[j]))
                    / math.sqrt(2 * math.pi * var[j]) for j in range(k)]
            s = sum(dens) or 1e-300
            ll_new += math.log(s)
            resp.append([d / s for d in dens])
        # M step
        for j in range(k):
            rj = sum(r[j] for r in resp)
            if rj < 1e-9:
                continue
            mu[j] = sum(r[j] * x for r, x in zip(resp, values)) / rj
            var[j] = max(sum(r[j] * (x - mu[j]) ** 2
                             for r, x in zip(resp, values)) / rj, 1e-3)
            w[j] = rj / n
        if equal_var:
            pooled = sum(wi * vi for wi, vi in zip(w, var))
            var = [max(pooled, 1e-3)] * k
        if ll is not None and abs(ll_new - ll) < 1e-8:
            ll = ll_new
            break
        ll = ll_new

    nparams = (k + (1 if equal_var else k) + (k - 1))
    bic = -2 * ll + nparams * math.log(n)
    assign = [max(range(k), key=lambda j: r[j]) for r in resp]
    return bic, assign, mu


def gmm_1d(values: list[float], k: int) -> list[int]:
    """Cluster 1-D values into k groups; returns tier number (1 = best) per
    value. Mirrors mclust: fit equal- and varying-variance, keep the lower BIC."""
    k = min(k, max(1, len(set(values))))
    best = min((_em(values, k, ev) for ev in (True, False)), key=lambda t: t[0])
    _, assign, mu = best
    # order clusters by mean; renumber 1..k, dropping empties (Chen does the same)
    order = sorted(set(assign), key=lambda j: mu[j])
    remap = {j: i + 1 for i, j in enumerate(order)}
    return [remap[a] for a in assign]


# ── ownership annotation from the league payload ─────────────────────────────
def _norm(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    return " ".join(t for t in s.split() if t not in ("jr", "sr", "ii", "iii", "iv"))


def _ownership(payload: dict) -> dict:
    my_id = payload.get("me", {}).get("team_id")
    teams = {t["team_id"]: t for t in payload.get("teams", [])}
    out = {}
    for p in (payload.get("players") or {}).values():
        owner = p.get("owner_team_id")
        if owner is None:
            tag = "FA"
        elif owner == my_id:
            tag = "MINE"
        else:
            t = teams.get(owner, {})
            tag = f"owned: {t.get('manager') or t.get('name') or owner}"
        out[_norm(p.get("name"))] = tag
    return out


def _scoring_for(payload: dict) -> str:
    label = (payload.get("league", {}).get("scoring_label") or "").lower()
    if "half" in label or "0.5" in label:
        return "half-ppr"
    if "ppr" in label:
        return "ppr"
    return "standard"


# ── the tool ─────────────────────────────────────────────────────────────────
def league_tiers_data(league: str | None = None, position: str = "RB",
                      source: str = "consensus", horizon: str | None = None) -> dict:
    """Structured tiers — used by league_tiers (text) and the UI's /api/tiers.

    Two axes: source ('consensus' = FantasyPros ranks, 'projections' = the
    league's own points) x horizon ('week' or 'ros'). Legacy source value
    'projections_week' still accepted. Defaults preserve old behavior:
    consensus->week, projections->ros."""
    if source == "projections_week":
        source, horizon = "projections", "week"
    if horizon is None:
        horizon = "week" if source == "consensus" else "ros"
    if horizon not in ("week", "ros"):
        raise ValueError("horizon must be 'week' or 'ros'")
    pos = position.upper().replace("D/ST", "DST").replace("FLX", "FLEX").strip()
    if pos not in CHEN_PARAMS:
        raise ValueError(f"position must be one of {sorted(CHEN_PARAMS)}")
    ctx = leaguetools._resolve(league)
    payload = leaguetools._payload(ctx)
    scoring = _scoring_for(payload)
    own = _ownership(payload)
    params = CHEN_PARAMS[pos].get("*") or CHEN_PARAMS[pos][scoring]
    low, high, k = params

    if source == "consensus":
        data = webtools.fp_ecr(pos, scoring, horizon)
        rows = [(p["player_name"], p.get("player_team_id") or "",
                 float(p["rank_ave"]), p.get("player_opponent") or "")
                for p in data["players"] if p.get("rank_ave") is not None]
        rows = rows[low - 1: high]
        values = [r[2] for r in rows]
        span = ("rest-of-season" if horizon == "ros"
                else f"week {data.get('week')}")
        view = ("a VALUE view for waivers and trades (note: far fewer experts "
                "submit ROS ranks than weekly)" if horizon == "ros"
                else "a THIS-WEEK start/sit view")
        head = (f"{pos} · {span} tiers · from FantasyPros expert consensus "
                f"ranks ({data.get('total_experts')} experts, {scoring} "
                f"scoring) · top {high}, asked for {k} tiers · {view}")
        unit = "avg rank"
    elif source == "projections":
        weekly = horizon == "week"
        wk = payload.get("week")

        def metric(p):
            if weekly:
                v = leaguetools._this_week(payload, p)
                return v if v is not None else 0.0
            return leaguetools._n(p.get("ros_ppg"))

        pool = [p for p in (payload.get("players") or {}).values()
                if p.get("pos") == pos or
                (pos == "FLEX" and p.get("pos") in ("RB", "WR", "TE"))]
        pool.sort(key=lambda p: -metric(p))
        pool = [p for p in pool if metric(p) > 0][low - 1: high]
        if len(pool) < k + 2:
            raise ValueError(f"only {len(pool)} {pos} players with projections "
                             f"in {ctx.key} — not enough to tier (k={k})")
        rows = [(p["name"], p.get("team") or "", round(metric(p), 1), "")
                for p in pool]
        # cluster on NEGATIVE points so tier 1 = best, matching rank direction
        values = [-r[2] for r in rows]
        horizon = (f"week {wk} projected points — a THIS-WEEK start/sit view"
                   if weekly else
                   "rest-of-season points per game — a VALUE view for trades "
                   "and waivers, not weekly lineups")
        head = (f"{pos} · tiers from {ctx.key}'s OWN projections (ESPN stat "
                f"lines scored with this league's actual rules) · {horizon} · "
                f"top {high}, asked for {k} tiers · data "
                f"{leaguetools._freshness(payload)}")
        unit = "ppg"
    else:
        raise ValueError("source must be 'consensus', 'projections_week', "
                         "or 'projections'")

    tiers_assigned = gmm_1d(values, k)
    grouped: dict[int, list[dict]] = {}
    for (name, team, val, opp), tier in zip(rows, tiers_assigned):
        grouped.setdefault(tier, []).append({
            "name": name, "team": team, "opp": opp,
            "value": round(abs(val) if source == "projections" else val, 1),
            "tag": own.get(_norm(name), ""),
        })
    return {"league": ctx.key, "position": pos, "source": source,
            "scoring": scoring, "unit": unit, "k": k, "range": [low, high],
            "head": head,
            "tiers": [{"tier": t, "players": grouped[t]}
                      for t in sorted(grouped)]}


def league_tiers(league: str | None = None, position: str = "RB",
                 source: str = "consensus", horizon: str | None = None) -> str:
    d = league_tiers_data(league, position, source, horizon)
    lines = [d["head"], ""]
    for grp in d["tiers"]:
        entries = []
        for p in grp["players"]:
            e = f"{p['name']} ({p['team']}" + (f", {p['opp']})" if p["opp"] else ")")
            e += f" {p['value']:g} {'ppg' if d['unit'] == 'ppg' else 'avg'}"
            if p["tag"]:
                e += f" [{p['tag']}]"
            entries.append(e)
        lines.append(f"Tier {grp['tier']}: " + "; ".join(entries))
    lines.append("\n[MINE] = on the user's roster, [FA] = free agent in this "
                 "league. Tier boundaries are where the model finds a real "
                 "gap; within a tier, differences are noise.")
    if d["source"] == "consensus":
        lines.append("Note: consensus ranks reflect generic scoring — for "
                     "league-specific quirks, compare source='projections'.")
    return "\n".join(lines)
