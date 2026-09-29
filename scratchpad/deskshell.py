#!/usr/bin/env python3
"""deskshell.py -- SCRATCH harness for the Settings / Risk / Strategies rooms.

    .venv/Scripts/python scratchpad/deskshell.py --port 8142 --scenario banktriple

Not part of the suite and not deployed. `mockshell.py` already serves the real
shell, the real hub, the real bank (over a sandboxed copy of the four stores)
and the real perf.py. Two things it does not serve, because nothing had needed
them until these three pages were rebuilt:

  * `/api/risk` -- app.py's exposure route. Derived here from the SAME stub
    engines the hub routes read, so the donut, the concentration bars and the
    hub strategy list cannot disagree about what is held. Every key and unit is
    app.py:risk()'s.
  * `global` on `/api/overview` -- fleet.GLOBAL_DEFAULTS, which is where the
    Settings Engine tab reads every box it fills. Without it the form paints
    blank and the page verifies nothing.

NO BROKER, NO KEYS, NO ORDERS: it is mockshell's handler plus two computed
payloads. Nothing here imports app.py or broker.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import mockserver as MS          # noqa: E402
import mockshell as MSH          # noqa: E402
import fleet as FLEET            # noqa: E402


def _risk(scen: str, acct: str) -> dict:
    """app.py:risk()'s payload, off the stub fleet.

    ATR is the one figure the real route measures from bars; there are no bars
    here, so it is derived from the symbol's own price at a fixed 0.9% of it
    and that is stated in the note below rather than passed off as measured.
    A harness that invented a plausible ATR per ticker would let a reviewer
    read meaning into a number this file made up.
    """
    ctx, providers = MS._hub_ctx(scen, acct)
    with MS._Providers(providers):
        engines = dict(ctx.fleet.engines)
        p = MS.hub_portfolio(scen, acct)
    quotes = MS._HUB_QUOTES
    g = dict(FLEET.GLOBAL_DEFAULTS)
    g.update({"max_total_exposure": 40000.0, "reserve_cash": 5000.0,
              "account_daily_loss_limit": 1500.0, "max_running_tickers": 4})

    out = []
    for sym in sorted(engines):
        e = engines[sym]
        s = e.summary()
        px = float((quotes.get(sym) or {}).get("ap")
                   or (quotes.get(sym) or {}).get("bp") or 0.0)
        atr = round(px * 0.009, 4)
        maxlots = int(s.get("max_lots") or 0)
        lots = int(s.get("lot_count") or 0)
        spl = 100.0
        tp, add = 0.10, 0.10
        cost = round(sum(l.cost for l in e.ledger.open_lots), 2)
        held = abs(e.ledger.signed_shares)
        full = round(spl * maxlots * px, 2) if maxlots < 1000 else round(cost * 3, 2)
        depth = add * maxlots if maxlots < 1000 else 0.0
        out.append({
            "symbol": sym, "price": round(px, 4), "atr": atr,
            "atr_pct": round(100 * atr / px, 3) if px else 0.0,
            "take_profit": tp, "add_distance": add,
            "tp_in_atr": round(tp / atr, 2) if atr else None,
            "add_in_atr": round(add / atr, 2) if atr else None,
            "shares_per_lot": spl, "max_lots": maxlots, "lots_open": lots,
            "shares_held": held, "cost_basis": cost, "max_exposure": full,
            "used_pct": round(100 * lots / maxlots, 1) if maxlots else 0.0,
            "unrealized": s.get("unrealized") or 0.0,
            "ladder_depth": round(depth, 2),
            "ladder_depth_pct": round(100 * depth / px, 2) if px else 0.0,
            "armed": not bool(s.get("dry_run")),
            "running": bool(s.get("running")),
            "exit_mode": "limit",
            "loss_1atr": round(-atr * held, 2),
            "loss_full_ladder": round(-(depth / 2) * spl * maxlots, 2) if depth else 0.0,
        })

    def val(m):
        return (m or {}).get("value")

    equity = val(p.get("value")) or 0.0
    deployed = val(p.get("invested")) or 0.0
    return {
        "ok": True,
        "account": {
            "equity": equity, "cash": val(p.get("cash")),
            "buying_power": round((val(p.get("cash")) or 0.0) * 2, 2),
            "deployed": deployed,
            "deployed_pct": round(100 * deployed / equity, 1) if equity else 0.0,
            "open_pl": val((p.get("pl") or {}).get("open")),
            "made_today": val((p.get("pl") or {}).get("today")),
        },
        "limits": {k: g.get(k) or 0 for k in
                   ("max_total_exposure", "reserve_cash",
                    "account_daily_loss_limit", "max_running_tickers")},
        "tickers": out,
        "worst_case": round(sum(t["max_exposure"] for t in out), 2),
        "_harness_note": "ATR here is 0.9% of price, not measured from bars.",
    }


class Handler(MSH.Handler):
    """mockshell's routes, plus /api/risk and the overview's `global`."""

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        with MS.LOCK:
            scen = MS.STATE["scenario"]
        acct = MS.hub_account_of(p)
        bare = ("/api" + p[len("/api/a/%s" % acct):]) if p.startswith("/api/a/") else p

        if bare == "/api/risk":
            self._note(self.path)
            try:
                return self._json(_risk(scen, acct))
            except Exception as e:                       # noqa: BLE001
                return self._fail(500, "deskshell /api/risk: %r" % (e,))
        if bare == "/api/settings":
            self._note(self.path)
            return self._json({"global": dict(FLEET.GLOBAL_DEFAULTS),
                               "defaults": dict(FLEET.GLOBAL_DEFAULTS),
                               "supervised": True, "paper": True})
        if bare.endswith("/overview"):
            self._note(self.path)
            if scen == "hubfail":
                return self._fail(502, "mock: the overview blew up")
            ov = MS.hub_overview(scen, acct)
            # THE SETTINGS ENGINE TAB READS ov.global AND NOTHING ELSE.
            g = dict(FLEET.GLOBAL_DEFAULTS)
            g.update({"max_total_exposure": 40000.0, "reserve_cash": 5000.0,
                      "account_daily_loss_limit": 1500.0,
                      "max_running_tickers": 4})
            ov["global"] = g
            ov["supervised"] = True
            ov["feed"] = g["feed"]
            pf = ov.get("portfolio") or {}
            pf.setdefault("buying_power", 21000.0)
            pf.setdefault("deployed", pf.get("invested"))
            ov["portfolio"] = pf
            return self._json(ov)
        return MSH.Handler.do_GET(self)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8142)
    ap.add_argument("--scenario", default="banktriple", choices=MS.SCENARIOS)
    a = ap.parse_args()
    with MS.LOCK:
        MS.STATE["scenario"] = a.scenario
    MS._HUB_TMP = os.path.join(os.path.dirname(MS._HUB_TMP),
                               "tickaverager-deskshell-%d" % a.port)
    MS.hub_reset()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print("desk shell on http://127.0.0.1:%d/  scenario=%s" % (a.port, a.scenario))
    print("NO BROKER, NO KEYS, NO ORDERS.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
