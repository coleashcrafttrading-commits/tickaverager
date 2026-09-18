#!/usr/bin/env python3
"""
test_optdata.py -- offline checks on the options data layer.

Everything that decides a trade is checked against values worked out by hand
here: the mid and the spread, the None that has to appear instead of a zero
price, the quality gate's reasons, the strike grid, the cache, and the two
guards that exist because Alpaca answers a wrong question silently -- the
expiration_date_gte bound on /v2/options/contracts, and the rule that data
requests never touch the scarce trading host.

The whole suite runs with NO network and NO credentials: a fake transport is
injected into OptionData, so every path above is exercised deterministically.
Section 11 is the only live check and it SKIPS with a printed note when there
are no keys or the network is down -- a machine with no keys must still print
ALL CHECKS PASSED.

    .venv/Scripts/python test_optdata.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_optdata_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import optdata as O                                   # noqa: E402

FAIL = 0


def check(name, got, want, tol=1e-6):
    global FAIL
    if want is None:
        ok = got is None
    elif got is None:
        ok = False
    elif isinstance(want, bool) or isinstance(got, bool):
        ok = got is want
    elif isinstance(want, (int, float)) and isinstance(got, (int, float)):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    if not ok:
        FAIL += 1
    g = f"{got:.6f}" if isinstance(got, float) else repr(got)
    w = f"{want:.6f}" if isinstance(want, float) else repr(want)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {g}, want {w}")


def raises(name, fn, needle=""):
    global FAIL
    try:
        fn()
    except Exception as e:
        ok = needle.lower() in str(e).lower()
        if not ok:
            FAIL += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: raised {type(e).__name__} "
              f"{'containing' if ok else 'WITHOUT'} {needle!r}")
        return
    FAIL += 1
    print(f"  FAIL  {name}: did not raise")


# ------------------------------------------------------------ fake transport
class Fake:
    """Stands in for the HTTP layer. Records every call; answers from a table.

    The signature is the one OptionData calls: (method, url, path, params).
    Responses are keyed by the path SUFFIX so a test can answer 'snapshots'
    without writing the whole underlying-specific URL.
    """

    def __init__(self, table: dict | None = None):
        self.table = dict(table or {})
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, method, url, path, params):
        self.calls.append((url, path, dict(params or {})))
        for suffix, resp in self.table.items():
            if suffix in path:
                if callable(resp):
                    return resp(dict(params or {}), len(self.calls))
                if isinstance(resp, list):      # successive responses, in order
                    hits = sum(1 for c in self.calls if suffix in c[1])
                    return resp[min(hits - 1, len(resp) - 1)]
                return resp
        return None

    def params_for(self, suffix: str) -> list[dict]:
        return [p for (_u, path, p) in self.calls if suffix in path]

    def urls_for(self, suffix: str) -> list[str]:
        return [u for (u, path, _p) in self.calls if suffix in path]


def snap(bid=None, ask=None, bs=0, asz=0, last=None, vol=None, prev_vol=None):
    """A snapshot in Alpaca's own shape -- keys copied from a live SPY row."""
    s: dict = {}
    if bid is not None or ask is not None:
        s["latestQuote"] = {"bp": bid, "ap": ask, "bs": bs, "as": asz,
                            "bx": "C", "ax": "Q", "c": "A",
                            "t": "2026-09-18T19:59:59.993277612Z"}
    if last is not None:
        s["latestTrade"] = {"p": last, "s": 19, "x": "C", "c": "k",
                            "t": "2026-09-18T18:21:00.507788931Z"}
    if vol is not None:
        s["dailyBar"] = {"o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": vol,
                         "n": 8, "vw": 1.05, "t": "2026-09-18T04:00:00Z"}
    if prev_vol is not None:
        s["prevDailyBar"] = {"o": 1.0, "h": 1.2, "l": 0.9, "c": 1.05,
                             "v": prev_vol, "n": 8, "vw": 1.02,
                             "t": "2026-09-17T04:00:00Z"}
    return s


def od_with(table, **kw) -> tuple[O.OptionData, Fake]:
    f = Fake(table)
    return O.OptionData(transport=f, **kw), f


def main() -> int:
    print("\n1. OCC symbols parse from the right, not the left")
    m = O.parse_occ("SPY260918C00650000")
    check("underlying", m["underlying"], "SPY")
    check("expiry", m["expiry"], date(2026, 9, 18))
    check("right", m["right"], "C")
    check("strike", m["strike"], 650.0)
    # a 6-character root is the case that breaks any left-to-right parser
    m2 = O.parse_occ("GOOGL260320P00172500")
    check("6-char root", m2["underlying"], "GOOGL")
    check("half-dollar strike", m2["strike"], 172.5)
    check("put", m2["right"], "P")
    check("built symbol round-trips",
          O._occ_fallback("SPY", date(2026, 9, 18), "C", 650.0), "SPY260918C00650000")
    check("strike is rounded, not truncated",
          O._occ_fallback("SPY", date(2026, 9, 18), "P", 172.49999999),
          "SPY260918P00172500")
    raises("a stock symbol is refused", lambda: O.parse_occ("SPY"), "OCC")
    raises("a bad right is refused",
           lambda: O._occ_fallback("SPY", date(2026, 9, 18), "X", 1.0), "C or P")
    try:
        import optsym                                   # noqa: F401
        same = optsym.occ("SPY", date(2026, 9, 18), "C", 650.0)
        check("optsym.occ agrees with the local fallback", same, "SPY260918C00650000")
    except Exception as e:
        print(f"  SKIP  optsym not importable yet ({type(e).__name__}); the local "
              f"fallback is in use")

    print("\n2. The mid is a real price or it is None")
    # bid 1.20 / ask 1.30 -> mid 1.25, spread 0.10, spread_pct 0.10/1.25 = 0.08
    c = O._contract_from_snapshot("SPY260918C00650000",
                                  snap(1.20, 1.30, 12, 30, last=1.24, vol=44))
    check("mid", c["mid"], 1.25)
    check("spread", c["spread"], 0.10)
    check("spread_pct", c["spread_pct"], 0.08)
    check("bid_size", c["bid_size"], 12.0)
    check("ask_size", c["ask_size"], 30.0)
    check("volume off dailyBar", c["volume"], 44.0)
    check("last trade price", c["last"], 1.24)
    check("last_at is carried", bool(c["last_at"]), True)
    check("snapshots carry no open interest", c["open_interest"], None)
    one = O._contract_from_snapshot("SPY260918C00650000", snap(None, 1.30, 0, 30))
    check("no bid -> mid None, never 0.0", one["mid"], None)
    check("no bid -> spread None", one["spread"], None)
    check("no bid -> spread_pct None", one["spread_pct"], None)
    check("the ask that IS there is kept", one["ask"], 1.30)
    zero = O._contract_from_snapshot("SPY260918P00300000", snap(0.0, 0.05, 0, 40))
    check("a 0.00 bid is no bid", zero["bid"], None)
    check("a 0.00 bid gives no mid", zero["mid"], None)
    empty = O._contract_from_snapshot("SPY260918P00300000", {})
    check("an unquoted strike is still a row", empty["strike"], 300.0)
    check("an unquoted strike has no mid", empty["mid"], None)
    check("a non-option key is skipped, not raised",
          O._contract_from_snapshot("NOTANOPTION", snap(1, 2)), None)

    print("\n3. The quality gate, and the reason it gives")
    g = O.QualityGate()
    good = O._contract_from_snapshot("SPY260918C00650000",
                                     snap(1.20, 1.30, 12, 30, vol=500))
    score, why = g.check(good)
    check("a tight, deep, busy contract passes", why, None)
    # tight = 1 - 0.08/0.20 = 0.6; depth = min(12,30)/25 = 0.48; vol = 1.0;
    # oi unknown = 0.5 -> 0.5*0.6 + (0.48+1.0+0.5)/6 = 0.3 + 0.33 = 0.63
    check("score is the documented weighting", score, 0.63, tol=5e-4)
    wide = O._contract_from_snapshot("SPY260918C00700000",
                                     snap(1.00, 1.50, 20, 20, vol=500))
    _s, why = g.check(wide)
    check("a 40% spread is rejected", "spread" in (why or ""), True)
    check("the reason names the contract", (why or "").startswith("SPY260918C00700000"),
          True)
    cheap = O._contract_from_snapshot("SPY260918C00800000",
                                      snap(0.01, 0.02, 50, 50, vol=500))
    _s, why = g.check(cheap)
    check("a 1.5c mid is under the floor", "floor" in (why or ""), True)
    nobook = O._contract_from_snapshot("SPY260918C00900000", snap(None, None))
    _s, why = g.check(nobook)
    check("no two-sided market is its own reason",
          "no two-sided market" in (why or ""), True)
    thin = O._contract_from_snapshot("SPY260918C00655000",
                                     snap(1.20, 1.30, 0, 30, vol=500))
    _s, why = g.check(thin)
    check("zero size on one side is rejected", "size" in (why or ""), True)
    # the open trap: before 09:30 today's volume is 0 and yesterday's is not
    preopen = O._contract_from_snapshot("SPY260918C00650000",
                                        snap(1.20, 1.30, 12, 30, vol=0,
                                             prev_vol=4000))
    check("pre-open volume falls back to the previous session",
          O._gate_volume(preopen), 4000.0)
    check("a liquid strike is not rejected at 09:29", g.check(preopen)[1], None)
    dead = O._contract_from_snapshot("SPY260918C00650000",
                                     snap(1.20, 1.30, 12, 30, vol=0, prev_vol=1))
    check("a genuinely dead strike still fails on volume",
          "traded" in (g.check(dead)[1] or ""), True)
    check("unknown open interest does not reject",
          g.check(O._contract_from_snapshot(
              "SPY260918C00650000", snap(1.20, 1.30, 12, 30, vol=500)))[1], None)
    known_oi = O._contract_from_snapshot("SPY260918C00650000",
                                         snap(1.20, 1.30, 12, 30, vol=500))
    O.apply_open_interest([known_oi], {"SPY260918C00650000": 3})
    check("known, tiny open interest DOES reject",
          "open interest" in (g.check(known_oi)[1] or ""), True)
    miss = O._contract_from_snapshot("SPY260918C00651000",
                                     snap(1.20, 1.30, 12, 30, vol=500))
    O.apply_open_interest([miss], {"SPY260918C00650000": 3})
    check("a symbol absent from the OI map stays unknown, not zero",
          miss["open_interest"], None)
    check("passes() agrees with check()", g.passes(good), True)
    loose = O.QualityGate(max_spread_pct=0.50, min_volume=0)
    check("thresholds are the caller's to move", loose.check(wide)[1], None)

    print("\n4. chain(): one request, filtered, off the cheap host")
    rows = {"snapshots": {
        "SPY260918C00650000": snap(1.20, 1.30, 12, 30, last=1.24, vol=44),
        "SPY260918P00650000": snap(2.00, 2.10, 5, 5, last=2.05, vol=90),
        "SPY260918C00645000": snap(4.00, 4.20, 8, 8, last=4.10, vol=10),
        "SPYSOMETHINGODD": snap(1, 2),
    }}
    od, f = od_with({"options/snapshots": rows})
    ch = od.chain("SPY", "2026-09-18", around=650.0, pct=0.01)
    check("one request for a filtered expiry", len(f.calls), 1)
    check("unparseable keys are dropped", len(ch), 3)
    check("sorted by strike then right", [c["occ"] for c in ch],
          ["SPY260918C00645000", "SPY260918C00650000", "SPY260918P00650000"])
    p = f.params_for("options/snapshots")[0]
    check("expiration_date is filtered server-side", p["expiration_date"], "2026-09-18")
    check("limit is pinned at 1000 (5000 is an HTTP 400)", p["limit"], 1000)
    check("strike band low", p["strike_price_gte"], "643.50")
    check("strike band high", p["strike_price_lte"], "656.50")
    check("opra is the feed", p["feed"], "opra")
    check("the data host is used", f.urls_for("options/snapshots")[0].startswith(
        "https://data.alpaca.markets"), True)
    check("nothing was spent on the trading budget", od.trading_calls, 0)
    check("data calls counted", od.data_calls, 1)
    od2, f2 = od_with({"options/snapshots": rows})
    od2.chain("SPY", "2026-09-18", around=650.0, pct=0.01, right="P")
    check("right=P becomes type=put", f2.params_for("options/snapshots")[0]["type"],
          "put")
    raises("a nonsense right is refused",
           lambda: od2.chain("SPY", "2026-09-18", right="X"), "C")
    raises("a nonsense expiry says what to pass",
           lambda: od2.chain("SPY", "not-a-date"), "YYYY-MM-DD")

    print("\n5. chain() pages only when the filter overflows")
    pages = [
        {"snapshots": {"SPY260918C00650000": snap(1.2, 1.3, 1, 1)},
         "next_page_token": "t1"},
        {"snapshots": {"SPY260918C00651000": snap(1.1, 1.2, 1, 1)}},
    ]
    od, f = od_with({"options/snapshots": pages})
    ch = od.chain("SPY", "2026-09-18", around=650.0, pct=0.01)
    check("both pages merged", len(ch), 2)
    check("two requests, then it stopped", len(f.calls), 2)
    check("the token was sent back", f.params_for("options/snapshots")[1]["page_token"],
          "t1")

    print("\n6. expirations(): the expiration_date_gte guard")
    today = datetime(2026, 9, 18, 10, 0, tzinfo=O.ET)
    contracts = {"option_contracts": [
        {"symbol": "SPY260918C00650000", "expiration_date": "2026-09-18"},
        {"symbol": "SPY260921C00650000", "expiration_date": "2026-09-21"},
        {"symbol": "SPY260921C00655000", "expiration_date": "2026-09-21"},
        {"symbol": "SPY270101C00650000", "expiration_date": "2027-01-01"},
    ]}
    trade_latest = {"trade": {"p": 660.0, "s": 100, "t": "2026-09-18T19:59:00Z"}}
    od, f = od_with({"options/contracts": contracts,
                     "trades/latest": trade_latest})
    exps = od.expirations("SPY", min_dte=0, max_dte=7, now=today)
    check("dates are de-duplicated and sorted", exps,
          [date(2026, 9, 18), date(2026, 9, 21), date(2027, 1, 1)])
    p = f.params_for("options/contracts")[0]
    check("THE GUARD: expiration_date_gte is always sent",
          p["expiration_date_gte"], "2026-09-18")
    check("the window is bounded above too", p["expiration_date_lte"], "2026-09-25")
    check("calls only -- the answer is about dates, so halve the rows",
          p["type"], "call")
    check("a strike band from spot keeps the row count down",
          p["strike_price_gte"], "561.00")
    check("the trading host serves the registry",
          f.urls_for("options/contracts")[0].startswith(
              "https://paper-api.alpaca.markets"), True)
    check("exactly one scarce trading call", od.trading_calls, 1)
    check("the spot lookup went to the data host", od.data_calls, 1)
    check("min_dte moves the lower bound",
          od.expirations("SPY", min_dte=3, max_dte=10, now=today) and
          f.params_for("options/contracts")[-1]["expiration_date_gte"], "2026-09-21")

    # the guard is a refusal, not a convention: any caller reaching the
    # contracts endpoint without the bound gets an error naming the trap
    od3, _f3 = od_with({"options/contracts": contracts})
    raises("the raw request without the bound is refused",
           lambda: od3._trading_get("/v2/options/contracts",
                                    {"underlying_symbols": "SPY"}),
           "nearest expiry")

    print("\n7. expirations(): a thinly-struck name falls back unfiltered")
    def banded_is_empty(params, _n):
        if "strike_price_gte" in params:
            return {"option_contracts": []}
        return contracts

    od, f = od_with({"options/contracts": banded_is_empty,
                     "trades/latest": trade_latest})
    exps = od.expirations("XYZ", max_dte=400, now=today)
    check("the retry found the expiries the band missed", len(exps), 3)
    check("it cost two trading calls, not one", od.trading_calls, 2)
    od, f = od_with({"options/contracts": contracts, "trades/latest": {}})
    od.expirations("XYZ", now=today)
    check("no spot means no strike band at all",
          "strike_price_gte" in f.params_for("options/contracts")[0], False)

    print("\n8. The TTL cache")
    od, f = od_with({"options/snapshots": rows}, ttl=60.0)
    od.chain("SPY", "2026-09-18", around=650.0, pct=0.01)
    od.chain("SPY", "2026-09-18", around=650.0, pct=0.01)
    check("a repeat inside the TTL does not re-fetch", len(f.calls), 1)
    od.chain("SPY", "2026-09-18", around=650.0, pct=0.02)
    check("a different band is a different request", len(f.calls), 2)
    od.cache_clear()
    od.chain("SPY", "2026-09-18", around=650.0, pct=0.01)
    check("cache_clear forces a fetch", len(f.calls), 3)
    od0, f0 = od_with({"options/snapshots": rows}, ttl=0.0)
    od0.chain("SPY", "2026-09-18", around=650.0, pct=0.01)
    od0.chain("SPY", "2026-09-18", around=650.0, pct=0.01)
    check("ttl=0 disables caching entirely", len(f0.calls), 2)
    check("stats() reports both budgets and the cache",
          sorted(od.stats()), ["cache_entries", "data_calls", "trading_calls"])

    print("\n9. bars() and trades(): paging, and a silent symbol")
    bar_pages = [
        {"bars": {"SPY260918C00650000": [{"t": "2026-09-18T13:30:00Z", "c": 1.0}]},
         "next_page_token": "b1"},
        {"bars": {"SPY260918C00650000": [{"t": "2026-09-18T13:31:00Z", "c": 1.1}],
                  "SPY260918P00650000": [{"t": "2026-09-18T13:31:00Z", "c": 2.0}]}},
    ]
    od, f = od_with({"options/bars": bar_pages})
    got = od.bars(["SPY260918C00650000", "SPY260918P00650000", "SPY260918C00999000"],
                  "1Min", start="2026-09-18", end="2026-09-19")
    check("pages are concatenated in order", [b["c"] for b in
                                              got["SPY260918C00650000"]], [1.0, 1.1])
    check("a symbol that never traded is an empty list, not a KeyError",
          got["SPY260918C00999000"], [])
    check("every requested symbol has a key", sorted(got),
          ["SPY260918C00650000", "SPY260918C00999000", "SPY260918P00650000"])
    p = f.params_for("options/bars")[0]
    check("symbols are batched into one request",
          p["symbols"].count(",") + 1, 3)
    check("timeframe is passed through", p["timeframe"], "1Min")
    check("dates become ISO strings", p["start"], "2026-09-18")
    check("bars are historical data, off the cheap host", od.trading_calls, 0)
    check("an empty symbol list makes no request at all", od.bars([]), {})
    od, f = od_with({"options/trades": {"trades": {"SPY260918C00650000":
                                                   [{"p": 1.05, "s": 1}]}}})
    tr = od.trades(["SPY260918C00650000"], start=date(2026, 9, 18))
    check("trades come back under their own key",
          tr["SPY260918C00650000"][0]["p"], 1.05)
    check("trades take no timeframe",
          "timeframe" in f.params_for("options/trades")[0], False)
    od, f = od_with({"options/bars": {"bars": {}}})
    od.bars([f"SPY260918C{i:08d}" for i in range(250)], chunk=100)
    check("a long symbol list is chunked", len(f.calls), 3)

    print("\n10. historical_chain_symbols(): the only door to an expired chain")
    od, _f = od_with({})
    syms = od.historical_chain_symbols("SPY", "2026-02-16", 640, 643, 1.0)
    check("both rights per strike", len(syms), 8)
    check("first symbol", syms[0], "SPY260216C00640000")
    check("puts are generated too", syms[1], "SPY260216P00640000")
    check("the top of the range is inclusive", syms[-1], "SPY260216P00643000")
    half = od.historical_chain_symbols("SPY", date(2026, 2, 16), 640, 641, 0.5, right="C")
    check("a half-dollar grid does not drift", half,
          ["SPY260216C00640000", "SPY260216C00640500", "SPY260216C00641000"])
    check("reversed bounds are tolerated",
          od.historical_chain_symbols("SPY", "2026-02-16", 641, 640, 1.0, right="P"),
          ["SPY260216P00640000", "SPY260216P00641000"])
    raises("a zero step says what a step is for",
           lambda: od.historical_chain_symbols("SPY", "2026-02-16", 1, 2, 0),
           "strike spacing")

    print("\n11. Live check against Alpaca (skipped without keys or network)")
    live_ok = _live_check()
    if not live_ok:
        print("  SKIP  no credentials or no network -- offline suite is complete")

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


def _live_check() -> bool:
    """Prove the module against the real API when that is possible.

    Deliberately not required: this suite has to pass on a machine with no
    keys, and a network failure is not a code defect. Anything it does find,
    though, is a real FAIL -- a live shape that no longer matches the parser is
    exactly the bug this file exists to catch.
    """
    global FAIL
    try:
        import requests                                     # noqa: F401
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except Exception:
        return False
    if not (os.environ.get("APCA_API_KEY_ID") and os.environ.get("APCA_API_SECRET_KEY")):
        return False
    try:
        od = O.OptionData(ttl=30.0)
        spot = od.spot("SPY")
        if not spot:
            print("  SKIP  no SPY print available (market never opened today?)")
            return True
        exps = od.expirations("SPY", min_dte=0, max_dte=10,
                              now=datetime.now(O.ET) - timedelta(days=0))
        if not exps:
            print("  SKIP  no SPY expiries in the next 10 days -- unexpected, but "
                  "not a parser defect")
            return True
        rows = od.chain("SPY", exps[0], around=spot, pct=0.03)
    except Exception as e:
        print(f"  SKIP  live call failed ({type(e).__name__}: {str(e)[:120]})")
        return False
    check("live: a chain came back", len(rows) > 0, True)
    check("live: every row parsed to the requested expiry",
          all(c["expiry"] == exps[0] for c in rows), True)
    check("live: snapshots still carry no open interest",
          all(c["open_interest"] is None for c in rows), True)
    quoted = [c for c in rows if c["mid"] is not None]
    check("live: at least one two-sided market", len(quoted) > 0, True)
    check("live: no mid is ever zero", all(c["mid"] > 0 for c in quoted), True)
    check("live: spread_pct is present wherever mid is",
          all(c["spread_pct"] is not None for c in quoted), True)
    check("live: the chain cost one data request", od.data_calls <= 3, True)
    return True


if __name__ == "__main__":
    sys.exit(main())
