#!/usr/bin/env python3
"""mockshell.py -- the WHOLE dashboard shell against mockserver's fixtures.

    .venv/Scripts/python mockshell.py --scenario hub
    .venv/Scripts/python mockshell.py --scenario hubwatch --port 8099

`mockserver.py` serves the hub routes (produced by running the REAL `hub.py`
over a stub fleet) but its own host page mounts ONE view. The ticker pages and
the Add flow live inside the shell -- the rail, the router, the account
switcher, the tab bar -- so verifying them needs `static/index.html` itself,
with `app.js` booting against those same fixtures. That is all this file adds:

  * `/` serves the REAL static/index.html and `/ui/...` the real modules, so
    the page under test is byte-for-byte the page that ships.
  * the handful of LADDER routes the shell and the Ladder tab call, which
    mockserver has no fixtures for: /api/ticker/<sym>, its trades and orders,
    /api/bars, /api/ticks, /api/presets, /api/search, /api/inspect/<sym>.
  * everything else falls through to `mockserver.Handler`, so /api/hub/*,
    /api/accounts and /api/overview are the same objects, from the same code,
    that mockserver already serves. There is no second copy of them here.

-------------------------------------------------------------------- the trap
A harness that disagrees with the real route hides bugs instead of finding
them -- mockserver's own docstring says so, and it is the reason this file
serves a REDUCED `/api/ticker/<sym>`: it carries exactly the keys the Ladder
tab reads and no others, and every one is in the unit `engine.status()` emits
(`unrealized_plpc` is PERCENT POINTS there, not a fraction; `shares` is SIGNED;
`alpaca.qty` is the broker's). A key that is missing here renders as a dash in
the browser, which is visible. A key invented here with the wrong unit would
not be, so none are.

THIS FILE PLACES NO ORDER AND HAS NO KEYS. The POST routes record what they
were sent and answer `{"ok": true}`; nothing reaches a network.
"""
from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import mockserver as MS

ROOT = MS.ROOT
STATIC = MS.STATIC

#: what the POSTs were told to do, so a click can be asserted without a broker
ACTIONS: list = []


# ----------------------------------------------------------------- the bars
def _anchor(symbol: str) -> float:
    """The price the HUB fixture reports for this symbol, so the chart and the
    tiles beside it cannot disagree. A candle series that ends somewhere the
    price tile does not is the harness contradicting itself, and a reviewer
    would go hunting in the view for a bug that was only ever in here."""
    with MS.LOCK:
        scen = MS.STATE["scenario"]
    try:
        rows = MS.hub_tickers(scen, MS.HUB_DEFAULT_ACCOUNT).get("tickers", [])
    except Exception:
        return 0.0
    for r in rows:
        if str(r.get("symbol", "")).upper() == symbol.upper():
            v = (r.get("price") or {}).get("value")
            if v:
                return float(v)
    return 0.0


def _bars(symbol: str, timeframe: str, days: float, limit: int) -> dict:
    """A deterministic OHLCV walk, ending where the hub fixture says the price
    is. Seeded off the symbol, so the same ticker draws the same chart on every
    run and two screenshots are comparable."""
    step = MS.__dict__.get("_TF_SECONDS", {}).get(timeframe) or {
        "1Min": 60, "5Min": 300, "15Min": 900, "30Min": 1800,
        "1Hour": 3600, "1Day": 86400,
    }.get(timeframe, 60)
    n = max(30, min(int(limit), int(max(0.05, days) * 86400 // step)))
    seed = sum(ord(c) for c in symbol)
    base = _anchor(symbol) or (20.0 + (seed % 90))
    end = int(time.time() // step) * step
    out = []
    for i in range(n):
        t = end - (n - 1 - i) * step
        # two sine waves and a slow drift: no randomness, so it is reproducible
        w = math.sin(i / 11.0 + seed) * (base * 0.012) \
            + math.sin(i / 47.0) * (base * 0.03) + i * (base * 0.0004)
        o = round(base + w, 2)
        c = round(base + w + math.sin(i / 3.0 + seed) * (base * 0.004), 2)
        h = round(max(o, c) + base * 0.002, 2)
        lo = round(min(o, c) - base * 0.002, 2)
        out.append({"t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)),
                    "o": o, "h": h, "l": lo, "c": c,
                    "v": 1000 + (i * 37) % 9000})
    return {"ok": True, "symbol": symbol, "timeframe": timeframe,
            "count": len(out), "feeds": ["mock"], "bars": out}


def _ticks(symbol: str, since: float) -> dict:
    now = time.time()
    bars = _bars(symbol, "1Min", 0.05, 60)["bars"]
    last = bars[-1]["c"] if bars else 10.0
    rows = [{"t": round(now - k, 3), "p": round(last * (1 + math.sin(k) * 2e-4), 4)}
            for k in range(30, -1, -2) if now - k > since]
    return {"ok": True, "symbol": symbol, "now": round(now, 3),
            "poll_seconds": 2.0, "last": rows[-1] if rows else None,
            "ticks": rows}


# --------------------------------------------------------------- the ladder
def _engine_of(scen: str, acct: str, sym: str):
    ctx, providers = MS._hub_ctx(scen, acct)
    with MS._Providers(providers):
        return dict(ctx.fleet.engines).get(sym.upper())


def _ticker_status(scen: str, acct: str, sym: str) -> dict:
    """The REDUCED engine.status(). See the module docstring: every key here is
    one the Ladder tab reads, in engine.py's own unit."""
    sym = sym.upper()
    e = _engine_of(scen, acct, sym)
    if e is None:
        raise KeyError(sym)
    s = e.summary()
    px = _bars(sym, "1Min", 0.05, 60)["bars"][-1]["c"]
    shares = e.ledger.signed_shares
    lots = []
    costs = [l.cost for l in e.ledger.open_lots]
    per = (abs(shares) / len(costs)) if costs else 0.0
    for i, cost in enumerate(costs):
        entry = round(abs(cost) / per, 4) if per else px
        lots.append({
            "id": "%s-%04d" % (sym, i), "shares": round(per, 6),
            "entry_price": entry,
            "tp_price": round(entry + (s.get("take_profit") or 0.10), 4),
            "tp_client_id": "tp-%s-%04d" % (sym, i),
            "side": "short" if shares < 0 else "long",
            "armed": False, "peak": 0.0,
            "entry_latency_ms": 180.0 + i * 7, "tp_latency_ms": 210.0 + i * 5,
        })
    cfg = {"symbol": sym, "dry_run": e.cfg.get("dry_run", True),
           "preset": e.cfg.get("preset") or "basic",
           "max_lots": s.get("max_lots"), "shares_per_lot": round(per or 1, 6),
           "take_profit": s.get("take_profit") or 0.10,
           "bar_size": "1Min", "add_mode": "fixed", "add_distance": 0.10,
           "add_depth": 3, "add_trigger": "touch", "exit_mode": "limit",
           "side_mode": "auto", "autostart": False, "fractional": "off",
           "notes": ""}
    cost_basis = round(sum(abs(c) for c in costs), 2)
    upl = s.get("unrealized")
    return {
        "symbol": sym, "state": s.get("state"),
        "running": s.get("running"), "halted": s.get("halted"),
        "halt_reason": "a stub halt, so the red banner can be looked at"
                       if s.get("halted") else "",
        "dry_run": s.get("dry_run"), "paper": True,
        "block_reason": s.get("block_reason") or "",
        "in_sync": s.get("in_sync"), "broker_qty": shares,
        "shares": shares, "avg_price": s.get("avg_price") or 0.0,
        "next_add_at": s.get("next_add_at") or 0.0,
        "anchor": {"kind": "last_fill", "price": lots[-1]["entry_price"]
                   if lots else 0.0},
        "lot_count": s.get("lot_count"), "closed_count": s.get("closed_count"),
        "realized_all": s.get("realized_all"),
        "max_exposure": round((per or 1) * (s.get("max_lots") or 0) * px, 2),
        "last_price": px, "bid": round(px - 0.01, 2), "ask": round(px + 0.01, 2),
        "last_tick_at": time.strftime("%H:%M:%S"),
        "last_bar": {"t": "", "o": px - 0.02, "c": px, "color": "green"},
        "side": "short" if shares < 0 else "long",
        "lots": lots,
        "resting_adds": [
            {"k": k, "shares": round(per or 1, 6),
             "price": round(px - 0.10 * k, 2), "side": "long", "booked": 0,
             "resting": True, "state": "working", "placed_ms": 190.0,
             "age_s": 42 * k, "coid": "en-%s-add%d" % (sym, k)}
            for k in range(1, 4)] if s.get("running") else [],
        "add_trigger": "touch", "add_depth": 3, "adds_hold": "",
        "offbook_shares": 0.0, "reconciles_this_hour": 0,
        "attention": [],
        "reconcile": {"uncovered": 0.0, "in_sync": s.get("in_sync")},
        "trend": {
            "bias": "long", "R": 1, "D": 1, "M": 1, "t15": 2.31, "S": 1.04,
            "atr15": 0.083, "bars_1m": 390,
            "stack": [
                {"name": "R regime", "timeframe": "4h",
                 "params": "EMA50 ± 0.25 ATR", "last": 1, "bias": "long",
                 "note": "close above the band"},
                {"name": "D day bias", "timeframe": "session",
                 "params": "VWAP, 5-bar hysteresis + 15m DMI", "last": 1,
                 "bias": "long", "note": ""},
                {"name": "M trend change", "timeframe": "1h",
                 "params": "SuperTrend 12 / 4.0", "last": 1, "bias": "long",
                 "note": "drives the unwind, never the entry"},
            ]},
        "events": [{"t": time.strftime("%H:%M:%S"), "level": "info",
                    "msg": "mock: nothing here reached a broker"}],
        "config": cfg,
        "account": {"id": acct, "account_number": "PA-MOCK", "label": acct},
        "alpaca": {
            "qty": shares, "avg_entry_price": s.get("avg_price") or 0.0,
            "cost_basis": cost_basis,
            "market_value": round(shares * px, 2),
            "unrealized_pl": upl if upl is not None else 0.0,
            # PERCENT POINTS, the unit engine.status() emits
            "unrealized_plpc": round(100.0 * (upl or 0.0) / cost_basis, 2)
                               if cost_basis else 0.0,
            "current_price": px,
            "orders": [{"coid": l["tp_client_id"], "side": "sell",
                        "qty": l["shares"], "filled": 0,
                        "remaining": l["shares"], "limit": l["tp_price"],
                        "status": "new", "extended_hours": False,
                        "submitted_at": "14:31:02"} for l in lots],
        },
        "pnl": {"realized_ladder": s.get("realized_today") or 0.0,
                "realized_today": s.get("realized_today") or 0.0},
    }


def _ticker_trades(scen: str, acct: str, sym: str) -> dict:
    """Entries, exits and closed pairs for the chart's markers."""
    bars = _bars(sym, "1Min", 2, 400)["bars"]
    if len(bars) < 40:
        return {"entries": [], "exits": [], "pairs": [], "open_lots": []}
    entries, exits, pairs = [], [], []
    for i, k in enumerate(range(20, len(bars) - 20, 60)):
        a, b = bars[k], bars[min(k + 25, len(bars) - 1)]
        lot = "%s-%04d" % (sym.upper(), i)
        entries.append({"t": a["t"], "price": a["l"], "side": "long",
                        "lot_id": lot, "shares": 1, "why": "rung touched"})
        exits.append({"t": b["t"], "price": b["h"], "side": "long",
                      "lot_id": lot, "shares": 1, "why": "target",
                      "realized": round(b["h"] - a["l"], 2)})
        pairs.append({"lot_id": lot, "entry_t": a["t"], "entry_price": a["l"],
                      "exit_t": b["t"], "exit_price": b["h"],
                      "win": b["h"] >= a["l"]})
    return {"entries": entries, "exits": exits, "pairs": pairs,
            "open_lots": []}


def _ticker_orders(sym: str) -> list:
    px = _bars(sym, "1Min", 0.05, 60)["bars"][-1]["c"]
    return [{"submitted_at": "2026-09-28T14:%02d:00Z" % (10 + i),
             "client_order_id": "tp-%s-%04d" % (sym.upper(), i),
             "side": "sell" if i % 2 else "buy", "type": "limit",
             "qty": 1, "filled_qty": 1 if i % 2 else 0,
             "filled_avg_price": px if i % 2 else None,
             "limit_price": round(px + 0.1, 2),
             "status": "filled" if i % 2 else "new"} for i in range(6)]


PRESETS = {"default": "basic", "presets": [
    {"id": "basic", "label": "Basic $0.10 ladder",
     "description": "First red 1-minute candle, 1 share, +1 every $0.10 down, "
                    "each lot exits $0.10 up. No filter, no flip, any hour."},
    {"id": "ladder_v3", "label": "Ladder v3",
     "description": "1-hour SuperTrend picks the side; ATR rungs; "
                    "stop-and-reverse on the flip."},
    {"id": "ladder_v3_flatten", "label": "Ladder v3 (staged unwind)",
     "description": "Ladder v3 with the researched staged flatten instead of "
                    "the reverse."},
]}

UNIVERSE = [
    ("RAM", "Aries I Acquisition Corp", "NASDAQ"),
    ("MSTX", "Defiance Daily Target 2X Long MSTR ETF", "NASDAQ"),
    ("SPY", "SPDR S&P 500 ETF Trust", "ARCA"),
    ("QQQ", "Invesco QQQ Trust", "NASDAQ"),
    ("AAPL", "Apple Inc", "NASDAQ"),
    ("NVDA", "NVIDIA Corporation", "NASDAQ"),
    ("TSLA", "Tesla Inc", "NASDAQ"),
    ("META", "Meta Platforms Inc", "NASDAQ"),
    ("T", "AT&T Inc", "NYSE"),
]


def _search(q: str, known: set) -> dict:
    q = (q or "").strip().upper()
    rows = [{"symbol": s, "name": n, "exchange": x, "in_fleet": s in known}
            for s, n, x in UNIVERSE
            if not q or q in s or q in n.upper()]
    return {"ok": True, "ready": True, "loading": False, "results": rows[:25]}


def _inspect(sym: str, known: set) -> dict:
    sym = sym.upper()
    row = next((r for r in UNIVERSE if r[0] == sym), None)
    if row is None:
        return {"ok": False, "msg": "%s is not a tradable US equity here." % sym}
    px = _bars(sym, "1Min", 0.05, 60)["bars"][-1]["c"]
    return {"ok": True, "symbol": sym, "name": row[1], "exchange": row[2],
            "price": px, "bid": round(px - 0.01, 2), "ask": round(px + 0.01, 2),
            "fractionable": sym in ("SPY", "AAPL", "NVDA", "TSLA"),
            "min_order_size": "0.001", "min_trade_increment": "0.001",
            "in_fleet": sym in known}


def _known(scen: str, acct: str) -> set:
    try:
        rows = MS.hub_tickers(scen, acct).get("tickers", [])
    except Exception:
        return set()
    return {str(r.get("symbol", "")).upper() for r in rows}


# ------------------------------------------------------------- the handler
class Handler(MS.Handler):
    """mockserver's routes, plus the shell and the ladder ones it lacks."""

    def _mine(self, p, q):
        """The response for `p`, or None to fall through to mockserver."""
        with MS.LOCK:
            scen = MS.STATE["scenario"]
        acct = MS.hub_account_of(p)
        # /api/a/<acct>/bars and /api/bars are the same route -- the unprefixed
        # form is the default account's alias in the real server. Stripping the
        # pair without putting "/api" back left "/bars", which matched nothing
        # and 404'd every chart the moment an account id was in the hash.
        bare = ("/api" + p[len("/api/a/%s" % acct):]) if p.startswith("/api/a/") else p

        if p in ("/", "/index.html"):
            return ("html", (os.path.join(STATIC, "index.html"),))
        if p.startswith("/ui/"):
            return ("file", (os.path.join(STATIC, "ui", p[len("/ui/"):]),))

        if bare == "/api/bars":
            return ("json", _bars((q.get("symbol") or ["SPY"])[0].upper(),
                                  (q.get("timeframe") or ["1Min"])[0],
                                  float((q.get("days") or [2])[0]),
                                  int((q.get("limit") or [1500])[0])))
        if bare == "/api/ticks":
            return ("json", _ticks((q.get("symbol") or ["SPY"])[0].upper(),
                                   float((q.get("since") or [0])[0])))
        if bare == "/api/indicators/custom":
            # the shell asks on boot; without it the console carries a 404 on
            # every load and "zero console errors" stops meaning anything
            return ("json", {"ready": False, "how": "",
                             "problem": "this is a mock harness with no model "
                                        "credential",
                             "fix": "nothing to fix here"})
        if bare == "/api/presets":
            return ("json", PRESETS)
        if bare == "/api/search":
            return ("json", _search((q.get("q") or [""])[0], _known(scen, acct)))
        if bare.startswith("/api/inspect/"):
            return ("json", _inspect(bare.rsplit("/", 1)[-1], _known(scen, acct)))

        if bare.startswith("/api/ticker/"):
            rest = bare[len("/api/ticker/"):].split("/")
            sym = rest[0].upper()
            tail = rest[1] if len(rest) > 1 else ""
            try:
                if tail == "trades":
                    return ("json", _ticker_trades(scen, acct, sym))
                if tail == "orders":
                    return ("json", _ticker_orders(sym))
                if not tail:
                    return ("json", _ticker_status(scen, acct, sym))
            except KeyError:
                return ("fail", (404, "%s has no ladder on this account." % sym))
        return None

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        hit = self._mine(p, q)
        if hit is None:
            return MS.Handler.do_GET(self)
        self._note(self.path)
        kind, body = hit
        if kind == "json":
            return self._json(body)
        if kind == "fail":
            return self._fail(*body)
        full = os.path.normpath(body[0])
        if not full.startswith(STATIC) or not os.path.exists(full):
            return self._fail(404, "no such file %s" % p)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if full.endswith(".js"):
            ctype = "text/javascript"
        with open(full, "rb") as fh:
            return self._send(200, fh.read(), ctype + "; charset=utf-8")

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        acct = MS.hub_account_of(p)
        bare = ("/api" + p[len("/api/a/%s" % acct):]) if p.startswith("/api/a/") else p
        if bare.startswith("/api/ticker/") or bare.startswith("/api/fleet/"):
            n = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else "{}"
            try:
                body = json.loads(raw or "{}")
            except ValueError:
                body = {"_raw": raw}
            ACTIONS.append({"path": bare, "body": body})
            self._note(self.path)
            # every one of these is a LADDER control. Nothing here reaches a
            # broker; the answer is the shape the view reads and no more.
            return self._json({"ok": True, "mock": True, "path": bare,
                               "started": 1, "stopped": 1, "disarmed": 1,
                               "placed": 0, "failed": 0, "cancelled": 0,
                               "sold": 0, "refused": []})
        return MS.Handler.do_POST(self)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--scenario", default="hub", choices=MS.SCENARIOS)
    a = ap.parse_args()
    with MS.LOCK:
        MS.STATE["scenario"] = a.scenario
    # Its OWN scratch directory, keyed by port. mockserver keeps one shared
    # path under the system temp dir and `hub_reset` rmtree's it -- with two
    # harnesses up at once (which is the normal case while several people are
    # working) one starting up deletes the directory the other is still
    # serving from, and every request after that raises FileNotFoundError.
    MS._HUB_TMP = os.path.join(os.path.dirname(MS._HUB_TMP),
                               "tickaverager-mockshell-%d" % a.port)
    MS.hub_reset()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print("mock shell on http://127.0.0.1:%d/  scenario=%s" % (a.port, a.scenario))
    print("switch with  curl -X POST http://127.0.0.1:%d/mock/scenario/<name>"
          % a.port)
    print("NO BROKER, NO KEYS, NO ORDERS.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
