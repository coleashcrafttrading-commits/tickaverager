#!/usr/bin/env python3
"""
optdata.py -- the ONE place this codebase reaches Alpaca for options data.

It is the truth about what an option contract currently costs, what it looks
like on the tape, and whether it is worth touching at all. Nothing else should
build an options URL: every trap below was paid for once, in probing time, and
encoding them here is what stops the next module paying for them again.

What was measured against account PA3ILNUY5E4F on 2026-09-18, and why the code
is shaped the way it is:

  * TWO HOSTS, TWO RATE LIMITS. Market data (https://data.alpaca.markets) is
    10000 req/min and is ours alone. The trading host
    (https://paper-api.alpaca.markets) is 200 req/min and is SHARED with the
    live share-trading fleet -- every request spent here is a request the
    ladder does not have. So chain polling goes to the data host and only the
    contract *registry* (/v2/options/contracts) touches the trading host. The
    module counts what it spent on each: see `stats()`.
  * SNAPSHOTS DO CARRY GREEKS AND IMPLIED VOLATILITY -- EXCEPT ON 0DTE.
    This module used to say the opposite, in capitals, and it was WRONG. The
    original claim came from probing exactly one expiry, 2026-09-18, which was
    the 0DTE expiry on the day it was probed; a single sample of the one case
    that returns nothing was generalised into "never, at any feed".

    Re-measured 2026-09-18 ~20:50 ET, SPY, every strike within +/-7% of spot,
    both feeds, counting contracts whose snapshot carried a `greeks` object:

        expiry        DTE   opra        indicative
        2026-09-18      0     0 / 214      0 / 214
        2026-09-21      3   120 / 192    124 / 192
        2026-09-22      4   136 / 196    138 / 196
        2026-09-23      5   140 / 192    146 / 192
        2026-09-24      6   160 / 192    154 / 192
        2026-09-30     12   208 / 214    206 / 214
        2027-06-30    285   142 / 142    142 / 142

    So coverage is total far out and thins as expiry approaches, and 0DTE is
    a hard zero on both feeds even though 116 of those 214 contracts had a
    two-sided quote. The feed barely matters; DTE is what matters.

    The gaps are not random. In every row above, the set of contracts with
    greeks is a SUBSET of the contracts with a two-sided quote, and the
    quoted-but-greekless remainder is near-zero-extrinsic ITM calls (e.g.
    SPY260921C00748000, mid 13.69 against 13.69 of intrinsic). That is the
    same rowset our own solver refuses as "iv did not solve", for the same
    reason: there is no vol in the price. Alpaca is not withholding those,
    it cannot compute them either.

    WHY 0DTE IS EMPTY, which is not the market being shut -- every other
    expiry above was measured in the same closed market. ALPACA MEASURES
    TIME-TO-EXPIRY IN WHOLE DAYS. On expiration day that is 0, their
    Black-Scholes divides by zero, and the keys are silently omitted rather
    than erroring. Alpaca staff confirmed it on their forum; no subscription
    tier changes it. That cause comes from an 8 Sep 2026 probe in another
    repo, not from the measurement above -- today's 0DTE sample was taken
    after that expiry had already expired and so cannot separate "0DTE" from
    "expired" on its own. Verify it here by probing intraday against the
    session's own 0DTE expiry if it ever matters.

    It does not matter to this code either way: `broker_greeks` is used where
    it is present and greeks.py computes where it is not. It DOES matter to
    the system, because whole-day T is the same bug our optsym.year_fraction
    exists to avoid, and it is why our 0DTE numbers are not merely a
    substitute for Alpaca's but better-founded than they would be.
  * limit=1000 IS THE CEILING on snapshots. limit=5000 is answered with
    HTTP 400 "invalid limit: larger than the allowed maximum of 1000".
  * /v2/options/contracts LIES BY OMISSION without expiration_date_gte: it
    returns ONLY the nearest expiration (SPY: 642 contracts, one date). With
    the bound it returns 12,954 across 32 expirations. `expirations()` refuses
    to issue that request without the bound -- see `_trading_get`.
  * THERE IS NO HISTORICAL OPTIONS QUOTES ENDPOINT. /v1beta1/options/quotes is
    a 404. There is no historical bid/ask for options at all, so any backtest
    fill price has to be built from trades/bars plus an assumed spread. Do not
    go looking for it again.
  * EXPIRED CONTRACTS ARE NEVER LISTED, whatever `status` is passed -- but
    historical bars and trades DO come back for a synthesized expired OCC
    symbol. `historical_chain_symbols()` is the only way to reach an expired
    chain and exists for exactly that reason.
  * open_interest lives on the TRADING host's contract rows, is null for most
    far-dated strikes, and is stamped with an open_interest_date two sessions
    back (2026-09-16 on 2026-09-18). It is therefore optional everywhere here:
    a gate that demanded it would reject the entire chain.

    from optdata import OptionData, QualityGate
    od = OptionData()                       # or OptionData(broker_alpaca)
    for e in od.expirations("SPY", max_dte=7):
        for c in od.chain("SPY", e, around=od.spot("SPY"), pct=0.05):
            ok, why = od.gate.check(c)

Every method is safe to call when the market is shut: these are all reads, and
a stale quote is answered rather than an error. Nothing here submits an order.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Expiry is a New York calendar date, so "today" for DTE purposes has to be
# today in New York -- on a Windows box at 21:00 local, UTC is already
# tomorrow and every DTE would be off by one.
DATA_HOST = "https://data.alpaca.markets"
TRADE_HOST = "https://paper-api.alpaca.markets"

SNAPSHOT_MAX_LIMIT = 1000        # 5000 is an HTTP 400, measured
BARS_MAX_LIMIT = 10000


class OptDataError(RuntimeError):
    """A request this module cannot make sense of, with what to do about it."""


# --------------------------------------------------------------- OCC symbols
def _occ_fallback(underlying: str, expiry: date, right: str, strike: float) -> str:
    """OCC 21-character symbol: ROOT + YYMMDD + C/P + strike * 1000, 8 digits.

    optsym.py owns this format for the rest of the codebase and is preferred
    when it is importable (see `_occ_builder`). This copy exists so optdata
    keeps working -- and keeps being testable -- standalone. The test asserts
    the two agree whenever optsym is present, so a drift is caught rather than
    traded on.
    """
    r = str(right).upper()[:1]
    if r not in ("C", "P"):
        raise OptDataError(f"right must be C or P, got {right!r}")
    # round, never truncate: a $0.001 float error on a 1/2-strike would shift
    # the symbol by a whole strike and quietly request a different contract
    thousandths = int(round(float(strike) * 1000))
    return f"{underlying.upper()}{expiry:%y%m%d}{r}{thousandths:08d}"


def _occ_builder() -> Callable[[str, date, str, float], str]:
    """optsym.occ when the module is there, else the local copy."""
    try:
        import optsym                                   # type: ignore
    except Exception:
        return _occ_fallback
    fn = getattr(optsym, "occ", None)
    return fn if callable(fn) else _occ_fallback


def parse_occ(symbol: str) -> dict:
    """OCC symbol -> {underlying, expiry, right, strike}.

    Parsed from the RIGHT, not the left: the root is 1-6 characters and there
    is no separator, so counting from the front guesses. The last 8 characters
    are the strike, the 9th from the end is C/P, the 6 before that the date.
    """
    s = str(symbol).strip().upper()
    if len(s) < 16:
        raise OptDataError(
            f"{symbol!r} is not an OCC option symbol (too short). "
            f"Expected e.g. SPY260918C00650000."
        )
    strike_part, right, day_part, root = s[-8:], s[-9], s[-15:-9], s[:-15]
    if right not in ("C", "P") or not strike_part.isdigit() or not day_part.isdigit():
        raise OptDataError(
            f"{symbol!r} is not an OCC option symbol. "
            f"Expected root + YYMMDD + C/P + 8-digit strike."
        )
    return {
        "underlying": root,
        "expiry": datetime.strptime(day_part, "%y%m%d").date(),
        "right": right,
        "strike": int(strike_part) / 1000.0,
    }


# ------------------------------------------------------------- quality gate
@dataclass(frozen=True)
class QualityGate:
    """Is this contract worth touching at all?

    DEFAULTS TO TUNE -- these are starting points chosen to be defensible, not
    measured optima. Tune them per underlying once there is evidence; a gate
    calibrated on SPY 0DTE will throttle a $9 small cap and vice versa.

      max_spread_pct 0.10   The spread is paid on the way in AND the way out,
                            so 10% of mid is already a ~20% round-trip tax. SPY
                            0DTE near the money runs 1-3%; 10% is the point at
                            which a wing is being quoted, not traded.
      min_mid        0.05   Under a nickel one tick is >20% of the position and
                            slippage, not the thesis, decides the trade.
      min_size       1      At least one contract quoted a side. Size 0 on
                            either side is a phantom quote.
      min_volume     10     Contracts traded TODAY, or on the previous session
                            (see `_gate_volume`) -- before 09:30 today's volume
                            is legitimately 0 and a naive volume gate would
                            reject the entire chain at the open.
      min_open_interest 25  SKIPPED ENTIRELY when open interest is unknown,
                            which on this account is most of the time: Alpaca
                            serves OI only on the trading host and nulls it for
                            most far-dated strikes. A hard OI requirement here
                            would reject every contract chain() ever returns.
    """

    max_spread_pct: float = 0.10
    min_mid: float = 0.05
    min_size: int = 1
    min_volume: int = 10
    min_open_interest: int = 25

    def check(self, c: dict) -> tuple[Optional[float], Optional[str]]:
        """-> (score 0..1, reason). reason is None when the contract passes.

        The score is returned even on a failure so a caller can rank the
        least-bad candidates when nothing passes; a caller that wants a
        yes/no should look at the reason, never at the score.
        """
        occ = c.get("occ", "?")
        mid, spct = c.get("mid"), c.get("spread_pct")
        bs, asz = c.get("bid_size"), c.get("ask_size")
        vol = _gate_volume(c)
        oi = c.get("open_interest")

        score = _tradability_score(spct, bs, asz, vol, oi)

        if mid is None:
            # one-sided or empty book; mid is None rather than 0.0 precisely so
            # this branch exists instead of something sizing off a zero price
            return score, f"{occ}: no two-sided market (bid/ask missing)"
        if mid < self.min_mid:
            return score, (f"{occ}: mid {mid:.2f} is under the {self.min_mid:.2f} "
                           f"floor -- one tick would swamp the edge")
        if spct is not None and spct > self.max_spread_pct:
            return score, (f"{occ}: spread {spct * 100:.1f}% of mid, over the "
                           f"{self.max_spread_pct * 100:.0f}% limit -- widen the "
                           f"strike search or trade a nearer expiry")
        if (bs or 0) < self.min_size or (asz or 0) < self.min_size:
            return score, (f"{occ}: quoted size {bs}x{asz} is under "
                           f"{self.min_size} -- the book is a placeholder")
        if vol is not None and vol < self.min_volume:
            return score, (f"{occ}: {vol:.0f} contracts traded today or the "
                           f"previous session, under {self.min_volume}")
        if oi is not None and oi < self.min_open_interest:
            return score, (f"{occ}: open interest {oi} under "
                           f"{self.min_open_interest} -- nobody is holding this "
                           f"strike, so an exit may have no bid")
        return score, None

    def passes(self, c: dict) -> bool:
        return self.check(c)[1] is None


DEFAULT_GATE = QualityGate()


def _gate_volume(c: dict) -> Optional[float]:
    """Today's volume, or the previous session's when today has not started.

    Deliberately the MAXIMUM of the two rather than today's alone: at 09:29 a
    perfectly liquid strike has traded 0 contracts today, and a gate that read
    that literally would refuse to trade anything at the open -- the one moment
    the 0DTE strategies actually want to act.
    """
    vals = [v for v in (c.get("volume"), c.get("prev_volume")) if v is not None]
    return max(vals) if vals else None


def _tradability_score(spread_pct: Optional[float], bid_size: Optional[float],
                       ask_size: Optional[float], volume: Optional[float],
                       open_interest: Optional[int]) -> Optional[float]:
    """0..1, higher is more tradable. None when there is nothing to score.

    A blunt weighted average, not a model: tightness of the spread is half of
    it, because the spread is the only one of these costs that is certain to
    be paid. Depth, volume and open interest each carry a sixth and saturate
    quickly -- the difference between 500 and 5000 contracts of volume does not
    change how a 1-lot fills.
    """
    if spread_pct is None and volume is None and open_interest is None:
        return None
    tight = 0.0 if spread_pct is None else max(0.0, 1.0 - min(spread_pct, 0.20) / 0.20)
    depth_raw = min(bid_size or 0, ask_size or 0)
    depth = min(1.0, depth_raw / 25.0)
    vol = 0.0 if volume is None else min(1.0, volume / 500.0)
    # unknown OI scores neutral rather than zero: it is unknown on this account
    # most of the time, and scoring it zero would rank every contract the same
    oi = 0.5 if open_interest is None else min(1.0, open_interest / 1000.0)
    return round(0.5 * tight + (depth + vol + oi) / 6.0, 4)


# ------------------------------------------------------------------- client
class OptionData:
    """Options market data, with the trading host used as sparingly as possible.

    `api` may be a broker.Alpaca (its retry/backoff `_req` and authenticated
    session are reused -- we do not want a second, dumber HTTP client in this
    process), or None, in which case credentials come from .env. `transport`
    overrides both and exists so tests can drive every code path with no
    network at all.
    """

    def __init__(self, api: Any = None, *, transport: Optional[Callable] = None,
                 ttl: float = 2.0, expiry_ttl: float = 900.0, feed: str = "opra",
                 data_url: str = "", base_url: str = "",
                 key: str = "", secret: str = "") -> None:
        # 2 s: long enough that a dashboard refresh and an engine tick in the
        # same second share one fetch, short enough that nothing trades off a
        # quote it could notice was stale. Callers that want a chain frozen for
        # a whole decision pass a bigger ttl explicitly.
        self.ttl = float(ttl)
        self.expiry_ttl = float(expiry_ttl)   # expiries change once a day
        self.feed = feed                      # "opra" is real NBBO on this account
        self.gate = DEFAULT_GATE
        self.trading_calls = 0                # the SCARCE budget (200/min, shared)
        self.data_calls = 0                   # the cheap one (10000/min)
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._occ = _occ_builder()

        self._api = api
        self._session = None
        if transport is not None:
            self._transport = transport
            self.data = (data_url or DATA_HOST).rstrip("/")
            self.base = (base_url or TRADE_HOST).rstrip("/")
            return

        if api is not None and hasattr(api, "_req"):
            # reuse broker.Alpaca: its _req already retries, backs off on 429
            # and turns a non-2xx into AlpacaError with the server's message
            self._transport = lambda m, url, path, params: api._req(
                m, url, path, params=params)
            self.data = (data_url or getattr(api, "data", DATA_HOST)).rstrip("/")
            self.base = (base_url or getattr(api, "base", TRADE_HOST)).rstrip("/")
            return

        self.data, self.base, self._session = _session_from_env(
            data_url, base_url, key, secret)
        self._transport = self._session_get

    # ------------------------------------------------------------ plumbing
    def _session_get(self, method: str, url: str, path: str,
                     params: Optional[dict]) -> Any:
        import requests
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = self._session.request(method, url, params=params, timeout=20)
            except requests.RequestException as e:      # transient network
                last = e
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                time.sleep(1.0 * (attempt + 1))
                last = OptDataError(f"HTTP 429 rate limited on {path}")
                continue
            if r.status_code == 404:
                return None                             # e.g. options/quotes
            if not r.ok:
                raise OptDataError(f"HTTP {r.status_code} on {path}: {r.text[:400]}")
            return r.json() if r.content else None
        raise last if last else OptDataError(f"no response from {path}")

    def _data_get(self, path: str, params: dict) -> Any:
        """Market-data host. 10000/min and ours alone -- poll this freely."""
        self.data_calls += 1
        return self._transport("GET", f"{self.data}{path}", path, params)

    def _trading_get(self, path: str, params: dict) -> Any:
        """Trading host. 200/min and SHARED with the live share fleet.

        Only the contract registry belongs here. Anything quote-shaped must go
        through _data_get or it is stealing requests from the ladder.
        """
        if path.endswith("/options/contracts") and "expiration_date_gte" not in params:
            # THE TRAP: without this bound Alpaca answers with only the nearest
            # expiration and says nothing about the rest, so a caller sees one
            # date and concludes the underlying has one expiry. Refused rather
            # than silently under-answered.
            raise OptDataError(
                "/v2/options/contracts without expiration_date_gte returns ONLY "
                "the nearest expiry. Pass expiration_date_gte (expirations() "
                "always does)."
            )
        self.trading_calls += 1
        return self._transport("GET", f"{self.base}{path}", path, params)

    def _cached(self, key: tuple, ttl: Optional[float], build: Callable[[], Any]) -> Any:
        ttl = self.ttl if ttl is None else float(ttl)
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
        val = build()
        if ttl > 0:
            self._cache[key] = (now + ttl, val)
        return val

    def cache_clear(self) -> None:
        self._cache.clear()

    def stats(self) -> dict:
        """What this module has spent. trading_calls is the number that matters:
        it comes out of the same 200/min the share ladder lives on."""
        return {"trading_calls": self.trading_calls, "data_calls": self.data_calls,
                "cache_entries": len(self._cache)}

    # -------------------------------------------------------------- expiries
    def expirations(self, underlying: str, min_dte: int = 0, max_dte: int = 60,
                    now: Optional[datetime] = None, pct: float = 0.15,
                    max_pages: int = 6) -> list[date]:
        """Expiration dates between min_dte and max_dte, soonest first.

        SPY and QQQ list an expiry EVERY trading day, so min_dte=0 is a real
        0DTE chain on any session.

        Costs ONE trading-host request in the normal case. The row count is
        held down by asking for calls only inside a +/-pct band around spot
        (one cheap data-host trade lookup) -- without that, 60 days of SPY is
        ~12k rows and several pages of the scarce budget. If the band comes
        back empty (a thinly-struck name), it retries once unfiltered rather
        than reporting that the underlying has no expiries.
        """
        sym = underlying.upper()
        today = (now or datetime.now(ET)).astimezone(ET).date()
        lo = today + timedelta(days=max(0, int(min_dte)))
        hi = today + timedelta(days=max(int(min_dte), int(max_dte)))
        key = ("expirations", sym, lo, hi, round(pct, 4))

        def build() -> list[date]:
            band = self._strike_band(self.spot(sym), pct)
            dates = self._expiry_scan(sym, lo, hi, band, max_pages)
            if not dates and band:
                dates = self._expiry_scan(sym, lo, hi, None, max_pages)
            return sorted(dates)

        return self._cached(key, self.expiry_ttl, build)

    def _expiry_scan(self, sym: str, lo: date, hi: date,
                     band: Optional[tuple[float, float]],
                     max_pages: int) -> set[date]:
        out: set[date] = set()
        token = ""
        for _ in range(max(1, max_pages)):
            p: dict[str, Any] = {
                "underlying_symbols": sym,
                "expiration_date_gte": lo.isoformat(),    # the guard, always
                "expiration_date_lte": hi.isoformat(),
                # calls only: every listed expiry has calls, and asking for one
                # right halves the rows for an answer that is about dates
                "type": "call",
                "limit": 1000,
            }
            if band:
                p["strike_price_gte"] = f"{band[0]:.2f}"
                p["strike_price_lte"] = f"{band[1]:.2f}"
            if token:
                p["page_token"] = token
            d = self._trading_get("/v2/options/contracts", p) or {}
            for row in d.get("option_contracts") or []:
                try:
                    out.add(date.fromisoformat(row["expiration_date"]))
                except (KeyError, TypeError, ValueError):
                    continue
            token = d.get("next_page_token") or ""
            if not token:
                break
        return out

    @staticmethod
    def _strike_band(spot: Optional[float], pct: float) -> Optional[tuple[float, float]]:
        if not spot or spot <= 0 or pct <= 0:
            return None
        return (spot * (1 - pct), spot * (1 + pct))

    def spot(self, underlying: str, ttl: Optional[float] = None) -> Optional[float]:
        """Last printed price of the UNDERLYING, from the data host.

        None rather than 0.0 when there is no trade to report -- a zero spot
        would silently centre a strike search on nothing. Uses the last trade,
        not a bar close, so it is current while the session runs and simply
        stale (not wrong) once it ends.
        """
        sym = underlying.upper()

        def build() -> Optional[float]:
            d = self._data_get(f"/v2/stocks/{sym}/trades/latest", {"feed": "sip"})
            p = ((d or {}).get("trade") or {}).get("p")
            return float(p) if p else None

        return self._cached(("spot", sym), ttl, build)

    # ----------------------------------------------------------------- chain
    def chain(self, underlying: str, expiry: date | str, around: Optional[float] = None,
              pct: float = 0.10, right: str = "", ttl: Optional[float] = None,
              max_pages: int = 4) -> list[dict]:
        """One expiry's contracts, quoted, sorted by strike then right.

        ONE request in the normal case: filtering by expiration_date plus a
        strike band of +/-pct around `around` returned 154 SPY contracts in
        0.42s. limit is pinned at 1000 because 5000 is an HTTP 400. Paging only
        happens if the filter genuinely overflows, which for a single expiry
        means the band was left wide open.

        `around` defaults to the underlying's last trade. Pass it explicitly
        (from the fleet snapshot, say) to avoid the extra data-host lookup.

        Returned dicts carry occ, underlying, expiry, strike, right, bid, ask,
        mid, bid_size, ask_size, spread, spread_pct, last, last_at, last_size,
        volume, prev_volume, day_close, prev_close, quote_at and open_interest
        (None unless enriched -- snapshots do not carry it), plus `greeks` and
        `implied_volatility` WHEN ALPACA SENDS THEM. It does for most expiries
        and never for 0DTE, thinning as expiry approaches -- measured on SPY,
        0 of 30 rows at 0DTE against 30 of 30 from 12 DTE out. This module
        passes through whatever arrived and invents nothing; greeks.py fills
        the gaps and labels which row came from where.
        """
        sym = underlying.upper()
        exp = _as_date(expiry)
        r = (right or "").upper()[:1]
        if r and r not in ("C", "P"):
            raise OptDataError(f"right must be 'C', 'P' or empty, got {right!r}")
        centre = around if around is not None else self.spot(sym)
        band = self._strike_band(centre, pct)
        key = ("chain", sym, exp, None if not band else (round(band[0], 2),
                                                         round(band[1], 2)), r)

        def build() -> list[dict]:
            rows: list[dict] = []
            token = ""
            for _ in range(max(1, max_pages)):
                p: dict[str, Any] = {
                    "feed": self.feed,
                    "limit": SNAPSHOT_MAX_LIMIT,
                    "expiration_date": exp.isoformat(),
                }
                if band:
                    p["strike_price_gte"] = f"{band[0]:.2f}"
                    p["strike_price_lte"] = f"{band[1]:.2f}"
                if r:
                    p["type"] = "call" if r == "C" else "put"
                if token:
                    p["page_token"] = token
                d = self._data_get(f"/v1beta1/options/snapshots/{sym}", p) or {}
                for occ, snap in (d.get("snapshots") or {}).items():
                    row = _contract_from_snapshot(occ, snap)
                    if row is not None:
                        rows.append(row)
                token = d.get("next_page_token") or ""
                if not token:
                    break
            rows.sort(key=lambda c: (c["strike"], c["right"]))
            return rows

        return self._cached(key, ttl, build)

    def contract(self, occ: str, ttl: Optional[float] = None) -> Optional[dict]:
        """One contract's quote, by OCC symbol. Data host, one request.

        Uses the underlying's snapshot endpoint filtered to this expiry and
        strike rather than a per-symbol endpoint, so the parsing is shared with
        chain() and there is exactly one snapshot parser in the module.
        """
        meta = parse_occ(occ)
        rows = self.chain(meta["underlying"], meta["expiry"],
                          around=meta["strike"], pct=0.001,
                          right=meta["right"], ttl=ttl)
        return next((c for c in rows if c["occ"] == occ.upper()), None)

    def open_interest(self, underlying: str, expiry: date | str,
                      ttl: float = 3600.0, max_pages: int = 4) -> dict[str, int]:
        """{occ: open interest} for one expiry -- SPENDS THE SCARCE BUDGET.

        Open interest is not in a snapshot. It only exists on the trading
        host's contract rows, so this is the one data-shaped call that has to
        use the 200/min budget; it is separate from chain() for that reason and
        is not called automatically. It is also STALE: Alpaca stamped it
        2026-09-16 on 2026-09-18, i.e. two sessions back, and returns null for
        most far-dated strikes. Treat a missing entry as unknown, never zero.

        Merge into a chain with `apply_open_interest`.
        """
        sym = underlying.upper()
        exp = _as_date(expiry)

        def build() -> dict[str, int]:
            out: dict[str, int] = {}
            token = ""
            for _ in range(max(1, max_pages)):
                p: dict[str, Any] = {
                    "underlying_symbols": sym,
                    "expiration_date_gte": exp.isoformat(),   # the guard again
                    "expiration_date_lte": exp.isoformat(),
                    "limit": 1000,
                }
                if token:
                    p["page_token"] = token
                d = self._trading_get("/v2/options/contracts", p) or {}
                for row in d.get("option_contracts") or []:
                    oi = row.get("open_interest")
                    if oi is None:
                        continue                      # unknown, not zero
                    try:
                        out[row["symbol"]] = int(oi)
                    except (KeyError, TypeError, ValueError):
                        continue
                token = d.get("next_page_token") or ""
                if not token:
                    break
            return out

        return self._cached(("oi", sym, exp), ttl, build)

    # ------------------------------------------------------------- history
    def bars(self, occ_symbols: Sequence[str], timeframe: str = "1Min",
             start: str | datetime | date = "", end: str | datetime | date = "",
             chunk: int = 100, max_pages: int = 50) -> dict[str, list[dict]]:
        """Historical option bars -> {occ: [oldest..newest]}.

        Every requested symbol gets a key, empty when it never traded (an
        expired strike nobody touched, or a synthesized symbol that never
        listed). One silent symbol must not fail the batch -- half of a
        synthesized historical chain is normally empty.

        Paged by next_page_token, never by `limit`: on the multi-symbol
        endpoints Alpaca's limit is a TOTAL row budget across symbols, so the
        first symbol can eat it and the rest come back empty with no error.
        That trap is documented in CLAUDE.md for stocks and applies identically
        here.
        """
        return self._history("/v1beta1/options/bars", "bars", occ_symbols,
                             timeframe, start, end, chunk, max_pages)

    def trades(self, occ_symbols: Sequence[str],
               start: str | datetime | date = "", end: str | datetime | date = "",
               chunk: int = 100, max_pages: int = 50) -> dict[str, list[dict]]:
        """Historical option trades -> {occ: [oldest..newest]}.

        The only historical record of what an option actually changed hands at:
        there is NO historical quotes endpoint (/v1beta1/options/quotes is a
        404), so a backtest that needs a bid/ask has to model the spread from
        these prints rather than look it up.
        """
        return self._history("/v1beta1/options/trades", "trades", occ_symbols,
                             "", start, end, chunk, max_pages)

    def _history(self, path: str, key: str, symbols: Sequence[str], timeframe: str,
                 start: Any, end: Any, chunk: int, max_pages: int
                 ) -> dict[str, list[dict]]:
        syms = [s.upper() for s in symbols if s]
        out: dict[str, list[dict]] = {s: [] for s in syms}
        if not syms:
            return out
        for i in range(0, len(syms), max(1, chunk)):
            batch = syms[i:i + max(1, chunk)]
            token = ""
            for _ in range(max(1, max_pages)):
                p: dict[str, Any] = {"symbols": ",".join(batch),
                                     "limit": BARS_MAX_LIMIT, "sort": "asc"}
                if timeframe:
                    p["timeframe"] = timeframe
                if start:
                    p["start"] = _as_iso(start)
                if end:
                    p["end"] = _as_iso(end)
                if token:
                    p["page_token"] = token
                d = self._data_get(path, p) or {}
                for sym, rows in (d.get(key) or {}).items():
                    out.setdefault(sym, []).extend(rows or [])
                token = d.get("next_page_token") or ""
                if not token:
                    break
        return out

    def historical_chain_symbols(self, underlying: str, expiry: date | str,
                                 low: float, high: float, step: float = 1.0,
                                 right: str = "") -> list[str]:
        """SYNTHESIZED OCC symbols for a strike grid -- the ONLY way to reach an
        EXPIRED chain.

        /v2/options/contracts NEVER returns an expired contract, whatever
        `status` is passed. It is not a filter that can be turned off; those
        rows are simply gone. But the historical bars and trades endpoints DO
        answer for a correctly-formed expired OCC symbol -- verified back to
        2024-02. So an expired chain is reconstructed by generating the symbols
        that WOULD have existed and asking history which of them traded.

        Consequences the caller must accept:
          * Some generated symbols never listed. They come back empty, and
            empty means "no data", never "no volume".
          * The strike grid has to be guessed. SPY/QQQ list $1 (and $0.50 near
            the money); other names go $2.50 or $5. Too fine a step is only
            wasted symbols; too coarse silently skips real strikes.
          * There is no historical bid/ask to pair with these (no quotes
            endpoint), so a backtest fill has to be modelled.
        """
        exp = _as_date(expiry)
        r = (right or "").upper()[:1]
        rights = (r,) if r in ("C", "P") else ("C", "P")
        if r and r not in ("C", "P"):
            raise OptDataError(f"right must be 'C', 'P' or empty, got {right!r}")
        if step <= 0:
            raise OptDataError("step must be positive -- it is the strike spacing, "
                               "e.g. 1.0 for SPY or 2.5 for a $50 name.")
        if high < low:
            low, high = high, low
        out: list[str] = []
        # integer thousandths throughout: 0.5-wide grids accumulate float error
        # fast, and 172.49999 would build a symbol for a strike that never was
        lo_t, hi_t, st_t = (int(round(x * 1000)) for x in (low, high, step))
        for t in range(lo_t, hi_t + 1, max(1, st_t)):
            for rr in rights:
                out.append(self._occ(underlying.upper(), exp, rr, t / 1000.0))
        return out


# --------------------------------------------------------------- snapshot io
def _contract_from_snapshot(occ: str, snap: dict) -> Optional[dict]:
    """One snapshot -> one contract dict, or None if the symbol is unreadable.

    A snapshot for a contract that has never been quoted still comes back, with
    an empty latestQuote. That is a real state (a listed strike with no market)
    and is represented as bid/ask/mid None, NOT dropped -- the caller's gate
    decides, not the parser.
    """
    try:
        meta = parse_occ(occ)
    except OptDataError:
        return None                 # not an option symbol; skip rather than raise
    q = snap.get("latestQuote") or {}
    t = snap.get("latestTrade") or {}
    day = snap.get("dailyBar") or {}
    prev = snap.get("prevDailyBar") or {}
    bg, biv = _broker_greeks(snap)

    bid, ask = _pos(q.get("bp")), _pos(q.get("ap"))
    # A 0.00 bid is not a typo, it is a strike nobody will buy back. Treating it
    # as a price would let a position be marked (and sized) at a value it can
    # never be sold for, so it is folded into "no bid" here.
    mid = round((bid + ask) / 2.0, 4) if (bid is not None and ask is not None) else None
    spread = round(ask - bid, 4) if (bid is not None and ask is not None) else None
    spread_pct = round(spread / mid, 6) if (spread is not None and mid) else None

    return {
        "occ": occ.upper(),
        "underlying": meta["underlying"],
        "expiry": meta["expiry"],
        "strike": meta["strike"],
        "right": meta["right"],
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "bid_size": _num(q.get("bs")),
        "ask_size": _num(q.get("as")),
        "spread": spread,
        "spread_pct": spread_pct,
        "quote_at": q.get("t"),
        "last": _num(t.get("p")),
        "last_size": _num(t.get("s")),
        "last_at": t.get("t"),
        "volume": _num(day.get("v")),
        "prev_volume": _num(prev.get("v")),
        "day_close": _num(day.get("c")),
        "prev_close": _num(prev.get("c")),
        # Snapshots carry no open interest -- that one really does have to
        # come from the trading host's contract rows, and filling it here
        # would put a made-up number into a gate.
        "open_interest": None,
        # What ALPACA said about this contract, kept under its own names so
        # nothing downstream can confuse it with what greeks.py worked out.
        # Both are None on 0DTE and on any strike Alpaca could not solve; see
        # the module docstring for the measured coverage. greeks.chain_greeks
        # reads exactly these two keys.
        "broker_greeks": bg,
        "broker_iv": biv,
    }


# The five Alpaca publishes, in the order the docstring lists them. There is
# no sixth: no lambda, no charm, and no vanna.
BROKER_GREEK_NAMES = ("delta", "gamma", "theta", "vega", "rho")


def _broker_greeks(snap: dict) -> tuple[Optional[dict], Optional[float]]:
    """Alpaca's own greeks and IV off one snapshot, or (None, None).

    Returns the greeks as a dict of the five names with a float or None
    against each, NOT a dict with holes -- a caller that has to ask whether a
    key exists before reading it will one day forget, and a missing delta
    that reads as absent is the whole point of the exercise.

    UNITS ARE ALPACA'S, PASSED THROUGH UNCONVERTED. Measured against our own
    solver on 116 SPY contracts at 3 DTE, theirs and ours agree to a median
    of 0.0012 in IV and -0.021 in delta, which is only possible if the units
    already match: theta per calendar day, vega per vol point, per share.
    Rescaling would show up immediately as a factor of 365 or 100 and does
    not. Do not "fix" these into our conventions -- they are already in them.
    """
    raw = snap.get("greeks")
    iv = _num(snap.get("impliedVolatility"))
    if not isinstance(raw, dict):
        return None, iv
    out = {k: _num(raw.get(k)) for k in BROKER_GREEK_NAMES}
    # An object that came back with nothing usable in it is not data. Saying
    # None here keeps "Alpaca had no opinion" as one state rather than two.
    if all(v is None for v in out.values()):
        return None, iv
    return out, iv


def apply_open_interest(rows: Iterable[dict], oi: dict[str, int]) -> list[dict]:
    """Merge an open_interest() map into chain rows, in place, and return them.

    A symbol missing from the map keeps open_interest None -- unknown. It is
    never filled with 0, because 0 reads as "nobody holds this" and the gate
    would reject a contract for a fact nobody established.
    """
    out = []
    for c in rows:
        val = oi.get(c.get("occ", ""))
        if val is not None:
            c["open_interest"] = int(val)
        out.append(c)
    return out


# -------------------------------------------------------------------- utils
def _pos(v: Any) -> Optional[float]:
    """A price that is missing, zero or negative is no price at all -> None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _as_date(v: date | str | datetime) -> date:
    if isinstance(v, datetime):
        return v.astimezone(ET).date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        raise OptDataError(
            f"{v!r} is not a date. Pass a datetime.date or 'YYYY-MM-DD'."
        ) from None


def _as_iso(v: str | date | datetime) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _session_from_env(data_url: str, base_url: str, key: str, secret: str):
    """Build an authenticated requests session from .env.

    Only used when no broker.Alpaca was handed in. Keys are read from the same
    .env the fleet uses, so there is one place they live.
    """
    import requests
    if not (key and secret):
        try:
            from dotenv import load_dotenv
            load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
        except Exception:
            pass                      # env may already be populated by the caller
        key = key or os.environ.get("APCA_API_KEY_ID", "")
        secret = secret or os.environ.get("APCA_API_SECRET_KEY", "")
    if not (key and secret):
        raise OptDataError(
            "No Alpaca credentials. Set APCA_API_KEY_ID and APCA_API_SECRET_KEY "
            "in .env, or construct OptionData(api=<broker.Alpaca>)."
        )
    data = (data_url or os.environ.get("APCA_DATA_URL") or DATA_HOST).rstrip("/")
    base = (base_url or os.environ.get("APCA_API_BASE_URL") or TRADE_HOST).rstrip("/")
    s = requests.Session()
    s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                      "accept": "application/json"})
    return data, base, s
