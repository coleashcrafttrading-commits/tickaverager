#!/usr/bin/env python3
"""
broker.py -- thin Alpaca REST client for the TickAverager ladder.

Only the endpoints the strategy actually needs. Every call raises AlpacaError
with the server's own message on a non-2xx, so failures are never silent.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

LOG = logging.getLogger("broker")


class AlpacaError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        self.status = status
        self.body = body
        self.path = path
        super().__init__(f"HTTP {status} on {path}: {body}")


class Alpaca:
    def __init__(self, key: str, secret: str, base_url: str, data_url: str, feed: str = "sip"):
        self.base = base_url.rstrip("/")
        self.data = data_url.rstrip("/")
        self.feed = feed
        self.s = requests.Session()
        self.s.headers.update({
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
            "accept": "application/json",
        })

    # ---------------- plumbing ----------------
    def _req(self, method: str, url: str, path: str, **kw) -> Any:
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                r = self.s.request(method, url, timeout=15, **kw)
            except requests.RequestException as e:
                last = e
                time.sleep(0.5 * (attempt + 1))
                continue
            if r.status_code == 429:                       # rate limited -- back off
                time.sleep(1.0 * (attempt + 1))
                last = AlpacaError(429, r.text, path)
                continue
            if r.status_code == 404:
                return None
            if not r.ok:
                raise AlpacaError(r.status_code, r.text, path)
            if not r.content:
                return None
            return r.json()
        raise last if last else RuntimeError("unreachable")

    def _trade(self, method: str, path: str, **kw) -> Any:
        return self._req(method, f"{self.base}/v2{path}", path, **kw)

    def _mkt(self, method: str, path: str, **kw) -> Any:
        return self._req(method, f"{self.data}/v2{path}", path, **kw)

    # ---------------- account / meta ----------------
    def account(self) -> dict:
        return self._trade("GET", "/account")

    def clock(self) -> dict:
        return self._trade("GET", "/clock")

    def asset(self, symbol: str) -> Optional[dict]:
        return self._trade("GET", f"/assets/{symbol}")

    def assets(self, asset_class: str = "us_equity", status: str = "active") -> list:
        """The whole tradable universe. Big (~11k rows) and slow, so the fleet
        caches it -- this is only ever called to feed the ticker search."""
        return self._trade("GET", "/assets",
                           params={"status": status, "asset_class": asset_class}) or []

    # ---------------- positions ----------------
    def position(self, symbol: str) -> Optional[dict]:
        """None when flat -- Alpaca 404s on a symbol with no position."""
        return self._trade("GET", f"/positions/{symbol}")

    def positions(self) -> list:
        """EVERY open position in one call.

        With several ladders running this replaces one request per symbol per
        tick. Alpaca's rate limit is 200/min for the whole account, not per
        symbol, so batching is what makes a multi-ticker fleet possible at all.
        """
        return self._trade("GET", "/positions") or []

    def close_position(self, symbol: str) -> Any:
        return self._trade("DELETE", f"/positions/{symbol}")

    # ---------------- orders ----------------
    def orders(self, status: str = "open", symbols: str = "", limit: int = 500,
               after: str = "") -> list:
        p: dict[str, Any] = {"status": status, "limit": limit, "direction": "desc"}
        if symbols:
            p["symbols"] = symbols
        if after:
            p["after"] = after
        return self._trade("GET", "/orders", params=p) or []

    def order_by_client_id(self, client_order_id: str) -> Optional[dict]:
        return self._trade("GET", "/orders:by_client_order_id",
                           params={"client_order_id": client_order_id})

    def submit(self, **body) -> dict:
        return self._trade("POST", "/orders", json=body)

    def cancel(self, order_id: str) -> Any:
        try:
            return self._trade("DELETE", f"/orders/{order_id}")
        except AlpacaError as e:
            if e.status in (404, 422):      # already gone / already filled
                return None
            raise

    def buy_market(self, symbol: str, qty: int, client_order_id: str) -> dict:
        return self.submit(symbol=symbol, qty=str(qty), side="buy", type="market",
                           time_in_force="day", client_order_id=client_order_id)

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> dict:
        """Market SELL. Opens a short when flat; flattens a long when not."""
        return self.submit(symbol=symbol, qty=str(qty), side="sell", type="market",
                           time_in_force="day", client_order_id=client_order_id)

    def buy_limit(self, symbol: str, qty: int, limit_price: float, client_order_id: str,
                  extended_hours: bool = False) -> dict:
        return self.submit(symbol=symbol, qty=str(qty), side="buy", type="limit",
                           limit_price=f"{limit_price:.2f}", time_in_force="day",
                           extended_hours=extended_hours,
                           client_order_id=client_order_id)


    def sell_limit(self, symbol: str, qty: int, limit_price: float, client_order_id: str,
                   extended_hours: bool = False) -> dict:
        """Day limit SELL -- short entries (or flattening into a level)."""
        return self.submit(symbol=symbol, qty=str(qty), side="sell", type="limit",
                           limit_price=f"{limit_price:.2f}", time_in_force="day",
                           extended_hours=extended_hours,
                           client_order_id=client_order_id)

    def buy_limit_gtc(self, symbol: str, qty: int, limit_price: float,
                      client_order_id: str, extended_hours: bool = False) -> dict:
        """GTC buy limit -- cover a short lot at a fixed target."""
        return self.submit(symbol=symbol, qty=str(qty), side="buy", type="limit",
                           limit_price=f"{limit_price:.2f}", time_in_force="gtc",
                           extended_hours=extended_hours,
                           client_order_id=client_order_id)

    def trailing_stop_gtc(self, symbol: str, qty: int, trail_price: float,
                          client_order_id: str, side: str = "sell",
                          extended_hours: bool = False) -> dict:
        """Rest a GTC trailing stop at the broker (dollars, not percent).

        Longs exit with side=sell; shorts cover with side=buy. trail_price is
        the dollar offset from the high (long) or low (short). If this process
        dies the order still lives at Alpaca.
        """
        return self.submit(symbol=symbol, qty=str(qty), side=side, type="trailing_stop",
                           time_in_force="gtc", trail_price=f"{float(trail_price):.2f}",
                           extended_hours=extended_hours,
                           client_order_id=client_order_id)

    def sell_limit_gtc(self, symbol: str, qty: int, limit_price: float,
                       client_order_id: str, extended_hours: bool = False) -> dict:
        """The per-lot take-profit. GTC so it outlives this process.

        extended_hours=True is what makes it eligible to fill in the overnight,
        pre-market and after-hours sessions. Without it the order simply sits
        idle outside 09:30-16:00 ET, however good the price gets.
        """
        return self.submit(symbol=symbol, qty=str(qty), side="sell", type="limit",
                           limit_price=f"{limit_price:.2f}", time_in_force="gtc",
                           extended_hours=extended_hours,
                           client_order_id=client_order_id)

    # ---------------- activities (actual booked fills) ----------------
    def activities(self, activity_type: str = "FILL", date: str = "",
                   page_size: int = 100, max_pages: int = 10) -> list:
        """The account's own record of what actually executed.

        Alpaca caps page_size at 100 and rejects anything larger outright, so a
        busy day across several ladders has to be paged through. Without this
        the fleet only ever saw the newest 100 fills and every ladder past the
        first would under-report what it had realized.
        """
        page_size = max(1, min(100, int(page_size)))
        out: list = []
        token = ""
        for _ in range(max_pages):
            p: dict[str, Any] = {"page_size": page_size, "direction": "desc"}
            if date:
                p["date"] = date
            if token:
                p["page_token"] = token
            rows = self._trade("GET", f"/account/activities/{activity_type}",
                               params=p) or []
            out.extend(rows)
            if len(rows) < page_size:
                break
            token = rows[-1].get("id", "")
            if not token:
                break
        return out

    # ---------------- market data ----------------
    def latest_quote(self, symbol: str) -> dict:
        d = self._mkt("GET", f"/stocks/{symbol}/quotes/latest", params={"feed": self.feed})
        return (d or {}).get("quote", {})

    def latest_trade(self, symbol: str) -> dict:
        d = self._mkt("GET", f"/stocks/{symbol}/trades/latest", params={"feed": self.feed})
        return (d or {}).get("trade", {})

    def latest_quotes(self, symbols: list[str]) -> dict:
        """Quotes for many symbols in ONE request -> {symbol: quote}."""
        if not symbols:
            return {}
        d = self._mkt("GET", "/stocks/quotes/latest",
                      params={"symbols": ",".join(symbols), "feed": self.feed})
        return (d or {}).get("quotes", {}) or {}

    def latest_trades(self, symbols: list[str]) -> dict:
        if not symbols:
            return {}
        d = self._mkt("GET", "/stocks/trades/latest",
                      params={"symbols": ",".join(symbols), "feed": self.feed})
        return (d or {}).get("trades", {}) or {}

    def bars(self, symbol: str, timeframe: str = "1Min", limit: int = 10) -> list:
        d = self._mkt("GET", f"/stocks/{symbol}/bars",
                      params={"timeframe": timeframe, "limit": limit,
                              "feed": self.feed, "sort": "desc", "adjustment": "raw"})
        rows = (d or {}).get("bars", []) or []
        return list(reversed(rows))          # oldest -> newest

    def bars_multi_range(self, symbols: list[str], timeframe: str, start: str,
                         end: str = "", max_pages: int = 40,
                         adjustment: str = "raw") -> dict:
        """Bars for many symbols over a date RANGE -> {symbol: [oldest..newest]}.

        Safe where the limit-based multi-symbol call is not: paging is driven by
        next_page_token over the whole range, so every symbol gets its full
        series instead of the first one eating a shared row budget.
        """
        if not symbols:
            return {}
        out: dict[str, list] = {s: [] for s in symbols}
        token = ""
        for _ in range(max_pages):
            p: dict[str, Any] = {"symbols": ",".join(symbols), "timeframe": timeframe,
                                 "start": start, "feed": self.feed, "sort": "asc",
                                 "adjustment": adjustment, "limit": 10000}
            if end:
                p["end"] = end
            if token:
                p["page_token"] = token
            d = self._mkt("GET", "/stocks/bars", params=p) or {}
            for sym, rows in (d.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows or [])
            token = d.get("next_page_token") or ""
            if not token:
                break
        return out

    def bars_range(self, symbol: str, timeframe: str, start: str, end: str = "",
                   max_pages: int = 60, adjustment: str = "raw") -> list:
        """Every bar between two timestamps, oldest first, paged through.

        The backtester needs whole days at a time, which is far past the single
        -request cap, so this follows next_page_token until the range is done.
        """
        out: list = []
        token = ""
        for _ in range(max_pages):
            p: dict[str, Any] = {"timeframe": timeframe, "start": start,
                                 "feed": self.feed, "sort": "asc",
                                 "adjustment": adjustment, "limit": 10000}
            if end:
                p["end"] = end
            if token:
                p["page_token"] = token
            d = self._mkt("GET", f"/stocks/{symbol}/bars", params=p) or {}
            out.extend(d.get("bars") or [])
            token = d.get("next_page_token") or ""
            if not token:
                break
        return out
