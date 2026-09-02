#!/usr/bin/env python3
"""
htmlreport.py -- reports as self-contained HTML, with charts that actually draw.

WHY NOT PDF
-----------
The PDF route sent Content-Disposition: attachment, so opening one in a new tab
downloaded the file and left an empty "Untitled" tab behind. That was a real
bug and it is fixed, but HTML is the better format here anyway: a chart can be
drawn, a table can be sorted by eye without pagination fighting it, and the tab
carries a title.

Everything is inline -- CSS, SVG, no scripts, no fonts, no network. The file
opens from a server, from disk, from an email attachment, and it prints. Charts
are SVG rather than canvas for exactly that reason: canvas does not print.
"""
from __future__ import annotations

import html
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports"


def esc(s: Any) -> str:
    return html.escape(str(s if s is not None else ""))


def money(n, dp=2) -> str:
    if n is None:
        return "—"
    n = float(n)
    return ("-" if n < 0 else "") + "$" + format(abs(n), ",.%df" % dp)


def signed(n, dp=2) -> str:
    if n is None:
        return "—"
    return ("+" if float(n) > 0 else "") + money(n, dp)


def num(n, dp=3) -> str:
    return "—" if n is None else ("%+.*f" % (dp, float(n)))


def cls(n) -> str:
    if n is None:
        return ""
    return "up" if float(n) > 0 else ("down" if float(n) < 0 else "")


# ====================================================================== charts
def svg_line(series: Sequence[Optional[float]], *, width: int = 720,
             height: int = 200, zero: bool = True, label: str = "",
             stamps: Optional[Sequence] = None, colour: str = "") -> str:
    """A line chart with the zero line always on the scale.

    Zero is forced into the range on purpose: a P/L curve autoscaled to its own
    values makes a strategy that lost money all year look like a tidy rising
    line, which is the single easiest way for a report to mislead.
    """
    pts = [(i, v) for i, v in enumerate(series) if v is not None]
    if len(pts) < 2:
        return '<div class="empty">not enough data to plot</div>'
    ys = [v for _, v in pts]
    lo, hi = min(ys), max(ys)
    if zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if hi == lo:
        hi += 1
        lo -= 1
    pad = (hi - lo) * 0.08
    lo -= pad
    hi += pad
    n = len(series)
    padL, padR, padT, padB = 4, 62, 10, 20
    pw = width - padL - padR
    ph = height - padT - padB
    X = lambda i: padL + (i / max(1, n - 1)) * pw
    Y = lambda v: padT + ph - ((v - lo) / (hi - lo)) * ph

    last = ys[-1]
    col = colour or ("var(--up)" if last >= 0 else "var(--down)")
    d = " ".join(("M" if k == 0 else "L") + "%.1f %.1f" % (X(i), Y(v))
                 for k, (i, v) in enumerate(pts))
    area = ("M%.1f %.1f " % (X(pts[0][0]), Y(0 if zero else lo))
            + " ".join("L%.1f %.1f" % (X(i), Y(v)) for i, v in pts)
            + " L%.1f %.1f Z" % (X(pts[-1][0]), Y(0 if zero else lo)))

    grid = []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        grid.append('<line x1="0" y1="%.1f" x2="%.1f" y2="%.1f" class="g"/>'
                    % (y, padL + pw, y))
        grid.append('<text x="%.1f" y="%.1f" class="ax">%s</text>'
                    % (padL + pw + 7, y + 3.5, esc(_short(v))))
    if zero and lo < 0 < hi:
        grid.append('<line x1="0" y1="%.1f" x2="%.1f" y2="%.1f" class="z"/>'
                    % (Y(0), padL + pw, Y(0)))

    times = ""
    if stamps is not None and len(stamps) == n:
        marks = []
        for k in range(5):
            i = round((n - 1) * k / 4)
            anchor = "start" if k == 0 else ("end" if k == 4 else "middle")
            marks.append('<text x="%.1f" y="%d" class="ax" text-anchor="%s">%s</text>'
                         % (X(i), height - 5, anchor, esc(str(stamps[i])[:10])))
        times = "".join(marks)

    return ('<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="%s">'
            '%s<path d="%s" fill="%s" opacity=".14"/>'
            '<path d="%s" fill="none" stroke="%s" stroke-width="1.8"/>%s</svg>'
            % (width, height, esc(label or "chart"), "".join(grid), area, col,
               d, col, times))


def svg_bars(labels: Sequence[str], values: Sequence[Optional[float]], *,
             width: int = 720, height: int = 210, label: str = "",
             fmt=None) -> str:
    """Horizontal-ish column chart, one bar per label, signed colours."""
    vals = [(l, v) for l, v in zip(labels, values) if v is not None]
    if not vals:
        return '<div class="empty">nothing to plot</div>'
    fmt = fmt or (lambda v: "%+.2f" % v)
    lo = min(0.0, min(v for _, v in vals))
    hi = max(0.0, max(v for _, v in vals))
    if hi == lo:
        hi += 1
    padT, padB, padL, padR = 14, 34, 4, 56
    ph = height - padT - padB
    pw = width - padL - padR
    Y = lambda v: padT + ph - ((v - lo) / (hi - lo)) * ph
    n = len(vals)
    bw = min(58, pw / max(1, n) * 0.62)
    step = pw / max(1, n)

    out = []
    y0 = Y(0)
    out.append('<line x1="0" y1="%.1f" x2="%.1f" y2="%.1f" class="z"/>'
               % (y0, padL + pw, y0))
    for k, (lbl, v) in enumerate(vals):
        cx = padL + step * (k + 0.5)
        y = Y(v)
        top = min(y, y0)
        h = max(1.0, abs(y - y0))
        c = "var(--up)" if v >= 0 else "var(--down)"
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                   'rx="2" fill="%s" opacity=".85"/>'
                   % (cx - bw / 2, top, bw, h, c))
        out.append('<text x="%.1f" y="%.1f" class="bv" text-anchor="middle">%s</text>'
                   % (cx, (top - 4) if v >= 0 else (top + h + 11), esc(fmt(v))))
        out.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
                   % (cx, height - 6, esc(lbl)))
    return ('<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="%s">'
            '%s</svg>' % (width, height, esc(label or "chart"), "".join(out)))


def _short(v: float) -> str:
    a = abs(v)
    s = "-" if v < 0 else ""
    if a >= 1_000_000:
        return "%s%.1fM" % (s, a / 1_000_000)
    if a >= 1000:
        return "%s%.1fk" % (s, a / 1000)
    if a >= 10:
        return "%s%.0f" % (s, a)
    return "%s%.2f" % (s, a)


# ======================================================================= shell
CSS = """
:root{
  --bg:#ffffff; --panel:#f7f9fc; --ink:#16202e; --dim:#5b6b80;
  --line:#dde4ec; --up:#0a7a42; --down:#c0392b; --warn:#9a6b00;
  --accent:#2b5fa8;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
@media (prefers-color-scheme:dark){
  :root{ --bg:#0f1319; --panel:#161c24; --ink:#e6ecf4; --dim:#8b9bb0;
         --line:#26303c; --up:#35c98b; --down:#f2555a; --warn:#e8a33d;
         --accent:#5b93e6; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;}
.wrap{max-width:960px;margin:0 auto;padding:34px 22px 80px}
h1{font-size:27px;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:19px;margin:38px 0 10px;padding-bottom:7px;
   border-bottom:1px solid var(--line);letter-spacing:-.01em}
h3{font-size:15px;margin:24px 0 8px;color:var(--accent)}
h4{font-size:13px;margin:20px 0 6px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.sub{color:var(--dim);font-size:13px;margin:0 0 8px}
p{margin:0 0 11px}
.lead{font-size:15px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin:14px 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:1px;background:var(--line);border:1px solid var(--line);
  border-radius:10px;overflow:hidden;margin:16px 0}
.kpi{background:var(--bg);padding:13px 15px}
.kpi .k{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--dim)}
.kpi .v{font-size:21px;font-weight:640;margin-top:3px;font-variant-numeric:tabular-nums}
.kpi .s{font-size:11.5px;color:var(--dim);margin-top:2px}
table{width:100%;border-collapse:collapse;margin:10px 0;font-size:13px}
th{text-align:right;font-weight:600;color:var(--dim);font-size:11px;
   letter-spacing:.05em;text-transform:uppercase;padding:7px 9px;
   border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:right;
   font-variant-numeric:tabular-nums}
tbody tr:hover{background:var(--panel)}
.up{color:var(--up)} .down{color:var(--down)} .warn{color:var(--warn)}
.dim{color:var(--dim)} .mono{font-family:var(--mono);font-size:12px}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;
  background:var(--line);color:var(--dim)}
.pill.ok{background:rgba(10,122,66,.15);color:var(--up)}
.pill.no{background:rgba(192,57,43,.15);color:var(--down)}
.note{border-left:3px solid var(--accent);background:var(--panel);
  padding:11px 14px;border-radius:0 8px 8px 0;margin:14px 0;font-size:13.5px}
.note.bad{border-left-color:var(--down)}
.note.good{border-left-color:var(--up)}
.note.warn{border-left-color:var(--warn)}
.chart{width:100%;height:auto;display:block;margin:6px 0 2px}
.chart .g{stroke:var(--line);stroke-width:1}
.chart .z{stroke:var(--dim);stroke-width:1;stroke-dasharray:4 3}
.chart .ax{fill:var(--dim);font-size:10px;font-family:var(--mono)}
.chart .bv{fill:var(--ink);font-size:10.5px;font-family:var(--mono)}
.empty{color:var(--dim);padding:20px;text-align:center;font-size:13px}
.check{margin:3px 0;font-size:13px}
.check .m{font-weight:700;margin-right:6px}
ul{margin:0 0 12px;padding-left:20px} li{margin:4px 0}
code{font-family:var(--mono);font-size:12.5px;background:var(--panel);
  padding:1px 5px;border-radius:4px;border:1px solid var(--line)}
.foot{margin-top:44px;padding-top:14px;border-top:1px solid var(--line);
  color:var(--dim);font-size:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 18px}
.bar a{font-size:12.5px;color:var(--accent);text-decoration:none;
  border:1px solid var(--line);padding:5px 11px;border-radius:7px}
.bar a:hover{background:var(--panel)}
@media print{
  body{background:#fff;color:#000}
  .wrap{max-width:none;padding:0}
  .bar{display:none}
  h2{page-break-after:avoid} .card,table{page-break-inside:avoid}
  a{text-decoration:none;color:#000}
}
"""


def page(title: str, body: str, subtitle: str = "") -> str:
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>%s</title><style>%s</style></head><body><div class=\"wrap\">"
        "<h1>%s</h1><p class=\"sub\">%s</p>"
        "<div class=\"bar\"><a href=\"#\" onclick=\"window.print();return false\">"
        "Print / save as PDF</a></div>%s"
        "<div class=\"foot\">Generated by TickAverager from real Alpaca bars. "
        "Nothing in this document was traded, and nothing was armed.</div>"
        "</div></body></html>"
        % (esc(title), CSS, esc(title), subtitle, body))


def kpi(k: str, v: str, s: str = "", klass: str = "") -> str:
    return ('<div class="kpi"><div class="k">%s</div>'
            '<div class="v %s">%s</div>%s</div>'
            % (esc(k), klass, v, ('<div class="s">%s</div>' % s) if s else ""))


def table(heads: Sequence[str], rows: Sequence[Sequence[str]],
          empty: str = "Nothing here.") -> str:
    if not rows:
        return '<div class="empty">%s</div>' % esc(empty)
    th = "".join("<th>%s</th>" % esc(h) for h in heads)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r)
                   for r in rows)
    return "<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>" % (th, body)


# ============================================================== operational
def build_operational(fleet: Any, kind: str = "daily", days: int = 1,
                      note: str = "") -> Path:
    """The daily / weekly / inventory / full-history report, as HTML.

    Realized P/L is always shown NEXT TO open inventory and its age. This
    strategy has no stop loss, so realized alone looks excellent right up until
    the day it does not; the age and size of what is still open is where the
    risk lives, and separating the two on different pages is how a report
    flatters a ladder.
    """
    import journal

    REPORT_DIR.mkdir(exist_ok=True)
    now = datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d_%H%M")
    out = REPORT_DIR / ("%s_%s.html" % (kind, stamp))

    ov = fleet.overview()
    p = ov["portfolio"]
    rows = journal.load(days=None if kind == "full" else days)
    stats = journal.stats(rows)
    inv = journal.open_inventory(journal.load())

    titles = {"daily": "Daily report", "weekly": "Weekly report",
              "inventory": "Open inventory", "full": "Full history"}
    title = titles.get(kind, "Report")
    sub = ("%s &middot; Alpaca %s %s%s"
           % (now.strftime("%A, %d %B %Y at %H:%M"),
              "paper" if ov["paper"] else "LIVE", esc(ov["account"]["number"]),
              (" &middot; " + esc(note)) if note else ""))

    B = []

    # ---- headline ----
    made = p["made_today"]
    B.append('<div class="kpis">'
             + kpi("Net today", signed(made), "", cls(made))
             + kpi("Account", money(p["account_value"]))
             + kpi("Open P/L", signed(p["open_pl"]), "", cls(p["open_pl"]))
             + kpi("Deployed", money(p["deployed"], 0),
                   "%.1f%% of equity" % (100 * p["deployed"] / p["account_value"])
                   if p["account_value"] else "")
             + "</div>")

    # ---- the curve ----
    closes = [r for r in rows if r.get("event") in ("close", "partial")
              and not journal.is_bookkeeping(r)]
    closes.sort(key=lambda r: str(r.get("ts", "")))
    run = 0.0
    curve, stamps = [], []
    for r in closes:
        run += float(r.get("realized") or 0)
        curve.append(round(run, 2))
        stamps.append(str(r.get("ts", ""))[:10])
    B.append("<h2>Realized, trade by trade</h2>")
    if len(curve) >= 2:
        B.append('<div class="card">%s</div>'
                 % svg_line(curve, stamps=stamps, height=210,
                            label="cumulative realized P/L"))
        B.append('<p class="sub">Cumulative realized only. It does NOT include '
                 'the %d lot(s) still open, worth %s at the moment this was '
                 'written &mdash; which is the number that can move against '
                 'you.</p>'
                 % (len(inv), signed(p["open_pl"])))
    else:
        B.append('<div class="empty">Not enough closed trades in this window '
                 'to draw a curve.</div>')
    bk = stats.get("bookkeeping_rows") or 0
    if bk:
        B.append('<div class="note warn"><b>%d row(s) in this window are '
                 'bookkeeping, not trades</b>, and are excluded from every '
                 'figure above. They are written when the journal and the '
                 'ledger are re-synced: a lot that had already gone is closed '
                 'in the journal at no price so the two agree. Counting them '
                 'would report closed trades on a day nothing traded.</div>'
                 % bk)

    # ---- per ticker ----
    B.append("<h2>Per ticker</h2>")
    trs = []
    for t in ov["tickers"]:
        sym = t["symbol"]
        sr = [r for r in rows if r.get("symbol") == sym
              and not journal.is_bookkeeping(r)]
        o = len([r for r in sr if r.get("event") == "open"])
        c = len([r for r in sr if r.get("event") == "close"])
        pf = len([r for r in sr if r.get("event") == "partial"])
        trs.append([
            "<b>%s</b>" % esc(sym),
            '<span class="pill">%s</span>' % esc(t["state"]),
            str(o), str(c), str(pf),
            '<span class="%s">%s</span>' % (cls(t["realized_today"]),
                                            signed(t["realized_today"])),
            '<span class="%s">%s</span>' % (cls(t["unrealized"]),
                                            signed(t["unrealized"])),
            "%d/%d" % (t["lot_count"], t["max_lots"]),
            str(t["shares"]),
        ])
    B.append(table(["Ticker", "State", "Opened", "Closed", "Partials",
                    "Realized", "Open P/L", "Lots", "Shares"], trs,
                   "No tickers configured."))

    syms = [t["symbol"] for t in ov["tickers"]]
    pls = [t["realized_today"] + t["unrealized"] for t in ov["tickers"]]
    if syms:
        B.append('<div class="card">%s</div>'
                 % svg_bars(syms, pls, height=190, label="P/L by ticker",
                            fmt=lambda v: signed(v, 0)))

    # ---- where the money came from ----
    B.append("<h2>Where the money came from</h2>")
    B.append(table(["", "Amount"], [
        ["Realized &mdash; lots that sold",
         '<span class="%s">%s</span>' % (cls(p["realized_today"]),
                                         signed(p["realized_today"]))],
        ["Open lots &mdash; move since yesterday's close",
         '<span class="%s">%s</span>' % (cls(p["open_today"]),
                                         signed(p["open_today"]))],
        ["<b>Net today</b>",
         '<b class="%s">%s</b>' % (cls(made), signed(made))],
    ]))

    # ---- the journal window ----
    B.append("<h2>Journal &mdash; %s</h2>"
             % ("all history" if kind == "full" else "last %d day(s)" % days))
    B.append(table(["Metric", "Value"], [
        ["Lots opened", str(stats["opens"])],
        ["Lots closed", str(stats["closes"])],
        ["Realized", signed(stats["realized"])],
        ["Capital deployed", money(stats["capital_deployed"], 0)],
        ["Return on deployed", "%.3f%%" % stats["return_on_deployed_pct"]],
        ["Median hold", "%d min" % (stats["median_hold_seconds"] // 60)],
        ["Longest hold", "%.1f h" % (stats["max_hold_seconds"] / 3600)],
        ["Deepest ladder", str(stats["max_ladder_depth"])],
    ]))

    # ---- rungs ----
    br = stats.get("by_rung") or {}
    if br:
        B.append("<h2>By ladder rung</h2>")
        B.append(table(["Rung", "Opened", "Closed", "Still open", "Realized",
                        "Avg hold"],
                       [[r, str(d["opened"]), str(d["closed"]),
                         '<span class="%s">%d</span>'
                         % ("warn" if d["opened"] - d["closed"] > 0 else "dim",
                            d["opened"] - d["closed"]),
                         '<span class="%s">%s</span>' % (cls(d["realized"]),
                                                         signed(d["realized"])),
                         "%dm" % (d["avg_hold_seconds"] // 60)
                         if d["avg_hold_seconds"] else "&mdash;"]
                        for r, d in br.items()]))
        B.append('<p class="sub">A rung that opens often and closes rarely is '
                 'where the add distance is putting money it does not get back '
                 'quickly.</p>')

    # ---- inventory ----
    B.append("<h2>Open inventory</h2>")
    if inv:
        aged = [x for x in inv if x["age_days"] > 3]
        B.append('<div class="note%s"><b>%d lot(s)</b> holding <b>%s</b>%s</div>'
                 % (" warn" if aged else "", len(inv),
                    money(sum(x["cost"] for x in inv), 0),
                    (", of which <b>%d</b> are more than 3 days old (<b>%s</b>)."
                     % (len(aged), money(sum(x["cost"] for x in aged), 0)))
                    if aged else "."))
        B.append(table(["Lot", "Symbol", "Age", "Shares", "Entry", "Target",
                        "Cost"],
                       [['<span class="mono">%s</span>' % esc(x["lot_id"]),
                         esc(x["symbol"]),
                         '<span class="%s">%.1fd</span>'
                         % ("down" if x["age_days"] > 3
                            else "warn" if x["age_days"] > 1 else "dim",
                            x["age_days"]),
                         str(x["shares"]), money(x["entry_price"], 4),
                         money(x["tp_price"]), money(x["cost"], 0)]
                        for x in inv[:60]]))
    else:
        B.append('<div class="empty">Nothing open.</div>')

    B.append('<div class="note">Realized P/L is stated next to open inventory '
             'on purpose. This strategy has <b>no stop loss</b>, so a ladder '
             'always looks profitable until the day it does not &mdash; the age '
             'and size of what is still open is where the risk actually is. '
             'Per-ticker realized uses each ladder\'s specific-lot accounting; '
             'the account figure uses Alpaca\'s average-cost convention, so the '
             'two can differ on shares carried in from a prior session.</div>')

    out.write_text(page(title, "".join(B), sub), encoding="utf-8")
    return out


# ================================================================== research
def build_research(passes: list, winners: list, abl: dict, val: dict,
                   verdict_of, top: int = 5) -> Path:
    """The strategy research report, as HTML with charts.

    Leads with what would have happened OUT OF SAMPLE, states the selection
    method before the results, and puts the control families next to the
    winners. A strategy report that does not show what a coin flip scored on
    the same bars is a sales document.
    """
    REPORT_DIR.mkdir(exist_ok=True)
    now = datetime.now().astimezone()
    out = REPORT_DIR / ("strategy_research_%s.html" % now.strftime("%Y-%m-%d_%H%M"))

    n_bt = sum(p.get("n_backtests", 0) for p in passes)
    secs = sum(p.get("seconds", 0) for p in passes)
    B = []

    # ---------------------------------------------------------- the verdict
    B.append("<h2>The short version</h2>")
    if not winners:
        B.append('<div class="note bad"><b>Nothing survived.</b> No strategy '
                 'was profitable out of sample on a majority of symbols after '
                 'costs. That is a real result, and the honest thing to do '
                 'with it is nothing.</div>')
    else:
        full = [w for w in winners
                if verdict_of(w, abl, val)[0].startswith("5 of")]
        B.append('<p class="lead">%d strateg%s came through the search. '
                 '<b>%d cleared all five checks.</b> Each check below can kill '
                 'a result on its own, and the ranking is by evidence, not by '
                 'score &mdash; the score is measured on the window that '
                 'produced it and is the weakest thing here.</p>'
                 % (len(winners), "y" if len(winners) == 1 else "ies",
                    len(full)))

    pfs = [(w["family"], (w.get("risk") or {}).get("profit_factor"))
           for w in winners if w.get("risk")]
    thin = [n for n, v in pfs if v is not None and v < 1.15]
    if thin:
        B.append('<div class="note bad"><b>Read the profit factors before the '
                 'scores.</b> %d of these %s a profit factor under 1.15 &mdash; '
                 'gross profit barely exceeds gross loss. The headline score '
                 'measures profit against DRAWDOWN, so a strategy can score '
                 'well by risking little while having almost no edge per '
                 'trade. %s. At that margin the whole result sits inside the '
                 'cost assumption.</div>'
                 % (len(thin), "have" if len(thin) > 1 else "has",
                    esc(", ".join(thin))))

    # a chart of the field
    names = [w["family"][:16] for w in winners]
    scores = [w["test"]["median_score"] for w in winners]
    if names:
        B.append('<div class="card"><h3>Out-of-sample score, top %d</h3>%s'
                 '<p class="sub">Total P/L per dollar of maximum drawdown, on '
                 'the median symbol. Unitless, so a $600 index and a $12 stock '
                 'can sit on the same axis.</p></div>'
                 % (len(names), svg_bars(names, scores, height=200,
                                         label="out-of-sample score")))
    pfv = [(w.get("risk") or {}).get("profit_factor") for w in winners]
    if any(v is not None for v in pfv):
        # plotted against 1.0, not zero: for a profit factor, one is the line
        # between making money and losing it, and a bar chart anchored at zero
        # makes 1.03 and 1.36 look like the same healthy green column
        B.append('<div class="card"><h3>Profit factor, minus the 1.0 '
                 'break-even line</h3>%s<p class="sub">Gross profit &divide; '
                 'gross loss. Anything at or below the line loses money. This '
                 'is plotted as the DISTANCE from 1.0 because a chart anchored '
                 'at zero makes 1.03 and 1.36 look equally healthy, and they '
                 'are not remotely.</p></div>'
                 % svg_bars(names, [None if v is None else v - 1.0 for v in pfv],
                            height=185, label="profit factor above break-even",
                            fmt=lambda v: "%.2f" % (v + 1.0)))

    # ---------------------------------------------------------- the method
    B.append("<h2>How these were chosen &mdash; read this first</h2>")
    B.append("<ul>" + "".join("<li>%s</li>" % x for x in [
        "<b>Chronological split.</b> Each symbol's history is cut in two. "
        "Parameters were chosen on the FIRST 60% only. The last 40% chose "
        "nothing &mdash; it only reports what the choice was worth.",
        "<b>A third window.</b> The survivors were then re-run on a period "
        "<i>before</i> the training data began, with the same parameters and "
        "no re-tuning. That is the check almost everything failed.",
        "<b>Median symbol, not best.</b> One spectacular symbol among eight is "
        "a curve fit, and an average lets it hide.",
        "<b>Controls in the same race.</b> Random entry and always-long ran "
        "with identical stops, targets and costs.",
        "<b>Ablation.</b> Every winner was re-run with random entries but its "
        "own execution parameters, to separate the signal from the position "
        "management. Several failed that.",
        "<b>Costs always on</b>, estimated per symbol from its own median bar "
        "range, and every winner re-run at half, double and quadruple it.",
        "<b>No look-ahead, structurally.</b> The runner physically refuses to "
        "let a strategy read a bar it could not have seen.",
    ]) + "</ul>")

    # ---------------------------------------------------------- the control
    B.append("<h2>What a coin flip scored</h2>")
    crows = []
    for p in passes:
        for c in p.get("selected", []):
            if not c.get("family", "").startswith("CONTROL"):
                continue
            te = c.get("test") or {}
            tr = c.get("train") or {}
            crows.append([
                esc(p.get("_label") or p.get("timeframe")),
                esc(c["family"].replace("CONTROL: ", "")),
                '<span class="%s">%s</span>' % (cls(tr.get("median_score")),
                                                num(tr.get("median_score"))),
                '<span class="%s">%s</span>' % (cls(te.get("median_score")),
                                                num(te.get("median_score"))),
                "%d/%d" % (te.get("symbols_profitable", 0),
                           te.get("symbols_scored", 0)),
                format(te.get("total_trades", 0), ","),
            ])
    B.append(table(["Bars", "Control", "In sample", "Out of sample",
                    "Symbols +", "Trades"], crows,
                   "No control ran, so nothing here is compared to anything."))
    B.append('<div class="note warn">Read the two score columns together. '
             'Random entry &mdash; an entry carrying <b>no information at '
             'all</b> &mdash; scored strongly positive in sample and negative '
             'out of it, on every timeframe independently. That is not a '
             'curiosity, it is the whole reason the split exists: the '
             'selection process can make pure noise look excellent.</div>')

    # ---------------------------------------------------------- the winners
    B.append("<h2>The top %d</h2>" % len(winners))
    for i, w in enumerate(winners, 1):
        te, tr = w["test"], w["train"]
        headline, lines = verdict_of(w, abl, val)
        note = ""
        for p in passes:
            note = (p.get("families") or {}).get(w["family"]) or note

        B.append('<h3>%d. %s <span class="pill">%s bars</span> '
                 '<span class="pill %s">%s</span></h3>'
                 % (i, esc(w["family"]), esc(w["timeframe"]),
                    "ok" if headline.startswith("5 of") else "",
                    esc(headline)))
        if note:
            B.append('<p class="sub">%s</p>' % esc(note))

        for ln in lines:
            bad = ln.startswith("NOT: ")
            B.append('<div class="check"><span class="m %s">%s</span>%s</div>'
                     % ("down" if bad else "up", "&#10007;" if bad else "&#10003;",
                        esc(ln.replace("NOT: ", ""))))

        ps = ", ".join("%s=%s" % (k, v) for k, v in sorted(w["params"].items())
                       if k not in ("shares", "add_scale", "cooldown",
                                    "time_stop", "exit_on_flip", "atr_n")
                       and v not in (0, 0.0))
        B.append('<p class="sub" style="margin-top:9px"><b>Settings:</b> '
                 '<code>%s</code></p>' % esc(ps))

        # per-symbol out of sample
        per = w.get("per_symbol_test") or {}
        syms = list(per)
        pls = [per[s].get("total_pl") for s in syms]
        if syms:
            B.append('<div class="card">%s<p class="sub">Out-of-sample P/L by '
                     'symbol, 100 shares a trade. The spread across symbols is '
                     'the honest picture &mdash; the headline is the middle '
                     'one, not the best.</p></div>'
                     % svg_bars(syms, pls, height=185,
                                label="P/L by symbol",
                                fmt=lambda v: signed(v, 0)))

        rows = [
            ["Out-of-sample score", '<b class="%s">%s</b>'
             % (cls(te["median_score"]), num(te["median_score"]))],
            ["In-sample score, for comparison", num(tr["median_score"])],
            ["Decay out of sample (1.0 = held perfectly)",
             "&mdash;" if w.get("decay") is None else "%.2f" % w["decay"]],
            ["Symbols profitable out of sample",
             "%d of %d" % (te["symbols_profitable"], te["symbols_scored"])],
            ["Worst symbol", num(te["worst_score"])],
            ["Trades out of sample", format(te["total_trades"], ",")],
            ["Summed P/L, 100 shares a trade", signed(te["sum_pl"])],
        ]
        a = abl.get((w["family"], w.get("timeframe")))
        if a:
            rows += [
                ["<b>Random entry, same execution parameters</b>",
                 '<span class="%s">%s</span>'
                 % (cls(a["exec_only"]["median_score"]),
                    num(a["exec_only"]["median_score"]))],
                ["The same signal with averaging-down off",
                 num(a["no_adds"]["median_score"])],
            ]
        v = val.get((w["family"], w.get("timeframe")))
        if v and v.get("earlier") is not None:
            rows.append(["<b>On an earlier window the search never saw</b>"
                         "<br><span class='sub'>%s to %s</span>"
                         % (str(v.get("_from"))[:10], str(v.get("_to"))[:10]),
                         '<b class="%s">%s</b><br><span class="sub">control '
                         'there: %s</span>'
                         % (cls(v["earlier"]), num(v["earlier"]),
                            num(v.get("_bar")))])
        scan = w.get("cost_scan") or {}
        for m, lbl in (("0.5", "half"), ("1.0", "the assumed"),
                       ("2.0", "double"), ("4.0", "quadruple")):
            d = scan.get(m)
            if d:
                rows.append(["At %s cost" % lbl,
                             '<span class="%s">%s</span>'
                             % (cls(d["median_score"]), num(d["median_score"]))])
        B.append(table(["", "Value"], rows))

        rk = w.get("risk")
        if rk:
            B.append("<h4>Trade statistics and risk</h4>")
            B.append(table(["", "Value", ""], [
                ["Percent profitable",
                 '<b>%.1f%%</b>' % rk["win_rate"],
                 '<span class="sub">%s winners, %s losers, %s trades</span>'
                 % (format(rk["winners"], ","), format(rk["losers"], ","),
                    format(rk["trades"], ","))],
                ["Gross profit",
                 '<span class="up">%s</span>' % signed(rk["gross_profit"]),
                 '<span class="sub">everything the winners made</span>'],
                ["Gross loss",
                 '<span class="down">%s</span>' % money(-abs(rk["gross_loss"])),
                 '<span class="sub">everything the losers cost</span>'],
                ["Profit factor",
                 '<b>%s</b>' % ("&mdash;" if rk["profit_factor"] is None
                                else "%.2f" % rk["profit_factor"]),
                 '<span class="sub">gross profit &divide; gross loss. Below 1.0 '
                 'loses money</span>'],
                ["Average winning trade",
                 '<span class="up">%s</span>' % signed(rk["avg_win"]), ""],
                ["Average losing trade",
                 '<span class="down">%s</span>' % signed(rk["avg_loss"]), ""],
                ["Win / loss size ratio",
                 "&mdash;" if rk["win_loss_ratio"] is None
                 else "%.2f" % rk["win_loss_ratio"],
                 '<span class="sub">above 1.0 means winners are bigger than '
                 'losers</span>'],
                ["Largest winning trade",
                 '<span class="up">%s</span>' % signed(rk["largest_win"]), ""],
                ["Largest losing trade",
                 '<b class="down">%s</b>' % signed(rk["largest_loss"]),
                 '<span class="sub">the worst single trade on any symbol, not '
                 'an average</span>'],
                ["Expectancy per trade",
                 '<span class="%s">%s</span>' % (cls(rk["expectancy"]),
                                                 signed(rk["expectancy"])),
                 '<span class="sub">what one trade is worth on average</span>'],
                ["Worst drawdown, any symbol",
                 '<b class="down">%s</b>' % signed(rk["worst_drawdown"]),
                 '<span class="sub">what you would have had to sit '
                 'through</span>'],
                ["Median symbol drawdown",
                 '<span class="down">%s</span>' % signed(rk["median_drawdown"]),
                 '<span class="sub">the typical case rather than the worst '
                 'one</span>'],
                ["Longest losing streak",
                 "%d trades" % rk["max_consecutive_losses"],
                 '<span class="sub">consecutive losers, on the worst '
                 'symbol</span>'],
                ["Average lots open at once",
                 '<b>%.2f</b>' % rk["avg_open_lots"],
                 '<span class="sub">measured only while a position is held</span>'],
                ["Most lots open at once", str(rk["max_open_lots"]),
                 '<span class="sub">drives the worst-case capital</span>'],
                ["Time in the market", "%.1f%%" % rk["exposure_pct"],
                 '<span class="sub">of all bars</span>'],
                ["Average bars in a trade", "%.1f" % rk["avg_bars_in_trade"], ""],
            ]))

            ps = rk.get("per_symbol") or {}
            if ps:
                B.append(table(["Symbol", "Trades", "Win %", "Total P/L",
                                "Max DD", "PF", "Largest loss", "Avg lots",
                                "Time in"],
                               [[esc(sym),
                                 format(d["trades"], ","),
                                 "%.0f%%" % (d["win_rate"] or 0),
                                 '<span class="%s">%s</span>'
                                 % (cls(d["total_pl"]), signed(d["total_pl"])),
                                 '<span class="down">%s</span>' % signed(d["max_dd"]),
                                 "&mdash;" if d["pf"] is None else "%.2f" % d["pf"],
                                 '<span class="down">%s</span>'
                                 % signed(d["largest_loss"]),
                                 "%.1f" % (d["avg_open"] or 0),
                                 "%.0f%%" % (d["expo"] or 0)]
                                for sym, d in ps.items()]))

        # ---------------------------------------------- buy and hold
        bn = w.get("bench")
        if bn:
            edge = bn["edge"]
            won = bn["beat_on"] >= (bn["symbols"] + 1) // 2 and edge > 0
            tilt = bn.get("median_tilt", 0.0)
            ds = bn.get("median_drift_share")
            shape = ("a net-LONG book" if tilt > 0.25 else
                     "a net-SHORT book" if tilt < -0.25 else
                     "a genuinely two-sided book")
            B.append("<h4>Against buy and hold &mdash; the passive twin</h4>")
            B.append('<div class="note %s"><b>%s the passive twin by %s in '
                     'total, and on %d of %d symbols.</b> The twin holds a '
                     'CONSTANT position equal to the strategy\'s own average '
                     'SIGNED share exposure, from the first bar to the last, '
                     'with no timing at all. Signed, so a short strategy is '
                     'measured against a short twin; time-weighted, so idle '
                     'bars count as no exposure; and timing-free, because a '
                     'twin that copied the entry and exit bars would BE the '
                     'strategy and could only ever show zero edge. '
                     'Median tilt %+.2f &mdash; %s.</div>'
                     % ("good" if won else "bad",
                        "Beats" if edge > 0 else "LOSES to", signed(edge),
                        bn["beat_on"], bn["symbols"], tilt, shape))
            if ds is not None and ds < 0.10:
                B.append('<div class="note good"><b>Drift cannot explain this '
                         'result.</b> The book held only %.1f net shares on the '
                         'median symbol, so passive exposure accounts for under '
                         '10%% of the P/L. That rules drift OUT; it does not on '
                         'its own prove a signal &mdash; that is what the five '
                         'checks above are for.</div>'
                         % abs(bn.get("median_tilt", 0) * 100))
            elif ds is not None and ds > 0.75:
                B.append('<div class="note bad"><b>Most of this is drift.</b> '
                         'The passive twin accounts for %.0f%% of the P/L on '
                         'the median symbol. The trading is adding little over '
                         'simply holding that much exposure.</div>' % (ds * 100))
            if bn.get("edge_long") or bn.get("edge_short"):
                B.append(table(["Leg", "P/L", "Its own twin would have made",
                                "Edge"], [
                    ["Long leg",
                     '<span class="%s">%s</span>'
                     % (cls(sum(d.get("long_pl", 0) for d in (ps or {}).values())),
                        signed(sum(d.get("long_pl", 0) for d in (ps or {}).values()))),
                     "", '<b class="%s">%s</b>' % (cls(bn["edge_long"]),
                                                   signed(bn["edge_long"]))],
                    ["Short leg",
                     '<span class="%s">%s</span>'
                     % (cls(sum(d.get("short_pl", 0) for d in (ps or {}).values())),
                        signed(sum(d.get("short_pl", 0) for d in (ps or {}).values()))),
                     "", '<b class="%s">%s</b>' % (cls(bn["edge_short"]),
                                                   signed(bn["edge_short"]))],
                ]))
                B.append('<p class="sub">Each leg is charged a twin with its '
                         'OWN sign. A short leg that lost money while a passive '
                         'short would have lost more still has positive edge; a '
                         'long leg that made money in a rising market can have '
                         'negative edge. The two edges sum exactly to the total.'
                         '</p>')
            B.append(table(["", "Strategy", "Passive twin", "Difference"], [
                ["Total P/L across all symbols",
                 '<b class="%s">%s</b>' % (cls(bn["strategy_total"]),
                                           signed(bn["strategy_total"])),
                 '<span class="%s">%s</span>' % (cls(bn["buy_hold_total"]),
                                                 signed(bn["buy_hold_total"])),
                 '<b class="%s">%s</b>' % (cls(edge), signed(edge))],
                ["Worst drawdown",
                 '<span class="down">%s</span>' % signed(rk["worst_drawdown"]
                                                         if rk else 0),
                 '<span class="down">%s</span>' % signed(bn["worst_bh_drawdown"]),
                 '<span class="%s">%s</span>'
                 % (cls((bn["worst_bh_drawdown"]) - (rk["worst_drawdown"] if rk else 0)),
                    signed((rk["worst_drawdown"] if rk else 0)
                           - bn["worst_bh_drawdown"]))],
                ["Median symbol drawdown",
                 '<span class="down">%s</span>' % signed(rk["median_drawdown"]
                                                         if rk else 0),
                 '<span class="down">%s</span>' % signed(bn["median_bh_drawdown"]),
                 ""],
            ]))
            if ps:
                bars_l = list(ps)
                B.append('<div class="card">%s<p class="sub">Strategy P/L per '
                         'symbol MINUS what capital-matched buy-and-hold made '
                         'on the same symbol. Bars above the line are where the '
                         'strategy actually added something; bars below are '
                         'where holding that exposure passively, with no '
                         'timing, would have done better.</p></div>'
                         % svg_bars(bars_l, [ps[s2].get("vs_bh") for s2 in bars_l],
                                    height=190, label="edge over buy and hold",
                                    fmt=lambda v: signed(v, 0)))
                B.append(table(["Symbol", "Strategy", "Passive twin",
                                "Edge", "Tape moved", "Twin shares",
                                "Strategy DD", "B&amp;H DD"],
                               [[esc(sym),
                                 '<span class="%s">%s</span>'
                                 % (cls(d["total_pl"]), signed(d["total_pl"])),
                                 '<span class="%s">%s</span>'
                                 % (cls(d.get("bh_dollars")),
                                    signed(d.get("bh_dollars"))),
                                 '<b class="%s">%s</b>' % (cls(d.get("vs_bh")),
                                                           signed(d.get("vs_bh"))),
                                 '<span class="%s">%+.1f%%</span>'
                                 % (cls(d.get("bh_pct")), d.get("bh_pct") or 0),
                                 "%+.0f" % (d.get("bh_shares") or 0),
                                 '<span class="down">%s</span>' % signed(d["max_dd"]),
                                 '<span class="down">%s</span>'
                                 % signed(d.get("bh_drawdown"))]
                                for sym, d in ps.items()]))
                drift = [s2 for s2, d in ps.items()
                         if (d.get("bh_pct") or 0) > 5 and (d.get("vs_bh") or 0) < 0]
                if drift:
                    B.append('<div class="note warn"><b>Drift warning.</b> On '
                             '%s the tape rose more than 5%% and the strategy '
                             'still failed to beat simply holding it. On those '
                             'names it was doing work for nothing.</div>'
                             % esc(", ".join(drift)))

        # ---------------------------------------------- concentration
        cn = w.get("concentration")
        if cn and cn.get("top3_pct") is not None:
            heavy = cn["top3_pct"] > 40
            B.append("<h4>Is the profit concentrated?</h4>")
            B.append(table(["", "Value", ""], [
                ["Net profit, all symbols", signed(cn["net"]), ""],
                ["Best single trade as a share of net",
                 '<b class="%s">%.1f%%</b>' % ("warn" if (cn["top1_pct"] or 0) > 20
                                               else "", cn["top1_pct"] or 0), ""],
                ["Best three trades as a share of net",
                 '<b class="%s">%.1f%%</b>' % ("warn" if heavy else "",
                                               cn["top3_pct"]), ""],
                ["Net with the best three removed",
                 '<b class="%s">%s</b>' % (cls(cn["net_ex_top3"]),
                                           signed(cn["net_ex_top3"])),
                 '<span class="sub">still profitable?</span>'],
                ["Median trade",
                 '<span class="%s">%s</span>' % (cls(cn["median_trade"]),
                                                 signed(cn["median_trade"])),
                 '<span class="sub">the typical trade, not the average</span>'],
            ]))
            if heavy:
                B.append('<div class="note warn">%.0f%% of the profit comes '
                         'from three trades. That is a real result but a '
                         'fragile one &mdash; miss those three and %s. Size '
                         'accordingly, and do not assume the next window '
                         'contains its own three.</div>'
                         % (cn["top3_pct"],
                            "it still makes %s" % signed(cn["net_ex_top3"])
                            if cn["net_ex_top3"] > 0 else "it loses money"))

    # ---------------------------------------------------------- everything
    B.append("<h2>Every family, and what happened to it</h2>")
    B.append('<p class="sub">The rejections are the most useful part of this '
             'document. They are ideas that will otherwise be suggested again '
             'in a month.</p>')
    for p in passes:
        B.append("<h3>%s bars, %d days</h3>"
                 % (esc(p.get("_label") or p.get("timeframe")), p.get("days", 0)))
        rows = []
        for c in p.get("ranked", []):
            if c["family"].startswith("CONTROL"):
                continue
            te = c["test"]
            rows.append([esc(c["family"]),
                         '<span class="pill ok">survived</span>',
                         '<span class="%s">%s</span>' % (cls(te["median_score"]),
                                                         num(te["median_score"])),
                         "%d/%d" % (te["symbols_profitable"],
                                    te["symbols_scored"]), ""])
        for c in p.get("rejected", []):
            if c["family"].startswith("CONTROL"):
                continue
            te = c.get("test") or {}
            rows.append([esc(c["family"]),
                         '<span class="pill no">rejected</span>',
                         num(te.get("median_score")),
                         "%d/%d" % (te.get("symbols_profitable", 0),
                                    te.get("symbols_scored", 0)),
                         '<span class="sub">%s</span>'
                         % esc(c.get("rejected") or c.get("why") or "")])
        B.append(table(["Family", "", "OOS score", "Symbols +", "Why"], rows))

    # ---------------------------------------------------------- caveats
    B.append("<h2>What this does not tell you</h2>")
    B.append("<ul>" + "".join("<li>%s</li>" % x for x in [
        "<b>One out-of-sample window is one sample.</b> Enough to reject an "
        "idea; not enough to trust one. Paper-trade anything here disarmed and "
        "compare what it actually does against what this says it should.",
        "<b>The costs are an estimate.</b> Every winner was re-run at double "
        "and quadruple it, and where two score alike, prefer the one that "
        "trades less.",
        "<b>No borrow costs or short availability.</b> Two of these are "
        "short-only, and on a hard-to-borrow name the fills modelled here may "
        "not be reachable at all.",
        "<b>Survivorship in the universe.</b> These eight symbols are liquid "
        "and interesting <i>today</i>. That is a mild forward-looking bias and "
        "it flatters everything equally.",
    ]) + "</ul>")

    sub = ("%s &middot; %s backtests &middot; %d passes &middot; %.0f minutes "
           "of compute" % (now.strftime("%A, %d %B %Y at %H:%M"),
                           format(n_bt, ","), len(passes), secs / 60))
    out.write_text(page("Strategy research", "".join(B), sub), encoding="utf-8")
    return out
