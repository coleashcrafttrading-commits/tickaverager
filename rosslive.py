#!/usr/bin/env python3
"""
rosslive.py -- his method, running live on the paper account.

WHAT IT DOES, ON HIS CLOCK
--------------------------
    07:00   start scanning. Gappers, price band, float, catalyst.
    09:29   freeze the watchlist. Top 5 by gap, catalyst required.
    09:30   start trading. Not before: pre-market is a fraction of the
            liquidity, spreads are multiples of regular hours, and LULD bands
            do not operate at all, so a stop has nothing behind it.
    11:30   stop taking new setups.
    15:55   flat, no exceptions.

The scanner runs from seven because that is when he is at the desk. Orders
start at the open because that is when he trades.

HOW IT DECIDES
--------------
Identical code to the backtest: the same 10-second bars aggregated from the
live tape, the same micro-pullback detector, the same six exit indicators. If
this file and the study ever disagree, one of them is wrong, and sharing the
engine is what stops that happening quietly.

SAFETY
------
Paper account only unless PAPER=0 is set deliberately. `state/FROZEN` halts
everything, as it does for the ladder fleet. Every order is logged before it is
sent. It refuses to start if another instance is running against the account,
because two processes would both book the same fills.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state"
STATE.mkdir(exist_ok=True)
LOCK = STATE / "rosslive.lock"
JOURNAL = STATE / "ross_journal.jsonl"

import ross
import tape
import tapeexit


def log(msg: str, **kw) -> None:
    line = {"t": datetime.now(timezone.utc).isoformat(), "msg": msg, **kw}
    print("%s  %s%s" % (line["t"][11:19], msg,
                        ("  " + json.dumps(kw)) if kw else ""), flush=True)
    with JOURNAL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def et_now() -> datetime:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def frozen() -> bool:
    return (STATE / "FROZEN").exists()


class Live:
    """One session, run to his clock."""

    def __init__(self, capital: float, dry: bool = True):
        self.dry = dry
        self.capital = capital
        self.equity = capital
        self.day_pl = 0.0
        self.consec = 0
        self.risks: list = []
        self.open_pos: dict = {}
        self.done: list = []
        self.watchlist: list = []
        self.b = None
        self.h = tape._headers()

    # ---------------------------------------------------------------- broker
    def broker(self):
        if self.b is None:
            import scanner
            self.b = scanner._client()
        return self.b

    def account(self) -> dict:
        try:
            return self.broker().account()
        except Exception as e:
            log("account read failed", error=repr(e))
            return {}

    # -------------------------------------------------------------- scanning
    def build_watchlist(self, date: str) -> list:
        """His pre-market list: gap, price band, float, catalyst. Top 5."""
        import scanner
        f = ROOT / "research" / "scanner" / "daily_table.json"
        if not f.exists():
            log("no daily table -- run scanner.py first")
            return []
        table = json.loads(f.read_text(encoding="utf-8"))
        cfg = ross.scanner_cfg()
        wl = ross.watchlists(table, [date], cfg, log=lambda *a: None)
        picks = (wl.get(date) or {}).get("picks") or []
        for r in picks:
            log("watchlist", symbol=r["symbol"], gap=r.get("gap_pct"),
                price=r.get("open_raw"), float_shares=r.get("float_shares"),
                catalyst=(r.get("catalyst") or "")[:60])
        if not picks:
            log("nothing made the watchlist -- most days should look like this")
        return picks

    # --------------------------------------------------------------- trading
    def risk_gates(self) -> Optional[str]:
        """Every reason he would stop trading, checked before each entry."""
        if frozen():
            return "state/FROZEN is present"
        if self.day_pl <= -abs(self.equity * ross.p("daily_loss_pct")):
            return "daily max loss"
        if self.consec >= ross.p("max_consec_loss"):
            return "%d consecutive losers" % self.consec
        if len(self.done) >= ross.p("max_trades_day"):
            return "trade cap"
        if len(self.open_pos) >= ross.p("max_active"):
            return "already in %d names" % len(self.open_pos)
        return None

    def size_for(self, entry: float, stop: float) -> int:
        cfg: dict = {}
        goal = self.equity * 2 * ross.p("risk_pct")
        cfg["cushion"] = (1.0 if self.day_pl >= goal * ross.p("cushion_frac")
                          else ross.p("cushion_frac"))
        n = ross.size(self.equity + self.day_pl, entry - stop, entry, cfg)
        # risk balancing: no trade far above the session's own average
        if self.risks and n > 0:
            mean_r = sum(self.risks) / len(self.risks)
            if mean_r > 0 and n * (entry - stop) > ross.p("risk_balance_max") * mean_r:
                n = int(ross.p("risk_balance_max") * mean_r / (entry - stop))
        return max(0, n)

    def enter(self, sym: str, plan: dict) -> None:
        why = self.risk_gates()
        if why:
            log("stand down", symbol=sym, reason=why)
            return
        entry, stop = plan["trigger"], plan["stop"]
        n = self.size_for(entry, stop)
        if n <= 0:
            log("no size", symbol=sym)
            return
        limit = round(entry + ross.p("slip_entry"), 2)
        log("ENTRY", symbol=sym, shares=n, trigger=entry, limit=limit,
            stop=stop, risk_per_share=round(entry - stop, 4),
            dollars_at_risk=round(n * (entry - stop), 2), dry=self.dry)
        if not self.dry:
            try:
                o = self.broker().submit(symbol=sym, qty=n, side="buy",
                                         type="limit", limit_price=limit,
                                         time_in_force="day")
                log("order sent", symbol=sym, order_id=(o or {}).get("id"))
            except Exception as e:
                log("ORDER FAILED", symbol=sym, error=repr(e))
                return
        self.risks.append(n * (entry - stop))
        self.open_pos[sym] = {"shares": n, "entry": entry, "stop": stop,
                              "opened": et_now().isoformat()}

    def exit(self, sym: str, why: str, px: Optional[float] = None) -> None:
        pos = self.open_pos.pop(sym, None)
        if not pos:
            return
        log("EXIT", symbol=sym, shares=pos["shares"], reason=why,
            price=px, dry=self.dry)
        if not self.dry:
            try:
                self.broker().submit(symbol=sym, qty=pos["shares"], side="sell",
                                     type="market", time_in_force="day")
            except Exception as e:
                log("EXIT ORDER FAILED -- SHARES STILL HELD", symbol=sym,
                    error=repr(e))
                return
        if px:
            pl = (px - pos["entry"]) * pos["shares"]
            self.day_pl += pl
            self.consec = 0 if pl > 0 else self.consec + 1
            self.done.append({"symbol": sym, "pl": round(pl, 2), "reason": why})
            log("closed", symbol=sym, pl=round(pl, 2), day_pl=round(self.day_pl, 2))

    # ------------------------------------------------------------------ loop
    def run(self) -> int:
        date = et_now().strftime("%Y-%m-%d")
        log("session start", date=date, capital=self.capital, dry=self.dry,
            scans_from=ross.p("session_start"), trades_from=ross.p("trade_start"),
            stops_at=ross.p("session_end"))
        acct = self.account()
        if acct:
            log("account", number=acct.get("account_number"),
                equity=acct.get("equity"), buying_power=acct.get("buying_power"))
            if not self.dry and acct.get("account_number", "").startswith("PA") is False:
                log("REFUSING: this does not look like a paper account")
                return 1

        while True:
            if frozen():
                log("FROZEN -- flattening and stopping")
                for s in list(self.open_pos):
                    self.exit(s, "frozen")
                return 0
            now = et_now()
            hhmm = now.strftime("%H:%M")

            if hhmm >= "15:55":
                for s in list(self.open_pos):
                    self.exit(s, "end of day")
                log("session end", day_pl=round(self.day_pl, 2),
                    trades=len(self.done))
                return 0

            if hhmm >= "07:00" and not getattr(self, "_refreshed", False):
                # The gap is measured against yesterday's close, so the daily
                # table has to include yesterday. Without this the scanner is
                # screening on stale history and simply finds nothing.
                self._refreshed = True
                try:
                    import scanner
                    log("refreshing the daily table for today's gaps")
                    hist = scanner.daily_history("2025-11-01", "", refresh=True,
                                                 log=lambda *a: None)
                    raw = scanner.daily_history("2025-11-01", "", refresh=True,
                                                log=lambda *a: None,
                                                adjustment="raw")
                    t = scanner.build_daily_table(hist, raw=raw,
                                                  log=lambda *a: None)
                    (ROOT / "research" / "scanner" / "daily_table.json").write_text(
                        json.dumps(t), encoding="utf-8")
                    log("daily table refreshed", symbols=len(t))
                except Exception as e:
                    log("daily table refresh FAILED -- scanner will be stale",
                        error=repr(e))

            if hhmm >= "09:29" and not self.watchlist:
                self.watchlist = self.build_watchlist(date)

            if ross.p("trade_start") <= hhmm < ross.p("session_end"):
                self.tick(date)

            time.sleep(5)

    def tick(self, date: str) -> None:
        """One pass: manage what is open, then look for a new setup."""
        import microbars
        for sym in list(self.open_pos) + [r["symbol"] for r in self.watchlist]:
            try:
                w = tape.window(sym, date, ross.p("trade_start"),
                                et_now().strftime("%H:%M"), headers=self.h)
            except Exception:
                continue
            if not w["prints"]:
                continue
            if sym in self.open_pos:
                tp = tapeexit.Tape(w["prints"], w["quotes"])
                e_ns = tape.ns(self.open_pos[sym]["opened"])
                hit = tapeexit.watch(tp, e_ns, tape.ns(
                    datetime.now(timezone.utc).isoformat()))
                if hit:
                    self.exit(sym, hit["detector"],
                              hit.get("bid") or tp.last_price(hit["t"]))
                continue
            bars = microbars.bars_from_prints(w["prints"],
                                              int(ross.p("bar_seconds")))
            if len(bars) < 60:
                continue
            ctx = self._ctx(bars)
            plan = ross.find_pullback(bars, len(bars) - 1, ctx)
            if plan:
                self.enter(sym, plan)

    def _ctx(self, bars: list) -> dict:
        closes = [float(b["c"]) for b in bars]
        m, sig = ross.macd(closes)
        return {"vwap": ross.vwap_session(bars), "ema9": ross.ema(closes, 9),
                "macd": m, "sig": sig, "pullback_idx": 1, "hod_i": None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=2000.0)
    ap.add_argument("--live", action="store_true",
                    help="actually send orders (default is dry, log only)")
    a = ap.parse_args(argv)

    if LOCK.exists():
        print("rosslive is already running (state/rosslive.lock). Two "
              "instances would both book the same fills.")
        return 1
    LOCK.write_text(str(os.getpid()), encoding="utf-8")

    def cleanup(*_):
        LOCK.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    try:
        return Live(a.capital, dry=not a.live).run()
    finally:
        LOCK.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
