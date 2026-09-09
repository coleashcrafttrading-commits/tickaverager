#!/usr/bin/env python3
"""
report.py -- printable PDF reports, generated on demand or by an agent.

Everything an agent needs to hand you a document lives here, so a scheduled
job can call one function and produce a file you can open, print or send.

    from report import build_report
    path = build_report(fleet, kind="daily")

Reports land in reports/ and are listed by the dashboard.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports"

INK = "#1a2332"; DIM = "#6b7a8f"; GRN = "#0a7a42"; RED = "#c0392b"
AMB = "#9a6b00"; LINE = "#d6dde5"; BG = "#f4f7fa"


def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    st = getSampleStyleSheet()
    return {
        "H1": ParagraphStyle("H1", parent=st["Title"], fontSize=18,
                             textColor=colors.HexColor(INK), alignment=0,
                             spaceAfter=3, fontName="Helvetica-Bold"),
        "SUB": ParagraphStyle("SUB", parent=st["Normal"], fontSize=9.5,
                              textColor=colors.HexColor(DIM), spaceAfter=14),
        "H2": ParagraphStyle("H2", parent=st["Normal"], fontSize=11.5,
                             textColor=colors.HexColor(INK),
                             fontName="Helvetica-Bold", spaceBefore=15, spaceAfter=7),
        "BODY": ParagraphStyle("BODY", parent=st["Normal"], fontSize=9,
                               textColor=colors.HexColor(INK), leading=13.5,
                               spaceAfter=6),
        "NOTE": ParagraphStyle("NOTE", parent=st["Normal"], fontSize=8,
                               textColor=colors.HexColor(DIM), leading=11.5),
        "CELL": ParagraphStyle("CELL", parent=st["Normal"], fontSize=8.2,
                               textColor=colors.HexColor(INK), leading=11.5),
    }


def money(n, dp=2):
    n = float(n or 0)
    return ("-" if n < 0 else "") + f"${abs(n):,.{dp}f}"


def signed(n, dp=2):
    n = float(n or 0)
    return ("+" if n > 0 else "") + money(n, dp)


def build_report(fleet: Any, kind: str = "daily", days: int = 1,
                 note: str = "") -> Path:
    """Write a PDF and return its path.

    kind: daily | weekly | inventory | full
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)
    import journal

    S = _styles()
    REPORT_DIR.mkdir(exist_ok=True)
    now = datetime.now().astimezone()
    stamp = now.strftime("%Y-%m-%d_%H%M")
    out = REPORT_DIR / f"{kind}_{stamp}.pdf"

    ov = fleet.overview()
    p = ov["portfolio"]
    rows = journal.load(path=getattr(fleet, 'journal_path', None), days=days if kind != "full" else None)
    stats = journal.stats(rows)
    inv = journal.open_inventory(journal.load(path=getattr(fleet, 'journal_path', None)))

    def tbl(head, body, widths):
        t = Table([head] + body, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8.2),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(DIM)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(LINE)),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor(BG)]),
        ]))
        return t

    doc = SimpleDocTemplate(str(out), pagesize=letter,
                            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title=f"TickAverager {kind} report {stamp}")
    F = []
    titles = {"daily": "Daily report", "weekly": "Weekly report",
              "inventory": "Open inventory", "full": "Full history"}
    F.append(Paragraph(titles.get(kind, "Report"), S["H1"]))
    F.append(Paragraph(
        f"{now:%A, %d %B %Y at %H:%M} &nbsp;&middot;&nbsp; Alpaca "
        f"{'paper' if ov['paper'] else 'LIVE'} {ov['account']['number']}"
        + (f" &nbsp;&middot;&nbsp; {note}" if note else ""), S["SUB"]))

    # ---- headline ----
    made = p["made_today"]
    head = Table([[
        Paragraph(f"<font size=8.5 color='{DIM}'><b>NET TODAY</b></font><br/>"
                  f"<font size=18 color='{GRN if made >= 0 else RED}'>"
                  f"<b>{signed(made)}</b></font>", S["BODY"]),
        Paragraph(f"<font size=8.5 color='{DIM}'><b>ACCOUNT</b></font><br/>"
                  f"<font size=18 color='{INK}'><b>{money(p['account_value'])}</b>"
                  f"</font>", S["BODY"]),
        Paragraph(f"<font size=8.5 color='{DIM}'><b>OPEN P/L</b></font><br/>"
                  f"<font size=18 color='{GRN if p['open_pl'] >= 0 else RED}'>"
                  f"<b>{signed(p['open_pl'])}</b></font>", S["BODY"]),
        Paragraph(f"<font size=8.5 color='{DIM}'><b>DEPLOYED</b></font><br/>"
                  f"<font size=18 color='{INK}'><b>{money(p['deployed'], 0)}</b>"
                  f"</font>", S["BODY"]),
    ]], colWidths=[1.78 * inch] * 4)
    head.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(BG)),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(LINE)),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor(LINE)),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    F.append(head)

    # ---- per ticker ----
    F.append(Paragraph("Per ticker", S["H2"]))
    body = []
    for t in ov["tickers"]:
        sym = t["symbol"]
        sr = [r for r in rows if r.get("symbol") == sym]
        o = len([r for r in sr if r.get("event") == "open"])
        c = len([r for r in sr if r.get("event") == "close"])
        pf = len([r for r in sr if r.get("event") == "partial"])
        body.append([sym, t["state"], str(o), str(c), str(pf),
                     signed(t["realized_today"]), signed(t["unrealized"]),
                     f"{t['lot_count']}/{t['max_lots']}", str(t["shares"])])
    F.append(tbl(["Ticker", "State", "Opened", "Closed", "Partials",
                  "Realized", "Open P/L", "Lots", "Shares"], body,
                 [0.72, 1.0, 0.62, 0.6, 0.65, 0.85, 0.85, 0.6, 0.6]))

    # ---- flow ----
    F.append(Paragraph("Where the money came from", S["H2"]))
    F.append(tbl(["", "Amount"], [
        ["Realized — lots that sold", signed(p["realized_today"])],
        ["Open lots — move since yesterday's close", signed(p["open_today"])],
        ["Net today", signed(made)],
    ], [4.8 * inch, 1.4 * inch]))

    # ---- journal window ----
    F.append(Paragraph(
        f"Journal — {'all history' if kind == 'full' else f'last {days} day(s)'}",
        S["H2"]))
    F.append(tbl(["Metric", "Value"], [
        ["Lots opened", str(stats["opens"])],
        ["Lots closed", str(stats["closes"])],
        ["Realized", signed(stats["realized"])],
        ["Capital deployed", money(stats["capital_deployed"], 0)],
        ["Return on deployed", f"{stats['return_on_deployed_pct']:.3f}%"],
        ["Median hold", f"{stats['median_hold_seconds'] // 60} min"],
        ["Longest hold", f"{stats['max_hold_seconds'] / 3600:.1f} h"],
        ["Deepest ladder", str(stats["max_ladder_depth"])],
    ], [3.4 * inch, 2.8 * inch]))

    # ---- per rung ----
    br = stats.get("by_rung") or {}
    if br:
        F.append(Paragraph("By ladder rung", S["H2"]))
        F.append(tbl(["Rung", "Opened", "Closed", "Still open", "Realized",
                      "Avg hold"],
                     [[r, str(d["opened"]), str(d["closed"]),
                       str(d["opened"] - d["closed"]), signed(d["realized"]),
                       f"{d['avg_hold_seconds'] // 60}m" if d["avg_hold_seconds"] else "—"]
                      for r, d in br.items()],
                     [0.7, 0.9, 0.9, 1.0, 1.2, 1.0]))
        F.append(Paragraph(
            "A rung that opens often and closes rarely is where the add distance is "
            "putting money it does not get back quickly.", S["NOTE"]))

    # ---- inventory ----
    F.append(Paragraph("Open inventory", S["H2"]))
    if inv:
        aged = [x for x in inv if x["age_days"] > 3]
        F.append(Paragraph(
            f"<b>{len(inv)} lot(s)</b> holding <b>{money(sum(x['cost'] for x in inv), 0)}</b>"
            + (f", of which <b>{len(aged)}</b> are more than 3 days old "
               f"(<b>{money(sum(x['cost'] for x in aged), 0)}</b>)." if aged else "."),
            S["BODY"]))
        F.append(tbl(["Lot", "Symbol", "Age", "Shares", "Entry", "Target", "Cost"],
                     [[x["lot_id"], x["symbol"], f"{x['age_days']:.1f}d",
                       str(x["shares"]), money(x["entry_price"], 4),
                       money(x["tp_price"]), money(x["cost"], 0)]
                      for x in inv[:40]],
                     [1.9, 0.7, 0.6, 0.65, 0.85, 0.8, 0.75]))
    else:
        F.append(Paragraph("Nothing open.", S["BODY"]))

    F.append(Spacer(1, 12))
    F.append(Paragraph(
        "Realized P/L is stated next to open inventory on purpose. This strategy has "
        "no stop loss, so a ladder always looks profitable until the day it does not "
        "— the age and size of what is still open is where the risk actually is. "
        "Per-ticker realized uses each ladder's specific-lot accounting; the account "
        "figure uses Alpaca's average-cost convention, so the two can differ on shares "
        "carried in from a prior session.", S["NOTE"]))

    doc.build(F)
    return out


def listing(limit: int = 60) -> list[dict]:
    if not REPORT_DIR.exists():
        return []
    out = []
    files = [p for p in REPORT_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in (".pdf", ".html")]
    for f in sorted(files, key=lambda x: x.stat().st_mtime,
                    reverse=True)[:limit]:
        s = f.stat()
        out.append({
            "name": f.name,
            "format": f.suffix.lower().lstrip("."),
            "kind": f.name.split("_")[0],
            "size": s.st_size,
            "modified": datetime.fromtimestamp(s.st_mtime).astimezone()
                        .isoformat(timespec="seconds"),
        })
    return out
