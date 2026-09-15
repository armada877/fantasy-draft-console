#!/usr/bin/env python3
"""Smoke test for the WS-2 research engine (scraping/sources/).

Three things this guards, none of which the live coverage run can prove:

1. **Envelope shape** — every adapter returns exactly the frozen WS-2 keys from
   docs/contracts.md, and every record carries `norm` from `extract_csg.norm_name`.
2. **Self-skip** — with the network killed for one source, that source returns
   `ok:false` + an error string, the OTHER sources are untouched, and nothing
   raises. A third-party outage must degrade the output, never fail a pipeline.
3. **Parameterisation** — two leagues with different scoring/size/QB-count really
   do resolve to different upstream URLs.

    python3 tests/sources_smoke.py            # offline-safe; uses cached envelopes
    python3 tests/sources_smoke.py --live     # also hit the network once per source
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import leagues                                   # noqa: E402
from scraping import sources                     # noqa: E402
from scraping.sources import common              # noqa: E402

ENVELOPE_KEYS = {"source", "fetched", "season", "week", "scoring", "ok", "error",
                 "coverage", "records"}
RECORD_KEYS = {"name", "norm", "pos", "team", "espn_id", "fields"}
FAILURES = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)
    return ok


def _ctxs():
    try:
        return leagues.all()
    except Exception:
        return []


# ── 1. envelope + record shape ───────────────────────────────────────────────
def test_shape(ctx, live):
    print(f"\n[1] envelope shape — {ctx.key}")
    for name in sources.ORDER:
        env = sources.fetch(name, ctx, week=1, refresh=live,
                            ttl=(0 if live else 10 ** 9))
        keys = set(env) - {"_cache"}
        check(f"{name}: envelope keys", keys == ENVELOPE_KEYS,
              f"extra {sorted(keys - ENVELOPE_KEYS)} missing {sorted(ENVELOPE_KEYS - keys)}")
        check(f"{name}: source field matches", env["source"] == name)
        check(f"{name}: season is the league's", env["season"] == ctx.season)
        if not env["ok"]:
            check(f"{name}: failure carries an error string",
                  isinstance(env["error"], str) and env["error"])
            continue
        bad = [r for r in env["records"] if set(r) != RECORD_KEYS]
        check(f"{name}: record keys", not bad, f"{len(bad)} malformed")
        wrong = [r for r in env["records"]
                 if r["norm"] != common.norm_name(r["name"])]
        check(f"{name}: norm is extract_csg.norm_name", not wrong,
              f"{len(wrong)} records disagree")
        cov = env["coverage"]
        check(f"{name}: coverage is reported",
              {"matched", "total", "pct_of_pool"} <= set(cov)
              and cov["matched"] <= cov["total"],
              f"{cov.get('matched')}/{cov.get('total')}")


# ── 2. self-skip when a source's host goes dark ──────────────────────────────
def test_self_skip(ctx):
    print(f"\n[2] a dead source degrades, never raises — {ctx.key}")
    victim, bystander = "fantasypros", "fantasycalc"
    real_get, real_json, real_csv = common.http_get, common.http_json, common.http_csv

    def dead(url, *a, **kw):
        if "fantasypros.com" in url:
            raise RuntimeError("simulated outage: Name or service not known")
        return real_get(url, *a, **kw)

    common.http_get = dead
    try:
        env = sources.fetch(victim, ctx, week=1, refresh=True, write=False)
        ok = sources.fetch(bystander, ctx, week=1, ttl=10 ** 9, write=False)
    except Exception as e:
        check("killing a source does not raise", False, f"{type(e).__name__}: {e}")
        common.http_get, common.http_json, common.http_csv = real_get, real_json, real_csv
        return
    finally:
        common.http_get, common.http_json, common.http_csv = real_get, real_json, real_csv

    check("dead source returns ok:false", env["ok"] is False)
    check("dead source names the failure", "simulated outage" in (env["error"] or ""),
          repr(env["error"])[:90])
    check("dead source still returns a well-formed envelope",
          set(env) - {"_cache"} == ENVELOPE_KEYS)
    check("dead source reports zero coverage, not a fake number",
          env["coverage"]["matched"] == 0)
    check("a neighbouring source is unaffected", ok["ok"] is True,
          ok.get("error") or "")
    check("the cached envelope on disk was not clobbered",
          os.path.exists(common.cache_file(ctx, victim)))


# ── 3. the profile really drives the upstream URLs ───────────────────────────
def test_parameterisation(ctxs):
    print("\n[3] scoring / size / QB-count change what is fetched")
    seen = {}
    for ctx in ctxs:
        v = sources.variants(ctx)
        if v:
            seen[ctx.key] = v
    if len(seen) < 2:
        check("at least two leagues to compare", False, f"{len(seen)} profiled")
        return
    fp = {k: v["fantasypros_ros"] for k, v in seen.items()}
    fc = {k: tuple(v["fantasycalc_params"].values()) for k, v in seen.items()}
    bc = {k: tuple(v["borischen"]) for k, v in seen.items()}
    for label, m in (("fantasypros URL", fp), ("fantasycalc params", fc),
                     ("borischen file set", bc)):
        check(f"{label} differs across leagues", len(set(m.values())) > 1,
              " | ".join(f"{k}={v}" for k, v in m.items())[:200])
    for k, v in seen.items():
        print(f"      {k:18s} {v['size']:>2}tm {v['scoring']:<9} "
              f"fc(numQbs={v['fantasycalc_params']['numQbs']},"
              f"numTeams={v['fantasycalc_params']['numTeams']},"
              f"ppr={v['fantasycalc_params']['ppr']})")


def main():
    live = "--live" in sys.argv
    ctxs = _ctxs()
    print(f"sources smoke test  ({len(ctxs)} league(s), {'live' if live else 'cached'})")
    if not ctxs:
        print("  SKIP — no leagues registered (run `python3 leagues.py`).")
        return 0
    test_shape(ctxs[0], live)
    test_self_skip(ctxs[0])
    test_parameterisation(ctxs)
    print(f"\n{'ALL PASS' if not FAILURES else f'{len(FAILURES)} FAILURE(S): ' + ', '.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
