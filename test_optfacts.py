#!/usr/bin/env python3
"""
test_optfacts.py -- the per-ticker fact vector, offline.

No network and no credentials: a fake transport drives `optdata.OptionData`
through every path, and the chain it serves is priced with `greeks.price` so
the solver has something real to recover.

What is actually being defended here, in the owner's terms:

  * "never placeholders". A fact that cannot be measured is None WITH A
    REASON. Section 2 checks that the two constructors make the other case
    impossible to write by accident.
  * "say how much history backs it, or say there is not enough". Section 4
    proves a thin IV history is refused rather than ranked, and that a history
    between the floor and a full year is served labelled `thin` with its day
    count.
  * "use theirs, compute only what they do not send". Section 8 runs the same
    symbol twice -- once with Alpaca's greeks in the snapshot and once
    without -- and checks the source stamp flips and the IV survives both.
  * "never guess a regime". Section 7 removes one regime input at a time and
    checks the answer is `unusable` naming that input, not a fallback.
  * "report what a refresh cost". Section 9 checks the trading-host spend is
    counted and that the budget actually stops the optional call.

    .venv/Scripts/python test_optfacts.py
"""
from __future__ import annotations

import datetime as _dt
import logging
import math
import sys
from pathlib import Path

import greeks as G
import optfacts as F
import optdata as O
import optsym

# Several sections deliberately break an endpoint; the module logs a warning
# for each, and those warnings interleaved with the checks read as failures.
logging.getLogger("optfacts").setLevel(logging.CRITICAL)

FAIL = 0


def check(name, got, want, tol=1e-6) -> None:
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
    g = f"{got:.4f}" if isinstance(got, float) else repr(got)
    w = f"{want:.4f}" if isinstance(want, float) else repr(want)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {g}, want {w}")


def contains(name, haystack, needle) -> None:
    global FAIL
    ok = needle.lower() in str(haystack or "").lower()
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: {needle!r} in "
          f"{str(haystack)[:110]!r}")


# ----------------------------------------------------------- the fake wire
NOW = _dt.datetime(2026, 9, 21, 15, 30, tzinfo=_dt.timezone.utc)   # a Monday
TS = NOW.timestamp()
TODAY_ET = NOW.astimezone(F.ET).date()
SPOT = 100.0
RATE = 0.043
FRONT = TODAY_ET + _dt.timedelta(days=21)
BACK = TODAY_ET + _dt.timedelta(days=49)


def occ(expiry: _dt.date, right: str, strike: float) -> str:
    return optsym.occ("SPY", expiry, right, strike)


def priced(expiry, right, strike, sigma, *, with_greeks, spread=0.02,
           size=50, volume=500):
    """One snapshot, in Alpaca's own shape, priced off a real model.

    The mid is a genuine Black-Scholes price, so `greeks.chain_greeks` has
    something to invert and the test is checking the pipe, not a constant.
    `with_greeks=False` is the 0DTE case: Alpaca sends no greeks object and no
    implied volatility, and everything has to be solved here.
    """
    T = optsym.year_fraction(expiry, now=NOW)
    mid = G.price(SPOT, strike, T, RATE, sigma, right)
    half = max(0.005, mid * spread / 2.0)
    snap = {
        "latestQuote": {"bp": round(mid - half, 4), "ap": round(mid + half, 4),
                        "bs": size, "as": size,
                        "t": "2026-09-21T15:29:59Z"},
        "latestTrade": {"p": round(mid, 4), "s": 5, "t": "2026-09-21T15:29Z"},
        "dailyBar": {"o": mid, "h": mid, "l": mid, "c": mid, "v": volume},
    }
    if with_greeks:
        g = G.greeks(SPOT, strike, T, RATE, sigma, right)
        snap["impliedVolatility"] = sigma
        snap["greeks"] = {"delta": g.delta, "gamma": g.gamma,
                          "theta": g.theta, "vega": g.vega, "rho": g.rho}
    return snap


def smile(strike: float, base: float) -> float:
    """A plain put-skew smile: implied volatility rises as the strike falls."""
    return base + 0.30 * max(0.0, (SPOT - strike) / SPOT)


def build_snapshots(expiry, base_iv, *, with_greeks, strikes=None, **kw):
    out = {}
    for k in (strikes or [88, 91, 94, 97, 100, 103, 106, 109, 112]):
        for right in ("C", "P"):
            out[occ(expiry, right, k)] = priced(expiry, right, float(k),
                                                smile(k, base_iv),
                                                with_greeks=with_greeks, **kw)
    return out


class Wire:
    """A fake transport for OptionData. Records every call and its host."""

    def __init__(self, *, with_greeks=True, expiries=(FRONT, BACK),
                 front_iv=0.30, back_iv=0.34, spot=SPOT, chain_kw=None):
        self.with_greeks = with_greeks
        self.expiries = list(expiries)
        self.front_iv, self.back_iv = front_iv, back_iv
        self.spot = spot
        self.chain_kw = chain_kw or {}
        self.calls: list[tuple[str, dict]] = []
        self.oi = {}

    def __call__(self, method, url, path, params):
        self.calls.append((path, dict(params or {})))
        if path.endswith("/trades/latest"):
            return {"trade": {"p": self.spot}} if self.spot else {"trade": {}}
        if path.endswith("/options/contracts"):
            lo = params.get("expiration_date_gte")
            hi = params.get("expiration_date_lte")
            rows = []
            for e in self.expiries:
                if lo and e.isoformat() < lo:
                    continue
                if hi and e.isoformat() > hi:
                    continue
                for k in (95.0, 100.0, 105.0):
                    rows.append({"symbol": occ(e, "C", k),
                                 "expiration_date": e.isoformat(),
                                 "open_interest": self.oi.get(e)})
            return {"option_contracts": rows}
        if "/options/snapshots/" in path:
            want = params.get("expiration_date")
            if want == self.expiries[0].isoformat():
                return {"snapshots": build_snapshots(
                    self.expiries[0], self.front_iv,
                    with_greeks=self.with_greeks, **self.chain_kw)}
            if len(self.expiries) > 1 and want == self.expiries[1].isoformat():
                return {"snapshots": build_snapshots(
                    self.expiries[1], self.back_iv,
                    with_greeks=self.with_greeks, **self.chain_kw)}
            return {"snapshots": {}}
        raise AssertionError("the fake wire was asked for %s" % path)

    def trading_paths(self):
        return [p for p, _ in self.calls if "/v2/options/contracts" in p]


class Alp:
    """The bits of broker.Alpaca a fact vector touches: daily bars, and
    nothing else. No `_req`, so any accidental direct call is a crash rather
    than a silent live request.

    `bars()` returns NOTHING, on purpose. That is what the real client does
    for a limit-based daily call on this account -- probed 19 Sep 2026, 0 bars
    on both feeds -- and a fake that answered it would hide the bug the range
    call exists to route around.
    """

    def __init__(self, bars):
        self._bars = list(bars)
        self.range_calls = 0
        self.limit_calls = 0
        self.adjustments: list[str] = []

    def bars(self, symbol, timeframe="1Min", limit=10, feed=""):
        self.limit_calls += 1
        return []

    def bars_range(self, symbol, timeframe, start, end="", max_pages=60,
                   adjustment="raw", feed=""):
        self.range_calls += 1
        self.adjustments.append(adjustment)
        return list(self._bars)


class Cal:
    """A fake optcal.EventCalendar. The two *_known methods are the whole
    point: optcal returns None both for "unknown" and for "none scheduled",
    and a fact vector that conflated them would print a clear board over an
    unread calendar."""

    def __init__(self, earn=None, earn_known=True, div=None, div_known=True):
        self.earn, self.earn_known = earn, earn_known
        self.div, self.div_known = div, div_known

    def earnings_known(self, symbol):
        return self.earn_known

    def next_earnings(self, symbol, now=None):
        return self.earn

    def dividends_known(self, symbol, now=None, through=None):
        return self.div_known

    def next_dividend(self, symbol, now=None, through=None):
        return self.div


def sine_bars(n=40, sigma=0.18, start=100.0):
    """Deterministic closes whose realised volatility is close to `sigma`.

    A sine wave, not a random walk: the suite must give the same numbers on
    every machine, and the exact realised figure is checked against
    optvol.realized_vol rather than against a constant, so the shape only has
    to be non-degenerate.
    """
    step = sigma / math.sqrt(252)
    out, px = [], start
    for i in range(n):
        px *= math.exp(step * math.sin(i * 1.7))
        out.append({"o": px, "h": px * 1.004, "l": px * 0.996, "c": px,
                    "v": 1_000_000})
    return out


def vector(*, wire=None, cal=None, bars=None, iv_history=None, **kw):
    w = wire or Wire()
    od = O.OptionData(transport=w)
    v = F.facts(Alp(bars if bars is not None else sine_bars()), "SPY", od=od,
                calendar=cal if cal is not None else Cal(),
                iv_history=iv_history if iv_history is not None else [],
                now=TS, rate=RATE, **kw)
    return v, w, od


def fdict(**over):
    """A complete, healthy set of the facts the regime reads."""
    base = {
        "liquidity_grade": F.known("liquidity_grade", "A", "computed", TS),
        "iv": F.known("iv", 0.30, "alpaca", TS),
        "realized_vol_20": F.known("realized_vol_20", 0.20, "computed", TS),
        "vrp_ratio": F.known("vrp_ratio", 1.50, "computed", TS),
        "earnings_in_days": F.known("earnings_in_days", 40, "calendar", TS),
        "atm_spread_pct": F.known("atm_spread_pct", 0.01, "alpaca", TS),
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def main() -> int:
    print("\n1. the registry is the closed vocabulary, and it describes itself")
    check("every entry is keyed by its own name",
          all(k == s.name for k, s in F.FACTS.items()), True)
    check("every entry names the function that produces it",
          all("." in s.provider for s in F.FACTS.values()), True)
    check("every entry has a freshness budget",
          all(s.max_age_s > 0 for s in F.FACTS.values()), True)
    check("every entry answers 'why do we compute this'",
          all(len(s.note) > 20 for s in F.FACTS.values()), True)
    check("the facts the owner asked for by name are all present",
          sorted(n for n in ("spot", "iv", "iv_rank", "iv_rank_days",
                             "iv_percentile", "realized_vol_20", "vrp",
                             "term_slope", "skew_25d", "liquidity_grade",
                             "atm_spread_pct", "open_interest_atm",
                             "earnings_in_days", "ex_div_in_days",
                             "next_expiry", "dte_to_next")
                 if n in F.FACTS),
          ["atm_spread_pct", "dte_to_next", "earnings_in_days",
           "ex_div_in_days", "iv", "iv_percentile", "iv_rank", "iv_rank_days",
           "liquidity_grade", "next_expiry", "open_interest_atm",
           "realized_vol_20", "skew_25d", "spot", "term_slope", "vrp"])
    check("IV is flagged as Alpaca's to serve, not ours to compute",
          F.FACTS["iv"].computed, False)
    check("IV RANK is flagged as ours -- no endpoint serves it",
          F.FACTS["iv_rank"].computed, True)
    check("registry() is JSON-ready for the board",
          len(F.registry()), len(F.FACTS))

    print("\n2. a value without a source, or a None without a reason, cannot "
          "be written")
    try:
        F.known("iv", None, "alpaca", TS)
        check("known(None) is refused", False, True)
    except ValueError as exc:
        contains("known(None) is refused", exc, "use unknown")
    try:
        F.unknown("iv", "")
        check("unknown() with no reason is refused", False, True)
    except ValueError as exc:
        contains("unknown() with no reason is refused", exc, "needs a reason")
    f = F.unknown("iv", "the chain came back empty")
    check("an unknown fact is None, never 0.0", f.value, None)
    check("an unknown fact is not 'known'", f.known, False)
    check("an unknown fact carries its unit anyway", f.unit, "ratio")
    ok = F.known("iv", 0.31, "alpaca", TS)
    check("a known fact reports its age", ok.age_s(TS + 30.0), 30.0)
    check("inside its budget it is not stale", ok.is_stale(TS + 30.0), False)
    check("past its budget it is", ok.is_stale(TS + F.AGE_QUOTE + 1), True)
    check("to_dict carries the registry's 'computed' answer",
          ok.to_dict(TS)["computed"], False)

    print("\n3. recorded IV history collapses to one reading per SESSION")
    import json
    import tempfile
    log = Path(tempfile.mkdtemp(prefix="ta_optfacts_")) / "q.jsonl"
    rows = []
    for day, ivs in ((21, [0.20, 0.24, 0.28]), (22, [0.30]), (23, [0.40])):
        for n, iv in enumerate(ivs):
            t = _dt.datetime(2026, 9, day, 14 + n, 0,
                             tzinfo=_dt.timezone.utc).timestamp()
            rows.append({"ts": t, "underlying": "SPY", "symbol": "X",
                         "iv": iv, "moneyness": 1.0})
        # a row far from the money must not reach the surface
        rows.append({"ts": t + 60, "underlying": "SPY", "symbol": "Y",
                     "iv": 9.9, "moneyness": 1.4})
    log.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    hist = F.iv_history_daily("SPY", path=log)
    check("three sessions in, three readings out", len(hist), 3)
    check("the session with three samples contributes its MEDIAN, once",
          hist[0][1], 0.24)
    check("the out-of-band row is excluded",
          max(v for _d, v in hist), 0.40)
    check("oldest first", [d.day for d, _v in hist], [21, 22, 23])

    print("\n4. thin IV history is reported as thin, never ranked")
    thin = [(TODAY_ET - _dt.timedelta(days=i), 0.20 + 0.001 * i)
            for i in range(6, 0, -1)]
    v, _w, _od = vector(iv_history=thin)
    check("six sessions: iv_rank_days says six", v.value("iv_rank_days"), 6)
    check("six sessions: there is no rank", v.value("iv_rank"), None)
    check("six sessions: there is no percentile",
          v.value("iv_percentile"), None)
    contains("and the reason says how many sessions there are",
             v.get("iv_rank").reason, "only 6 session(s)")
    contains("and names the floor it fell short of",
             v.get("iv_rank").reason, str(F.MIN_IV_RANK_DAYS))
    check("the day count itself is flagged as not enough",
          v.get("iv_rank_days").quality, "missing")

    mid_hist = [(TODAY_ET - _dt.timedelta(days=i), 0.10 + 0.003 * (100 - i))
                for i in range(100, 0, -1)]
    v, _w, _od = vector(iv_history=mid_hist)
    check("100 sessions: a rank is served", v.value("iv_rank") is not None,
          True)
    check("...but labelled thin", v.get("iv_rank").quality, "thin")
    contains("...with the count and the full year in the reason",
             v.get("iv_rank").reason, "100 of %d" % F.FULL_IV_RANK_DAYS)
    check("iv_rank_days is the length of the history",
          v.value("iv_rank_days"), 100)

    flat = [(TODAY_ET - _dt.timedelta(days=i), 0.25) for i in range(80, 0, -1)]
    v, _w, _od = vector(iv_history=flat)
    check("a collapsed range yields no rank at all", v.value("iv_rank"), None)
    contains("and says the range has collapsed",
             v.get("iv_rank").reason, "collapsed")
    check("but percentile still answers, which is its whole advantage",
          v.value("iv_percentile") is not None, True)

    print("\n5. the liquidity grade is read off the median spread, not one row")
    def row(spread_pct, mid=1.0, size=50, vol=500, oi=None):
        return {"occ": "X", "mid": mid, "spread_pct": spread_pct,
                "bid_size": size, "ask_size": size, "volume": vol,
                "open_interest": oi, "strike": 100.0}

    check("penny-wide is an A", F.liquidity_grade([row(0.01)] * 5)[0], "A")
    check("a nickel of a dollar is a B", F.liquidity_grade([row(0.04)] * 5)[0],
          "B")
    check("a dime is a C", F.liquidity_grade([row(0.08)] * 5)[0], "C")
    check("a fifth is a D", F.liquidity_grade([row(0.20)] * 5)[0], "D")
    check("one wide quote among four tight ones does not move the grade",
          F.liquidity_grade([row(0.01), row(0.01), row(0.01), row(0.01),
                             row(0.40)])[0], "A")
    nobid = [{"occ": "X", "mid": None, "spread_pct": None, "strike": 100.0}]
    check("no two-sided market anywhere is an F",
          F.liquidity_grade(nobid)[0], "F")
    check("an empty chain has no grade at all -- not an F",
          F.liquidity_grade([])[0], None)
    contains("and says why", F.liquidity_grade([])[2], "no at-the-money")

    print("\n6. every regime is reachable, and each names the facts that "
          "decided it")
    reg, why = F.regime_of(fdict(), now=TS)
    check("rich premium is rich_vol", reg, "rich_vol")
    contains("and the reason quotes implied against realized", why, "implied")
    contains("and names the band it crossed", why, "%.2f" % F.VRP_RICH_RATIO)

    reg, why = F.regime_of(fdict(
        vrp_ratio=F.known("vrp_ratio", 0.80, "computed", TS)), now=TS)
    check("cheap premium is cheap_vol", reg, "cheap_vol")
    contains("and names the cheap band", why, "%.2f" % F.VRP_CHEAP_RATIO)

    reg, why = F.regime_of(fdict(
        vrp_ratio=F.known("vrp_ratio", 1.05, "computed", TS)), now=TS)
    check("in between is neutral", reg, "neutral")
    contains("and says so in the band's own numbers", why, "neither rich nor")

    reg, why = F.regime_of(fdict(
        earnings_in_days=F.known("earnings_in_days", 4, "calendar", TS)),
        now=TS)
    check("earnings inside the window outrank the premium reading", reg,
          "event_risk")
    contains("and the reason is the print", why, "earnings in 4 day")

    reg, why = F.regime_of(fdict(
        liquidity_grade=F.known("liquidity_grade", "D", "computed", TS)),
        now=TS)
    check("an untradable spread is unusable", reg, "unusable")
    contains("and the reason is the spread", why, "liquidity_grade d")

    reg, why = F.regime_of(fdict(
        iv_rank=F.known("iv_rank", 8.0, "recorded", TS)), now=TS)
    check("rich against realized but cheap against its own year is NEITHER",
          reg, "neutral")
    contains("and says the two measures disagree", why, "disagree")

    reg, why = F.regime_of(fdict(
        iv_rank=F.known("iv_rank", 72.0, "recorded", TS)), now=TS)
    check("rich on both measures is still rich_vol", reg, "rich_vol")
    contains("and says the rank agrees", why, "agrees")
    check("every regime name the module publishes is one of the five",
          all(r in F.REGIMES for r in
              ("cheap_vol", "rich_vol", "neutral", "event_risk", "unusable")),
          True)

    print("\n7. a missing input yields unusable, never a guessed regime")
    for name in F.REGIME_INPUTS:
        reg, why = F.regime_of(fdict(**{name: None}), now=TS)
        check("without %s the regime refuses" % name, reg, "unusable")
        contains("...and names %s" % name, why, name)
    gone = F.regime_of(fdict(
        earnings_in_days=F.unknown("earnings_in_days",
                                   "nothing has asserted the schedule")),
        now=TS)
    check("an UNKNOWN earnings schedule is unusable, not 'no earnings'",
          gone[0], "unusable")
    clear = F.regime_of(fdict(
        earnings_in_days=F.Fact(name="earnings_in_days", value=None,
                                unit="days", source="calendar", as_of=TS,
                                quality="ok",
                                reason="known, none scheduled")), now=TS)
    check("a KNOWN-empty earnings schedule is not a gap", clear[0] != "unusable",
          True)
    stale = F.regime_of(fdict(
        iv=F.known("iv", 0.30, "alpaca", TS - 10 * F.AGE_QUOTE)), now=TS)
    check("a stale input is a gap too", stale[0], "unusable")
    contains("and the reason says stale, which is a DATA reason not a market "
             "one", stale[1], "stale")

    print("\n8. against a fake broker: the sources are labelled correctly")
    v, w, od = vector(iv_history=mid_hist)
    check("no error along the way", list(v.errors), [])
    check("spot came from Alpaca", v.get("spot").source, "alpaca")
    check("spot is the price, not a default", v.value("spot"), SPOT)
    check("the front expiry is the first one at or past the floor",
          v.value("next_expiry"), FRONT.isoformat())
    check("days to it are arithmetic on their date and our clock",
          v.value("dte_to_next"), (FRONT - TODAY_ET).days)
    check("when Alpaca publishes greeks, IV is THEIRS", v.iv_source, "alpaca")
    check("iv_source is the same thing as the fact's own source",
          v.iv_source, v.get("iv").source)
    check("and it is the at-the-money volatility we asked the model for",
          v.value("iv"), smile(100.0, 0.30), tol=2e-3)
    check("the whole chain came from Alpaca", v.value("greeks_source"),
          "alpaca")
    contains("and the count is on the fact", v.get("greeks_source").reason,
             "of 18 rows came from Alpaca")
    check("realized volatility is ours", v.get("realized_vol_20").source,
          "computed")
    alp = Alp(sine_bars())
    F.facts(alp, "SPY", od=O.OptionData(transport=Wire()), calendar=Cal(),
            iv_history=[], now=TS, rate=RATE)
    check("the bars came by RANGE -- the limit-based daily call returns "
          "nothing on this account", (alp.range_calls, alp.limit_calls), (1, 0))
    check("...and split-adjusted, or a reverse split reads as a fake trend",
          alp.adjustments, ["split"])
    check("vrp is implied minus realized",
          v.value("vrp"),
          round(v.value("iv") - v.value("realized_vol_20"), 6), tol=1e-5)
    check("vrp_ratio is the comparable form",
          v.value("vrp_ratio"),
          round(v.value("iv") / v.value("realized_vol_20"), 4), tol=1e-3)
    check("the liquidity grade came back", v.value("liquidity_grade") in
          ("A", "B", "C"), True)
    check("the spread it saw is reported next to it",
          v.value("atm_spread_pct") is not None, True)
    check("a put smile reads as put skew", v.value("put_skew") > 0, True)
    check("the back month is richer, so the term slope is positive",
          v.value("term_slope") > 0, True)
    check("the regime was formed", v.regime in F.REGIMES, True)
    check("every registry name is present in the vector",
          sorted(v.facts) == sorted(F.FACTS), True)
    check("every None in the vector carries a reason",
          [n for n, f in v.facts.items() if f.value is None and not f.reason],
          [])
    check("to_dict is JSON-ready",
          isinstance(json.dumps(v.to_dict()), str), True)

    print("\n9. the same symbol with NO broker greeks -- the 0DTE case")
    v2, _w2, _od2 = vector(wire=Wire(with_greeks=False), iv_history=mid_hist)
    check("nothing came from Alpaca", v2.value("greeks_source"), "computed")
    check("so the IV is stamped as ours", v2.iv_source, "computed")
    check("and it is still the right number",
          v2.value("iv"), smile(100.0, 0.30), tol=5e-3)
    check("which is what the merge promises: same answer, different stamp",
          abs(v.value("iv") - v2.value("iv")) < 5e-3, True)

    print("\n10. what the calendar cannot answer, the vector does not invent")
    v3, _w3, _od3 = vector(cal=Cal(earn_known=False, div_known=False),
                           iv_history=mid_hist)
    check("an unread earnings schedule is missing, not clear",
          v3.get("earnings_in_days").quality, "missing")
    contains("and the reason is an operational one",
             v3.get("earnings_in_days").reason, "state/earnings.json")
    check("an unread dividend calendar is missing too",
          v3.get("ex_div_in_days").quality, "missing")
    check("so the regime refuses", v3.regime, "unusable")
    contains("naming the calendar", v3.regime_reason, "earnings_in_days")

    v4, _w4, _od4 = vector(
        cal=Cal(earn=TODAY_ET + _dt.timedelta(days=3),
                div=TODAY_ET + _dt.timedelta(days=9)), iv_history=mid_hist)
    check("a known print is counted in days",
          v4.value("earnings_in_days"), 3)
    check("a known ex-date too", v4.value("ex_div_in_days"), 9)
    check("and a print in three days sets the regime", v4.regime, "event_risk")

    print("\n11. a dead endpoint costs its own facts and nothing else")
    class Broken(Wire):
        def __call__(self, method, url, path, params):
            if "/options/contracts" in path:
                raise RuntimeError("HTTP 500 on the contract registry")
            return super().__call__(method, url, path, params)

    v5, _w5, _od5 = vector(wire=Broken(), iv_history=mid_hist)
    check("the expiry is gone", v5.value("next_expiry"), None)
    contains("with the failure on the fact", v5.get("next_expiry").reason,
             "HTTP 500")
    check("the failure is collected, not swallowed", len(v5.errors) > 0, True)
    check("but spot still came back", v5.value("spot"), SPOT)
    check("and realized volatility, which never needed the chain",
          v5.value("realized_vol_20") is not None, True)
    check("the regime refuses rather than half-forming", v5.regime, "unusable")

    print("\n12. one listed expiry is not a term structure")
    v6, _w6, _od6 = vector(wire=Wire(expiries=(FRONT,)), iv_history=mid_hist)
    check("no slope", v6.value("term_slope"), None)
    contains("and the reason says why", v6.get("term_slope").reason,
             "not a term structure")
    check("everything else still formed", v6.value("iv") is not None, True)

    print("\n13. no spot means no vector, and it says so rather than zeroing")
    v7, _w7, _od7 = vector(wire=Wire(spot=0.0), iv_history=mid_hist)
    check("spot is None, not 0.0", v7.value("spot"), None)
    check("iv could not be read either", v7.value("iv"), None)
    check("the regime is unusable", v7.regime, "unusable")
    check("and no fact anywhere in the vector is a zero standing in for a "
          "missing measurement",
          [n for n, f in v7.facts.items()
           if f.value == 0 and n not in ("dte_to_next", "chain_rows",
                                         "open_interest_atm")], [])

    print("\n14. what a refresh costs the share fleet's budget")
    w = Wire()
    od = O.OptionData(transport=w)
    res = F.refresh_all(Alp(sine_bars()), ["SPY", "QQQ", "IWM"], od=od,
                        calendar=Cal(), iv_history=mid_hist, now=TS)
    check("three vectors", sorted(res.vectors), ["IWM", "QQQ", "SPY"])
    check("the trading-host spend is counted",
          res.trading_calls, len(w.trading_paths()))
    check("it is not zero -- the registry and open interest both live there",
          res.trading_calls > 0, True)
    check("the cheap host carried the rest", res.data_calls > res.trading_calls,
          True)
    contains("and the note says which budget is which", res.to_dict()["cost_note"],
             "shared with the live share fleet")
    check("the per-symbol spend is on each vector too",
          sum(v.trading_calls for v in res.vectors.values()),
          res.trading_calls)

    w2 = Wire()
    od2 = O.OptionData(transport=w2)
    tight = F.refresh_all(Alp(sine_bars()), ["SPY", "QQQ", "IWM"], od=od2,
                          calendar=Cal(), iv_history=mid_hist, now=TS,
                          trading_budget=1)
    check("a tight budget still produces a vector for every symbol",
          len(tight.vectors), 3)
    check("but it stops spending on the optional call",
          len(tight.budget_stopped) > 0, True)
    stopped = tight.vectors[tight.budget_stopped[0]]
    check("and the skipped fact is honestly absent",
          stopped.value("open_interest_atm"), None)
    contains("naming the budget as the reason",
             stopped.get("open_interest_atm").reason, "trading_budget")
    check("open_interest=False keeps us off the trading host for that fact",
          F.refresh_all(Alp(sine_bars()), ["SPY"],
                        od=O.OptionData(transport=Wire()), calendar=Cal(),
                        iv_history=mid_hist, now=TS, open_interest=False
                        ).vectors["SPY"].value("open_interest_atm"), None)

    print("\n15. one client across the board, because caching is the budget")
    w3 = Wire()
    od3 = O.OptionData(transport=w3)
    alp = Alp(sine_bars())
    F.refresh_all(alp, ["SPY", "SPY", "SPY"], od=od3, calendar=Cal(),
                  iv_history=mid_hist, now=TS)
    once = O.OptionData(transport=Wire())
    F.refresh_all(Alp(sine_bars()), ["SPY"], od=once, calendar=Cal(),
                  iv_history=mid_hist, now=TS)
    check("three passes over one symbol cost no more trading calls than one",
          od3.trading_calls, once.trading_calls)

    print("\n16. this module authors no orders and opens no second HTTP client")
    src = Path(F.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[-1]
    # "order" alone matches "recorder"; these are the actual order paths.
    for needle in ("import requests", "urllib", "OptionTrader", "submit(",
                   "client_order_id", "buy_market", "_trade(",
                   "import options\n"):
        check("no %r in the body" % needle, needle in body, False)
    check("it does not open the state db either -- optstate owns that",
          "sqlite" in body.lower(), False)

    print()
    if FAIL:
        print("FAILED: %d check(s)" % FAIL)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
