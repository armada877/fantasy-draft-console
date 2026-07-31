#!/usr/bin/env python3
"""EVAL harness for the 2KDOME draft advisor (/api/advise).

Runs a deterministic, seeded mock auction driven by the managers' calibrated
tendencies, then probes the live LLM advisor at fixed checkpoints with a
structured question and grades its answers for grounding, evolution, and
strategy alignment. Pure stdlib.

Usage:  python3 eval_advisor.py
Writes: eval_report.md  (next to this file)
"""
import json
import os
import re
import time
import copy
import random
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "static", "data.json")
API = "http://127.0.0.1:8000/api/advise"
MODEL = "claude-haiku-4-5"
CHECKPOINTS = [0, 12, 30, 60, 90]
SEED = 42

QUESTION = (
    "Given the CURRENT board only, answer in EXACTLY this format on one line: "
    "TARGET: <player> | MAX: $<number> | OUT: <team> | WHY: <8 words>. "
    "TARGET = the single best available player I should pursue next; "
    "MAX = my max bid; OUT = one team that can no longer meaningfully compete "
    "(low budget/needs filled)."
)

# ------------------------------------------------------------------ mock auction
DATA = json.load(open(DATA_PATH))
STARTERS = DATA["starters"]
FLEX = DATA["flex"]
BENCH = DATA["bench"]
BUDGET = DATA["budget"]
ME = DATA.get("me") or DATA["managers"][0]["name"]  # the team we probe the advisor for


def new_players():
    return [dict(p, id=i, drafted=None, price=0, available=True)
            for i, p in enumerate(DATA["players"])]


def new_teams():
    t = {}
    for m in DATA["managers"]:
        t[m["name"]] = {
            "name": m["name"], "mult": m["mult"], "conc": m["conc"],
            "maxbuy": m["maxbuy"], "budget": BUDGET, "need": dict(STARTERS),
            "flex": FLEX, "bench": BENCH, "roster": [],  # roster = list of (name,pos,price)
        }
    return t


def slot_for(team, pos):
    if team["need"].get(pos, 0) > 0:
        return "start"
    if pos in ("RB", "WR", "TE") and team["flex"] > 0:
        return "flex"
    if team["bench"] > 0:
        return "bench"
    return None


def open_slots(team):
    return sum(team["need"].values()) + team["flex"] + team["bench"]


def inflation(players, log):
    paid = worth = 0.0
    for l in log:
        p = players[l["id"]]
        paid += l["price"]
        worth += max(p["worth"], 1)
    if worth < 40:
        return 1.0
    return min(1.5, max(0.6, paid / worth))


def team_bid(team, p, infl, rng):
    slot = slot_for(team, p["pos"])
    if slot is None or team["budget"] < 1:
        return 0.0
    base = max(0.5, p["worth"])
    bid = base * team["mult"].get(p["pos"], 1) * infl
    if team["conc"] > 72:
        if base >= 25:
            bid *= 1.12
        elif base >= 8:
            bid *= 0.82
    if slot == "bench":
        bid *= 0.35
    bid = min(bid, team["budget"] - (open_slots(team) - 1), team["maxbuy"])
    bid *= rng.uniform(0.9, 1.12)  # small seeded noise
    return max(0.0, bid)


def predict_price(teams, p, infl, rng):
    """Second-highest team_bid across all 12 + 1 (min 1)."""
    bids = sorted((team_bid(t, p, infl, rng) for t in teams.values()), reverse=True)
    bids = [b for b in bids if b >= 1]
    if not bids:
        return 1
    top = bids[0]
    second = bids[1] if len(bids) > 1 else 1
    return max(1, min(round(top), round(second) + 1))


def assign(team, p, price):
    slot = slot_for(team, p["pos"])
    if slot == "start":
        team["need"][p["pos"]] -= 1
    elif slot == "flex":
        team["flex"] -= 1
    else:
        team["bench"] -= 1
    team["budget"] -= price
    team["roster"].append({"name": p["name"], "pos": p["pos"], "price": price})


def snapshot(teams, players, infl, picks):
    return {
        "picks": picks,
        "infl": round(infl, 2),
        "teams": copy.deepcopy(teams),
        "avail": [p["id"] for p in players if p["available"] and p["drafted"] is None],
    }


def run_auction():
    rng = random.Random(SEED)
    players = new_players()
    teams = new_teams()
    log = []
    snaps = {}
    order = list(teams.keys())
    nom_i = 0
    need = set(CHECKPOINTS)

    def avail_players():
        return [p for p in players if p["available"] and p["drafted"] is None]

    guard = 0
    while len(log) < 96 and avail_players() and guard < 5000:
        guard += 1
        picks = len(log)
        if picks in need:
            snaps[picks] = snapshot(teams, players, inflation(players, log), picks)
            need.discard(picks)

        # rotate nominators until one has an open slot and something to nominate
        nominator = None
        for _ in range(len(order)):
            cand = teams[order[nom_i % len(order)]]
            nom_i += 1
            if open_slots(cand) > 0:
                nominator = cand
                break
        if nominator is None:
            break

        # nominator picks the available player maximizing worth*mult for a pos they can slot
        pool = avail_players()
        best, best_score = None, -1e9
        for p in pool:
            if slot_for(nominator, p["pos"]) is None:
                continue
            score = p["worth"] * nominator["mult"].get(p["pos"], 1)
            if score > best_score:
                best, best_score = p, score
        if best is None:  # nominator can't slot anything; fall back to top worth overall
            best = max(pool, key=lambda p: p["worth"])

        infl = inflation(players, log)
        bids = [(name, team_bid(t, best, infl, rng)) for name, t in teams.items()]
        bids = [(n, b) for n, b in bids if b >= 1]
        if not bids:
            best["available"] = False
            continue
        bids.sort(key=lambda x: x[1], reverse=True)
        winner_name, top = bids[0]
        second = bids[1][1] if len(bids) > 1 else 1
        price = max(1, min(round(top), round(second) + 1))
        winner = teams[winner_name]
        assign(winner, best, price)
        best["drafted"] = winner_name
        best["price"] = price
        log.append({"id": best["id"], "name": best["name"], "winner": winner_name, "price": price})

    # capture any remaining checkpoints (e.g. auction ended before 90)
    for cp in sorted(need):
        if len(log) >= cp or not avail_players():
            snaps[cp] = snapshot(teams, players, inflation(players, log), len(log))
    return players, snaps, log


# ------------------------------------------------------------------ state builder
def build_state(snap, players):
    teams = snap["teams"]
    infl = snap["infl"]
    rng = random.Random(SEED + 1000 + snap["picks"])  # isolated RNG for est_price
    team_state = {}
    for name, t in teams.items():
        needs = []
        for k, v in t["need"].items():
            needs.extend([k] * v)
        needs.extend(["FLEX"] * t["flex"])
        team_state[name] = {"budget": t["budget"], "needs": needs, "roster": t["roster"]}
    avail = [players[i] for i in snap["avail"]]
    best_avail = {}
    for pos in ("RB", "WR", "TE", "QB"):
        ps = sorted([p for p in avail if p["pos"] == pos], key=lambda p: -p["worth"])[:8]
        best_avail[pos] = [
            {"name": p["name"], "tier": p["tier"], "worth": p["worth"],
             "est_price": predict_price(teams, p, infl, rng)} for p in ps]
    return {
        "me": ME, "my_budget": teams[ME]["budget"], "inflation": infl,
        "picks_made": snap["picks"], "on_the_block": None,
        "teams": team_state, "best_available": best_avail,
    }


# ------------------------------------------------------------------ advisor call
def ask(state):
    body = json.dumps({"question": QUESTION, "state": state, "model": MODEL}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=90) as r:
        out = json.loads(r.read())
    return out, time.time() - t0


# ------------------------------------------------------------------ parsing / checks
def _norm(s):
    s = s.lower()
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    s = re.sub(r"[^a-z0-9 ]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse(answer):
    def grab(pat):
        m = re.search(pat, answer, re.I)
        return m.group(1).strip() if m else None
    target = grab(r"TARGET:\s*(.+?)\s*(?:\||$)")
    max_s = grab(r"MAX:\s*\$?\s*(\d+(?:\.\d+)?)")
    out = grab(r"OUT:\s*(.+?)\s*(?:\||$)")
    why = grab(r"WHY:\s*(.+?)\s*$")
    return {
        "target": target,
        "max": float(max_s) if max_s else None,
        "out": out,
        "why": why,
    }


def match_player(name, candidates):
    """Return the candidate player-name best matching `name`, or None."""
    if not name:
        return None
    n = _norm(name)
    if not n:
        return None
    ntok = set(n.split())
    for c in candidates:
        cn = _norm(c)
        if cn == n:
            return c
    for c in candidates:
        cn = _norm(c)
        ctok = set(cn.split())
        if cn in n or n in cn or len(ntok & ctok) >= 2:
            return c
    # last-name only fallback
    for c in candidates:
        ctok = set(_norm(c).split())
        if ntok and ntok <= ctok:
            return c
    return None


def match_team(name, team_names):
    if not name:
        return None
    n = _norm(name)
    for t in team_names:
        if _norm(t) == n:
            return t
    ntok = set(n.split())
    for t in team_names:
        ttok = set(_norm(t).split())
        if ntok & ttok:
            return t
    return None


def run_checks(state, parsed):
    teams = state["teams"]
    all_rostered = {r["name"] for t in teams.values() for r in t["roster"]}
    avail_names = [p["name"] for pos in state["best_available"].values() for p in pos]
    # broaden availability universe: any player not on a roster is "available"
    all_names = [p["name"] for p in DATA["players"]]
    checks = {}

    # 1 grounding-target: named player is available (not on any roster)
    tp = match_player(parsed["target"], all_names)
    if tp is None:
        checks["target"] = ("FAIL", f"'{parsed['target']}' matches no player")
    elif tp in all_rostered:
        checks["target"] = ("FAIL", f"{tp} is already drafted")
    else:
        tag = " (in best_available)" if match_player(parsed["target"], avail_names) else ""
        checks["target"] = ("PASS", f"{tp} available{tag}")

    # 2 grounding-budget: MAX <= my_budget
    mb = state["my_budget"]
    if parsed["max"] is None:
        checks["budget"] = ("FAIL", "no MAX parsed")
    elif parsed["max"] <= mb:
        checks["budget"] = ("PASS", f"${parsed['max']:.0f} <= ${mb}")
    else:
        checks["budget"] = ("FAIL", f"${parsed['max']:.0f} > my_budget ${mb}")

    # 3 grounding-out: team exists and is genuinely constrained
    ot = match_team(parsed["out"], list(teams.keys()))
    if ot is None:
        checks["out"] = ("FAIL", f"'{parsed['out']}' matches no team")
    else:
        budgets = sorted(t["budget"] for t in teams.values())
        third = budgets[max(0, len(budgets) // 3 - 1)]  # lower-third threshold
        b = teams[ot]["budget"]
        needs_empty = len(teams[ot]["needs"]) == 0
        low = b <= third
        if low or needs_empty:
            why = []
            if low:
                why.append(f"budget ${b} in lower third (<=${third})")
            if needs_empty:
                why.append("needs filled")
            checks["out"] = ("PASS", f"{ot}: " + "; ".join(why))
        else:
            checks["out"] = ("FAIL", f"{ot} budget ${b} not low & needs {teams[ot]['needs']}")
    return checks, tp, ot


# ------------------------------------------------------------------ main
def main():
    print("Running mock auction (seed=%d)..." % SEED)
    players, snaps, log = run_auction()
    print("Auction produced %d picks; checkpoints captured: %s"
          % (len(log), sorted(snaps.keys())))

    results = []
    for cp in CHECKPOINTS:
        snap = snaps.get(cp)
        if snap is None:
            continue
        state = build_state(snap, players)
        try:
            out, latency = ask(state)
        except Exception as e:
            out, latency = {"answer": f"[ERROR {e}]", "model": MODEL, "truncated": False}, 0.0
        parsed = parse(out.get("answer", ""))
        checks, tp, ot = run_checks(state, parsed)
        # ground truth
        avail_all = [p for pos in state["best_available"].values() for p in pos]
        top3 = sorted(avail_all, key=lambda p: -p["worth"])[:3]
        poorest = min(state["teams"].items(), key=lambda kv: kv[1]["budget"])
        results.append({
            "cp": cp, "state": state, "answer": out.get("answer", ""),
            "truncated": out.get("truncated"), "model": out.get("model"),
            "latency": latency, "parsed": parsed, "checks": checks,
            "top3": top3, "poorest": poorest,
        })
        print(f"  cp={cp}: {latency:.2f}s  target={parsed['target']}  "
              f"checks={{{', '.join(k+':'+v[0] for k,v in checks.items())}}}")

    # EVOLUTION check (overall)
    answers = [r["answer"] for r in results]
    targets = [(_norm(r["parsed"]["target"] or "")) for r in results]
    evo_pass = len(set(answers)) > 1 and len(set(targets)) > 1
    evo = ("PASS" if evo_pass else "FAIL",
           f"{len(set(targets))} distinct targets across {len(results)} checkpoints")

    write_report(results, evo, log)
    print_summary(results, evo)


def strategy_note(r):
    cp = r["cp"]
    p = r["parsed"]
    tp = match_player(p["target"], [x["name"] for x in DATA["players"]])
    pl = next((x for x in DATA["players"] if x["name"] == tp), None)
    notes = []
    if pl:
        if cp < 20:
            notes.append("favors elite RB" if pl["pos"] == "RB" and pl["worth"] >= 40
                         else f"early pick is {pl['pos']} worth ${pl['worth']:.0f}")
        if pl["pos"] == "WR" and 8 <= pl["worth"] <= 24:
            notes.append("WARN recommends mid-tier WR ($8-24 trap)")
        if pl["pos"] in ("QB", "TE") and pl["worth"] >= 9:
            notes.append(f"surfaces value {pl['pos']}")
    return "; ".join(notes) or "-"


def write_report(results, evo, log):
    L = []
    L.append("# Draft Advisor EVAL Report\n")
    L.append(f"- Model: `{MODEL}`  |  Endpoint: `{API}`  |  Seed: {SEED}")
    L.append(f"- Mock auction picks simulated: {len(log)}")
    L.append(f"- Checkpoints: {', '.join(str(r['cp']) for r in results)}\n")

    for r in results:
        s = r["state"]
        L.append(f"## Checkpoint — picks_made = {r['cp']}\n")
        L.append(f"**Ground truth:** my_budget = **${s['my_budget']}**, "
                 f"inflation = {s['inflation']}")
        t3 = ", ".join(f"{p['name']} (${p['worth']:.0f} {p['tier']})" for p in r["top3"])
        L.append(f"- Top-3 available by worth: {t3}")
        pn, pt = r["poorest"]
        L.append(f"- Poorest team: **{pn}** (${pt['budget']}, needs {pt['needs'] or 'none'})\n")
        L.append(f"**Advisor raw answer** ({r['latency']:.2f}s, truncated={r['truncated']}):")
        L.append(f"\n> {r['answer']}\n")
        L.append(f"Parsed: TARGET=`{r['parsed']['target']}` MAX=`{r['parsed']['max']}` "
                 f"OUT=`{r['parsed']['out']}`\n")
        L.append("| Check | Result | Detail |")
        L.append("|---|---|---|")
        L.append(f"| GROUNDING-target | {r['checks']['target'][0]} | {r['checks']['target'][1]} |")
        L.append(f"| GROUNDING-budget | {r['checks']['budget'][0]} | {r['checks']['budget'][1]} |")
        L.append(f"| GROUNDING-out | {r['checks']['out'][0]} | {r['checks']['out'][1]} |")
        L.append(f"\n_Strategy note:_ {strategy_note(r)}\n")

    # pass-rate table
    cats = ["target", "budget", "out"]
    L.append("## Overall pass-rate\n")
    L.append("| Check | Passed / Total | Rate |")
    L.append("|---|---|---|")
    for c in cats:
        passed = sum(1 for r in results if r["checks"][c][0] == "PASS")
        L.append(f"| GROUNDING-{c} | {passed}/{len(results)} | "
                 f"{100*passed/len(results):.0f}% |")
    L.append(f"| EVOLUTION (overall) | {'1/1' if evo[0]=='PASS' else '0/1'} | {evo[0]} |")
    L.append(f"\nEVOLUTION detail: {evo[1]}")
    avg_lat = sum(r["latency"] for r in results) / len(results)
    L.append(f"\nAvg latency: {avg_lat:.2f}s  |  any truncated: "
             f"{any(r['truncated'] for r in results)}")

    path = os.path.join(HERE, "eval_report.md")
    open(path, "w").write("\n".join(L) + "\n")
    print("Report written:", path)


def print_summary(results, evo):
    print("\n===== SUMMARY =====")
    for c in ["target", "budget", "out"]:
        passed = sum(1 for r in results if r["checks"][c][0] == "PASS")
        print(f"GROUNDING-{c}: {passed}/{len(results)}")
    print(f"EVOLUTION: {evo[0]} ({evo[1]})")


if __name__ == "__main__":
    main()
