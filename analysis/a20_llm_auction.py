#!/usr/bin/env python3
"""Analysis 20: hybrid auction — LLM agents decide, calibrated agents bid.

Why hybrid. a18 already simulates this league's auction with each manager as a bidding agent
fitted to their own history, and for PRICE that model is hard to beat: the multipliers come
from twelve years of the bids these people actually made, so an LLM roleplaying a profile is a
lossy copy of something already exact. What the arithmetic cannot do is CHOOSE. a18's nominator
simply puts up the best player left, which is not what anyone does — nominations are used to
drain a rival, to bait the manager who overpays at a position, to force a decision while
someone is short.

So the split is: an LLM makes the decisions that are strategy, and the calibrated model makes
the ones that are arithmetic.

    nomination        LLM   (~14 per manager)
    RFA retain/decline LLM  (one per manager)
    every bid         calibrated multipliers, exactly as a18

That is ~170 calls for a whole draft instead of the ~2,000 a fully-LLM auction would need,
because bidding is where the call count explodes and where the LLM adds least.

The RFA round is modelled properly: each manager's first nomination must come from their own
prior roster and the incumbent may retain at whatever the field bids up to — so a nomination
there is a genuine dilemma (put up the player you want and risk paying market, or put up
someone you are happy to lose and hope a rival overpays).

    python3 analysis/a20_llm_auction.py --sims 3
    python3 analysis/a20_llm_auction.py --sims 1 --verbose

Needs ANTHROPIC_API_KEY (config/.env is read automatically). Without it every decision falls
back to the a18 rule and the run is labelled as such, so the harness still works offline.
"""
import argparse
import json
import os
import random
import re
import statistics
import sys
from collections import defaultdict

import a18_agent_auction as a18
import lib

_n = a18.norm

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
POS = a18.POS


def load_dotenv():
    path = os.path.join(ROOT, "config", ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def console_to_canon():
    """{console/display name: canonical name} for the current league.

    tool_data keys managers the way the console names them; build_agents keys them by
    canonical identity. Both resolve from the same owner id, so bridge through it — without
    this the keeper seed silently matches nobody and every sim drafts a board on which no
    keeper has been taken.
    """
    cfg_p = os.path.join(ROOT, "config", "league.json")
    season = 2026
    if os.path.exists(cfg_p):
        with open(cfg_p, encoding="utf-8") as f:
            season = int(json.load(f).get("season", 2026))
    raw = os.path.join(ROOT, "scraping", "raw", str(season), "league_full.json")
    out = {}
    if os.path.exists(raw):
        with open(raw) as f:
            d = json.load(f)
        d = (d[0] if d else {}) if isinstance(d, list) else d
        for mem in (d.get("members") or []):
            disp = (mem.get("displayName") or "").strip()
            canon = lib.MANAGER_CANON.get(mem.get("id"))
            if disp and canon:
                out[disp] = canon
    return out


def tool_data():
    p = os.path.join(ROOT, "draft_sheets", "tool_data.json")
    if not os.path.exists(p):
        sys.exit("draft_sheets/tool_data.json missing — run `python3 pipeline.py build` first.")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def describe(agent):
    """The agent's own tendencies, in words it can act on."""
    m = agent["mult"]
    lean = []
    for p in POS:
        v = m.get(p, 1.0)
        if v >= 1.25:
            lean.append("you pay well over the odds for %s" % p)
        elif v <= 0.6:
            lean.append("you refuse to pay up for %s" % p)
    shape = ("you concentrate your budget into two or three players"
             if agent.get("conc", 50) > 72 else "you spread your budget around")
    return "%s; %s; your realistic ceiling on any one player is about $%d." % (
        "; ".join(lean) or "you pay roughly market at every position", shape, agent.get("maxbuy", 100))


class Brain:
    """LLM decisions, with the a18 rule as the fallback on any failure."""

    def __init__(self, model, verbose=False):
        self.model = model
        self.verbose = verbose
        self.calls = 0
        self.fallbacks = 0
        self.client = None
        if os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic
                self.client = anthropic.Anthropic()
            except Exception as e:              # noqa: BLE001 — offline is a supported mode
                print("  ! anthropic client unavailable (%s) — rule-based fallback" % e)

    def _ask(self, prompt, max_tokens=160):
        if not self.client:
            return None
        try:
            self.calls += 1
            r = self.client.messages.create(
                model=self.model, max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}])
            return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        except Exception as e:                  # noqa: BLE001 — never let a draft die on an API blip
            if self.verbose:
                print("  ! api error: %s" % e)
            return None

    def nominate(self, who, agent, budget, roster, options, rivals, phase, mine=None):
        """Pick one player to put up. `options` is [(name,pos,worth,est_price)]."""
        if not options:
            return None, "no options"
        mine = mine or set()
        board = "\n".join("  %-24s %-3s worth $%-3d likely sells ~$%d%s"
                          % (n, p, w, e, "   <- YOURS last season" if _n(n) in mine else "")
                          for n, p, w, e in options[:14])
        need = ", ".join("%s x%d" % (k, v) for k, v in roster["need"].items() if v) or "starters full"
        rule = ("RESTRICTED round. If — and only if — you nominate a player who was on YOUR "
                "roster last season (marked YOURS) may you take him at the winning bid, no "
                "matter who wins it. Nominating anyone else is an ordinary lot you can lose. "
                "So the choice is: put up a player you want and expect to pay the market for "
                "him, or put up one you are content to lose and let a rival overpay."
                if phase == "rfa" else
                "Open auction: you have no special claim on whoever you nominate.")
        prompt = (
            "You are %s in a 12-team fantasy football auction. %s\n"
            "Budget $%d. Still to fill: %s (+%d flex, +%d bench).\n"
            "%s\n\nAvailable:\n%s\n\nRival budgets: %s\n\n"
            "Nominate ONE player. You may nominate someone you do NOT want, to drain a rival's "
            "money or bait a manager who overpays at that position.\n"
            'Reply as JSON only: {"player": "<exact name from the list>", "why": "<8 words max>"}'
            % (who, describe(agent), budget, need, roster["flex"], roster["bench"], rule, board,
               ", ".join("%s $%d" % (m, b) for m, b in rivals[:6])))
        txt = self._ask(prompt)
        pick = self._match(txt, [o[0] for o in options])
        if pick is None:
            self.fallbacks += 1
            return options[0][0], "rule: best available"
        why = ""
        m = re.search(r'"why"\s*:\s*"([^"]{0,60})"', txt or "")
        if m:
            why = m.group(1)
        return pick, why

    def retain(self, who, agent, budget, roster, player, price, worth):
        """RFA: match the field's price, or let him go?"""
        prompt = (
            "You are %s in a 12-team fantasy football auction, in the restricted round. %s\n"
            "Budget $%d, still to fill %s.\n"
            "%s (%s) is projected to be worth $%d and the field has bid him to $%d. "
            "You may keep him by paying $%d, or let him go and spend elsewhere.\n"
            'Reply as JSON only: {"keep": true|false, "why": "<8 words max>"}'
            % (who, describe(agent), budget,
               ", ".join("%s x%d" % (k, v) for k, v in roster["need"].items() if v) or "bench only",
               player, "", worth, price, price))
        txt = self._ask(prompt, max_tokens=80)
        if txt is None:
            self.fallbacks += 1
            return price <= worth * agent["mult"].get("RB", 1.0)   # the a18 rule
        return bool(re.search(r'"keep"\s*:\s*true', txt))

    @staticmethod
    def _match(txt, names):
        if not txt:
            return None
        m = re.search(r'"player"\s*:\s*"([^"]+)"', txt)
        want = (m.group(1) if m else txt).strip().lower()
        for n in names:
            if n.lower() == want:
                return n
        for n in names:                          # tolerate "Gibbs" for "Jahmyr Gibbs"
            if n.lower() in want or want in n.lower():
                return n
        return None


def run(agents, brain, keepers, seed, verbose=False, prior=None):
    """One auction. Returns {manager: {budget, roster}}."""
    rng = random.Random(seed)
    teams = {m: {"budget": a18.BUDGET, "roster": a18.new_roster()} for m in agents}
    pool = [p for p in a18.PROJ["2026"] if p["pos"] in POS and (p["proj_value"] or 0) > -3]
    remaining = sorted(pool, key=lambda p: -(p["proj_value"] or 0))

    # keepers are locked before anyone nominates
    byname = {a18.norm(p["name"]): p for p in remaining}
    for mgr, (name, price) in (keepers or {}).items():
        p = byname.get(a18.norm(name))
        if p and mgr in teams:
            remaining.remove(p)
            teams[mgr]["budget"] -= price
            a18.assign(teams[mgr]["roster"], p, price)

    order = list(agents.keys())
    rng.shuffle(order)
    turn = 0
    while remaining:
        live = [m for m in agents
                if sum(teams[m]["roster"]["need"].values()) + teams[m]["roster"]["flex"]
                + teams[m]["roster"]["bench"] > 0]
        if not live:
            break
        nominator = order[turn % len(order)]
        turn += 1
        if nominator not in live:
            continue
        phase = "rfa" if turn <= len(order) else "open"
        mine = (prior or {}).get(nominator) or set()

        # once nothing of value is left, stop paying for judgement — take the rule
        best = remaining[0]["proj_value"] or 0
        opts = [(p["name"], p["pos"], int(p["proj_value"] or 0),
                 int(round(a18.max_bid(agents[nominator], p, teams[nominator]["budget"],
                                       teams[nominator]["roster"], rng))))
                for p in remaining[:14]]
        if best >= 5:
            rivals = sorted(((m, teams[m]["budget"]) for m in agents if m != nominator),
                            key=lambda x: -x[1])
            pick, why = brain.nominate(nominator, agents[nominator], teams[nominator]["budget"],
                                       teams[nominator]["roster"], opts, rivals, phase,
                                       mine=mine)
        else:
            pick, why = remaining[0]["name"], "rule: scraps"
        player = next((p for p in remaining if p["name"] == pick), remaining[0])

        bids = []
        for m in agents:
            r = teams[m]["roster"]
            if sum(r["need"].values()) + r["flex"] + r["bench"] <= 0:
                continue
            mb = a18.max_bid(agents[m], player, teams[m]["budget"], r, rng)
            if mb >= 1:
                bids.append((mb, m))
        if not bids:
            remaining.remove(player)
            continue
        bids.sort(reverse=True)
        winner_mb, winner = bids[0]
        second = bids[1][0] if len(bids) > 1 else 1
        price = max(1, min(int(round(second)) + 1, int(winner_mb)))

        # The retain right belongs to the PLAYER, not to nominating first: only a nominee who
        # was on the nominator's own prior roster can be matched. Granting it on any round-one
        # nomination let a manager claim someone else's player off the board.
        if phase == "rfa" and winner != nominator and a18.norm(player["name"]) in mine:
            r = teams[nominator]["roster"]
            afford = teams[nominator]["budget"] - (sum(r["need"].values()) + r["flex"] + r["bench"] - 1)
            if afford >= price and brain.retain(nominator, agents[nominator],
                                                teams[nominator]["budget"], r, player["name"],
                                                price, int(player["proj_value"] or 0)):
                winner = nominator

        r = teams[winner]["roster"]
        price = max(1, min(price, teams[winner]["budget"]
                           - (sum(r["need"].values()) + r["flex"] + r["bench"] - 1)))
        teams[winner]["budget"] -= price
        a18.assign(r, player, price)
        remaining.remove(player)
        if verbose:
            print("   %-14s nominates %-22s -> %-14s $%-3d  [%s]"
                  % (nominator[:14], player["name"][:22], winner[:14], price, why))
    return teams


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sims", type=int, default=3)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    load_dotenv()
    td = tool_data()
    opp = a18.build_agents()
    me = a18._me_canonical(opp)
    field = [m for m in sorted(opp) if m != me][:11]
    agents = {m: opp[m] for m in field}
    agents[me] = opp[me]

    # start from the keepers the console has, so the sim drafts the board you actually face
    keepers, alias = {}, console_to_canon()
    for mgr, rows in (td.get("keeper_pool") or {}).items():
        said = (td.get("announced_keepers") or {}).get(mgr)
        if not said:
            continue
        hit = next((r for r in rows if r["name"] == said), None)
        canon = alias.get(mgr, mgr)
        if hit and canon in agents:
            keepers[canon] = (said, hit["cost"])

    # each manager's prior roster — the only players they may claim a retain right over
    prior = {}
    for mgr, rows in (td.get("keeper_pool") or {}).items():
        canon = alias.get(mgr, mgr)
        if canon in agents:
            prior[canon] = {a18.norm(r["name"]) for r in rows}

    brain = Brain(args.model, args.verbose)
    mode = "LLM (%s)" % args.model if brain.client else "RULE-BASED (no ANTHROPIC_API_KEY)"
    print("=" * 78)
    print("ANALYSIS 20 — hybrid auction: %s decides nominations, calibrated agents bid" % mode)
    print("=" * 78)
    print("keepers seeded: %d   sims: %d\n" % (len(keepers), args.sims))

    vbds, spends = [], []
    for s in range(args.sims):
        teams = run(agents, brain, keepers, 900 + s, args.verbose, prior=prior)
        vbd, slots = a18.starter_vbd(teams[me]["roster"])
        vbds.append(vbd)
        # roster entries are the projection dict plus `pay`/`slot`
        spends.append(sorted(((x["name"], x["pay"]) for x in teams[me]["roster"]["players"]),
                             key=lambda x: -x[1])[:6])
        print("sim %d: your starter VBD %.0f  |  %s" % (
            s + 1, vbd, ", ".join("%s $%d" % (n.split()[-1], pr) for n, pr in spends[-1])))

    print("\nmedian starter VBD %.0f  (range %.0f-%.0f)"
          % (statistics.median(vbds), min(vbds), max(vbds)))
    print("LLM calls %d, rule fallbacks %d" % (brain.calls, brain.fallbacks))


if __name__ == "__main__":
    main()
