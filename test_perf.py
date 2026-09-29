#!/usr/bin/env python3
"""test_perf.py -- the account's arithmetic, against fakes. No network.

What this has to prove, in the owner's own terms:

  * "p/l all time says plus 3 thousand but the history says 6200" -- ONE
    headline, equity less NET FUNDING, and it does not move when a chart
    window moves (sections 2 and 3),
  * "please remove booked" -- realised is never presented without the open
    inventory beside it, and on a wins-only log every risk metric derived
    from it is REFUSED rather than printed (sections 5 and 8),
  * the residual is published, not hidden (section 9),
  * a deposit is not a profit and a holiday is not a flat day (section 10),
  * profit factor is null and not infinity when nothing lost; Sortino is null
    under three down days; Sharpe is daily (sections 6 and 7),
  * per ticker and portfolio return THE SAME SHAPE (section 11),
  * every unmeasurable number is a dash WITH A REASON, never 0.0 (section 12).

Everything runs against hand-built snapshots, so it is the same test at 03:00
and at 15:31 -- no wall-clock branch and no broker anywhere.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_perf_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
# blank, not absent: app.py loads .env at import and load_dotenv fills only
# MISSING variables, so an empty value is what keeps the real keys out
for _k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
    os.environ[_k] = ""

import perf                                                 # noqa: E402

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ===================================================================== fakes
DAY = 86400.0
#: 2026-08-28 12:00 ET. A FIXED instant, so every date below is the same date
#: however the machine running the suite is set. Dates are always derived with
#: perf.et_date rather than written out, because a literal date here would be
#: the one thing in the file that is not measured.
T0 = 1787932800.0


def act(kind: str, amount, date: str = "2026-08-21") -> dict:
    return {"activity_type": kind, "net_amount": amount, "date": date,
            "description": ""}


def jrow(sym: str, realized: float, *, ts: str, entry: str = "",
         event: str = "close", dry: bool = False,
         inferred: bool = False, exit_price: float = 10.0) -> dict:
    return {"event": event, "symbol": sym, "realized": realized, "ts": ts,
            "entry_time": entry or ts, "shares": 1, "dry_run": dry,
            "inferred": inferred, "exit_price": exit_price}


class FakePos:
    """Enough of a play-ledger position for rows_from_option_positions."""

    def __init__(self, symbol, entry_net, close_net, contracts=1,
                 is_open=False, play="swing-atm-hourly"):
        self.symbol = symbol
        self.entry_net = entry_net
        self.close_net = close_net
        self.contracts = contracts
        self.is_open = is_open
        self.play = play
        self.filled_at = "2026-08-24T14:00:00Z"
        self.closed_at = "2026-08-26T14:00:00Z"
        self.risk = 500.0


class FakeRouteBroker:
    """Enough Alpaca for the routes: activities, history, nothing writable."""

    def __init__(self):
        self.calls = []

    def activities(self, kind="FILL", date="", page_size=100, max_pages=10):
        self.calls.append(kind)
        return [act("JNLC", "50000")] if kind == "JNLC" else []

    def portfolio_history(self, period="1D", timeframe="1Min", extended=True,
                          date_end=""):
        self.calls.append("history:%s" % period)
        pts = curve((0, 50000), (1, 50050), (2, 50100))
        return {"timestamp": [t for t, _v in pts],
                "equity": [v for _t, v in pts], "base_value": 50000.0}


class FakeRouteFleet:
    """Enough Fleet for the perf routes. No engine, no orders, no network."""

    def __init__(self, state_dir, journal_path):
        self.account_id = "default"
        self.label = "Default"
        self.state_dir = Path(state_dir)
        self.journal_path = Path(journal_path)
        self.account = {"equity": "50100", "last_equity": "50050",
                        "created_at": "2026-08-20T00:00:00Z"}
        self.positions = {}
        self.broker = FakeRouteBroker()
        self.snap_at = 1_700_000_000.0


def curve(*pairs) -> list:
    """[(epoch, equity)] from (day_offset, equity) pairs."""
    return [(T0 + d * DAY, float(v)) for d, v in pairs]


def main() -> int:
    print("\n1. the metric envelope is hub's, not a second one")
    m = perf.metric(1.5, 3, "usd")
    check("the keys are hub's exactly", sorted(m),
          ["as_of", "n", "reason", "thin", "unit", "value"])
    d = perf.dash(0, "pct", "nobody measured it")
    check("a dash is None with a reason", (d["value"], d["reason"]),
          (None, "nobody measured it"))
    check("pct is a FRACTION, so 0.5 stays 0.5",
          perf.metric(0.5, 1, "pct")["value"], 0.5)

    print("\n2. NET FUNDING is the cost basis, and a fee is not funding")
    f = perf.net_funding([act("JNLC", "50000"), act("FEE", "-1.59"),
                          act("FEE", "-2.00"), act("DIV", "3.00")])
    check("one JNLC of 50,000 is the funding", f["value"], 50000.0)
    check("two FEE rows are a cost, not funding", f["fees"], -3.59)
    check("and they are counted", f["fee_rows"], 2)
    check("a dividend is income, not funding", f["income"], 3.0)
    check("a withdrawal nets off",
          perf.net_funding([act("JNLC", "50000"),
                            act("CSW", "-1000")])["value"], 49000.0)
    check("no activity list read at all is None, never 0",
          perf.net_funding(None)["value"], None)
    check("an EMPTY list is a measurement of zero",
          perf.net_funding([])["value"], 0.0)
    # A securities journal moves shares with no dollar amount on the row.
    su = perf.net_funding([act("JNLC", "50000"), act("JNLS", None)])
    check("a securities transfer makes the basis uncertain",
          su["uncertain"], True)
    check("but it does NOT invent a number for it", su["value"], 50000.0)

    print("\n3. THE HEADLINE: equity less net funding")
    ctx = perf.Ctx(account={"equity": "53055.43", "last_equity": "53165.995",
                            "created_at": "2026-08-21T14:57:50Z"},
                   activities=[act("JNLC", "50000"), act("FEE", "-19.46")],
                   now=T0)
    a = perf.account_pl(ctx)
    check("the live measured case: 53,055.43 - 50,000",
          a["all_time"]["value"], 3055.43)
    check("as a FRACTION of the money put in",
          a["all_time_pct"]["value"], 0.061109)
    check("today is equity less Alpaca's own last_equity",
          a["today"]["value"], -110.57)
    check("the fee is INSIDE the P/L, not netted out of funding",
          a["funding"]["value"], 50000.0)
    # THE WHOLE POINT. base_value was 53,166 / 51,134.83 / 50,000 on this
    # account depending only on the window asked for. Funding is one number.
    check("no window was asked for and none is needed",
          "base_value" in str(a["basis"]["all_time"]), False)
    check("and the basis says base_value is NOT what this is",
          "base_value" in a["basis"]["funding"], True)
    blind = perf.account_pl(perf.Ctx(account={"equity": "53055.43"}, now=T0))
    check("with no activity read the headline is a DASH",
          blind["all_time"]["value"], None)
    check("and it says why", bool(blind["all_time"]["reason"]), True)

    print("\n4. journal rows in, trade rows out -- with journal's exclusions")
    rows = perf.rows_from_journal([
        jrow("RAM", 5.0, ts="2026-08-24T14:00:00Z"),
        jrow("RAM", 7.0, ts="2026-08-25T14:00:00Z"),
        jrow("RAM", 99.0, ts="2026-08-25T15:00:00Z", dry=True),
        jrow("RAM", 0.0, ts="2026-08-25T16:00:00Z", inferred=True,
             exit_price=0.0),
        jrow("RAM", 1.0, ts="2026-08-25T17:00:00Z", event="open"),
    ])
    check("a dry run never traded, so it is not a trade", len(rows), 2)
    check("and the two real ones sum", sum(r["realized"] for r in rows), 12.0)
    opt = perf.rows_from_option_positions([
        FakePos("SPY", 0.30, -0.10, contracts=10),      # credit kept 0.20
        FakePos("AAPL", -1.00, 1.50, contracts=1),      # debit, closed higher
        FakePos("QQQ", 0.30, None, contracts=10, is_open=True),
    ])
    check("entry_net and close_net SUM, they do not subtract",
          [r["realized"] for r in opt], [200.0, 50.0])
    check("an open structure has booked nothing", len(opt), 2)

    print("\n5. realised is NEVER printed without the open inventory")
    wins = [perf._row("RAM", 10.0, opened_at=T0, closed_at=T0 + 3600),
            perf._row("RAM", 5.0, opened_at=T0, closed_at=T0 + 7200)]
    m = perf.metrics(wins, None, now=T0, open_pl=-295.0, open_positions=6)
    check("net_pl is what was booked", m["net_pl"]["value"], 15.0)
    check("open_pl rides beside it", m["open_pl"]["value"], -295.0)
    check("and total_pl is the only honest sum",
          m["total_pl"]["value"], -280.0)
    blind = perf.metrics(wins, None, now=T0)
    check("with no mark, total_pl is a DASH and not the booked figure",
          blind["total_pl"]["value"], None)
    check("and it says why half a number is not published",
          "half a number" in blind["total_pl"]["reason"], True)

    print("\n6. profit factor is null, not infinity, when nothing lost")
    check("no loss -> no profit factor", m["profit_factor"]["value"], None)
    check("and the reason names the missing stop loss",
          "NO STOP LOSS" in m["profit_factor"]["reason"], True)
    mixed = perf.metrics(
        [perf._row("A", 100.0, closed_at=T0),
         perf._row("A", 50.0, closed_at=T0 + DAY),
         perf._row("A", -30.0, closed_at=T0 + 2 * DAY)], None, now=T0)
    check("with a loss it is gross win over gross loss",
          mixed["profit_factor"]["value"], 5.0)
    check("gross_loss is POSITIVE by contract",
          mixed["gross_loss"]["value"], 30.0)
    check("avg_loss is POSITIVE by contract too",
          mixed["avg_loss"]["value"], 30.0)
    check("win rate is a FRACTION", mixed["win_rate"]["value"], 0.6667)
    check("expectancy is the mean of every closed trade",
          mixed["expectancy"]["value"], 40.0)
    check("best and worst are the extremes",
          (mixed["best_trade"]["value"], mixed["worst_trade"]["value"]),
          (100.0, -30.0))

    print("\n7. Sharpe is DAILY; Sortino is refused under three down days")
    up = curve((0, 50000), (1, 50500), (2, 51000), (3, 51500), (4, 52000))
    r = perf.ratios(up)
    check("five rising days have a Sharpe", r["sharpe"]["value"] is not None,
          True)
    check("but no Sortino: zero down days", r["sortino"]["value"], None)
    check("and it says that is not an achievement",
          "nothing went down" in r["sortino"]["reason"], True)
    check("the convention travels with the number",
          "DAILY returns" in r["convention"], True)
    check("annualised by sqrt(252)", "sqrt(252)" in r["convention"], True)
    shaky = curve((0, 50000), (1, 49000), (2, 50000), (3, 49000),
                  (4, 50000), (5, 49000), (6, 50500))
    check("three down days is enough for a Sortino",
          perf.ratios(shaky)["sortino"]["value"] is not None, True)
    check("three daily points is not a volatility measurement",
          perf.ratios(curve((0, 1), (1, 2), (2, 3)))["sharpe"]["value"], None)
    # Under 90 days an annual return is an extrapolation, and says so.
    check("a five-day window's annual return is flagged thin",
          perf.ratios(up)["annual_return"]["thin"], True)

    print("\n8. THE ARTIFACT GUARD: no losses means no drawdown to report")
    check("max drawdown off a wins-only realised curve is REFUSED",
          m["max_drawdown"]["value"], None)
    check("so is its Sharpe", m["sharpe"]["value"], None)
    check("so is Calmar", m["calmar"]["value"], None)
    check("and the reason names the construction, not the strategy",
          "by construction" in m["max_drawdown"]["reason"], True)
    check("the win rate carries the same caveat rather than reading 100%",
          "NO STOP LOSS" in m["win_rate"]["reason"], True)
    check("and the caveat is on the block for a page to render once",
          any("NO STOP LOSS" in c for c in m["caveats"]), True)
    # An ACCOUNT equity curve is a real reading and is NOT refused, even
    # though the very same trades are wins-only: the account fell, the log
    # simply could not see it. That distinction is the whole file.
    real = perf.metrics(wins, curve((0, 50000), (1, 52000), (2, 48000),
                                    (3, 49000), (4, 53000)), now=T0,
                        open_pl=-295.0, open_positions=6)
    check("against ACCOUNT equity the drawdown is real and reported",
          real["max_drawdown"]["value"], -4000.0)
    check("as a fraction of the peak it fell from",
          real["max_drawdown_pct"]["value"], -0.076923)
    check("with the peak's own date on it",
          real["drawdown_peak_date"], perf.et_date(T0 + DAY))
    check("and whether it came back", real["drawdown_recovered"], True)

    print("\n9. the reconciliation publishes its residual")
    rctx = perf.Ctx(
        account={"equity": "53055.43", "created_at": "2026-08-21T14:57:50Z"},
        activities=[act("JNLC", "50000"), act("FEE", "-19.46")],
        journal_rows=[jrow("RAM", 2974.23, ts="2026-08-24T14:00:00Z")],
        broker_positions=[{"symbol": "AAPL", "unrealized_pl": "-295.00"}],
        ledger_present=False, now=T0)
    rec = perf.reconcile(rctx)
    steps = {s["key"]: s["value"] for s in rec["steps"]}
    check("funding", steps["funding"], 50000.0)
    check("realised by the strategies' own logs", steps["realized"], 2974.23)
    check("open at the broker's marks", steps["open"], -295.0)
    check("fees", steps["fees"], -19.46)
    check("explained", rec["explained"]["value"], 52659.77)
    check("THE RESIDUAL, named rather than hidden",
          rec["residual"]["value"], 395.66)
    check("and it says the walk does not balance", rec["balanced"], False)
    check("with candidate causes, not silence",
          len(rec["unexplained_by"]) > 0, True)
    check("the missing options ledger is named",
          any("options play ledger" in u for u in rec["unexplained_by"]), True)
    three = rec["the_three_numbers"]
    check("all three of the old numbers are in one place",
          sorted(three), ["account_all_time", "open_marks",
                          "strategy_realized"])
    check("the account one is the headline",
          three["account_all_time"]["value"], 3055.43)
    check("and the realised one is labelled as NOT the headline",
          "not the headline" in three["strategy_realized"]["means"], True)
    # THE VM's OWN CASE: the journal there claims +8,882.86 while the account
    # is up only +3,055.43. That gap is the owner's "history says 6200", and
    # the walk has to say what it is rather than leaving a bare residual.
    vm = perf.reconcile(perf.Ctx(
        account={"equity": "53055.43"},
        activities=[act("JNLC", "50000")],
        broker_positions=[{"symbol": "X", "unrealized_pl": "-295"}],
        ledger_present=True, now=T0),
        [perf._row("RAM", 8882.86, closed_at=T0)])
    said = " ".join(vm["unexplained_by"])
    check("a wins-only log beating the account is named, not left bare",
          "not outperforming it" in said, True)
    check("with both numbers in the sentence",
          "8882.86" in said and "3055.43" in said, True)
    # A walk that balances says so.
    ok = perf.reconcile(perf.Ctx(
        account={"equity": "50100"}, activities=[act("JNLC", "50000")],
        journal_rows=[jrow("RAM", 100.0, ts="2026-08-24T14:00:00Z")],
        broker_positions=[], now=T0))
    check("a walk that balances says so", ok["balanced"], True)
    check("with a zero residual", ok["residual"]["value"], 0.0)

    print("\n10. the calendar: a deposit is not a profit")
    days = perf.daily(
        [perf._row("RAM", 40.0, opened_at=T0, closed_at=T0 + DAY),
         # a trade on a day Alpaca printed no equity at all
         perf._row("RAM", 7.0, opened_at=T0, closed_at=T0 + 2 * DAY)],
        curve((0, 50000), (1, 50500), (3, 50200)),
        funding_rows=[{"date": perf.et_date(T0), "amount": 50000.0}],
        created_at="2026-08-21T00:00:00Z")
    by = {d["date"]: d for d in days}
    d0 = by[perf.et_date(T0)]
    check("the funding day's net is a DASH, not -50,002", d0["net"], None)
    check("and the square says why",
          "cash moved" in (d0["why"] or ""), True)
    check("but the funding itself is shown", d0["funding"], 50000.0)
    d1 = by[perf.et_date(T0 + DAY)]
    check("a normal day's net is the equity change", d1["net"], 500.0)
    check("realised that day", d1["realized"], 40.0)
    check("open_delta is net less realised, and is DERIVED",
          d1["open_delta"], 460.0)
    check("the trade count is on the square", d1["trades"], 1)
    d2 = by[perf.et_date(T0 + 2 * DAY)]
    check("a day with no equity print is None, not a flat zero",
          d2["net"], None)
    check("and it says nobody measured it",
          "no equity" in (d2["why"] or ""), True)
    check("the basis says open_delta is derived",
          "DERIVED" in perf.DAILY_BASIS, True)

    print("\n11. per ticker and portfolio are THE SAME SHAPE")
    mixed_rows = [
        perf._row("RAM", 10.0, opened_at=T0, closed_at=T0 + DAY),
        perf._row("RAM", -4.0, opened_at=T0, closed_at=T0 + 2 * DAY),
        perf._row("MSTX", 25.0, opened_at=T0, closed_at=T0 + DAY),
    ]
    port = perf.portfolio(mixed_rows, curve((0, 50000), (1, 50500),
                                            (2, 50200), (3, 50900)),
                          now=T0, open_pl=-100.0, open_positions=2)
    tick = perf.per_ticker(mixed_rows, now=T0,
                           open_pl_by_symbol={"RAM": -100.0},
                           open_positions_by_symbol={"RAM": 2})
    check("one component renders both",
          sorted(k for k in port) == sorted(k for k in tick[0]
                                            if k != "symbol"), True)
    check("two tickers", [t["symbol"] for t in tick], ["MSTX", "RAM"])
    check("the biggest net P/L sorts first",
          tick[0]["net_pl"]["value"], 25.0)
    check("the portfolio nets every ticker", port["net_pl"]["value"], 31.0)
    # A flat ticker with the book READ is 0.00, and without it a dash. That
    # difference is what made total_pl vanish on exactly the flat tickers.
    flat = perf.per_ticker(mixed_rows, now=T0, open_pl_by_symbol={},
                           book_read=True)
    check("book read, nothing held: open is a measured 0.00",
          flat[0]["open_pl"]["value"], 0.0)
    check("so the total is publishable", flat[0]["total_pl"]["value"], 25.0)
    unread = perf.per_ticker(mixed_rows, now=T0, open_pl_by_symbol={})
    check("book NOT read: open is a dash", unread[0]["open_pl"]["value"], None)

    print("\n12. nothing unmeasurable is ever a zero")
    empty = perf.metrics([], None, now=T0)
    for k in ("net_pl", "profit_factor", "expectancy", "avg_win", "avg_loss",
              "win_rate", "best_trade", "worst_trade", "avg_hold_days",
              "sharpe", "sortino", "calmar", "max_drawdown", "exposure"):
        check("%s is a dash with no trades" % k, empty[k]["value"], None)
        check("  ...and says why", bool(empty[k]["reason"]), True)
    check("but the trade COUNT is a real zero", empty["trades"]["value"], 0)
    for k, v in empty.items():
        if isinstance(v, dict) and "unit" in v:
            if v["unit"] not in ("usd", "pct", "ratio", "count", "qty",
                                 "days", "seconds"):
                check("%s carries a legal unit" % k, v["unit"], "<one of UNITS>")

    print("\n12b. a hold time cannot be negative")
    bent = perf.metrics(
        [perf._row("RAM", 5.0, opened_at=T0, closed_at=T0 + 3600),
         # a close stamped BEFORE its own open. Corrupt, not fast.
         perf._row("RAM", 5.0, opened_at=T0 + 3600, closed_at=T0),
         perf._row("RAM", 5.0, closed_at=T0 + 7200)], None, now=T0)
    check("only the sane span is averaged",
          bent["avg_hold_days"]["value"], round(3600 / 86400.0, 4))
    check("and it is one observation, not three",
          bent["avg_hold_days"]["n"], 1)
    check("the two that were dropped are named",
          "close before they open" in bent["avg_hold_days"]["reason"], True)
    check("exposure drops the bent span too", bent["exposure"]["n"], 1)
    allbent = perf.metrics(
        [perf._row("RAM", 5.0, opened_at=T0 + 3600, closed_at=T0)],
        None, now=T0)
    check("nothing sane left is a DASH, never a negative hold",
          allbent["avg_hold_days"]["value"], None)

    print("\n13. Alpaca's window padding is stripped from the equity curve")
    pts, why = perf.clean_equity([(T0, 0.0), (T0 + DAY, 0.0),
                                  (T0 + 2 * DAY, 50000.0)])
    check("leading zeros go", len(pts), 1)
    check("and the drop is reported", "leading point" in (why or ""), True)
    check("a zero in the MIDDLE stays -- it is a real, alarming reading",
          len(perf.clean_equity([(T0, 100.0), (T0 + DAY, 0.0),
                                 (T0 + 2 * DAY, 100.0)])[0]), 3)
    # The measured one: period=all returned 2026-08-20 at exactly $50,000 on
    # an account created 2026-08-21, which is the deposit projected backwards.
    pre, why2 = perf.clean_equity(curve((-1, 50000), (0, 49997), (1, 50500)),
                                  perf.et_date(T0) + "T14:57:50Z")
    check("a point dated before the account existed goes", len(pre), 2)
    check("and that is reported too",
          "before the account was opened" in (why2 or ""), True)
    check("no history read at all is empty WITH a reason",
          perf.clean_equity(None), ([], "no equity history was read"))

    print("\n14. the whole report, and its counts")
    rep = perf.report(rctx)
    check("ok", rep["ok"], True)
    check("the headline is the account's", rep["headline"]["all_time"]["value"],
          3055.43)
    check("the reconciliation rides with it",
          rep["reconciliation"]["residual"]["value"], 395.66)
    check("the portfolio block is there",
          rep["portfolio"]["net_pl"]["value"], 2974.23)
    check("one closed trade", rep["counts"]["closed_trades"], 1)
    check("one open position", rep["counts"]["open_positions"], 1)
    check("the daily rows are a list", isinstance(rep["daily"], list), True)
    check("an OCC contract is filed under its UNDERLYING",
          perf._underlying("AAPL261030C00340000"), "AAPL")
    check("and a plain ticker is itself", perf._underlying("RAM"), "RAM")

    print("\n15. the routes answer, scoped and legacy, off ONE cached report")
    try:
        from fastapi.testclient import TestClient
    except Exception as e:
        print(f"  SKIP  fastapi TestClient unavailable ({e})")
    else:
        import accounts
        accounts.STATE_DIR = SCRATCH / "regstate"
        accounts.ACCOUNTS_DIR = accounts.STATE_DIR / "accounts"
        accounts.REGISTRY_PATH = accounts.STATE_DIR / "accounts.json"
        import remoteauth
        remoteauth.TOKEN_FILE = SCRATCH / "dash_token.txt"
        import app as app_mod
        app_mod.REG = accounts.Registry(path=accounts.REGISTRY_PATH)
        jp = SCRATCH / "route_journal.jsonl"
        jp.write_text(
            '{"event":"close","symbol":"RAM","realized":100.0,'
            '"ts":"2026-08-28T14:00:00Z","entry_time":"2026-08-28T13:00:00Z",'
            '"shares":1,"exit_price":10.0}\n', encoding="utf-8")
        fleet = FakeRouteFleet(SCRATCH, jp)
        app_mod.app.dependency_overrides[app_mod.cur] = lambda: fleet
        app_mod._PERF_CACHE.clear()
        app_mod._PERF_FEED.clear()
        import journal as _j
        real_load = _j.load
        reads = []

        def counting_load(*a, **kw):
            reads.append(1)
            return real_load(*a, **kw)

        _j.load = counting_load
        try:
            with TestClient(app_mod.app) as c:
                c.headers["X-Dash-Key"] = remoteauth.token()
                r = c.get("/api/perf/account")
                check("account 200", r.status_code, 200)
                check("THE HEADLINE is equity less funding",
                      r.json()["all_time"]["value"], 100.0)
                check("the scoped path answers the same",
                      c.get("/api/a/default/perf/account").status_code, 200)
                r = c.get("/api/perf/reconcile")
                check("reconcile 200", r.status_code, 200)
                check("and it carries a residual",
                      "residual" in r.json(), True)
                r = c.get("/api/perf/metrics")
                check("metrics 200", r.status_code, 200)
                check("with no symbol the scope is the portfolio",
                      r.json()["scope"], "portfolio")
                r = c.get("/api/perf/metrics?symbol=RAM")
                check("one ticker 200", r.status_code, 200)
                check("same shape as the portfolio block",
                      sorted(k for k in r.json()["metrics"] if k != "symbol"),
                      sorted(c.get("/api/perf/metrics").json()["metrics"]))
                check("a symbol with nothing on it is a 404, not a page of 0s",
                      c.get("/api/perf/metrics?symbol=ZZZZ").status_code, 404)
                r = c.get("/api/perf/daily")
                check("daily 200", r.status_code, 200)
                check("and says what its columns mean",
                      bool(r.json()["basis"]), True)
                r = c.get("/api/perf/report")
                check("report 200", r.status_code, 200)
                # The journal is re-read once per cache window and not once per
                # poll. CLAUDE.md measured the alternative at 10-39 s a request.
                check("the report says how old it is",
                      "cache_age_s" in r.json(), True)
                before = len(reads)
                for _ in range(5):
                    c.get("/api/perf/report")
                check("five more polls re-read the journal zero times",
                      len(reads) - before, 0)
                check("and the whole page was built from one read",
                      len(reads), 1)
        finally:
            _j.load = real_load
            app_mod.app.dependency_overrides.pop(app_mod.cur, None)

    print("\n16. nothing here writes anything")
    guarded = SCRATCH / "journal.jsonl"
    guarded.write_bytes(b"")
    before = guarded.stat().st_size
    perf.report(rctx)
    perf.account_pl(rctx)
    perf.reconcile(rctx)
    check("the journal was not touched", guarded.stat().st_size, before)
    check("perf builds no broker", hasattr(perf, "Alpaca"), False)

    print()

    print("\n18. AN ACCOUNT WITH NO DEPOSIT RECORD HAS NO COST BASIS")
    # Measured on the real second account, PA3YVTECEQFE: equity $100,000 and
    # ZERO funding rows, because an Alpaca paper account opens with a balance
    # that is never written as a JNLC or a CSD. Subtracting zero reported the
    # whole balance as profit -- the dashboard claimed +$100,000 all-time on
    # an account that had never placed a trade. "We looked and found none" is
    # not "nothing was deposited".
    nofund = perf.Ctx(account={"equity": "100000", "last_equity": "100000"},
                      activities=[], journal_rows=[], option_positions=[],
                      broker_positions=[])
    ap = perf.account_pl(nofund)
    check("all-time is a DASH, not the whole balance",
          ap["all_time"]["value"], None)
    check("...and it says the cost basis is unknown",
          "unknown" in (ap["all_time"]["reason"] or "").lower(), True)
    check("...the percent goes with it", ap["all_time_pct"]["value"], None)
    check("...but equity is still reported, because that IS known",
          ap["equity"]["value"], 100000.0)
    # and the ordinary case still works
    funded = perf.Ctx(account={"equity": "53055.43", "last_equity": "53166.00"},
                      activities=[{"activity_type": "JNLC",
                                   "net_amount": "50000", "date": "2026-08-21",
                                   "id": "f1"}],
                      journal_rows=[], option_positions=[],
                      broker_positions=[])
    check("a funded account still reports equity less funding",
          perf.account_pl(funded)["all_time"]["value"], 3055.43)
    print("\n17. A LOSING TICKER DOES NOT CRASH THE PAGE")
    # The reproduction, verbatim from the adversarial pass: eight perfectly
    # ordinary closed lots whose cumulative realised curve opens +120 and
    # closes -400. ratios() guarded only `start > 0`, so a positive base was
    # raised to a fractional power of a NEGATIVE ratio, Python returned a
    # COMPLEX number, round() raised "type complex doesn't define __round__",
    # and app.py turned it into a 500. A losing ticker is not an edge case --
    # it is the case this dashboard exists to show.
    t0 = 1790000000
    losing = [{"symbol": "ZZZ", "realized": r, "ts": t0 + i * 86400,
               "at": "", "qty": 1}
              for i, r in enumerate([120, -60, -80, 40, -150, -90, 30, -210])]
    try:
        m = perf.metrics(losing, None)
        crashed = ""
    except Exception as exc:
        m, crashed = {}, "%s: %s" % (type(exc).__name__, exc)
    check("a curve that crosses zero does not raise", crashed, "")
    check("...and the net loss is reported", m["net_pl"]["value"], -400.0)
    check("...annualised return is a DASH, never a complex number",
          m["annual_return"]["value"], None)
    check("...and it says why", bool(m["annual_return"]["reason"]), True)
    check("...calmar goes with it rather than dividing by a complex",
          m["calmar"]["value"], None)
    # gross_loss is a POSITIVE MAGNITUDE -- the platform convention, and the
    # number profit_factor divides by. Pinned so the sign cannot drift.
    check("...and the loss side is still measured, as a magnitude",
          m["gross_loss"]["value"], 590.0)
    check("...so profit factor is wins over losses",
          m["profit_factor"]["value"], round(190.0 / 590.0, 3))

    if FAIL:
        print(f"{FAIL} CHECK(S) FAILED")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
