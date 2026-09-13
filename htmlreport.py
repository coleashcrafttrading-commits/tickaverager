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

WHAT THE OPERATIONAL REPORT LEADS WITH
--------------------------------------
TOTAL P/L, not realized. `journal.stats()` counts closed lots only, so a ladder
sitting on six lots 20% underwater reports a happy booked number and says
nothing about the hole. Every figure here exists to make the open side visible,
so realized is a supporting line labelled "booked" and never the headline.

The other half of that rule: a missing number is never printed as 0. An absent
unrealized renders as "needs live prices" and an absent MAE as "not recorded
before <date>", because a zero in those slots reads as "nothing is underwater",
which is precisely the false comfort this report exists to remove. See
`docs/history_contract.md` section 4 -- `na()` below is the only way a missing
figure reaches the page.
"""
from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from qty import qstr

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports"

# The exact words a missing figure is allowed to use -- contract section 4.
NEED_MARKS = "needs live prices"
NOTHING_LOST = "nothing lost yet"
NO_MAE = "not recorded yet"


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


def pct(n, dp=2) -> str:
    return "—" if n is None else ("%+.*f%%" % (dp, float(n)))


def cls(n) -> str:
    if n is None:
        return ""
    return "up" if float(n) > 0 else ("down" if float(n) < 0 else "")


def na(reason: str, why: str = "") -> str:
    """A figure that does not exist, saying WHY in the place of a number.

    Never `0`, never a bare dash. The two are not the same statement: "$0
    unrealized" says the open book is flat, "needs live prices" says nobody
    looked. Only one of those is true when the marks are missing.
    """
    return ('<span class="na"%s>%s</span>'
            % ((' title="%s"' % esc(why)) if why else "", esc(reason)))


def fig(n, reason: str, fmt=signed, colour: bool = True, why: str = "",
        bold: bool = False) -> str:
    """A number, or the reason there isn't one. The single gate for section 4."""
    if n is None:
        return na(reason, why)
    tag = "b" if bold else "span"
    return '<%s class="%s">%s</%s>' % (tag, cls(n) if colour else "", fmt(n), tag)


# ======================================================================= time
def _parse_t(t: Any) -> Optional[datetime]:
    """Whatever the journal or Alpaca hands over -> an aware datetime, or None.

    Alpaca's portfolio history stamps are epoch seconds; the journal writes
    ISO. Both land on the same axis, so both are parsed here.
    """
    if t is None or isinstance(t, bool):
        return None
    if isinstance(t, (int, float)):
        try:
            return datetime.fromtimestamp(float(t), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    s = str(t).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{9,12}(\.\d+)?", s):
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    for cand in (s.replace("Z", "+00:00"), s[:19]):
        try:
            d = datetime.fromisoformat(cand)
        except ValueError:
            continue
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _tick_fmt(span_days: float) -> str:
    if span_days <= 2:
        return "%d %b %H:%M"
    if span_days <= 120:
        return "%d %b"
    return "%b %Y"


def _stamp(d: Optional[datetime]) -> str:
    return d.strftime("%d %b %Y %H:%M UTC") if d else "unknown time"


# ====================================================================== charts
_UID = [0]


def _uid(prefix: str = "g") -> str:
    _UID[0] += 1
    return "%s%d" % (prefix, _UID[0])


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


def _short_money(v: float) -> str:
    """-$3.6k, not $-3.6k. The sign belongs outside the currency."""
    return ("-" if v < 0 else "") + "$" + _short(abs(v))


def _interp(series: Sequence[tuple], t: datetime) -> Optional[float]:
    """The series' value at time t, or None when t is outside its span.

    None rather than a clamp on purpose: clamping would draw a trade marker on
    a stretch of line that does not cover it, which is a quiet lie about when
    the money moved.
    """
    if not series or t < series[0][0] or t > series[-1][0]:
        return None
    prev = series[0]
    for cur in series[1:]:
        if cur[0] >= t:
            dt = (cur[0] - prev[0]).total_seconds()
            if dt <= 0:
                return cur[1]
            f = (t - prev[0]).total_seconds() / dt
            return prev[1] + (cur[1] - prev[1]) * f
        prev = cur
    return series[-1][1]


def _stepped(points: Sequence[tuple]) -> list:
    """(t, cumulative) -> a staircase. A booked total holds flat until the next
    close; drawing it as a slope invents profit on days nothing sold."""
    out: list = []
    for i, (t, v) in enumerate(points):
        if i:
            out.append((t, points[i - 1][1]))
        out.append((t, v))
    return out


def svg_pl_time(primary: Sequence[tuple], *, label: str = "",
                curve: Sequence[dict] = (), secondary: Sequence[tuple] = (),
                width: int = 980, height: int = 340,
                every: int = 0) -> str:
    """TOTAL P/L against real dated time, with the trade numbers on the line.

    `primary` and `secondary` are [(datetime, dollars)]. `curve` is the
    contract's `equity_curve`: one entry per closed lot carrying `n`, the trade
    number, which is what goes on the line as a labelled point.

    Zero is always on the scale. A P/L curve autoscaled to its own values makes
    a book that lost money all month look like a tidy rising line, which is the
    single easiest way for a report to mislead.
    """
    pts = [(t, float(v)) for t, v in (primary or [])
           if t is not None and v is not None]
    sec = [(t, float(v)) for t, v in (secondary or [])
           if t is not None and v is not None]
    if len(pts) < 2:
        return '<div class="empty">Not enough data to draw a curve.</div>'
    pts.sort(key=lambda x: x[0])
    sec.sort(key=lambda x: x[0])

    dots = []
    for c in (curve or []):
        t = _parse_t(c.get("t"))
        if t is not None:
            dots.append((t, c))
    dots.sort(key=lambda x: x[0])

    all_t = [t for t, _ in pts] + [t for t, _ in sec] + [t for t, _ in dots]
    tmin, tmax = min(all_t), max(all_t)
    span = (tmax - tmin).total_seconds() or 1.0
    ys = [v for _, v in pts] + [v for _, v in sec] + [0.0]
    lo, hi = min(ys), max(ys)
    if hi == lo:
        hi += 1.0
        lo -= 1.0
    pad = (hi - lo) * 0.12
    lo -= pad
    hi += pad

    padL, padR, padT, padB = 8, 78, 22, 54
    pw, ph = width - padL - padR, height - padT - padB
    X = lambda t: padL + ((t - tmin).total_seconds() / span) * pw   # noqa: E731
    Y = lambda v: padT + ph - ((v - lo) / (hi - lo)) * ph           # noqa: E731

    last = pts[-1][1]
    col = "var(--up)" if last > 0 else ("var(--down)" if last < 0 else "var(--accent)")
    gid = _uid("plfill")

    out = []
    out.append('<defs><linearGradient id="%s" x1="0" y1="0" x2="0" y2="1">'
               '<stop offset="0%%" stop-color="%s" stop-opacity=".42"/>'
               '<stop offset="100%%" stop-color="%s" stop-opacity=".02"/>'
               '</linearGradient></defs>' % (gid, col, col))

    # ---- horizontal grid + dollar axis ----
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = Y(v)
        out.append('<line class="g" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   % (padL, y, padL + pw, y))
        out.append('<text class="ax" x="%.1f" y="%.1f">%s</text>'
                   % (padL + pw + 8, y + 3.6, esc(_short_money(v))))

    # ---- the x axis is TIME, with real dated ticks ----
    fmt = _tick_fmt(span / 86400.0)
    for k in range(5):
        t = tmin + timedelta(seconds=span * k / 4)
        x = X(t)
        out.append('<line class="gv" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   % (x, padT, x, padT + ph))
        anchor = "start" if k == 0 else ("end" if k == 4 else "middle")
        out.append('<text class="ax" x="%.1f" y="%d" text-anchor="%s">%s</text>'
                   % (x, height - 34, anchor, esc(t.strftime(fmt))))
    out.append('<text class="axt" x="%.1f" y="%d" text-anchor="middle">%s</text>'
               % (padL + pw / 2, height - 12,
                  esc("time →   ·   marked points are closed trades, "
                      "numbered in order")))

    # ---- zero, always ----
    if lo < 0 < hi:
        out.append('<line class="z" x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f"/>'
                   % (padL, Y(0), padL + pw, Y(0)))
        out.append('<text class="axz" x="%.1f" y="%.1f">break even</text>'
                   % (padL + 4, Y(0) - 5))

    # ---- the booked-only staircase, behind ----
    if sec:
        out.append('<path class="sec" d="%s" fill="none"/>'
                   % " ".join(("M" if i == 0 else "L") + "%.1f %.1f" % (X(t), Y(v))
                              for i, (t, v) in enumerate(sec)))

    # ---- the primary series ----
    base_y = Y(0) if lo < 0 < hi else Y(lo)
    area = ("M%.1f %.1f " % (X(pts[0][0]), base_y)
            + " ".join("L%.1f %.1f" % (X(t), Y(v)) for t, v in pts)
            + " L%.1f %.1f Z" % (X(pts[-1][0]), base_y))
    line = " ".join(("M" if i == 0 else "L") + "%.1f %.1f" % (X(t), Y(v))
                    for i, (t, v) in enumerate(pts))
    out.append('<path d="%s" fill="url(#%s)"/>' % (area, gid))
    out.append('<path d="%s" fill="none" stroke="%s" stroke-width="2" '
               'stroke-linejoin="round"/>' % (line, col))

    # ---- peak and the max-drawdown trough of the drawn curve ----
    pk = max(range(len(pts)), key=lambda i: pts[i][1])
    run, worst, tr = pts[0][1], 0.0, None
    for i, (_, v) in enumerate(pts):
        run = max(run, v)
        if v - run < worst:
            worst, tr = v - run, i
    for i, tag, klass in ((pk, "peak %s" % signed(pts[pk][1], 0), "mk-pk"),
                          (tr, "max drawdown %s" % money(worst, 0), "mk-tr")):
        if i is None:
            continue
        x, y = X(pts[i][0]), Y(pts[i][1])
        up = klass == "mk-pk"
        out.append('<circle class="%s" cx="%.1f" cy="%.1f" r="4"><title>%s '
                   'on %s</title></circle>'
                   % (klass, x, y, esc(tag), esc(_stamp(pts[i][0]))))
        tx = min(max(x, padL + 46), padL + pw - 46)
        out.append('<text class="mkl %s" x="%.1f" y="%.1f" text-anchor="middle">'
                   '%s</text>' % (klass, tx, (y - 11) if up else (y + 15),
                                  esc(tag)))

    # ---- the trade numbers ----
    if dots:
        every = every or max(1, int(math.ceil(len(dots) / 12.0)))
        outside = 0
        for i, (t, c) in enumerate(dots):
            y = _interp(pts, t)
            off = False
            if y is None:
                y = _interp(sec, t)
                off = True
            if y is None:
                outside += 1
                continue
            x, yy = X(t), Y(y)
            n = c.get("n")
            tot = c.get("total_pl")
            now = bool(c.get("now"))
            # the curve's `realized` is CUMULATIVE (contract 1.3), so this
            # trade's own P/L is the step from the point before it
            cum = c.get("realized")
            prev = dots[i - 1][1].get("realized") if i else 0.0
            own = (None if cum is None or prev is None
                   else round(float(cum) - float(prev), 2))
            ttl = ("%s · %s · this trade %s · booked to date %s"
                   % ("Now" if now else "Trade %s" % (n if n is not None else "?"),
                      _stamp(t),
                      signed(own) if own is not None else "unknown",
                      signed(cum)))
            if tot is not None:
                ttl += " · total P/L %s" % signed(tot)
            elif now:
                ttl += " · total P/L %s" % NEED_MARKS
            else:
                ttl += (" · total P/L unknowable at a past moment: the "
                        "open book cannot be re-priced after the fact")
            if c.get("open_lots") is not None:
                ttl += " · %s lot(s) open" % c.get("open_lots")
            out.append('<g class="dot%s%s"><circle cx="%.1f" cy="%.1f" r="%s"/>'
                       '<circle class="hit" cx="%.1f" cy="%.1f" r="9"/>'
                       '<title>%s</title></g>'
                       % (" off" if off else "", " now" if now else "",
                          x, yy, "5" if now else "3", x, yy, esc(ttl)))
            if now:
                out.append('<text class="dotn now" x="%.1f" y="%.1f" '
                           'text-anchor="end">now</text>' % (x - 8, yy - 8))
            elif n is not None and (i % every == 0 or i == len(dots) - 1):
                out.append('<text class="dotn" x="%.1f" y="%.1f" '
                           'text-anchor="middle">%s</text>'
                           % (x, yy - 9, esc("#%s" % n)))
        if outside:
            out.append('<text class="axt" x="%.1f" y="%d">%s</text>'
                       % (padL, height - 44,
                          esc("%d closed trade(s) fall outside the drawn "
                              "window and carry no marker" % outside)))

    return ('<svg viewBox="0 0 %d %d" class="chart pl" role="img" '
            'aria-label="%s" preserveAspectRatio="xMidYMid meet">%s</svg>'
            % (width, height, esc(label or "total P/L over time"), "".join(out)))


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
    X = lambda i: padL + (i / max(1, n - 1)) * pw   # noqa: E731
    Y = lambda v: padT + ph - ((v - lo) / (hi - lo)) * ph   # noqa: E731

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
    Y = lambda v: padT + ph - ((v - lo) / (hi - lo)) * ph   # noqa: E731
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
                   'rx="3" fill="%s" opacity=".85"/>'
                   % (cx - bw / 2, top, bw, h, c))
        out.append('<text x="%.1f" y="%.1f" class="bv" text-anchor="middle">%s</text>'
                   % (cx, (top - 4) if v >= 0 else (top + h + 11), esc(fmt(v))))
        out.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
                   % (cx, height - 6, esc(lbl)))
    return ('<svg viewBox="0 0 %d %d" class="chart" role="img" aria-label="%s">'
            '%s</svg>' % (width, height, esc(label or "chart"), "".join(out)))


# ======================================================================= shell
# The dashboard's skin, inlined. static/ui/theme.css is the original: indigo
# ground lit by two soft hazes, violet leading and blue answering, glass
# surfaces, and green kept for one job only -- a number that went up.
#
# Dark is the default because that is what the product looks like. A viewer
# who asks for light gets the theme's own light tokens, and PRINT forces a
# white ground with dark ink: these reports are printed and emailed, and a
# black rectangle with a violet haze is not a document anyone can read on
# paper.
CSS = """
:root{
  --bg:#080611; --bg-2:#0d0a1d;
  --haze-a:rgba(124,92,255,.20); --haze-b:rgba(56,140,255,.15);
  --haze-c:rgba(168,85,247,.10);
  --glass:rgba(255,255,255,.045); --glass-2:rgba(255,255,255,.075);
  --glass-3:rgba(255,255,255,.11); --solid:#12102a;
  --hairline:rgba(255,255,255,.09); --hairline2:rgba(255,255,255,.16);
  --rim:rgba(255,255,255,.26);
  --ink:#eceaf8; --dim:#a6a2c4; --faint:#6f6a92;
  --accent:#7c5cff; --accent-2:#38a8ff;
  --accent-dim:rgba(124,92,255,.16); --accent-glow:rgba(124,92,255,.42);
  --grad:linear-gradient(135deg,#8b5cff,#5b8bff 55%,#38a8ff);
  --up:#3ddc97; --down:#ff6b8a; --warn:#ffc061;
  --radius:20px; --radius-sm:12px;
  --shadow:0 1px 1px rgba(0,0,0,.5),0 10px 30px -14px rgba(0,0,0,.8);
  --shadow-lift:0 1px 1px rgba(0,0,0,.5),0 26px 60px -22px rgba(0,0,0,.92);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --panel:var(--glass-2); --line:var(--hairline);
}
@media (prefers-color-scheme:light){
  :root{
    --bg:#f2f1fa; --bg-2:#fbfaff;
    --haze-a:rgba(124,92,255,.18); --haze-b:rgba(56,140,255,.14);
    --haze-c:rgba(168,85,247,.10);
    --glass:rgba(255,255,255,.72); --glass-2:rgba(255,255,255,.85);
    --glass-3:rgba(255,255,255,.94); --solid:#fff;
    --hairline:rgba(26,18,58,.10); --hairline2:rgba(26,18,58,.18);
    --rim:rgba(255,255,255,.95);
    --ink:#17123a; --dim:#565080; --faint:#837da8;
    --accent:#6242e8; --accent-2:#1b7fe0;
    --accent-dim:rgba(98,66,232,.13); --accent-glow:rgba(98,66,232,.3);
    --up:#12a06c; --down:#e04b6c; --warn:#b87407;
    --shadow:0 1px 1px rgba(26,18,58,.05),0 10px 30px -16px rgba(26,18,58,.28);
    --shadow-lift:0 1px 1px rgba(26,18,58,.06),0 26px 60px -24px rgba(26,18,58,.32);
  }
}
*{box-sizing:border-box}
html{color-scheme:dark light}
body{margin:0;background:var(--bg);color:var(--ink);position:relative;
  min-height:100vh;overflow-x:hidden;
  font:14px/1.62 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
       "Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}
body::before{content:"";position:fixed;inset:0;z-index:-1;pointer-events:none;
  background:
    radial-gradient(1000px 680px at 8% -10%,var(--haze-a),transparent 60%),
    radial-gradient(860px 620px at 96% 106%,var(--haze-b),transparent 58%),
    radial-gradient(1100px 700px at 50% 46%,var(--haze-c),transparent 70%),
    linear-gradient(170deg,var(--bg-2),var(--bg) 46%)}
.wrap{max-width:1040px;margin:0 auto;padding:34px 22px 90px}
h1{font-size:29px;margin:0 0 4px;letter-spacing:-.025em;font-weight:680}
h2{font-size:19px;margin:40px 0 10px;padding-bottom:8px;
   border-bottom:1px solid var(--hairline);letter-spacing:-.015em}
h3{font-size:15px;margin:24px 0 8px;color:var(--accent-2);letter-spacing:-.01em}
h4{font-size:12px;margin:22px 0 6px;color:var(--dim);text-transform:uppercase;
   letter-spacing:.08em}
.sub{color:var(--dim);font-size:13px;margin:0 0 8px}
p{margin:0 0 11px}
.lead{font-size:15px}
b,strong{font-weight:650}

/* the material: translucent fill, hairline, a lit top rim, a soft lift.
   backdrop-filter goes ONLY on surfaces that do not clip: a blurred element
   that also clips its overflow paints its children away in Chromium, which
   silently emptied the whole metrics block. The clipping surfaces get a
   slightly denser fill instead, which reads the same and always paints. */
.card,.note,.hero{position:relative;
  -webkit-backdrop-filter:blur(26px) saturate(180%);
  backdrop-filter:blur(26px) saturate(180%)}
.kpis,.tablewrap{position:relative;background:var(--glass-2)}
.card,.kpis,.hero{border-radius:var(--radius)}
.card,.note{background:var(--glass)}
.card,.kpis,.note,.tablewrap{border:1px solid var(--hairline);
  box-shadow:inset 0 1px 0 0 rgba(255,255,255,.09),
             inset 0 -1px 0 0 rgba(0,0,0,.22),var(--shadow)}
.card{padding:18px 20px;margin:16px 0}
.card::after,.kpis::after,.hero::after{content:"";position:absolute;
  inset:0 0 auto;height:1px;pointer-events:none;opacity:.75;
  background:linear-gradient(90deg,transparent 8%,var(--rim) 50%,transparent 92%)}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){
  .card,.note{background:var(--solid)}
}

/* the ONE gradient panel: the headline total P/L */
.hero{background:var(--grad);border:1px solid transparent;color:#fff;
  padding:24px 26px;margin:20px 0 6px;
  box-shadow:0 18px 50px -18px var(--accent-glow),
             inset 0 1px 0 0 rgba(255,255,255,.3)}
.hero .k{font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:rgba(255,255,255,.8)}
.hero .v{font-size:46px;line-height:1.05;font-weight:700;margin:6px 0 2px;
  letter-spacing:-.035em;font-variant-numeric:tabular-nums;color:#fff}
.hero .tag{display:inline-block;margin:2px 0 14px;padding:3px 11px;
  border-radius:999px;font-size:11.5px;background:rgba(0,0,0,.24);
  color:#fff;border:1px solid rgba(255,255,255,.22)}
.hero .legs{display:flex;flex-wrap:wrap;gap:10px 34px;
  border-top:1px solid rgba(255,255,255,.24);padding-top:14px;margin-top:2px}
.hero .leg .lk{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
  color:rgba(255,255,255,.78)}
.hero .leg .lv{font-size:19px;font-weight:640;color:#fff;margin-top:2px;
  font-variant-numeric:tabular-nums}
.hero .leg .ls{font-size:11px;color:rgba(255,255,255,.72)}
.hero .na{color:rgba(255,255,255,.9);border-bottom-color:rgba(255,255,255,.5)}

.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(148px,1fr));
  overflow:hidden;margin:16px 0}
.kpi{padding:14px 16px;border-right:1px solid var(--hairline);
  border-top:1px solid var(--hairline)}
.kpi .k{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--dim)}
.kpi .v{font-size:21px;font-weight:650;margin-top:4px;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.kpi .s{font-size:11.5px;color:var(--dim);margin-top:3px}

.tablewrap{overflow-x:auto;margin:12px 0;border-radius:var(--radius-sm);
  -webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;margin:0;font-size:13px}
th{text-align:right;font-weight:600;color:var(--dim);font-size:10.5px;
   letter-spacing:.06em;text-transform:uppercase;padding:9px 11px;
   border-bottom:1px solid var(--hairline);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
td{padding:8px 11px;border-bottom:1px solid var(--hairline);text-align:right;
   font-variant-numeric:tabular-nums;white-space:nowrap}
td:first-child{white-space:normal}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--glass-2)}
.up{color:var(--up)} .down{color:var(--down)} .warn{color:var(--warn)}
.dim{color:var(--dim)} .mono{font-family:var(--mono);font-size:12px}
/* An explanation standing where a number would be. It must be legible and
   unmissable, but not shouted at 46px -- it is a sentence, not a figure. */
.na{color:var(--faint);font-size:.92em;
  border-bottom:1px dotted var(--hairline2);cursor:help}
.hero .v .na{font-size:.46em;font-weight:600;letter-spacing:0}
.kpi .v .na{font-size:.66em;font-weight:500;letter-spacing:0}
.cav{color:var(--warn);font-size:11.5px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11px;
  background:var(--glass-3);color:var(--dim);border:1px solid var(--hairline)}
.pill.ok{background:rgba(61,220,151,.14);color:var(--up);
  border-color:rgba(61,220,151,.3)}
.pill.no{background:rgba(255,107,138,.14);color:var(--down);
  border-color:rgba(255,107,138,.3)}
.pill.acc{background:var(--accent-dim);color:var(--accent-2);
  border-color:rgba(124,92,255,.32)}
.note{padding:13px 16px;border-radius:var(--radius-sm);margin:14px 0;
  font-size:13.5px;border-left:3px solid var(--accent)}
.note.bad{border-left-color:var(--down)}
.note.good{border-left-color:var(--up)}
.note.warn{border-left-color:var(--warn)}

.chartwrap{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:4px -4px}
.chart{width:100%;height:auto;display:block;margin:6px 0 2px}
.chart .g{stroke:var(--hairline);stroke-width:1}
.chart .gv{stroke:var(--hairline);stroke-width:1;stroke-dasharray:2 5}
.chart .z{stroke:var(--dim);stroke-width:1;stroke-dasharray:4 3}
.chart .ax{fill:var(--dim);font-size:10.5px;font-family:var(--mono)}
.chart .axt{fill:var(--faint);font-size:10px}
.chart .axz{fill:var(--dim);font-size:9.5px;letter-spacing:.06em}
.chart .bv{fill:var(--ink);font-size:10.5px;font-family:var(--mono)}
.chart .sec{stroke:var(--accent-2);stroke-width:1.4;stroke-dasharray:5 4;
  opacity:.72}
.chart .dot circle{fill:var(--accent-2);stroke:var(--bg);stroke-width:1.2}
.chart .dot.now circle{fill:var(--accent);stroke-width:1.6}
.chart .dot.off circle{fill:none;stroke:var(--accent-2);stroke-width:1.4}
.chart .dot .hit{fill:transparent;stroke:none}
.chart .dotn{fill:var(--accent-2);font-size:9.5px;font-family:var(--mono)}
.chart .dotn.now{fill:var(--accent);font-size:10px}
.chart .mk-pk{fill:var(--up);stroke:none}
.chart .mk-tr{fill:var(--down);stroke:none}
.chart .mkl{font-size:9.5px;font-family:var(--mono)}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:11.5px;
  color:var(--dim);margin:8px 0 0}
.legend i{display:inline-block;width:16px;height:0;vertical-align:middle;
  margin-right:6px;border-top:2px solid var(--accent)}
.legend i.dash{border-top:2px dashed var(--accent-2)}
.legend i.dot{width:8px;height:8px;border:0;border-radius:50%;
  background:var(--accent-2)}
.chart-h{display:flex;flex-wrap:wrap;gap:6px 18px;
  align-items:flex-start;justify-content:space-between}
.chart-t{font-size:15px;font-weight:640;letter-spacing:-.01em}
.chart-s{font-size:12.5px;color:var(--dim);margin-top:2px}

.empty{color:var(--dim);padding:22px;text-align:center;font-size:13px}
.check{margin:3px 0;font-size:13px}
.check .m{font-weight:700;margin-right:6px}
ul{margin:0 0 12px;padding-left:20px} li{margin:4px 0}
code{font-family:var(--mono);font-size:12.5px;background:var(--glass-2);
  padding:1px 5px;border-radius:5px;border:1px solid var(--hairline)}
.foot{margin-top:46px;padding-top:16px;border-top:1px solid var(--hairline);
  color:var(--faint);font-size:12px}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 18px}
.bar a{font-size:12.5px;color:var(--ink);text-decoration:none;
  border:1px solid var(--hairline);padding:6px 13px;border-radius:999px;
  background:var(--glass-2)}
.bar a:hover{border-color:var(--hairline2)}
@media (max-width:700px){
  .wrap{padding:24px 14px 70px}
  h1{font-size:24px}
  .hero{padding:20px 18px}
  .hero .v{font-size:36px}
  .chartwrap .chart{min-width:660px}
}

/* Printed and emailed. Force a white ground and dark ink -- a report that
   arrives as a black rectangle with a violet haze is not a document. */
@media print{
  :root{
    --bg:#fff; --bg-2:#fff;
    --haze-a:transparent; --haze-b:transparent; --haze-c:transparent;
    --glass:#fff; --glass-2:#fff; --glass-3:#f2f4f8; --solid:#fff;
    --hairline:#c8cfda; --hairline2:#9aa4b4; --rim:transparent;
    --ink:#000; --dim:#333c48; --faint:#5a6472;
    --accent:#3a2bb0; --accent-2:#14538f; --accent-dim:#eef0fb;
    --accent-glow:transparent; --grad:none;
    --up:#0a7a42; --down:#a3232e; --warn:#7a5300;
    --shadow:none; --shadow-lift:none;
  }
  body{background:#fff !important;color:#000 !important}
  body::before{display:none !important}
  .card,.kpis,.note,.tablewrap,.hero{background:#fff !important;
    box-shadow:none !important;-webkit-backdrop-filter:none !important;
    backdrop-filter:none !important;border:1px solid #c8cfda !important;
    color:#000 !important}
  .hero{border:2px solid #3a2bb0 !important}
  .hero .k,.hero .v,.hero .tag,.hero .leg .lk,.hero .leg .lv,.hero .leg .ls,
  .hero .na{color:#000 !important}
  .hero .tag{background:#fff !important;border-color:#3a2bb0 !important}
  .hero .legs{border-top-color:#c8cfda !important}
  .card::after,.kpis::after,.hero::after{display:none !important}
  .chart .dot circle{stroke:#fff}
  .bar{display:none}
  .tablewrap,.chartwrap{overflow:visible !important}
  h2{page-break-after:avoid}
  .card,table,.kpis,.hero{page-break-inside:avoid}
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


def hero(k: str, v: str, tag: str, legs: Sequence[tuple]) -> str:
    """The headline panel -- the one place the gradient IS the surface.

    The value stays white, the way the dashboard's hero card does; the sign is
    carried by the tag beneath it and by the coloured legs, so green and rose
    keep meaning only "this number went up / down".
    """
    ls = "".join('<div class="leg"><div class="lk">%s</div>'
                 '<div class="lv">%s</div>%s</div>'
                 % (esc(a), b, ('<div class="ls">%s</div>' % c) if c else "")
                 for a, b, c in legs)
    return ('<div class="hero"><div class="k">%s</div><div class="v">%s</div>'
            '<div class="tag">%s</div><div class="legs">%s</div></div>'
            % (esc(k), v, tag, ls))


def table(heads: Sequence[str], rows: Sequence[Sequence[str]],
          empty: str = "Nothing here.") -> str:
    if not rows:
        return '<div class="empty">%s</div>' % esc(empty)
    th = "".join("<th>%s</th>" % esc(h) for h in heads)
    body = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r)
                   for r in rows)
    return ('<div class="tablewrap"><table><thead><tr>%s</tr></thead>'
            '<tbody>%s</tbody></table></div>' % (th, body))


# ========================================================= the data, gathered
_PH_PAIR = {"daily": ("1D", "5Min"), "weekly": ("1W", "1H"),
            "inventory": ("1W", "1H"), "full": ("all", "1D")}


def _stats(journal, rows: list, marks: dict, inv: list) -> dict:
    """journal.stats() with live marks where this build of the journal takes
    them. The older one-argument signature still works -- the report then says
    "needs live prices" rather than inventing an unrealized figure."""
    try:
        return journal.stats(rows, marks=marks, inventory=inv)
    except TypeError:
        s = journal.stats(rows)
        s["marked"] = False
        return s


def _equity(fleet: Any, kind: str, days: int):
    """Alpaca's own account curve for the window -- contract section 3.

    `(points, base)` where points is `[{t, equity, pl}]`, or `(None, None)`
    when the broker call fails or is not there. The caller then falls back to
    the stepped booked curve AND SAYS SO; it never passes off one for the
    other.
    """
    period, tf = _PH_PAIR.get(kind, ("1W", "1H"))
    if kind not in _PH_PAIR:
        period = ("1D" if days <= 1 else "1W" if days <= 7
                  else "1M" if days <= 31 else "all")
    b = getattr(fleet, "broker", None)
    if b is None:
        return None, None
    try:
        raw = b.portfolio_history(period, tf, extended=True) or {}
    except Exception:
        return None, None
    ts = raw.get("timestamp") or []
    eq = raw.get("equity") or []
    pl = raw.get("profit_loss") or []
    pts = []
    for i, t in enumerate(ts):
        if i >= len(eq) or eq[i] is None:
            continue
        pts.append({"t": float(t), "equity": round(float(eq[i]), 2),
                    "pl": (round(float(pl[i]), 2)
                           if i < len(pl) and pl[i] is not None else None)})
    if len(pts) < 2:
        return None, None
    try:
        base = float(raw.get("base_value"))
    except (TypeError, ValueError):
        base = 0.0
    if not base:
        base = pts[0]["equity"] - (pts[0]["pl"] or 0.0)
    return pts, round(float(base), 2)


def _curve(journal, rows: list, stats: dict) -> list:
    """The contract's `equity_curve`, or the same shape rebuilt from the rows.

    The rebuild is cumulative BOOKED P/L, one point per closed lot, with
    `unrealized: None` on every historical point -- the journal cannot know a
    past mark without refetching bars and inventing one would be a lie. Only
    the final "now" point carries a real total, and only when the stats block
    was marked.
    """
    c = stats.get("equity_curve")
    if c:
        return list(c)
    closes = [r for r in rows if r.get("event") in ("close", "partial")
              and not r.get("dry_run") and not journal.is_bookkeeping(r)]
    closes.sort(key=lambda r: str(r.get("ts") or ""))
    run = 0.0
    out = []
    for i, r in enumerate(closes, 1):
        run = round(run + float(r.get("realized") or 0.0), 2)
        out.append({"t": r.get("ts"), "n": i,
                    "realized": run, "unrealized": None,
                    "total_pl": None, "open_lots": None})
    if out and stats.get("marked"):
        out.append({"t": datetime.now(timezone.utc).isoformat(),
                    "n": len(out), "realized": run,
                    "unrealized": stats.get("unrealized"),
                    "total_pl": stats.get("total_pl"),
                    "open_lots": stats.get("open_lots"), "now": True})
    return out


def _derive(journal, rows: list) -> dict:
    """The closed-side metrics, recomputed here from the rows in the window.

    Only ever used to FILL keys `journal.stats()` did not return, so when the
    journal grows them this function goes quiet. Everything here is a pure
    function of closed lots; nothing about the OPEN side is guessed, because
    that needs live marks and guessing it is the whole failure mode.
    """
    closes = [r for r in rows if r.get("event") in ("close", "partial")
              and not r.get("dry_run") and not journal.is_bookkeeping(r)]
    closes.sort(key=lambda r: str(r.get("ts") or ""))
    pls = [float(r.get("realized") or 0.0) for r in closes]
    wins = [v for v in pls if v > 0]
    losses = [v for v in pls if v < 0]
    gw, gl = sum(wins), -sum(losses)

    run = peak = 0.0
    dd = 0.0
    for v in pls:
        run += v
        peak = max(peak, run)
        dd = min(dd, run - peak)

    # capital open at once, replayed in time order
    cost: dict = {}
    entry: dict = {}
    cur = peak_cap = 0.0
    for r in sorted(rows, key=lambda x: str(x.get("ts") or "")):
        if r.get("dry_run") or journal.is_bookkeeping(r):
            continue
        lid = r.get("lot_id")
        ev = r.get("event")
        if ev == "open":
            c = float(r.get("cost") or 0.0)
            if not c:
                c = float(r.get("shares") or 0) * float(r.get("entry_price") or 0)
            entry[lid] = float(r.get("entry_price") or 0)
            cost[lid] = cost.get(lid, 0.0) + c
            cur += c
            peak_cap = max(peak_cap, cur)
        elif ev in ("close", "partial"):
            c = min(float(r.get("shares") or 0) * entry.get(lid, 0.0),
                    cost.get(lid, 0.0))
            cost[lid] = max(0.0, cost.get(lid, 0.0) - c)
            cur = max(0.0, cur - c)

    n = len(pls)
    total = round(sum(pls), 2)
    return {
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(100.0 * len(wins) / n, 1) if n else 0.0,
        "win_rate_meaningful": bool(losses),
        "avg_win": round(gw / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
        "expectancy": round(total / n, 2) if n else 0.0,
        # null, never infinity: nothing lost is a fact about the window
        "profit_factor": round(gw / gl, 2) if gl > 0 else None,
        "max_drawdown": round(dd, 2),
        "max_drawdown_pct": round(100.0 * dd / peak, 2) if peak else 0.0,
        "peak_capital": round(peak_cap, 2),
        "return_on_peak_capital_pct":
            round(100.0 * total / peak_cap, 3) if peak_cap else 0.0,
    }


def _fill(journal, rows: list, stats: dict) -> tuple:
    """stats, plus the keys it did not carry, plus the set of filled names."""
    s = dict(stats)
    s.setdefault("marked", False)
    if not s.get("marked"):
        # section 4: absent is absent. Not zero.
        for k in ("unrealized", "total_pl", "open_market_value"):
            s[k] = None
    if s.get("open_lots") == 0:
        # The one case where zero is the truth rather than a guess: an EMPTY
        # book is worth nothing whether or not anybody fetched a price. Saying
        # "needs live prices" here would be its own kind of false alarm.
        s["unrealized"] = 0.0
        s["open_market_value"] = 0.0
        if s.get("realized") is not None:
            s["total_pl"] = s["realized"]
    d = _derive(journal, rows)
    filled = set()
    for k, v in d.items():
        if k not in s:
            s[k] = v
            filled.add(k)
    for k in ("avg_mae", "worst_mae", "mae_since", "unmarked_symbols",
              "open_lots", "open_shares", "open_cost"):
        s.setdefault(k, None)
    return s, filled


def _mae_reason(s: dict) -> str:
    since = s.get("mae_since")
    return ("not recorded before %s" % _pretty_day(since)) if since else NO_MAE


def _pretty_day(iso: Any) -> str:
    d = _parse_t(iso)
    return d.strftime("%d %b %Y") if d else str(iso)[:10]


# ============================================================== operational
def build_operational(fleet: Any, kind: str = "daily", days: int = 1,
                      note: str = "") -> Path:
    """The daily / weekly / inventory / full-history report, as HTML.

    Reads like the backtest's strategy results, and leads with TOTAL P/L.
    Realized is a supporting figure labelled "booked": this strategy has no
    stop loss, so booked alone looks excellent right up until the day it does
    not, and the age and size of what is still open is where the risk lives.
    """
    import journal

    REPORT_DIR.mkdir(exist_ok=True)
    now = datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d_%H%M")
    out = REPORT_DIR / ("%s_%s.html" % (kind, stamp))

    ov = fleet.overview()
    p = ov["portfolio"]
    jpath = getattr(fleet, "journal_path", None)
    rows = journal.load(path=jpath, days=None if kind == "full" else days)
    all_rows = journal.load(path=jpath)
    inv = journal.open_inventory(all_rows)
    marks = {}
    for t in ov.get("tickers", []):
        px = t.get("last_price")
        if px:
            marks[t["symbol"]] = float(px)
    raw_stats = _stats(journal, rows, marks, inv)
    stats, filled = _fill(journal, rows, raw_stats)
    curve = _curve(journal, rows, stats)
    eq_points, eq_base = _equity(fleet, kind, days)

    titles = {"daily": "Daily report", "weekly": "Weekly report",
              "inventory": "Open inventory", "full": "Full history"}
    title = titles.get(kind, "Report")
    window = "all history" if kind == "full" else "last %d day(s)" % days
    sub = ("%s &middot; Alpaca %s %s &middot; %s%s"
           % (now.strftime("%A, %d %B %Y at %H:%M"),
              "paper" if ov["paper"] else "LIVE", esc(ov["account"]["number"]),
              esc(window), (" &middot; " + esc(note)) if note else ""))

    marked = bool(stats.get("marked"))
    unmarked = stats.get("unmarked_symbols") or []
    B = []

    # ------------------------------------------------------------- headline
    total = stats.get("total_pl")
    realized = stats.get("realized")
    unreal = stats.get("unrealized")
    tag = ("up on this window" if (total or 0) > 0 else
           "down on this window" if (total or 0) < 0 else
           "flat on this window") if total is not None else NEED_MARKS
    B.append(hero(
        "Total P/L · %s" % window,
        signed(total) if total is not None
        else na(NEED_MARKS, "journal.stats() was called without live marks, so "
                            "the open side is unknown. It is not zero."),
        esc(tag),
        [("Booked (realized)", signed(realized),
          "%s closed lot(s)" % format(stats.get("closes") or 0, ",")),
         ("Unrealized, open book",
          signed(unreal) if unreal is not None else na(NEED_MARKS),
          ("%s lot(s), %s at cost"
           % (stats.get("open_lots") if stats.get("open_lots") is not None
              else len(inv), money(stats.get("open_cost")
                                   if stats.get("open_cost") is not None
                                   else sum(x["cost"] for x in inv), 0)))),
         ("Account value", money(p["account_value"]),
          "%s deployed" % money(p["deployed"], 0))]))
    B.append('<p class="sub">Total P/L is booked plus the open book marked to '
             'the live price. Booked alone is the number that always looks '
             'good: a ladder with no stop loss never closes a loser.</p>')

    if not marked:
        B.append('<div class="note bad"><b>The open side is not priced.</b> '
                 'This report was built without live marks, so the unrealized '
                 'figure and everything derived from it say '
                 '&ldquo;%s&rdquo; rather than a number. They are NOT zero '
                 '&mdash; %d lot(s) are open and their value is simply '
                 'unknown here.</div>' % (NEED_MARKS, len(inv)))
    if unmarked and marked:
        # only worth saying when SOME of the book was priced -- when none of it
        # was, the note above already says the whole open side is unknown
        B.append('<div class="note warn"><b>Part of the book has no mark.</b> '
                 '%s carr%s open lots with no live price (a ticker removed '
                 'from the fleet), so the unrealized figure covers only the '
                 'rest of the book.</div>'
                 % (esc(", ".join(map(str, unmarked))),
                    "ies" if len(unmarked) == 1 else "y"))
    if kind != "full":
        B.append('<div class="note"><b>Read the two halves for what they '
                 'are.</b> Booked is what closed inside the %s. Unrealized is '
                 'the WHOLE open book, including lots opened before this '
                 'window &mdash; that is deliberate, because a lot from last '
                 'week is exactly the risk a one-day report would otherwise '
                 'hide.</div>' % esc(window))

    # -------------------------------------------------------------- the graph
    B.append("<h2>Total P/L over time</h2>")
    B.append(_chart_block(eq_points, eq_base, curve, inv, stats))

    bk = stats.get("bookkeeping_rows") or 0
    if bk:
        B.append('<div class="note warn"><b>%d row(s) in this window are '
                 'bookkeeping, not trades</b>, and are excluded from every '
                 'figure above. They are written when the journal and the '
                 'ledger are re-synced: a lot that had already gone is closed '
                 'in the journal at no price so the two agree. Counting them '
                 'would report closed trades on a day nothing traded.</div>'
                 % bk)

    # ---------------------------------------------------- the strategy results
    B.append("<h2>Strategy results</h2>")
    B.append(_metrics(stats, filled, inv, p))

    # ------------------------------------------------------------- per ticker
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
            fig(t["realized_today"], NEED_MARKS),
            fig(t["unrealized"], NEED_MARKS),
            fig((t["realized_today"] + t["unrealized"])
                if t.get("unrealized") is not None else None, NEED_MARKS,
                bold=True),
            "%d/%d" % (t["lot_count"], t["max_lots"]),
            qstr(t["shares"]),
            money(t["last_price"], 4) if t.get("last_price") else na(NEED_MARKS),
        ])
    B.append(table(["Ticker", "State", "Opened", "Closed", "Partials",
                    "Booked today", "Unrealized", "Total", "Lots", "Shares",
                    "Mark"], trs, "No tickers configured."))

    syms = [t["symbol"] for t in ov["tickers"]]
    # None, not 0, where a ticker has no mark: svg_bars drops the bar rather
    # than drawing a flat one, which would read as "this name is break-even"
    pls = [None if t.get("unrealized") is None
           else (t["realized_today"] + t["unrealized"]) for t in ov["tickers"]]
    if syms:
        B.append('<div class="card"><div class="chart-t">Total P/L by ticker'
                 '</div><div class="chart-s">Booked today plus the open book '
                 'marked live.</div><div class="chartwrap">%s</div></div>'
                 % svg_bars(syms, pls, height=190, label="total P/L by ticker",
                            fmt=lambda v: signed(v, 0)))

    # -------------------------------------------------- where the money came from
    made = p["made_today"]
    B.append("<h2>Where today's money came from</h2>")
    B.append(table(["", "Amount"], [
        ["Booked &mdash; lots that sold today", fig(p["realized_today"], NEED_MARKS)],
        ["Open lots &mdash; move since yesterday's close",
         fig(p["open_today"], NEED_MARKS)],
        ["<b>Net today</b>", fig(made, NEED_MARKS, bold=True)],
    ]))

    # ------------------------------------------------------------ by rung
    B.append("<h2>By ladder rung</h2>")
    B.append(_by_rung_table(stats))

    # ------------------------------------------------------------ by config
    bc = stats.get("by_config") or {}
    if bc:
        B.append("<h2>By settings that were live at the time</h2>")
        crows = []
        for h, d in bc.items():
            cfg = d.get("cfg") or {}
            keys = ("add_distance", "take_profit", "shares_per_lot", "max_lots",
                    "exit_mode", "side_mode")
            desc = ", ".join("%s=%s" % (k, cfg[k]) for k in keys if k in cfg)
            crows.append(['<span class="mono">%s</span>' % esc(str(h)[:10]),
                          '<span class="sub">%s</span>' % esc(desc or "—"),
                          str(d.get("opens") or 0), str(d.get("closes") or 0),
                          fig(d.get("realized"), NEED_MARKS),
                          '<span class="sub">%s &rarr; %s</span>'
                          % (esc(str(d.get("first_seen"))[:16].replace("T", " ")),
                             esc(str(d.get("last_seen"))[:16].replace("T", " ")))])
        B.append(table(["Config", "Settings", "Opened", "Closed", "Booked",
                        "Seen"], crows))
        B.append('<p class="sub">A settings change splits the record. Compare '
                 'rows only where both saw enough closes to mean anything.</p>')

    # ------------------------------------------------------------ by session
    bs = stats.get("by_session") or {}
    if bs:
        B.append("<h2>By session</h2>")
        B.append(table(["Session", "Closed", "Booked", "Share of booked"],
                       [[esc(k), str(d.get("closes") or 0),
                         fig(d.get("realized"), NEED_MARKS),
                         "%.1f%%" % (100.0 * (d.get("realized") or 0)
                                     / (stats.get("realized") or 1))]
                        for k, d in bs.items()]))

    # ------------------------------------------------------------ velocity
    B.append("<h2>Velocity &mdash; how fast capital recycles</h2>")
    B.append(table(["Metric", "Value", ""], [
        ["Trading days in this window", str(stats.get("trading_days") or 0), ""],
        ["Booked per trading day", fig(stats.get("realized_per_day"), NEED_MARKS),
         '<span class="sub">booked only; says nothing about the open book</span>'],
        ["Closes per trading day",
         "%.2f" % float(stats.get("closes_per_day") or 0), ""],
        ["Lots opened / closed",
         "%s / %s" % (format(stats.get("opens") or 0, ","),
                      format(stats.get("closes") or 0, ",")), ""],
        ["Shares bought / sold",
         "%s / %s" % (qstr(stats.get("shares_bought") or 0),
                      qstr(stats.get("shares_sold") or 0)), ""],
        ["Capital deployed", money(stats.get("capital_deployed"), 0),
         '<span class="sub">summed cost of every lot opened</span>'],
        ["Return on deployed",
         "%.3f%%" % float(stats.get("return_on_deployed_pct") or 0),
         '<span class="sub">booked against that</span>'],
        ["Median hold", _hold(stats.get("median_hold_seconds")), ""],
        ["Average hold", _hold(stats.get("avg_hold_seconds")), ""],
        ["Longest hold", _hold(stats.get("max_hold_seconds")),
         '<span class="sub">the lot that took the longest to clear</span>'],
        ["Deepest ladder", str(stats.get("max_ladder_depth") or 0),
         '<span class="sub">most rungs used at once</span>'],
    ]))

    # ------------------------------------------------------------ inventory
    B.append("<h2>Open inventory &mdash; where the risk is</h2>")
    B.append(_inventory(inv, marks, limit=200 if kind == "inventory" else 60))

    # ------------------------------------------------------------ journal tail
    tail = [r for r in rows if r.get("event") in ("open", "close", "partial")][-40:]
    B.append("<h2>Journal tail &mdash; the last %d row(s)</h2>" % len(tail))
    B.append(table(["When", "Symbol", "Event", "Lot", "Rung", "Shares", "Price",
                    "Booked", "Why"],
                   [[esc(str(r.get("ts"))[:19].replace("T", " ")),
                     esc(r.get("symbol")),
                     '<span class="pill%s">%s</span>'
                     % (" acc" if r.get("event") == "open" else "",
                        esc(r.get("event"))),
                     '<span class="mono">%s</span>' % esc(r.get("lot_id")),
                     esc(r.get("rung")),
                     qstr(r.get("shares") or 0),
                     (money(r.get("exit_price") or r.get("entry_price"), 4)
                      if (r.get("exit_price") or r.get("entry_price"))
                      else '<span class="dim">no price on this row</span>'),
                     (fig(r.get("realized"), "no figure on this row")
                      if r.get("event") in ("close", "partial")
                      else '<span class="dim">not a close</span>'),
                     '<span class="sub">%s</span>' % esc(r.get("why") or "")]
                    for r in reversed(tail)],
                   "Nothing traded in this window."))

    B.append('<div class="note">Booked P/L is stated next to open inventory '
             'on purpose. This strategy has <b>no stop loss</b>, so a ladder '
             'always looks profitable until the day it does not &mdash; the age '
             'and size of what is still open is where the risk actually is. '
             'Per-ticker booked uses each ladder\'s specific-lot accounting; '
             'the account figure uses Alpaca\'s average-cost convention, so the '
             'two can differ on shares carried in from a prior session.</div>')

    out.write_text(page(title, "".join(B), sub), encoding="utf-8")
    return out


def _hold(secs) -> str:
    if not secs:
        return '<span class="dim">—</span>'
    s = float(secs)
    if s < 5400:
        return "%d min" % (s // 60)
    if s < 172800:
        return "%.1f h" % (s / 3600)
    return "%.1f days" % (s / 86400)


def _chart_block(eq_points, eq_base, curve, inv, stats) -> str:
    """The report's main chart: TIME across, TOTAL P/L up, trades numbered.

    Alpaca's own account curve is the primary series when it is there, because
    that is the true total P/L over time and needs no reconstruction. When the
    broker call failed the stepped BOOKED curve is drawn instead and the panel
    says which one is on screen -- the two are different numbers and silently
    swapping one for the other is how a report lies without a false figure in
    it.
    """
    sec = []
    for c in curve:
        t = _parse_t(c.get("t"))
        if t is not None and c.get("realized") is not None:
            sec.append((t, float(c["realized"])))
    sec = _stepped(sec) if sec else []

    acct = None
    if eq_points and eq_base is not None:
        primary = [(_parse_t(q["t"]), q["equity"] - eq_base) for q in eq_points]
        primary = [(t, v) for t, v in primary if t is not None]
        acct = primary[-1][1] if primary else None
        src = ("<b>Alpaca's own account equity</b> minus the %s it started the "
               "window at. This is the real total P/L over time: it moves with "
               "every open lot, not only with what sold." % money(eq_base))
        legend = ('<div class="legend">'
                  '<span><i></i>account total P/L (equity &minus; base)</span>'
                  '<span><i class="dash"></i>what the ladders booked, stepped '
                  'per close</span>'
                  '<span><i class="dot"></i>closed trade, numbered</span></div>')
        warn = ""
    else:
        # the fallback: the booked staircase becomes the primary line, and
        # there is no second series, because there is nothing to compare to
        primary, sec = sec, []
        src = ("<b>Cumulative BOOKED P/L only</b> &mdash; Alpaca's account "
               "curve was not available for this window, so this is the "
               "stepped realized curve instead.")
        legend = ('<div class="legend">'
                  '<span><i></i>cumulative booked P/L</span>'
                  '<span><i class="dot"></i>closed trade, numbered</span></div>')
        warn = ('<div class="note warn"><b>This is the fallback curve, not '
                'total P/L.</b> The line above is what the ladders BOOKED: it '
                'does not include the %d lot(s) still open, which is the half '
                'that can move against you. Alpaca\'s own account curve is '
                'what would normally be drawn here.</div>' % len(inv))

    body = svg_pl_time(primary, curve=curve, secondary=sec,
                       label="total P/L over time")

    lots = (stats.get("open_lots") if stats.get("open_lots") is not None
            else len(inv))
    if stats.get("total_pl") is not None:
        ladder = ('The <b>ladders\'</b> own total for this window is <b>%s</b> '
                  '&mdash; %s booked plus %s unrealized on %s open lot(s).'
                  % (signed(stats["total_pl"]), signed(stats.get("realized")),
                     signed(stats.get("unrealized")), lots))
    else:
        ladder = ("The <b>ladders'</b> own total is not known here: the open "
                  "side %s, so only the booked half is measured." % NEED_MARKS)
    if acct is not None:
        # The two are DIFFERENT measurements and are not expected to agree.
        # Saying so here is cheaper than a reader deciding one of them is wrong.
        tail = ('<p class="sub">The line ends at <b>%s</b>, which is the whole '
                'ACCOUNT: every position it holds, managed by a ladder or not, '
                'on Alpaca\'s average-cost convention. %s The two are different '
                'measurements of different things and will not match.</p>'
                % (signed(acct), ladder))
    else:
        tail = '<p class="sub">%s</p>' % ladder
    return ('<div class="card"><div class="chart-h"><div>'
            '<div class="chart-t">Total P/L, by time</div>'
            '<div class="chart-s">%s</div></div>%s</div>'
            '<div class="chartwrap">%s</div>%s</div>%s'
            % (src, legend, body, tail, warn))


def _metrics(stats: dict, filled: set, inv: list, p: dict) -> str:
    """The backtest's vocabulary, on the live book. Same names as
    `backtest.run()` wherever the concept is the same, so a journal report and
    a strategy result can be read side by side."""
    B = []
    mark = (lambda k: '<sup class="cav" title="recomputed by the report from '
            'the closed rows in this window">&dagger;</sup>' if k in filled
            else "")

    B.append('<div class="kpis">'
             + kpi("Total P/L",
                   fig(stats.get("total_pl"), NEED_MARKS, bold=True),
                   "booked + open, marked live")
             + kpi("Booked", fig(stats.get("realized"), NEED_MARKS),
                   "%s closed lot(s)" % format(stats.get("closes") or 0, ","))
             + kpi("Unrealized", fig(stats.get("unrealized"), NEED_MARKS),
                   "the open book")
             + kpi("Open lots",
                   str(stats.get("open_lots") if stats.get("open_lots")
                       is not None else len(inv)),
                   "%s shares" % qstr(stats.get("open_shares")
                                      if stats.get("open_shares") is not None
                                      else sum(x["shares"] for x in inv)))
             + kpi("Open cost",
                   money(stats.get("open_cost") if stats.get("open_cost")
                         is not None else sum(x["cost"] for x in inv), 0),
                   "still deployed")
             + kpi("Open market value",
                   (money(stats.get("open_market_value"), 0)
                    if stats.get("open_market_value") is not None
                    else na(NEED_MARKS)),
                   "what it is worth now")
             + "</div>")

    wr = stats.get("win_rate")
    wrm = stats.get("win_rate_meaningful")
    if wrm is None:
        wrm = bool(stats.get("losses"))
    wr_cell = ("%.1f%%" % float(wr) if wr is not None else na(NEED_MARKS))
    if not wrm:
        wr_cell += ('<div class="cav">an artefact, not an edge &mdash; see '
                    'below</div>')

    B.append("<h3>Trade statistics</h3>")
    B.append(table(["", "Value", ""], [
        ["Closed lots", format(stats.get("closes") or 0, ","),
         '<span class="sub">every lot that actually sold</span>'],
        ["Winners / losers%s" % mark("wins"),
         "%s / %s" % (format(stats.get("wins") or 0, ","),
                      format(stats.get("losses") or 0, ",")), ""],
        ["Percent profitable%s" % mark("win_rate"), wr_cell,
         '<span class="sub">closed lots only</span>'],
        ["Average winner%s" % mark("avg_win"),
         fig(stats.get("avg_win"), NEED_MARKS), ""],
        ["Average loser%s" % mark("avg_loss"),
         (fig(stats.get("avg_loss"), NEED_MARKS)
          if stats.get("losses") else na(NOTHING_LOST,
                                         "no closed lot has lost money in "
                                         "this window")), ""],
        ["Expectancy per closed lot%s" % mark("expectancy"),
         fig(stats.get("expectancy"), NEED_MARKS),
         '<span class="sub">booked &divide; closes</span>'],
        ["Profit factor%s" % mark("profit_factor"),
         ('<b>%.2f</b>' % stats["profit_factor"]
          if stats.get("profit_factor") is not None
          else na(NOTHING_LOST, "gross loss is zero, so the ratio has no "
                                "value. This is NOT an infinite edge.")),
         '<span class="sub">gross win &divide; gross loss. Below 1.0 loses '
         'money</span>'],
    ]))
    if not wrm:
        B.append('<div class="note warn"><b>The win rate is an artefact.</b> '
                 'Nothing closed at a loss in this window, and this strategy '
                 'has <b>no stop loss</b> &mdash; a losing lot is simply never '
                 'sold, it sits in the open inventory below. A ~100%% win rate '
                 'is what that always looks like and it is not evidence of an '
                 'edge. The number that can go down is the unrealized one.'
                 '</div>')

    B.append("<h3>Risk and drawdown</h3>")
    mae_reason = _mae_reason(stats)
    # with no marks the curve behind this figure is the BOOKED one, which
    # cannot dip on an open lot -- say so rather than let it pass as the total
    dd_of = ("worst peak-to-trough of the total P/L curve"
             if stats.get("marked") else
             "worst peak-to-trough of the BOOKED curve only &mdash; the open "
             "side is not priced, so nothing an open lot did is in this")
    B.append(table(["", "Value", ""], [
        ["Max drawdown%s" % mark("max_drawdown"),
         fig(stats.get("max_drawdown"), NEED_MARKS, bold=True),
         '<span class="sub">%s</span>' % dd_of],
        ["Max drawdown, percent%s" % mark("max_drawdown_pct"),
         (('<span class="%s">%.2f%%</span>'
           % (cls(stats.get("max_drawdown_pct")), stats["max_drawdown_pct"]))
          if stats.get("max_drawdown_pct") is not None else na(NEED_MARKS)),
         '<span class="sub">against the peak</span>'],
        ["Peak capital%s" % mark("peak_capital"),
         money(stats.get("peak_capital"), 0)
         if stats.get("peak_capital") is not None else na(NEED_MARKS),
         '<span class="sub">largest cost basis open at once &mdash; the '
         'worst-case capital this ladder actually asked for</span>'],
        ["Return on peak capital%s" % mark("return_on_peak_capital_pct"),
         (('<b class="%s">%.3f%%</b>'
           % (cls(stats.get("return_on_peak_capital_pct")),
              stats["return_on_peak_capital_pct"]))
          if stats.get("return_on_peak_capital_pct") is not None
          else na(NEED_MARKS)),
         '<span class="sub">total P/L against that capital</span>'],
        ["Average adverse excursion",
         fig(stats.get("avg_mae"), mae_reason,
             why="MAE is recorded tick by tick from deploy; lots opened "
                 "before carry none."),
         '<span class="sub">how far the average lot went underwater before it '
         'came back</span>'],
        ["Worst adverse excursion",
         fig(stats.get("worst_mae"), mae_reason,
             why="MAE is recorded tick by tick from deploy; lots opened "
                 "before carry none."),
         '<span class="sub">the deepest hole any single lot sat in</span>'],
    ]))
    if stats.get("avg_mae") is None:
        B.append('<div class="note"><b>Adverse excursion %s.</b> The journal '
                 'cannot reconstruct how far a closed lot went underwater '
                 '&mdash; the price path is gone &mdash; so the engine records '
                 'it tick by tick as it happens. Lots opened before that '
                 'started carry none, and this report says so rather than '
                 'printing a comforting zero.</div>' % mae_reason)
    if filled:
        B.append('<p class="sub">&dagger; recomputed by this report from the '
                 'closed rows in the window, because this build of '
                 '<code>journal.stats()</code> did not return it. Those '
                 'figures therefore cover the BOOKED curve only.</p>')
    return "".join(B)


def _by_rung_table(stats: dict) -> str:
    """Which depths get used, what they pay, and -- the point of the change --
    how far underwater each one went while it was being paid."""
    br = stats.get("by_rung") or {}
    if not br:
        return '<div class="empty">No rung has been used in this window.</div>'
    any_dd = any(d.get("max_drawdown") is not None for d in br.values())
    rows = []
    for r, d in br.items():
        opened = d.get("opened") or 0
        closed = d.get("closed") or 0
        op = d.get("open")
        if op is None:
            op = opened - closed
        dfrom = d.get("drawdown_from")
        # a rung holding nothing is worth nothing -- that is not a missing
        # price, and saying "needs live prices" for it would cry wolf
        unreal = d.get("unrealized")
        tot = d.get("total_pl")
        if op == 0 and unreal is None:
            unreal_cell = '<span class="dim">nothing open</span>'
            tot = d.get("realized")
        else:
            unreal_cell = fig(unreal, NEED_MARKS)
        rows.append([
            "rung %s" % esc(r), str(opened), str(closed),
            '<span class="%s">%d</span>' % ("warn" if op > 0 else "dim", op),
            fig(d.get("realized"), NEED_MARKS),
            unreal_cell,
            fig(tot, NEED_MARKS, bold=True),
            fig(d.get("max_drawdown"), _mae_reason(stats)),
            fig(d.get("avg_drawdown"), _mae_reason(stats)),
            ("%d of %d" % (dfrom, opened) if dfrom is not None
             else na(_mae_reason(stats))),
            _hold(d.get("avg_hold_seconds")),
        ])
    out = [table(["Rung", "Opened", "Closed", "Open", "Booked", "Unrealized",
                  "Total P/L", "Max drawdown", "Avg drawdown", "Recorded on",
                  "Avg hold"], rows)]
    out.append('<p class="sub">A rung that opens often and closes rarely is '
               'where the add distance is putting money it does not get back '
               'quickly. <b>Max drawdown</b> is the worst any single lot at '
               'that rung went underwater, in dollars &mdash; the deep rungs '
               'are supposed to show the biggest holes, and a shallow rung '
               'with a large one means the ladder is adding too early.</p>')
    if not any_dd:
        out.append('<div class="note"><b>No rung carries a drawdown yet.</b> '
                   'Adverse excursion is recorded per lot from the moment the '
                   'engine started keeping it (%s). Every lot older than that '
                   'shows the reason instead of a zero.</div>'
                   % _mae_reason(stats))
    return "".join(out)


def _inventory(inv: list, marks: dict, limit: int = 60) -> str:
    if not inv:
        return '<div class="empty">Nothing open. No unrealized risk.</div>'
    aged = [x for x in inv if x["age_days"] > 3]
    B = ['<div class="note%s"><b>%d lot(s)</b> holding <b>%s</b> at cost%s</div>'
         % (" warn" if aged else "", len(inv),
            money(sum(x["cost"] for x in inv), 0),
            (", of which <b>%d</b> are more than 3 days old (<b>%s</b>)."
             % (len(aged), money(sum(x["cost"] for x in aged), 0)))
            if aged else ".")]
    rows = []
    for x in inv[:limit]:
        mk = marks.get(x["symbol"])
        upl = None
        if mk:
            upl = round((float(mk) - float(x["entry_price"] or 0))
                        * float(x["shares"]), 2)
        rows.append([
            '<span class="mono">%s</span>' % esc(x["lot_id"]),
            esc(x["symbol"]),
            '<span class="%s">%.1fd</span>'
            % ("down" if x["age_days"] > 3
               else "warn" if x["age_days"] > 1 else "dim", x["age_days"]),
            esc(x.get("rung")),
            qstr(x["shares"]), money(x["entry_price"], 4),
            money(mk, 4) if mk else na(NEED_MARKS),
            money(x["tp_price"]), money(x["cost"], 0),
            fig(upl, NEED_MARKS),
        ])
    B.append(table(["Lot", "Symbol", "Age", "Rung", "Shares", "Entry", "Mark",
                    "Target", "Cost", "Unrealized"], rows))
    if len(inv) > limit:
        B.append('<p class="sub">%d further lot(s) not listed.</p>'
                 % (len(inv) - limit))
    return "".join(B)


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
