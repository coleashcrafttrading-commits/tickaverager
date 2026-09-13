#!/usr/bin/env python3
"""test_report.py -- the operational HTML report tells the truth about gaps.

The reports are the thing an owner reads when he is NOT looking at the
dashboard, so every way a missing number can be dressed up as a comfortable
one has to be closed here:

  1. a null profit factor is "nothing lost yet", never infinity
  2. an unmarked stats block never renders 0 for total P/L
  3. the by-rung table carries the drawdown columns, and a rung with no MAE
     recorded says so instead of showing 0
  4. with no account equity the graph falls back to the booked curve AND
     says on the page that that is what it is
  5. nothing anywhere renders a bare Python `None`
  6. the chart is real inline SVG with dated time ticks, numbered trades and
     hover titles, and the print stylesheet forces a white ground

    .venv/Scripts/python test_report.py

No fleet, no keys, no network, no app: the journal is a scratch file and the
broker is a stub.
"""
from __future__ import annotations

import html as _html
import inspect
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_report_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")
for k in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
    os.environ[k] = ""

import journal                                    # noqa: E402
import htmlreport                                 # noqa: E402

htmlreport.REPORT_DIR = SCRATCH / "reports"

FAIL = 0
NOW = datetime.now(timezone.utc)


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


# ==================================================================== helpers
def text(hm: str) -> str:
    """The page as a reader sees it: no CSS, no SVG, no tags, no runs of
    whitespace. Assertions run against THIS, not the markup."""
    hm = re.sub(r"<style.*?</style>", " ", hm, flags=re.S)
    hm = re.sub(r"<svg.*?</svg>", " ", hm, flags=re.S)
    hm = re.sub(r"<[^>]+>", " ", hm)
    return re.sub(r"\s+", " ", _html.unescape(hm)).strip()


def cell_after(hm: str, label: str) -> str:
    """The first <td> following the <td> whose text is `label`."""
    for row in re.findall(r"<tr>(.*?)</tr>", hm, flags=re.S):
        tds = re.findall(r"<td>(.*?)</td>", row, flags=re.S)
        if tds and text(tds[0]).startswith(label):
            return text(tds[1]) if len(tds) > 1 else ""
    return "<row %r not found>" % label


def heads_of(hm: str, first: str) -> list:
    """Header cells of the table whose first column header is `first`."""
    for tbl in re.findall(r"<table>(.*?)</table>", hm, flags=re.S):
        hs = [text(h) for h in re.findall(r"<th>(.*?)</th>", tbl, flags=re.S)]
        if hs and hs[0] == first:
            return hs
    return []


def kpi_value(hm: str, key: str) -> str:
    for blk in re.findall(r'<div class="kpi">(.*?)</div>\s*</div>', hm, flags=re.S):
        m = re.search(r'<div class="k">(.*?)</div>.*?<div class="v[^"]*">(.*?)</div>',
                      blk, flags=re.S)
        if m and text(m.group(1)) == key:
            return text(m.group(2))
    return "<kpi %r not found>" % key


# ====================================================================== fakes
MARKS = {"RAM": 11.42, "MSTX": 10.05}


class FakeBroker:
    def __init__(self, fail=False):
        self.fail = fail

    def portfolio_history(self, period="1D", timeframe="1Min", extended=True,
                          date_end=""):
        if self.fail:
            raise RuntimeError("portfolio history: read timed out")
        base, n, step = 100000.0, 60, 3600
        ts, eq, pl = [], [], []
        for i in range(n):
            v = base + 40.0 * i - 3.0 * i * i
            ts.append((NOW - timedelta(seconds=step * (n - 1 - i))).timestamp())
            eq.append(round(v, 2))
            pl.append(round(v - base, 2))
        return {"timestamp": ts, "equity": eq, "profit_loss": pl,
                "base_value": base}


class FakeFleet:
    """Everything build_operational reads off a fleet, and nothing else."""

    def __init__(self, *, broker_fails=False, marks=None, jpath=None):
        self.broker = FakeBroker(broker_fails)
        self.marks = MARKS if marks is None else marks
        self.journal_path = jpath or Path(os.environ["TICKAVERAGER_JOURNAL"])

    def overview(self):
        ts = []
        for sym, lots in (("RAM", 4), ("MSTX", 2)):
            px = self.marks.get(sym)
            ts.append({"symbol": sym, "state": "IN LADDER", "running": True,
                       "dry_run": False, "halted": False, "last_price": px,
                       "lot_count": lots, "max_lots": 8, "shares": lots * 40,
                       "realized_today": 12.5,
                       "unrealized": None if px is None else -40.0})
        return {"ok": True, "paper": True,
                "account": {"number": "PA0000TEST", "status": "ACTIVE",
                            "equity": 100000.0, "cash": 90000.0,
                            "buying_power": 180000.0},
                "session": "regular", "market_open": True, "feed": "iex",
                "portfolio": {"account_value": 100000.0, "start_of_day": 99800.0,
                              "base_value": 99000.0, "today_pl": 200.0,
                              "total_pl": 1000.0, "made_today": 200.0,
                              "realized_today": 120.0, "open_today": 80.0,
                              "unrealized_today": 80.0, "realized_total": 900.0,
                              "open_pl": -120.0, "unrealized_total": -120.0,
                              "ladder_realized": 880.0, "cash": 90000.0,
                              "buying_power": 180000.0, "deployed": 4200.0,
                              "positions": [], "orders": [], "unmanaged": [],
                              "account_as_of": NOW.isoformat()},
                "tickers": ts,
                "totals": {"count": 2, "running": 2, "armed": 2, "halted": 0,
                           "lots": 6, "shares": 240, "realized_today": 25.0,
                           "uncovered": 0, "offbook": 0},
                "events": [], "snap_age": 1.0, "snap_error": None,
                "ui_version": "test"}


def write_journal(path: Path, *, losers=True, mae=True) -> None:
    """Six days of a small ladder: some lots clear, some are left open."""
    rows = []
    lot = 0
    cfg = {"add_distance": 0.10, "take_profit": 0.10, "shares_per_lot": 40}
    for day in range(6):
        for rung in range(4):
            lot += 1
            ts = NOW - timedelta(days=5 - day, hours=3 - rung)
            sym = "RAM" if lot % 3 else "MSTX"
            entry = round(12.00 - 0.10 * rung, 4)
            lid = "%s-%s-%04d" % (sym, ts.strftime("%Y%m%d"), lot)
            rows.append({"ts": ts.isoformat(), "event": "open", "symbol": sym,
                         "lot_id": lid, "rung": rung, "shares": 40,
                         "side": "long", "entry_price": entry,
                         "tp_price": round(entry + 0.10, 4),
                         "cost": round(entry * 40, 2), "session": "regular",
                         "cfg": cfg, "cfg_hash": "cfgtest01",
                         "why": "rung %d" % rung})
            if rung == 3 and day >= 3:
                continue                      # left open -> the inventory
            pl = 4.0
            if losers and lot % 7 == 0:
                pl = -9.5
            c = {"ts": (ts + timedelta(seconds=3600)).isoformat(),
                 "event": "close", "symbol": sym, "lot_id": lid, "rung": rung,
                 "shares": 40, "side": "long", "entry_price": entry,
                 "exit_price": round(entry + pl / 40, 4), "realized": pl,
                 "hold_seconds": 3600, "session": "regular",
                 "cfg_hash": "cfgtest01", "why": "take profit"}
            if mae and day >= 2:
                c["mae"] = -round(3.0 * (rung + 1), 2)
                c["mae_at"] = (ts + timedelta(seconds=900)).isoformat()
            rows.append(c)
    rows.sort(key=lambda r: r["ts"])
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def render(fleet, kind="weekly", days=7) -> str:
    return htmlreport.build_operational(fleet, kind=kind, days=days).read_text(
        encoding="utf-8")


# ======================================================================= main
def main() -> int:
    jp = Path(os.environ["TICKAVERAGER_JOURNAL"])
    write_journal(jp)

    print("\n1. the four kinds render, and the signature did not change")
    check("build_operational signature",
          str(inspect.signature(htmlreport.build_operational)),
          "(fleet: 'Any', kind: 'str' = 'daily', days: 'int' = 1, "
          "note: 'str' = '') -> 'Path'")
    pages = {}
    for kind, days in (("daily", 1), ("weekly", 7), ("inventory", 7), ("full", 0)):
        pages[kind] = render(FakeFleet(), kind, days)
    for kind in pages:
        t = text(pages[kind])
        check("%s report has the strategy-results block" % kind,
              ("Strategy results" in t, "By ladder rung" in t,
               "Open inventory" in t, "Journal tail" in t),
              (True, True, True, True))
    full = pages["full"]

    print("\n2. a null profit factor is not infinity")
    write_journal(jp, losers=False)
    clean = render(FakeFleet(), "full", 0)
    s = journal.stats(journal.load(path=jp), marks=MARKS,
                      inventory=journal.open_inventory(journal.load(path=jp)))
    check("the fixture really has nothing lost", (s["losses"], s["profit_factor"]),
          (0, None))
    check("profit factor cell says nothing lost yet",
          cell_after(clean, "Profit factor"), "nothing lost yet")
    check("no infinity anywhere on the page",
          bool(re.search(r"∞|\bInfinity\b|\binf\b", text(clean))), False)
    check("the win rate carries its caveat when nothing lost",
          ("artefact" in text(clean), "no stop loss" in text(clean)),
          (True, True))
    check("average loser says nothing lost yet, not $0.00",
          cell_after(clean, "Average loser"), "nothing lost yet")
    write_journal(jp)                                  # back to the normal set

    print("\n3. an unmarked stats block never renders 0 for total P/L")
    un = render(FakeFleet(marks={}), "weekly", 7)
    check("hero total P/L says needs live prices",
          text(re.search(r'<div class="v">(.*?)</div>', un, re.S).group(1)),
          "needs live prices")
    check("the total P/L tile says it too", kpi_value(un, "Total P/L"),
          "needs live prices")
    for tile in ("Unrealized", "Open market value"):
        check("the %s tile does not print a number" % tile.lower(),
              kpi_value(un, tile), "needs live prices")
    check("and it says the open side is unpriced, in words",
          ("The open side is not priced" in text(un),
           "They are NOT zero" in text(un)), (True, True))
    check("no $0.00 stands in for the missing open side",
          "-$0.00" in text(un) or "+$0.00" in kpi_value(un, "Total P/L"), False)
    check("marked report does print a real total",
          bool(re.match(r"^[-+]\$[\d,]+\.\d\d$", kpi_value(full, "Total P/L"))),
          True)

    print("\n4. the by-rung table carries the drawdown columns")
    heads = heads_of(full, "Rung")
    for col in ("Open", "Booked", "Unrealized", "Total P/L", "Max drawdown",
                "Avg drawdown", "Recorded on"):
        check("by-rung column %r" % col, col in heads, True)
    check("'Recorded on' counts the lots that actually carry an MAE",
          bool(re.search(r"<td>\d+ of \d+</td>", full)), True)
    write_journal(jp, mae=False)
    nm = render(FakeFleet(), "full", 0)
    check("with NO mae at all, every rung says not recorded yet",
          ("not recorded yet" in text(nm), "No rung carries a drawdown yet"
           in text(nm)), (True, True))
    check("and still no zero pretending to be a drawdown",
          cell_after(nm, "Average adverse excursion"), "not recorded yet")
    write_journal(jp)

    print("\n5. the graph falls back, and says which curve it is drawing")
    fb = render(FakeFleet(broker_fails=True), "weekly", 7)
    t = text(fb)
    check("the fallback is labelled on the page",
          ("Cumulative BOOKED P/L only" in t, "fallback curve, not total P/L" in t),
          (True, True))
    check("it still draws a chart", 'class="chart pl"' in fb, True)
    check("the account curve is NOT claimed when it is missing",
          "Alpaca's own account equity" in t, False)
    ok = text(full)
    check("and WITH equity the account curve is named",
          ("Alpaca's own account equity" in ok,
           "account total P/L (equity - base)" in ok.replace("−", "-")),
          (True, True))

    print("\n6. the chart is inline SVG: dated time ticks, numbered trades")
    svg = re.search(r"<svg[^>]*class=\"chart pl\".*?</svg>", full, re.S).group(0)
    ticks = re.findall(r'<text class="ax"[^>]*>([^<]+)</text>', svg)
    dated = [x for x in ticks if re.match(r"^\d{2} [A-Z][a-z]{2}", x)]
    check("at least four dated x ticks", len(dated) >= 4, True)
    check("trade numbers are drawn on the line",
          bool(re.search(r'class="dotn"[^>]*>#\d+<', svg)), True)
    titles = re.findall(r"<title>([^<]+)</title>", svg)
    trades = [x for x in titles if x.startswith("Trade ")]
    check("every marker carries a hover title with its trade number",
          (len(titles) > 0, any(x.startswith("Trade 1 ") for x in titles)),
          (True, True))
    check("the title names the time, the trade and the running total",
          all(k in trades[0] for k in ("UTC", "this trade", "booked to date",
                                       "total P/L")), True)
    check("a past point does not claim a total P/L it cannot know",
          "unknowable at a past moment" in trades[0], True)
    check("zero, the peak and the trough are marked",
          ("break even" in svg, "peak" in svg, "max drawdown" in svg),
          (True, True, True))
    check("no script and no network in the whole document",
          (("<script" in full.lower()), ("http://" in full or "https://" in full)),
          (False, False))

    print("\n7. nothing renders a bare None, and print forces white")
    for kind, hm in pages.items():
        check("%s: no bare None in the text" % kind,
              bool(re.search(r"\bNone\b", text(hm))), False)
    check("unmarked: no bare None either",
          bool(re.search(r"\bNone\b", text(un))), False)
    check("print stylesheet forces a white ground and dark ink",
          ("@media print" in full, "background:#fff !important" in full,
           "color:#000 !important" in full), (True, True, True))
    check("a light scheme is still defined",
          "@media (prefers-color-scheme:light)" in full, True)
    check("the dashboard's own tokens are inlined",
          all(x in full for x in ("#080611", "#0d0a1d", "#7c5cff", "#38a8ff",
                                  "#3ddc97", "#ff6b8a",
                                  "linear-gradient(135deg,#8b5cff,#5b8bff 55%,#38a8ff)")),
          True)

    print("\n8. the old one-argument journal.stats still renders")
    real = journal.stats
    try:
        def one_arg(rows, **kw):
            if kw:
                raise TypeError("stats() takes 1 positional argument")
            return {"opens": 1, "closes": 1, "bookkeeping_rows": 0,
                    "realized": 4.0, "shares_bought": 40, "shares_sold": 40,
                    "capital_deployed": 480.0, "return_on_deployed_pct": 0.8,
                    "avg_hold_seconds": 3600, "median_hold_seconds": 3600,
                    "max_hold_seconds": 3600, "trading_days": 1,
                    "realized_per_day": 4.0, "closes_per_day": 1.0,
                    "max_ladder_depth": 3, "by_rung": {}, "by_config": {},
                    "by_session": {}}
        journal.stats = one_arg
        legacy = render(FakeFleet(), "weekly", 7)
    finally:
        journal.stats = real
    lt = text(legacy)
    check("it degrades to needs live prices rather than crashing",
          ("needs live prices" in lt, "The open side is not priced" in lt),
          (True, True))
    check("and the closed-side metrics are recomputed and flagged",
          ("recomputed by this report" in lt, "Profit factor" in lt),
          (True, True))

    print("\n9. build_research still builds")
    out = htmlreport.build_research([], [], {}, {}, lambda *a: ("", []))
    rt = text(out.read_text(encoding="utf-8"))
    check("the research report still renders its no-winner verdict",
          "Nothing survived" in rt, True)

    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
