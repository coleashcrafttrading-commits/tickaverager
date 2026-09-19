#!/usr/bin/env python3
"""
options.py -- Alpaca options for Tick Avenger.

SEPARATE FROM broker.py ON PURPOSE. The live ladder runs through
`broker.Alpaca` and nothing in this file may be able to break it: options
live on a different API version prefix (`v1beta1` for data, `v2/options` for
trading), no existing engine path touches them, and a defect here should cost
a research run rather than the fleet.

WHY THIS EXISTS. On 15 Sep 2026 the account was found to be options level 3
with about $36,800 of options buying power, and the data endpoints behave very
differently from the equity ones:

  * contracts, live snapshots and live bid/ask  -- available
  * historical option BARS                      -- available
  * historical option QUOTES                    -- HTTP 404, not on this plan

That last gap is the reason this module records. Bars are useless for judging
a spread, because a bar only exists where a TRADE happened and an
out-of-the-money contract does not trade until the market moves toward it --
measured directly: every SPY put sampled 3-5% out of the money had zero bars
until days later. So the spread a seller would actually face cannot be
reconstructed from history. `record_chain` writes live quotes to disk so that
in a few weeks there IS a dataset, gathered from this account's own view of
the book.

THE OPEN QUESTION THIS IS BUILT TO ANSWER. Measured on one indicative
snapshot, out-of-the-money puts quoted 1.7% wide on PLTR and 36% wide on RAM,
which would make selling them unprofitable after crossing. But crossing is a
choice. A resting limit at or inside the mid earns the spread instead of
paying it, exactly as the equity ladder's touch-mode adds already do. Whether
those limits actually fill is an empirical question that no backtest can
answer and this module can.

NOTHING HERE PLACES AN ORDER UNLESS IT IS ARMED. `OptionTrader` refuses to
trade while `armed` is False, which is the default, and honours `state/FROZEN`
the same way the ladder does.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Any, Optional

LOG = logging.getLogger("options")

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
QUOTE_LOG = STATE_DIR / "option_quotes.jsonl"



# ------------------------------------------------------------- greeks ----
# CORRECTION (18 Sep 2026). This comment used to say Alpaca returns NO greeks
# and NO implied volatility on this plan, "verified against both feeds". It was
# not verified against both feeds so much as against one EXPIRY -- the 0DTE one
# -- which is the single case that returns nothing. Alpaca does publish greeks
# and impliedVolatility on the snapshot for every expiry except 0DTE, with
# coverage thinning as expiry nears; optdata.py holds the measured counts.
#
# What is computed below is UNCHANGED and still correct for this module: these
# rows are the recorder's dataset, they must be reproducible from the quote
# alone, and at 0DTE -- the expiry this system trades -- Alpaca gives nothing
# anyway. The preference logic lives in greeks.chain_greeks_merged, which this
# file deliberately does not use. Every greek in the dataset is therefore
# only as good as the mid it was solved from: a contract quoted
# 0.23 x 0.33 has a mid that is 36% wide, and its greeks inherit that. Rows
# carry `iv_source` so a screen can insist on a tight quote before trusting
# a number derived from it.
SQRT_2PI = math.sqrt(2.0 * math.pi)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def bs_price(spot: float, strike: float, t: float, iv: float,
             rate: float = 0.04, is_call: bool = True) -> float:
    """Black-Scholes, no dividend. `t` is in YEARS."""
    if t <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        intrinsic = (spot - strike) if is_call else (strike - spot)
        return max(0.0, intrinsic)
    sq = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t) / sq
    d2 = d1 - sq
    disc = math.exp(-rate * t)
    if is_call:
        return spot * _ncdf(d1) - strike * disc * _ncdf(d2)
    return strike * disc * _ncdf(-d2) - spot * _ncdf(-d1)


def implied_vol(price: float, spot: float, strike: float, t: float,
                rate: float = 0.04, is_call: bool = True) -> Optional[float]:
    """Bisection rather than Newton: it cannot diverge, and deep in- or
    out-of-the-money quotes have a near-zero vega that makes Newton unstable
    exactly where these chains live."""
    if price is None or price <= 0 or t <= 0 or spot <= 0:
        return None
    intrinsic = max(0.0, (spot - strike) if is_call else (strike - spot))
    if price < intrinsic - 1e-9:        # below intrinsic: no real vol solves it
        return None
    lo, hi = 1e-6, 5.0
    if bs_price(spot, strike, t, hi, rate, is_call) < price:
        return None                      # past 500% vol -- treat as unsolvable
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, mid, rate, is_call) < price:
            lo = mid
        else:
            hi = mid
    return round(0.5 * (lo + hi), 6)


def greeks(spot: float, strike: float, t: float, iv: float,
           rate: float = 0.04, is_call: bool = True) -> dict:
    """Delta, gamma, theta, vega, rho. Theta is PER DAY and vega and rho are
    per ONE PERCENTAGE POINT, because those are the units a person reasons in;
    the raw per-year, per-1.0 forms are a common source of 365x and 100x
    errors when they reach a screen."""
    if t <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return {}
    sq = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * t) / sq
    d2 = d1 - sq
    disc = math.exp(-rate * t)
    pdf = _npdf(d1)
    delta = _ncdf(d1) if is_call else _ncdf(d1) - 1.0
    gamma = pdf / (spot * sq)
    vega = spot * pdf * math.sqrt(t)
    term = -(spot * pdf * iv) / (2.0 * math.sqrt(t))
    if is_call:
        theta = term - rate * strike * disc * _ncdf(d2)
        rho = strike * t * disc * _ncdf(d2)
    else:
        theta = term + rate * strike * disc * _ncdf(-d2)
        rho = -strike * t * disc * _ncdf(-d2)
    return {"delta": round(delta, 5), "gamma": round(gamma, 6),
            "theta": round(theta / 365.0, 5), "vega": round(vega / 100.0, 5),
            "rho": round(rho / 100.0, 5), "iv": round(iv, 5)}


def years_to_expiry(expiration: str, now: Optional[_dt.date] = None) -> float:
    """Calendar years. A contract expiring today is given part of a day rather
    than zero, so its greeks are large-but-finite instead of undefined."""
    try:
        exp = _dt.date.fromisoformat(str(expiration)[:10])
    except ValueError:
        return 0.0
    today = now or _dt.date.today()
    days = (exp - today).days
    return max(days, 0.25) / 365.0


# --------------------------------------------------------------- data ----
class OptionData:
    """Read-only options access. Takes a live `broker.Alpaca` and borrows its
    session, so keys are never handled here."""

    def __init__(self, alpaca: Any):
        self.a = alpaca

    def _data(self, path: str, **kw) -> Any:
        return self.a._req("GET", f"{self.a.data}/v1beta1/options{path}", path, **kw)

    def _trade(self, method: str, path: str, **kw) -> Any:
        return self.a._req(method, f"{self.a.base}/v2{path}", path, **kw)

    # ---- contracts ----
    def contracts(self, underlying: str, *, kind: str = "", status: str = "active",
                  exp_from: str = "", exp_to: str = "",
                  strike_min: Optional[float] = None, strike_max: Optional[float] = None,
                  limit: int = 1000, max_pages: int = 20) -> list[dict]:
        """The tradable universe. `status='inactive'` lists EXPIRED contracts,
        which is the only way to reach anything historical."""
        p: dict[str, Any] = {"underlying_symbols": underlying, "status": status,
                             "limit": limit}
        if kind:
            p["type"] = kind
        if exp_from:
            p["expiration_date_gte"] = exp_from
        if exp_to:
            p["expiration_date_lte"] = exp_to
        if strike_min is not None:
            p["strike_price_gte"] = strike_min
        if strike_max is not None:
            p["strike_price_lte"] = strike_max
        # PAGINATE. Alpaca caps a page at 10,000 but defaults far lower, and a
        # truncated chain is worse than no chain: a partial read is
        # indistinguishable from "these are all the contracts there are", and
        # the slice it returns is not representative. Measured 15 Sep 2026 --
        # an unpaginated 200-contract read of SPY over a 12% band came back as
        # 200 rows that were ALL 5-6 days out and 12% out of the money, none of
        # them quoted, so the chain looked empty and the engine reported "no
        # candidates" for a reason that had nothing to do with the market.
        out: list[dict] = []
        token = ""
        for _ in range(max_pages):
            q = dict(p)
            if token:
                q["page_token"] = token
            d = self._trade("GET", "/options/contracts", params=q) or {}
            out.extend(d.get("option_contracts") or [])
            token = d.get("next_page_token") or ""
            if not token:
                return out
        LOG.warning("options.contracts: hit max_pages=%d for %s with a page "
                    "token still outstanding -- the chain is TRUNCATED",
                    max_pages, underlying)
        return out

    def snapshots(self, underlying: str, feed: str = "opra",
                  limit: int = 1000, max_pages: int = 20) -> dict:
        # Same pagination argument as contracts(): a partial snapshot map joins
        # to nothing and silently empties the chain.
        out: dict = {}
        token = ""
        for _ in range(max_pages):
            q = {"feed": feed, "limit": limit}
            if token:
                q["page_token"] = token
            d = self._data(f"/snapshots/{underlying}", params=q) or {}
            out.update(d.get("snapshots") or {})
            token = d.get("next_page_token") or ""
            if not token:
                break
        return out

    def bars(self, symbol: str, timeframe: str = "1Day", start: str = "",
             limit: int = 1000) -> list[dict]:
        p: dict[str, Any] = {"symbols": symbol, "timeframe": timeframe, "limit": limit}
        if start:
            p["start"] = start
        d = self._data("/bars", params=p) or {}
        return (d.get("bars") or {}).get(symbol) or []

    # ---- the thing we actually reason about ----
    def chain(self, underlying: str, *, kind: str = "put", spot: float = 0.0,
              pct_band: float = 0.15, exp_from: str = "", exp_to: str = "",
              feed: str = "opra", rate: float = 0.04) -> list[dict]:
        """Contracts near the money, each joined to its live quote and priced.

        `spread_pct` is of the MID, and `edge_vs_mid` is what a seller gives up
        by hitting the bid rather than resting at the mid -- half the spread.
        That number, not the raw spread, is what a premium-selling strategy
        actually pays, and it is the one to compare against the volatility risk
        premium.
        """
        lo = hi = None
        if spot > 0:
            lo, hi = spot * (1 - pct_band), spot * (1 + pct_band)
        cs = self.contracts(underlying, kind=kind, exp_from=exp_from, exp_to=exp_to,
                            strike_min=lo, strike_max=hi)
        snaps = self.snapshots(underlying, feed=feed)
        out = []
        for c in cs:
            sym = c.get("symbol")
            snap = snaps.get(sym) or {}
            q = snap.get("latestQuote") or {}
            bid, ask = q.get("bp"), q.get("ap")
            # Quote SIZE is the liquidity evidence that survives when open
            # interest does not. Measured 15 Sep 2026: of 1,107 quoted SPY put
            # rows, 339 carried NO open interest figure at all -- absent even on
            # a direct single-contract fetch, while the contract was still
            # tradable and quoting two-sided. Gate G2 accepts a quote size in
            # place of open interest precisely for that case, and could not,
            # because this function was discarding it.
            bsz, asz = q.get("bs"), q.get("as")
            strike = _f(c.get("strike_price")) or 0.0
            expiry = c.get("expiration_date")
            is_call = str(c.get("type", "")).lower().startswith("c")
            t = years_to_expiry(expiry)
            row = {
                "symbol": sym,
                "type": c.get("type"),
                "strike": strike,
                "expiration": expiry,
                "dte": round(t * 365.0, 2),
                "style": c.get("style"),
                "spot": spot or None,
                "bid": _f(bid), "ask": _f(ask),
                "bid_size": _f(bsz), "ask_size": _f(asz),
                "quote_size": min(_f(bsz) or 0.0, _f(asz) or 0.0) or None,
                "mid": None, "spread": None, "spread_pct": None, "edge_vs_mid": None,
                "iv": None, "delta": None, "gamma": None, "theta": None,
                "vega": None, "rho": None,
                "iv_source": None,
                "moneyness": round(strike / spot, 4) if spot else None,
                "oi": _f(c.get("open_interest")),
            }
            if bid is not None and ask is not None and ask > 0:
                mid = (bid + ask) / 2.0
                spr = ask - bid
                row["mid"] = round(mid, 4)
                row["spread"] = round(spr, 4)
                if mid > 0:
                    row["spread_pct"] = round(100.0 * spr / mid, 2)
                    row["edge_vs_mid"] = round(100.0 * (spr / 2.0) / mid, 2)
                # greeks are SOLVED from the mid -- Alpaca supplies none
                if spot and mid > 0 and t > 0:
                    iv = implied_vol(mid, spot, strike, t, rate, is_call)
                    if iv:
                        row.update(greeks(spot, strike, t, iv, rate, is_call))
                        row["iv_source"] = "mid"
            out.append(row)
        out.sort(key=lambda r: (str(r["expiration"]), r["strike"] or 0))
        return out

    def record_chain(self, underlying: str, *, path: Path = QUOTE_LOG,
                     ts: Optional[float] = None, **kw) -> int:
        """Append a timestamped snapshot of the chain. Append-only, like the
        journal: a quote history that can be edited afterwards is not evidence.
        Pass `ts` to stamp several calls as ONE sample; see the note below.
        Returns how many rows carried a real two-sided quote."""
        rows = self.chain(underlying, **kw)
        quoted = [r for r in rows if r["mid"] is not None]
        path.parent.mkdir(parents=True, exist_ok=True)
        # ONE stamp per SAMPLE, not per call. A sample is usually two calls --
        # puts then calls -- and stamping them separately makes a single market
        # instant look like two independent observations of it. Anything that
        # counts samples then double-counts: measured on the first real file,
        # two recorder runs produced twelve "samples" and four at-the-money
        # implied-volatility readings where there were two of each.
        stamp = time.time() if ts is None else float(ts)
        with path.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps({"ts": stamp, "underlying": underlying, **r}) + "\n")
        return len(quoted)


# ------------------------------------------------------------ trading ----
class OptionTrader:
    """Order placement. Disarmed by default and refuses to act while frozen.

    Every order is a LIMIT. There is no market-order path here on purpose: the
    entire open question is whether a resting limit fills, and a market order
    would answer it by paying the spread every time.
    """

    def __init__(self, alpaca: Any, *, armed: bool = False,
                 state_dir: Path = STATE_DIR, max_assignment: float = 0.0):
        self.a = alpaca
        self.armed = bool(armed)
        self.state_dir = Path(state_dir)
        self.max_assignment = float(max_assignment)

    def frozen(self) -> bool:
        return (self.state_dir / "FROZEN").exists()

    def _guard(self, assignment_notional: float, open_notional: float) -> None:
        if self.frozen():
            raise RuntimeError("FROZEN is set -- refusing to place an option order")
        if not self.armed:
            raise RuntimeError("OptionTrader is not armed -- refusing to place an order")
        if self.max_assignment <= 0:
            raise RuntimeError("max_assignment is not set -- refusing to size blind")
        total = open_notional + assignment_notional
        if total > self.max_assignment:
            raise RuntimeError(
                "assignment cap: this order would take committed notional to "
                "$%.0f against a cap of $%.0f" % (total, self.max_assignment))

    def sell_put_limit(self, symbol: str, qty: int, limit_price: float, *,
                       strike: float, open_notional: float = 0.0,
                       tif: str = "day") -> dict:
        """Sell to open, at a limit. `strike * 100 * qty` is what assignment
        would cost, and it is checked against the cap before anything is sent."""
        need = float(strike) * 100.0 * int(qty)
        self._guard(need, open_notional)
        body = {"symbol": symbol, "qty": str(int(qty)), "side": "sell",
                "type": "limit", "time_in_force": tif,
                "limit_price": f"{float(limit_price):.2f}"}
        LOG.info("option sell-to-open %s x%d @ %.2f (assignment $%.0f)",
                 symbol, qty, limit_price, need)
        return self.a._req("POST", f"{self.a.base}/v2/orders", "/orders", json=body)

    def buy_to_close_limit(self, symbol: str, qty: int, limit_price: float,
                           tif: str = "day") -> dict:
        """Closing reduces risk, so it is allowed while frozen -- the same way
        the ladder's exits are sacred -- but it still needs an armed trader."""
        if not self.armed:
            raise RuntimeError("OptionTrader is not armed -- refusing to place an order")
        body = {"symbol": symbol, "qty": str(int(qty)), "side": "buy",
                "type": "limit", "time_in_force": tif,
                "limit_price": f"{float(limit_price):.2f}"}
        LOG.info("option buy-to-close %s x%d @ %.2f", symbol, qty, limit_price)
        return self.a._req("POST", f"{self.a.base}/v2/orders", "/orders", json=body)


# ------------------------------------------------------------ screener ----
def screen(rows: list[dict], *, kind: str = "", dte_min: float = 0.0,
           dte_max: float = 1e9, delta_min: Optional[float] = None,
           delta_max: Optional[float] = None, iv_min: Optional[float] = None,
           iv_max: Optional[float] = None, theta_min: Optional[float] = None,
           vega_max: Optional[float] = None, gamma_max: Optional[float] = None,
           max_spread_pct: Optional[float] = None, min_mid: float = 0.0,
           min_oi: float = 0.0, require_greeks: bool = True,
           sort_by: str = "edge_vs_mid", descending: bool = False) -> list[dict]:
    """Filter a chain on any greek, plus the two things that decide whether a
    premium trade is viable at all: the spread and the open interest.

    `max_spread_pct` is not an optional nicety. Measured live on 15 Sep 2026,
    out-of-the-money puts quoted 1.7% wide on PLTR and 143% wide on NIO -- and
    the volatility risk premium being harvested is worth perhaps 10% of an
    option's value. A screen that ranks by premium and ignores the spread
    selects, by construction, for contracts whose spread is larger than their
    entire edge. Default sort is therefore by `edge_vs_mid` ascending: cheapest
    to trade first, not fattest premium first.
    """
    out = []
    for r in rows:
        if kind and str(r.get("type", "")).lower() != kind.lower():
            continue
        if require_greeks and r.get("delta") is None:
            continue
        dte = r.get("dte")
        if dte is None or dte < dte_min or dte > dte_max:
            continue
        d = r.get("delta")
        if delta_min is not None and (d is None or d < delta_min):
            continue
        if delta_max is not None and (d is None or d > delta_max):
            continue
        for key, lo, hi in (("iv", iv_min, iv_max),):
            v = r.get(key)
            if lo is not None and (v is None or v < lo):
                break
            if hi is not None and (v is None or v > hi):
                break
        else:
            if theta_min is not None and (r.get("theta") is None or r["theta"] < theta_min):
                continue
            if vega_max is not None and (r.get("vega") is None or r["vega"] > vega_max):
                continue
            if gamma_max is not None and (r.get("gamma") is None or r["gamma"] > gamma_max):
                continue
            if max_spread_pct is not None and (
                    r.get("spread_pct") is None or r["spread_pct"] > max_spread_pct):
                continue
            if (r.get("mid") or 0) < min_mid:
                continue
            if (r.get("oi") or 0) < min_oi:
                continue
            out.append(r)
            continue
        continue
    out.sort(key=lambda r: (r.get(sort_by) is None, r.get(sort_by) or 0),
             reverse=descending)
    return out


def close_threshold(entry_credit: float, spread: float, margin: float = 1.5) -> float:
    """The price to buy a short option back at, for a profit that survives the
    round trip.

    Naive profit-taking ("it went green, close it") loses money whenever the
    green is smaller than the spread, which on a cheap contract it usually is:
    sell RAM at the 0.23 bid, buy back at the 0.33 ask, and 43% of the credit
    is gone before the position has done anything. So the target is set below
    the credit by a multiple of the spread, never by a flat percentage.
    """
    return max(0.01, float(entry_credit) - float(margin) * float(spread))


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
