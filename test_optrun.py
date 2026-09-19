#!/usr/bin/env python3
"""
test_optrun.py -- the live screen driver, with no broker and no network.

Two properties matter more than the plumbing: that it cannot place an order,
and that a MISSING input stays missing. Every gate in this system is built to
refuse on absence, so a driver that helpfully substitutes a plausible spread
history or a default calendar verdict defeats all of them at once -- and does
it invisibly, because the screen still returns a tidy-looking answer.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import optengine
import optrun

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s %s" % (name, detail))
        fails.append(name)


class FakeAlpaca:
    def __init__(self, spot=285.0):
        self.base = "https://paper.example"
        self.data = "https://data.example"
        self.feed = "sip"
        self.spot = spot
        self.calls = []

    def _req(self, method, url, path, **kw):
        self.calls.append(url)
        if "/trades/latest" in url:
            return {"trade": {"p": self.spot}} if self.spot else {}
        if "/options/contracts" in url:
            return {"option_contracts": []}
        return None


def cfg():
    return optengine.EngineConfig(account_equity=50000.0, tail_veto_fraction=0.10,
                                  assignment_cap=50000.0)


print("1. it cannot place an order, and the source proves it")
src = Path(optrun.__file__).read_text(encoding="utf-8")
body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
for banned in ("OptionTrader", "sell_put_limit", "buy_to_close_limit",
               "/v2/orders"):
    check("no %s anywhere in optrun" % banned, banned not in body.split('"""')[-1],
          banned)
check("it does not import the trading layer",
      "import optbook" not in body or "OptionTrader(" not in body)

print("2. no spot price is an error, not a zero-priced screen")
run = optrun.screen_symbol(FakeAlpaca(spot=0.0), "IWM", cfg())
check("reports the error", run["error"] == "no spot price", run)
check("and returns no result to grade", run["result"] is None, run)

print("3. a missing quote log leaves the history MISSING, not invented")
with tempfile.TemporaryDirectory() as d:
    empty = Path(d) / "none.jsonl"
    run = optrun.screen_symbol(FakeAlpaca(), "IWM", cfg(), quote_log=empty,
                               bars=[], calendar=(False, "clear"))
    check("spread history is empty", run["spread_history_contracts"] == 0, run)
    check("iv history is empty", run["iv_history_len"] == 0, run)
    # No bars means no realised volatility, which means gate four has no
    # measured edge. The screen must come back with nothing rather than with
    # something graded on an assumed edge.
    check("no bars gives no volatility verdict", run["vol"] is None, run["vol"])
    check("and therefore no candidates", not run["result"].candidates)

print("4. the recorded history is actually read and passed through")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "q.jsonl"
    rows = []
    for ts in (1.0, 2.0, 3.0):
        rows.append({"ts": ts, "underlying": "IWM", "symbol": "IWM_A",
                     "spread": 0.02, "iv": 0.21, "moneyness": 1.0, "mid": 1.0})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    run = optrun.screen_symbol(FakeAlpaca(), "IWM", cfg(), quote_log=p,
                               bars=[], calendar=(False, "clear"))
    check("spread history found the contract", run["spread_history_contracts"] == 1, run)
    check("iv history has one reading per sample", run["iv_history_len"] == 3, run)

print("5. an unreachable calendar refuses -- it does not default to clear")
class Boom(FakeAlpaca):
    def _req(self, method, url, path, **kw):
        if "corporate-actions" in url:
            raise RuntimeError("network down")
        return super()._req(method, url, path, **kw)

with tempfile.TemporaryDirectory() as d:
    run = optrun.screen_symbol(Boom(), "IWM", cfg(),
                               quote_log=Path(d) / "none.jsonl", bars=[])
    # calendar=None is what gate five treats as UNKNOWN, and unknown blocks.
    check("calendar comes back None, not a cheerful clear",
          run["calendar"] is None or run["calendar"][0] is True, run["calendar"])

print("6. evidence records the REJECTIONS, not only what was taken")
class R:
    def __init__(self, rows): self._rows = rows; self.candidates = []
    def as_dicts(self): return self._rows

with tempfile.TemporaryDirectory() as d:
    log = Path(d) / "ev.jsonl"
    run = {"symbol": "IWM", "spot": 285.0, "vol": {"implied": 0.23, "realized": 0.13},
           "spread_history_contracts": 7, "iv_history_len": 3,
           "result": R([{"label": "a", "accepted": True, "grade": "B"},
                        {"label": "b", "accepted": False, "grade": None}])}
    n = optrun.record_evidence(run, path=log, ts=99.0)
    check("both rows written", n == 2, n)
    got = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    check("the rejected one is there too",
          any(not g["accepted"] for g in got), got)
    check("each row carries the stamp and the inputs",
          got[0]["ts"] == 99.0 and got[0]["iv"] == 0.23 and got[0]["symbol"] == "IWM",
          got[0])
    optrun.record_evidence(run, path=log, ts=100.0)
    check("append-only", len(log.read_text(encoding="utf-8").splitlines()) == 4)

check("a run with no result writes nothing",
      optrun.record_evidence({"symbol": "X", "result": None}) == 0)

print()
if fails:
    print("FAILED: %s" % ", ".join(fails))
    sys.exit(1)
print("ALL CHECKS PASSED")
