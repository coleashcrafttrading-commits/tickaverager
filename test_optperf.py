#!/usr/bin/env python3
"""
test_optperf.py -- the options performance overview, against ledgers we built.

Every check here pins a way a performance page lies:

  * a WIN RATE WITH NO AVERAGE LOSS beside it. Nine wins of $60 and one loss of
    $1,000 is a 90% win rate and a losing system, and the page must say both;
  * a RATE OVER FOUR TRADES printed as if it were a rate. Thin samples are
    marked and carry a Wilson interval wide enough to see;
  * REALIZED AND OPEN ADDED TOGETHER. A book with a take-profit looks brilliant
    on realized alone right up until the open bag is counted;
  * a POSITION THAT NEVER OPENED counted as a trade. A 422 refusal is a log
    line, not a flat trade, and padding the denominator with them manufactures
    a win rate;
  * an ADOPTED position judged on thresholds that were never set for it;
  * a PARTIAL FILL measured at the size we asked for rather than the size the
    broker confirmed -- the same mistake that sends a closing order bigger than
    the position;
  * 0.0 STANDING IN FOR "not measured". A zero is a claim.

It also pins the assignment arithmetic that stopped SPY and QQQ from ever
opening: a cash-secured short put is measured GROSS, a vertical is measured
NET of the long that protects it, and a ratio spread is measured as both.

    .venv/Scripts/python test_optperf.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

os.environ.setdefault("TICKAVERAGER_JOURNAL", os.path.join(
    tempfile.gettempdir(), "tickaverager_test_journal.jsonl"))

import optperf as OP
import optplaybook as PB
import optplays as P

FAIL = 0
DAY = 86400.0
T0 = 1790000000.0          # a fixed epoch so every duration is exact


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def approx(name, got, want, tol=1e-6) -> None:
    global FAIL
    ok = got is not None and abs(got - want) <= tol
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want}+-{tol}")


# =========================================================== ledger building
class Build:
    """A synthetic play ledger written the way optplaybook writes one.

    Deliberately assembled from EVENTS rather than from finished positions:
    the module has to replay the same stream the worker appends, including the
    ones that only exist in the middle of a trade.
    """

    def __init__(self, path):
        self.path = path
        self.t = T0
        open(path, "w", encoding="utf-8").close()

    def ev(self, pid, event, ts=None, **fields):
        row = {"ts": float(self.t if ts is None else ts),
               "at": "", "id": pid, "event": event, "fields": fields}
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    # --- the shapes the worker actually appends ---------------------------
    def opening(self, pid, symbol, play, kind, legs, *, requested=1,
                entry_net=-1.0, expiry="2026-10-30", target=None, stop=None,
                ts=None):
        self.ev(pid, "opening", ts=ts, symbol=symbol, play=play, kind=kind,
                expiry=expiry, state="pending", requested=requested,
                contracts=0, entry_net=entry_net, entry_at="",
                target_px=target, stop_px=stop, legs=legs)

    def filled(self, pid, contracts, ts):
        self.ev(pid, "filled", ts=ts, state="open", contracts=contracts)

    def rested(self, pid, oid="o-1", ts=None):
        self.ev(pid, "target_rested", ts=ts, rest_order_id=oid)

    def marked(self, pid, mark, pl, ts, pct=None):
        self.ev(pid, "marked", ts=ts, mark=mark, pl=pl, pl_pct=pct)

    def closed(self, pid, reason, ts, close_net=None):
        f = {"state": "closed", "closed_at": "", "close_reason": reason,
             "contracts": 0}
        if close_net is not None:
            f["close_net"] = close_net
        self.ev(pid, "closed", ts=ts, **f)

    def refused(self, pid, why, ts):
        self.ev(pid, "refused", ts=ts, state="closed", closed_at="",
                close_reason="not placed: %s" % why)

    def adopted(self, pid, occ, symbol, contracts, ts, *, side="buy",
                strike=100.0, right="call", expiry="2026-10-30"):
        self.ev(pid, "adopted", ts=ts, symbol=symbol, play="(adopted)",
                kind="monitored", expiry=expiry, state="open",
                contracts=contracts, requested=contracts, adopted=True,
                legs=[{"symbol": occ, "right": right, "strike": strike,
                       "side": side, "entry_px": 2.0}])


def LONG(occ="AAPL261030C00340000", strike=340.0, right="call"):
    return [{"symbol": occ, "right": right, "strike": strike, "side": "buy",
             "entry_px": 11.72}]


def SPREAD(short_k=741.0, long_k=739.0, ct_sym="SPY"):
    return [{"symbol": "%s261030P%08d" % (ct_sym, int(short_k * 1000)),
             "right": "put", "strike": short_k, "side": "sell",
             "entry_px": 2.10},
            {"symbol": "%s261030P%08d" % (ct_sym, int(long_k * 1000)),
             "right": "put", "strike": long_k, "side": "buy",
             "entry_px": 1.80}]


class Pos:
    """Anything position-shaped. `assignment_exposure` and `position_risk`
    read attributes, so a structure can be stated here in six lines without a
    ledger behind it."""

    def __init__(self, legs, contracts=1, entry_net=0.30,
                 kind=P.CREDIT_SPREAD, expiry="2026-10-30"):
        self.legs = legs
        self.contracts = contracts
        self.entry_net = entry_net
        self.kind = kind
        self.expiry = expiry


def tmp(name):
    return os.path.join(tempfile.gettempdir(), "optperf_%s.jsonl" % name)


def rep(path, **kw):
    kw.setdefault("now", T0 + 10 * DAY)
    kw.setdefault("decisions_path", tmp("nodecisions"))
    return OP.report(ledger_path=path, **kw)


def main() -> int:
    print("\n1. An empty ledger is a page of dashes, not a page of zeros")
    p = tmp("empty")
    Build(p)
    r = rep(p)
    check("it still renders", r["ok"], True)
    check("no positions", r["counts"]["positions"], 0)
    check("realized is None, not 0.0", r["pl"]["realized"]["value"], None)
    check("...and says why", bool(r["pl"]["realized"]["reason"]), True)
    check("open P/L is None", r["pl"]["open"]["value"], None)
    check("total is None", r["pl"]["total"]["value"], None)
    check("win rate is None", r["outcomes"]["win_rate"]["value"], None)
    check("...with n=0", r["outcomes"]["win_rate"]["n"], 0)
    check("expectancy is None", r["outcomes"]["expectancy"]["value"], None)
    check("profit factor is None", r["outcomes"]["profit_factor"]["value"], None)
    check("the Wilson interval is not [0,0]",
          r["outcomes"]["win_rate_lo"]["value"], None)
    check("capital at risk with nothing open really is zero",
          r["risk"]["at_risk"]["value"], 0.0)
    check("...but the ceiling is unknown, not unlimited",
          r["risk"]["ceiling"]["value"], None)
    check("...and says so", "never inferred" in
          (r["risk"]["ceiling"]["reason"] or ""), True)
    check("no exits", r["exits"]["n"], 0)
    check("nothing needs attention", r["attention"], [])

    print("\n2. Only open trades: realized stays None while open P/L is real")
    p = tmp("onlyopen")
    b = Build(p)
    b.opening("A-1", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72, target=17.58, stop=8.79)
    b.filled("A-1", 1, T0 + 60)
    b.rested("A-1")
    b.marked("A-1", 13.0, 128.0, T0 + 10 * DAY - 30)
    r = rep(p)
    check("one position", r["counts"]["positions"], 1)
    check("it is open", r["counts"]["open"], 1)
    check("nothing is judged", r["counts"]["judged"], 0)
    check("realized is None", r["pl"]["realized"]["value"], None)
    check("open P/L is the mark", r["pl"]["open"]["value"], 128.0)
    check("total equals the open leg alone", r["pl"]["total"]["value"], 128.0)
    check("...and says the other half is missing",
          r["pl"]["total"]["reason"], "no closed trade yet")
    check("open P/L basis is the mark",
          r["positions"][0]["open_pl_basis"], "mark")
    check("win rate is still None", r["outcomes"]["win_rate"]["value"], None)
    check("capital at risk is the debit", r["risk"]["at_risk"]["value"], 1172.0)
    check("it has a resting exit, so no alarm", r["attention"], [])
    check("age is measured from the FILL, not the request",
          r["positions"][0]["age_days"], round((10 * DAY - 60) / DAY, 4))

    print("\n3. Only losses: no profit factor, and the loss is the headline")
    p = tmp("onlyloss")
    b = Build(p)
    for i, loss in enumerate((-293.0, -140.0, -1012.0)):
        pid = "L-%d" % i
        b.opening(pid, "META", "swing-atm-hourly", P.LONG_SINGLE,
                  LONG("META261030P00750000", 750.0, "put"),
                  entry_net=-abs(loss) * 4 / 100.0)
        b.filled(pid, 1, T0 + i * DAY)
        b.marked(pid, 1.0, loss, T0 + (i + 1) * DAY - 10)
        b.closed(pid, "stop: sell at 3.00 <= 3.04", T0 + (i + 1) * DAY)
    r = rep(p)
    o = r["outcomes"]
    check("three judged trades", r["counts"]["judged"], 3)
    check("win rate is 0.0 -- a measurement, not a dash", o["win_rate"]["value"], 0.0)
    check("...and it is flagged thin", o["win_rate"]["thin"], True)
    check("...with n on it", o["win_rate"]["n"], 3)
    check("the Wilson upper bound is nowhere near 0",
          o["win_rate_hi"]["value"] > 0.5, True)
    check("avg_win is None, not 0", o["avg_win"]["value"], None)
    check("avg_loss is POSITIVE by contract", o["avg_loss"]["value"], 481.67)
    check("win/loss ratio needs one of each",
          o["win_loss_ratio"]["value"], None)
    check("profit factor refuses to be computed",
          o["profit_factor"]["value"], None)
    check("...and says a profit factor needs a loss AND a win",
          "no winning trade yet" in (o["profit_factor"]["reason"] or ""), True)
    check("expectancy is the mean loss", o["expectancy"]["value"], -481.67)
    check("largest loss", o["largest_loss"]["value"], -1012.0)
    check("realized is the sum", r["pl"]["realized"]["value"], -1445.0)
    check("every exit was a stop",
          [m["n"] for m in r["exits"]["mix"] if m["class"] == OP.EXIT_STOP][0], 3)

    print("\n4. The lie this page exists to stop: 90% wins, negative expectancy")
    p = tmp("lie")
    b = Build(p)
    for i in range(9):
        pid = "W-%d" % i
        b.opening(pid, "NVDA", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
                  entry_net=-1.0)
        b.filled(pid, 1, T0 + i * 3600)
        b.marked(pid, 1.6, 60.0, T0 + i * 3600 + 60)
        b.closed(pid, "profit target: sell at 1.60 >= 1.50", T0 + i * 3600 + 120)
    b.opening("X-1", "NVDA", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-10.0)
    b.filled("X-1", 1, T0 + 10 * 3600)
    b.marked("X-1", 0.0, -1000.0, T0 + 10 * 3600 + 60)
    b.closed("X-1", "stop: sell at 0.00 <= 7.50", T0 + 10 * 3600 + 120)
    r = rep(p)
    o = r["outcomes"]
    check("win rate reads 90%", o["win_rate"]["value"], 0.9)
    check("but expectancy is negative", o["expectancy"]["value"] < 0, True)
    approx("expectancy in dollars", o["expectancy"]["value"], -46.0, 0.01)
    check("avg_win", o["avg_win"]["value"], 60.0)
    check("avg_loss dwarfs it", o["avg_loss"]["value"], 1000.0)
    check("win/loss ratio says it in one number",
          o["win_loss_ratio"]["value"], 0.06)
    check("profit factor is below 1", o["profit_factor"]["value"] < 1.0, True)
    check("ten trades is still flagged thin", o["win_rate"]["thin"], True)
    check("...because the bar is %d" % OP.MIN_TRADES_FOR_RATE,
          o["sample"]["min_for_rate"], OP.MIN_TRADES_FOR_RATE)
    mix = {m["class"]: m["n"] for m in r["exits"]["mix"]}
    check("nine profit targets", mix[OP.EXIT_PROFIT], 9)
    check("one stop", mix[OP.EXIT_STOP], 1)

    print("\n5. A refusal is not a flat trade")
    p = tmp("refused")
    b = Build(p)
    b.opening("R-1", "SPY", "index-put-credit-spread", P.CREDIT_SPREAD,
              SPREAD(), requested=10, entry_net=0.30)
    b.refused("R-1", "position intent mismatch", T0 + 60)
    b.opening("R-2", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("R-2", 1, T0 + 120)
    b.marked("R-2", 17.60, 588.0, T0 + DAY)
    b.closed("R-2", "profit target: sell at 17.60 >= 17.58", T0 + DAY + 1)
    r = rep(p)
    check("two ledger rows", r["counts"]["positions"], 2)
    check("one refused", r["counts"]["refused"], 1)
    check("only one is judged", r["counts"]["judged"], 1)
    check("win rate is 1.0 over ONE trade, not 0.5 over two",
          r["outcomes"]["win_rate"]["value"], 1.0)
    check("...and n says one", r["outcomes"]["win_rate"]["n"], 1)
    check("realized counts only the real trade",
          r["pl"]["realized"]["value"], 588.0)
    check("the refusal is not in any exit bucket", r["exits"]["n"], 1)
    check("...and not in the ticker breakdown",
          [row["key"] for row in r["by_ticker"]], ["AAPL"])
    check("it has no risk", [t for t in r["positions"]
                             if t["id"] == "R-1"][0]["risk"], None)
    check("...for a stated reason", [t for t in r["positions"]
                                     if t["id"] == "R-1"][0]["risk_reason"],
          "never filled")

    print("\n6. A partial fill is measured at what the BROKER confirmed")
    p = tmp("partial")
    b = Build(p)
    b.opening("P-1", "SPY", "index-put-credit-spread", P.CREDIT_SPREAD,
              SPREAD(), requested=10, entry_net=0.30)
    b.filled("P-1", 4, T0 + 60)
    b.rested("P-1", "rest-4")
    b.marked("P-1", 0.28, 8.0, T0 + 9 * DAY)
    r = rep(p)
    t = r["positions"][0]
    check("requested was ten", t["requested"], 10)
    check("size is the four that filled", t["size"], 4)
    check("it is flagged as partial", t["partial"], True)
    # width 2.00 less the 0.30 credit, times 100, times FOUR contracts
    check("risk is sized on four, not ten", t["risk"], 680.0)
    check("capital at risk agrees", r["risk"]["at_risk"]["value"], 680.0)
    codes = [a["code"] for a in r["attention"]]
    check("the partial fill is surfaced", "partial_fill" in codes, True)
    check("assignment is netted to the width on four",
          r["assignment"]["net_of_hedge"]["value"], 800.0)
    check("...while gross would be the whole strike",
          r["assignment"]["gross"]["value"], 296400.0)

    print("\n7. An adopted position was never ours to judge")
    p = tmp("adopted")
    b = Build(p)
    b.adopted("adopted-TSLA", "TSLA261030P00400000", "TSLA", 2, T0,
              side="buy", strike=400.0, right="put")
    b.marked("adopted-TSLA", 5.0, None, T0 + 9 * DAY)
    b.opening("O-1", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("O-1", 1, T0 + 60)
    b.marked("O-1", 17.60, 588.0, T0 + DAY)
    b.closed("O-1", "profit target: sell at 17.60 >= 17.58", T0 + DAY + 1)
    r = rep(p)
    check("one adopted", r["counts"]["adopted"], 1)
    check("the adopted row is open", r["counts"]["open"], 1)
    check("only the play we opened is judged", r["counts"]["judged"], 1)
    ad = [t for t in r["positions"] if t["adopted"]][0]
    check("its open P/L is None", ad["open_pl"], None)
    check("...for the right reason",
          "no entry of ours" in (ad["open_pl_reason"] or ""), True)
    check("realized is the one real trade", r["pl"]["realized"]["value"], 588.0)
    check("open P/L has nothing measurable in it",
          r["pl"]["open"]["value"], None)
    codes = [a["code"] for a in r["attention"]]
    check("it is listed as adopted", "adopted" in codes, True)
    sev = [a["severity"] for a in r["attention"] if a["code"] == "adopted"][0]
    check("...as information, not an alarm", sev, "info")
    check("a long adopted position still has a bounded risk",
          ad["risk"], 400.0)

    print("\n8. An adopted SHORT has no bounded risk and will not say 0")
    p = tmp("adoptshort")
    b = Build(p)
    b.adopted("adopted-SPY", "SPY261030P00700000", "SPY", 1, T0,
              side="sell", strike=700.0, right="put")
    b.marked("adopted-SPY", 2.0, None, T0 + 9 * DAY)
    r = rep(p)
    t = r["positions"][0]
    check("risk is None", t["risk"], None)
    check("...and names the hole", "nothing here bounds the loss"
          in (t["risk_reason"] or ""), True)
    check("the total is flagged as a floor", r["risk"]["at_risk"]["thin"], True)
    check("one position could not be bounded", r["risk"]["unbounded"], 1)
    check("a naked short is measured GROSS",
          r["assignment"]["gross"]["value"], 70000.0)
    check("...and the net is the same, because nothing hedges it",
          r["assignment"]["net_of_hedge"]["value"], 70000.0)
    check("one uncovered contract", r["assignment"]["uncovered_contracts"], 1)

    print("\n9. Assignment: gross for the cash-secured, net for the vertical")
    # This is the arithmetic that refused every SPY and QQQ proposal all day.
    csp = Pos([{"symbol": "SPY261030P00741000", "right": "put",
                "strike": 741.0, "side": "sell"}], contracts=10,
              entry_net=2.10)
    e = OP.assignment_exposure(csp)
    check("a cash-secured put is still 741 x 100 x 10", e["gross"], 741000.0)
    check("...and nothing nets it down", e["net"], 741000.0)
    check("...it is uncovered", e["uncovered"], 10)

    e = OP.assignment_exposure(Pos(SPREAD(), contracts=10))
    check("the vertical's gross is unchanged", e["gross"], 741000.0)
    check("...but its true exposure is the $2 width", e["net"], 2000.0)
    check("...nothing is uncovered", e["uncovered"], 0)
    check("...and the hedge is not claimed to be free",
          "overnight" in OP.HEDGE_ASSUMPTION, True)

    print("\n10. A 1x2 ratio: the covered half nets, the extra short does not")
    ratio = Pos([
        {"symbol": "SPY261030P00741000", "right": "put", "strike": 741.0,
         "side": "sell"},
        {"symbol": "SPY261030P00740000", "right": "put", "strike": 740.0,
         "side": "sell"},
        {"symbol": "SPY261030P00739000", "right": "put", "strike": 739.0,
         "side": "buy"}])
    e = OP.assignment_exposure(ratio)
    check("gross is both shorts", e["gross"], 148100.0)
    # The one long protects ONE of the two shorts. It is matched to the nearest
    # protecting strike (740), leaving the 741 naked at its full strike.
    check("one short nets to its width, the other stays gross",
          e["net"], 74200.0)
    check("one contract is uncovered", e["uncovered"], 1)

    print("\n11. A long that does not protect is not a hedge")
    bad = Pos([
        {"symbol": "SPY261030P00741000", "right": "put", "strike": 741.0,
         "side": "sell"},
        # ABOVE the short put: it does not cap the downside at all.
        {"symbol": "SPY261030P00745000", "right": "put", "strike": 745.0,
         "side": "buy"}])
    e = OP.assignment_exposure(bad)
    check("a long put above the short does not net it", e["net"], 74100.0)
    check("...and it is counted uncovered", e["uncovered"], 1)
    call = Pos([
        {"symbol": "SPY261030C00741000", "right": "call", "strike": 741.0,
         "side": "sell"},
        {"symbol": "SPY261030P00739000", "right": "put", "strike": 739.0,
         "side": "buy"}])
    e = OP.assignment_exposure(call)
    check("a put does not hedge a short CALL", e["uncovered"], 1)

    print("\n12. Open with no resting exit is the loudest thing on the page")
    p = tmp("noexit")
    b = Build(p)
    b.opening("N-1", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72, target=17.58, stop=8.79)
    b.filled("N-1", 1, T0 + 60)
    b.marked("N-1", 12.0, 28.0, T0 + 10 * DAY - 30)
    r = rep(p)
    a = r["attention"][0]
    check("it is critical", a["severity"], "critical")
    check("with the code the UI keys on", a["code"], "no_resting_exit")
    check("the position says it has no resting exit",
          r["positions"][0]["has_resting_exit"], False)

    print("\n13. A refused resting exit is a known state, not an alarm")
    p = tmp("restrefused")
    b = Build(p)
    b.opening("N-2", "SPY", "index-put-credit-spread", P.CREDIT_SPREAD,
              SPREAD(), requested=10, entry_net=0.30)
    b.filled("N-2", 10, T0 + 60)
    b.ev("N-2", "rest_refused", ts=T0 + 61,
         rest_refused="422 position intent mismatch")
    b.marked("N-2", 0.28, 20.0, T0 + 10 * DAY - 30)
    r = rep(p)
    codes = {a["code"]: a["severity"] for a in r["attention"]}
    check("no silent hole", "no_resting_exit" in codes, False)
    check("the refusal is surfaced", codes.get("rest_refused"), "warn")
    check("...and says who owns the target",
          "the loop owns the target" in
          [a["message"] for a in r["attention"]
           if a["code"] == "rest_refused"][0], True)

    print("\n14. A position with no mark is not a position with no move")
    p = tmp("nomark")
    b = Build(p)
    b.opening("M-1", "GOOGL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("M-1", 1, T0 + 60)
    b.rested("M-1")
    r = rep(p)
    t = r["positions"][0]
    check("open P/L is None", t["open_pl"], None)
    check("...not 0.0", t["open_pl"] is not None, False)
    check("...and says no stop can trip",
          "no profit target and no stop can trip" in (t["open_pl_reason"] or ""),
          True)
    codes = {a["code"]: a["severity"] for a in r["attention"]}
    check("it is critical on the page", codes.get("no_mark"), "critical")
    check("the report warns that open P/L is incomplete",
          any(w["code"] == "unpriced_open" for w in r["warnings"]), True)
    check("the headline open P/L is None", r["pl"]["open"]["value"], None)

    print("\n15. A stale mark is called stale")
    p = tmp("stalemark")
    b = Build(p)
    b.opening("S-1", "MSFT", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("S-1", 1, T0 + 60)
    b.rested("S-1")
    b.marked("S-1", 12.0, 28.0, T0 + 10 * DAY - 600)
    r = rep(p)
    codes = [a["code"] for a in r["attention"]]
    check("stale is flagged", "stale_mark" in codes, True)
    approx("with the age in seconds", r["positions"][0]["mark_age_s"], 600.0, 0.5)
    fresh = rep(p, now=T0 + 10 * DAY - 590)
    check("a fresh mark is not flagged",
          "stale_mark" in [a["code"] for a in fresh["attention"]], False)

    print("\n16. Realized: booked beats estimated, and the split is shown")
    p = tmp("booked")
    b = Build(p)
    # Bought for 11.72, sold for 17.58. close_net is a CREDIT: positive.
    b.opening("B-1", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("B-1", 1, T0 + 60)
    b.marked("B-1", 17.40, 568.0, T0 + DAY)
    b.closed("B-1", "profit target: sell at 17.58 >= 17.58", T0 + DAY + 1,
             close_net=17.58)
    # Sold a spread for 0.30, bought it back for 0.15. close_net is a DEBIT.
    b.opening("B-2", "SPY", "index-put-credit-spread", P.CREDIT_SPREAD,
              SPREAD(), requested=10, entry_net=0.30)
    b.filled("B-2", 10, T0 + 60)
    b.marked("B-2", 0.16, 140.0, T0 + DAY)
    b.closed("B-2", "profit target: buy back at 0.15 <= 0.15", T0 + DAY + 2,
             close_net=-0.15)
    # No close_net at all: the estimate path.
    b.opening("B-3", "META", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-40.50)
    b.filled("B-3", 1, T0 + 60)
    b.marked("B-3", 30.0, -1050.0, T0 + DAY)
    b.closed("B-3", "stop: sell at 30.37 <= 30.37", T0 + DAY + 3)
    r = rep(p)
    approx("the long's booked P/L", [t for t in r["positions"]
                                     if t["id"] == "B-1"][0]["realized"],
           586.0, 0.01)
    check("...is booked, not estimated", [t for t in r["positions"]
                                          if t["id"] == "B-1"][0]["realized_basis"],
          "booked")
    approx("the spread's booked P/L", [t for t in r["positions"]
                                       if t["id"] == "B-2"][0]["realized"],
           150.0, 0.01)
    check("the third falls back to the last mark",
          [t for t in r["positions"] if t["id"] == "B-3"][0]["realized_basis"],
          "estimated")
    check("booked and estimated are reported apart",
          r["pl"]["realized_booked"]["value"], 736.0)
    check("...", r["pl"]["realized_estimated"]["value"], -1050.0)
    approx("estimated share of the trades",
           r["pl"]["estimated_share"]["value"], 1 / 3.0, 0.001)
    check("realized is the sum of both", r["pl"]["realized"]["value"], -314.0)

    print("\n17. Every realized number estimated is a warning, not a footnote")
    p = tmp("allest")
    b = Build(p)
    b.opening("E-1", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("E-1", 1, T0 + 60)
    b.marked("E-1", 17.60, 588.0, T0 + DAY)
    b.closed("E-1", "profit target: sell at 17.60 >= 17.58", T0 + DAY + 1)
    r = rep(p)
    check("the report says so",
          any(w["code"] == "realized_is_estimated" for w in r["warnings"]), True)
    check("...and the metric is marked thin", r["pl"]["realized"]["thin"], True)

    print("\n18. The broker's unrealized P/L outranks our mark")
    p = tmp("broker")
    b = Build(p)
    b.opening("K-1", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("K-1", 1, T0 + 60)
    b.rested("K-1")
    b.marked("K-1", 12.0, 28.0, T0 + 10 * DAY - 30)
    bp = [{"symbol": "AAPL261030C00340000", "qty": "1", "side": "long",
           "unrealized_pl": "41.50", "asset_class": "us_option"}]
    r = rep(p, broker_positions=bp)
    check("open P/L is the broker's", r["pl"]["open"]["value"], 41.5)
    check("...and says so", r["positions"][0]["open_pl_basis"], "broker")
    check("no 'stale mark' warning when a snapshot was given",
          any(w["code"] == "no_broker_snapshot" for w in r["warnings"]), False)
    # A size the broker does not agree with must NOT be attributed.
    bp2 = [{"symbol": "AAPL261030C00340000", "qty": "3", "side": "long",
            "unrealized_pl": "124.50", "asset_class": "us_option"}]
    r2 = rep(p, broker_positions=bp2)
    check("a size mismatch falls back to the mark",
          r2["positions"][0]["open_pl_basis"], "mark")
    check("...loudly", any(w["code"] == "size_mismatch" for w in r2["warnings"]),
          True)

    print("\n19. Holding periods come from the fill, not the request")
    p = tmp("hold")
    b = Build(p)
    for i, (fill, close) in enumerate(((T0, T0 + 2 * DAY),
                                       (T0, T0 + 4 * DAY),
                                       (T0, T0 + 12 * DAY))):
        pid = "H-%d" % i
        b.opening(pid, "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
                  entry_net=-11.72, ts=T0 - DAY)
        b.filled(pid, 1, fill)
        b.marked(pid, 12.0, 28.0, close - 1)
        b.closed(pid, "profit target: sell at 12.00 >= 11.00", close)
    r = rep(p, now=T0 + 20 * DAY)
    check("median hold", r["holding"]["median_days"]["value"], 4.0)
    approx("mean hold", r["holding"]["mean_days"]["value"], 6.0, 0.001)
    check("longest", r["holding"]["longest_days"]["value"], 12.0)
    check("shortest", r["holding"]["shortest_days"]["value"], 2.0)
    check("n is carried", r["holding"]["median_days"]["n"], 3)
    check("nothing open, so the open age is None",
          r["holding"]["open_median_days"]["value"], None)

    print("\n20. Breakdowns by play and by ticker carry their own samples")
    p = tmp("buckets")
    b = Build(p)
    b.opening("C-1", "SPY", "index-put-credit-spread", P.CREDIT_SPREAD,
              SPREAD(), requested=10, entry_net=0.30)
    b.filled("C-1", 10, T0 + 60)
    b.marked("C-1", 0.20, 100.0, T0 + DAY)
    b.closed("C-1", "profit target: buy back at 0.15 <= 0.15", T0 + DAY + 1)
    b.opening("C-2", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("C-2", 1, T0 + 60)
    b.rested("C-2")
    b.marked("C-2", 12.0, 28.0, T0 + 10 * DAY - 30)
    r = rep(p)
    plays = {row["key"]: row for row in r["by_play"]}
    check("both plays appear", sorted(plays), ["index-put-credit-spread",
                                               "swing-atm-hourly"])
    check("the spread booked one trade",
          plays["index-put-credit-spread"]["judged"], 1)
    check("...and its win rate is flagged thin on one trade",
          plays["index-put-credit-spread"]["win_rate"]["thin"], True)
    check("the swing has nothing closed", plays["swing-atm-hourly"]["judged"], 0)
    check("...so its win rate is None, not 0",
          plays["swing-atm-hourly"]["win_rate"]["value"], None)
    check("...and its open P/L is real",
          plays["swing-atm-hourly"]["open_pl"]["value"], 28.0)
    tick = {row["key"]: row for row in r["by_ticker"]}
    check("by ticker too", sorted(tick), ["AAPL", "SPY"])
    check("SPY realized", tick["SPY"]["realized"]["value"], 100.0)
    days = {d["date"]: d for d in r["daily"]}
    check("two sessions touched", len(days), 2)

    print("\n21. The decision log answers 'why did SPY never open'")
    dp = tmp("decisions")
    with open(dp, "w", encoding="utf-8") as fh:
        for sym in ("SPY", "QQQ"):
            for i in range(3):
                fh.write(json.dumps({
                    "ts": T0 + 9 * DAY + i, "at": "2026-09-27T14:00:0%dZ" % i,
                    "kind": "proposal", "symbol": sym,
                    "play": "index-put-credit-spread", "ok": False,
                    "reason": ("assignment_capacity: $0 open plus $741000 here "
                               "is $741000 against a $53155 cap"),
                    "submitted": False}) + "\n")
        fh.write(json.dumps({
            "ts": T0 + 9 * DAY, "at": "2026-09-27T14:00:00Z",
            "kind": "proposal", "symbol": "AAPL", "play": "swing-atm-hourly",
            "ok": True, "reason": "buy the 340 call", "submitted": True}) + "\n")
        fh.write(json.dumps({
            "ts": T0 + 9 * DAY, "at": "2026-09-27T14:05:00Z",
            "kind": "proposal", "symbol": "META", "play": "swing-atm-hourly",
            "ok": False, "submitted": False,
            "reason": ("$1760 of risk needs $1760 of room; $8922 open against "
                       "a $10515 ceiling (60% of $17524 BP)")}) + "\n")
    p = tmp("empty2")
    Build(p)
    r = rep(p, decisions_path=dp)
    d = r["decisions"]
    check("eight proposals in the window", d["proposals"], 8)
    check("one was submitted", d["submitted"], 1)
    check("seven refusals", d["refused"], 7)
    top = d["refusals"][0]
    check("the biggest reason is named", top["class"], "assignment capacity")
    check("...with a count", top["n"], 6)
    check("...and the tickers it blocked", sorted(top["symbols"]),
          ["QQQ", "SPY"])
    check("the ceiling refusal is its own class",
          d["refusals"][1]["class"], "risk ceiling")
    check("SPY's last refusal is one lookup",
          d["by_symbol"]["SPY"]["last_class"], "assignment capacity")
    check("AAPL's was allowed", d["by_symbol"]["AAPL"]["ok"], 1)
    check("a refusal outside the window is not counted",
          rep(p, decisions_path=dp, now=T0 + 20 * DAY)["decisions"]["proposals"],
          0)

    print("\n22. Refusal classes cover the strings the playbook really writes")
    cases = [
        ("assignment_capacity: $0 open plus $741000 here is $741000 against a "
         "$53155 cap", "assignment capacity"),
        ("$1760 of risk needs $1760 of room; $8922 open against a $10515 "
         "ceiling (60% of $17524 BP)", "risk ceiling"),
        ("24 positions open, ceiling is 24", "position count cap"),
        ("after the 15:30 ET entry cutoff (now 20:48 ET)",
         "outside the entry window"),
        ("before the 10:30 ET entry window opens (now 09:31 ET)",
         "outside the entry window"),
        ("closed 343.87 between VWAP and the 9 EMA (343.44-343.93)",
         "no signal"),
        ("already opened swing-atm-hourly today (2026-09-26)",
         "one entry per session"),
        ("buy the 340.0 call | would submit but not armed (no arm file)",
         "not armed"),
        ("buy the 340.0 call | FROZEN is set", "FROZEN"),
        ("no spot for NVDA", "no usable chain"),
        ("signal is up but this ticker is set to puts only",
         "direction not permitted for this ticker"),
        ("", "no reason recorded"),
    ]
    for reason, want in cases:
        check("%r" % reason[:38], OP.refusal_class(reason), want)

    print("\n23. Exit classes cover the close_reason strings that exist")
    class _Pos:
        def __init__(self, reason):
            self.close_reason = reason
    cases = [
        ("profit target: sell at 17.60 >= 17.58", OP.EXIT_PROFIT),
        ("profit target: buy back at 0.15 <= 0.15", OP.EXIT_PROFIT),
        ("resting profit target filled", OP.EXIT_PROFIT),
        ("stop: sell at 8.70 <= 8.79", OP.EXIT_STOP),
        ("stop: buy back at 0.38 >= 0.38", OP.EXIT_STOP),
        ("short leg is 2 day(s) from expiry, inside the 2-day close-out rule",
         OP.EXIT_GUARD),
        ("expires today, past the flatten deadline (16:00 ET)", OP.EXIT_GUARD),
        ("expires today -- short legs do not go into the bell (x)", OP.EXIT_GUARD),
        ("long option expires today -- close rather than let it lapse",
         OP.EXIT_EXPIRY),
        ("no legs remain at the broker", OP.EXIT_GONE),
        ("not placed: 422 position intent mismatch", OP.EXIT_REFUSED),
        ("closed by hand", OP.EXIT_OTHER),
    ]
    for reason, want in cases:
        check("%r" % reason[:40], OP.exit_class(_Pos(reason), True), want)

    print("\n24. A vanished position is not counted as a profit")
    p = tmp("gone")
    b = Build(p)
    b.opening("G-1", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("G-1", 1, T0 + 60)
    b.marked("G-1", 0.01, -1171.0, T0 + DAY)
    b.closed("G-1", "no legs remain at the broker", T0 + DAY + 1)
    r = rep(p)
    check("it is its own class", r["positions"][0]["exit_class"], OP.EXIT_GONE)
    check("...not a profit target",
          [m["n"] for m in r["exits"]["mix"]
           if m["class"] == OP.EXIT_PROFIT][0], 0)
    check("its P/L still counts, as an estimate",
          r["pl"]["realized"]["value"], -1171.0)

    print("\n25. The ceiling comes from a live account snapshot or from nothing")
    p = tmp("ceiling")
    b = Build(p)
    b.opening("Y-1", "SPY", "index-put-credit-spread", P.CREDIT_SPREAD,
              SPREAD(), requested=10, entry_net=0.30)
    b.filled("Y-1", 10, T0 + 60)
    b.rested("Y-1")
    b.marked("Y-1", 0.28, 20.0, T0 + 10 * DAY - 30)
    r = rep(p, account={"options_buying_power": 17524.0})
    check("risk is width less credit, ten wide",
          r["risk"]["at_risk"]["value"], 1700.0)
    approx("ceiling is 60% of options BP",
           r["risk"]["ceiling"]["value"], 10514.4, 0.01)
    approx("headroom", r["risk"]["headroom"]["value"], 8814.4, 0.01)
    approx("utilization", r["risk"]["utilization"]["value"], 0.1617, 0.0005)
    check("the fraction is the playbook's, not a copy",
          r["risk"]["fraction"], PB.MAX_OPEN_RISK_FRACTION)
    check("the cap is the playbook's too",
          r["risk"]["positions_cap"], PB.MAX_CONCURRENT_POSITIONS)
    check("the per-position sum agrees with the ledger's own total",
          any(w["code"] == "risk_disagrees" for w in r["warnings"]), False)
    check("zero buying power is not a divide",
          rep(p, account={"options_buying_power": 0.0})
          ["risk"]["utilization"]["value"], None)

    print("\n26. A torn last line costs the last event and nothing before it")
    p = tmp("torn")
    b = Build(p)
    b.opening("T-1", "AAPL", "swing-atm-hourly", P.LONG_SINGLE, LONG(),
              entry_net=-11.72)
    b.filled("T-1", 1, T0 + 60)
    b.rested("T-1")
    b.marked("T-1", 12.0, 28.0, T0 + 10 * DAY - 30)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write('{"ts": 1790, "id": "T-2", "event": "open')
    r = rep(p)
    check("the good rows survived", r["counts"]["open"], 1)
    check("open P/L is intact", r["pl"]["open"]["value"], 28.0)

    print("\n27. A missing ledger file is a page, not a traceback")
    r = rep(os.path.join(tempfile.gettempdir(), "optperf_does_not_exist.jsonl"))
    check("it renders", r["ok"], True)
    check("with no positions", r["counts"]["positions"], 0)

    print("\n28. The whole report is JSON-safe, because a route returns it")
    p = tmp("json")
    b = Build(p)
    b.opening("J-1", "SPY", "index-put-credit-spread", P.CREDIT_SPREAD,
              SPREAD(), requested=10, entry_net=0.30)
    b.filled("J-1", 10, T0 + 60)
    b.rested("J-1")
    b.marked("J-1", 0.28, 20.0, T0 + 10 * DAY - 30)
    b.adopted("adopted-X", "TSLA261030P00400000", "TSLA", 1, T0)
    r = rep(p, account={"options_buying_power": 17524.0})
    blob = json.dumps(r)                 # no default=str: it must already be
    check("it serialises", len(blob) > 1000, True)
    check("every metric has the five keys",
          sorted(r["pl"]["open"]), ["n", "reason", "thin", "unit", "value"])
    for name in ("ok", "as_of", "sources", "warnings", "counts", "pl",
                 "outcomes", "exits", "risk", "assignment", "holding",
                 "by_play", "by_ticker", "daily", "positions", "attention",
                 "decisions"):
        check("the contract carries %s" % name, name in r, True)

    print("\n29. Wilson: four trades is not a win rate and the interval says so")
    lo, hi = OP.wilson(3, 4)
    check("three of four is 75%...", round(3 / 4.0, 2), 0.75)
    check("...but the interval reaches below a coin flip", lo < 0.5, True)
    check("...and nearly to certainty", hi > 0.9, True)
    lo2, hi2 = OP.wilson(0, 5)
    check("zero wins from five does NOT claim 0%", hi2 > 0.4, True)
    check("...the normal interval would have said 0.0", lo2, 0.0)
    check("n=0 has no interval", OP.wilson(0, 0), (None, None))

    print("\n30. metric() will not hide a reason behind a value")
    m = OP.metric(None, 0, "usd", reason="nothing yet")
    check("a None carries its reason", m["reason"], "nothing yet")
    m = OP.metric(12.0, 40, "usd", reason="ignored")
    check("a healthy value drops the caveat", m["reason"], None)
    m = OP.metric(12.0, 2, "usd", reason="two trades", thin=True)
    check("a thin value keeps it", m["reason"], "two trades")
    check("...and is flagged", m["thin"], True)

    print("\n31. One spread, six spellings of the same leg")
    # DEMONSTRATED DEFECT. The protecting side was decided by an exact
    # lowercase comparison on the leg right, so ONE identical SPY 741/739 x10
    # measured $2,000 written "put" and $741,000 written "P": the long stopped
    # matching the short and ten hedged contracts reported naked. optplays
    # writes "put" today, optexec._norm_right writes "P", and greeks, optsym
    # and optdata all speak "P"/"C". One leg reaching the ledger in the other
    # spelling must not move the number by $739,000.
    def spelled(short_r, long_r, short_s="sell", long_s="buy"):
        legs = SPREAD()
        legs[0]["right"], legs[0]["side"] = short_r, short_s
        legs[1]["right"], legs[1]["side"] = long_r, long_s
        return OP.assignment_exposure(Pos(legs, contracts=10))

    for sr, lr in (("put", "put"), ("P", "P"), ("PUT", "PUT"),
                   ("Put", "put"), ("p", "P"), ("put", "P")):
        e = spelled(sr, lr)
        check("%s/%s nets to the $2 width" % (sr, lr), e["net"], 2000.0)
        check("...gross is unchanged", e["gross"], 741000.0)
        check("...nothing is uncovered", e["uncovered"], 0)

    print("\n32. An unreadable right hedges nothing, and never itself")
    # "" must not become a bucket: two legs nobody can read would otherwise
    # net against each other. Unknown falls back to GROSS, which is the only
    # direction it is safe to be wrong in.
    e = spelled("stock", "stock")
    check("an unreadable short is measured gross", e["net"], 741000.0)
    check("...and counted uncovered", e["uncovered"], 10)
    e = spelled("put", "stock")
    check("an unreadable LONG protects nothing", e["net"], 741000.0)
    check("...so the short is uncovered", e["uncovered"], 10)
    check("a readable long cannot rescue an unreadable short",
          spelled("stock", "put")["net"], 741000.0)
    check("a call long still does not hedge a put short",
          spelled("P", "C")["net"], 741000.0)
    check("...and the wrong-side long is unchanged by spelling",
          OP.assignment_exposure(Pos(
              [{"symbol": "SPY261030P00741000", "right": "P", "strike": 741.0,
                "side": "SELL"},
               {"symbol": "SPY261030P00745000", "right": "PUT",
                "strike": 745.0, "side": "Buy"}]))["net"], 74100.0)

    print("\n33. The side is normalised at the same boundary")
    # A side spelled "SELL" was neither buy nor sell: the short leg vanished
    # from the assignment total AND from the short-near-expiry warning while
    # the account was still short it.
    e = spelled("put", "put", short_s="SELL", long_s="BUY")
    check("SELL/BUY still reads as a hedged vertical", e["net"], 2000.0)
    check("...and is not uncovered", e["uncovered"], 0)
    e = spelled("put", "put", short_s="SELL", long_s="hold")
    check("an unreadable side is not a hedge", e["net"], 741000.0)
    check("...and the short is still counted", e["uncovered"], 10)
    # position_risk's adopted branch reads the same field: a short we did not
    # open has nothing bounding the loss, and "Sell" must not print a number.
    val, why = OP.position_risk(Pos(
        [{"symbol": "SPY261030P00741000", "right": "P", "strike": 741.0,
          "side": "Sell", "entry_px": 2.10}], contracts=10, entry_net=None))
    check("an adopted SHORT spelled 'Sell' still refuses a number", val, None)
    check("...and says nothing bounds it",
          "bounds the loss" in (why or ""), True)

    print("\n34. avg_loss is POSITIVE and largest_loss is NEGATIVE")
    # The rendering contract, pinned because static/ui/views/options.js now
    # depends on it. The overview drew avg_loss with the profit-and-loss
    # formatter and printed "+$1,000.00" in green beside a "+$60.00" average
    # win -- the one figure whose job is to stop a high win rate reading as
    # success, drawn as a profit. options.js's lossv() negates the magnitude;
    # if this field ever starts arriving signed, lossv's abs() is what stops
    # it flipping back to green and THIS is the check that says so.
    o = rep(tmp("onlyloss"))["outcomes"]
    check("avg_loss is a positive magnitude", o["avg_loss"]["value"] > 0, True)
    check("largest_loss is signed negative",
          o["largest_loss"]["value"] < 0, True)
    check("...so one formatter cannot honestly draw both",
          (o["avg_loss"]["value"] > 0) and (o["largest_loss"]["value"] < 0),
          True)

    print(f"\n{'ALL CHECKS PASSED' if not FAIL else f'{FAIL} CHECK(S) FAILED'}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
