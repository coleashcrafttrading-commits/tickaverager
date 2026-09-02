#!/usr/bin/env python3
"""
research_report.py -- turn research passes into a document you can act on.

Reads the JSON written by research.py (one file per timeframe), re-runs the
survivors to get their equity curves and trade detail, and writes a PDF.

    .venv/Scripts/python research_report.py
    .venv/Scripts/python research_report.py --top 5

The report leads with what would have happened OUT OF SAMPLE, states the
selection method before the results, and prints the control families next to
the winners. A strategy report that does not show you what a coin flip scored
on the same bars is a sales document.
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
RESEARCH_DIR = ROOT / "research"
REPORT_DIR = ROOT / "reports"

INK = "#1a2332"; DIM = "#6b7a8f"; GRN = "#0a7a42"; RED = "#c0392b"
AMB = "#9a6b00"; LINE = "#d6dde5"; BG = "#f4f7fa"; ACC = "#2b5fa8"


def money(n, dp=2):
    if n is None:
        return "n/a"
    n = float(n)
    return ("-" if n < 0 else "") + "$%s" % format(abs(n), ",.%df" % dp)


def signed(n, dp=2):
    if n is None:
        return "n/a"
    return ("+" if float(n) > 0 else "") + money(n, dp)


def num(n, dp=3):
    return "n/a" if n is None else ("%+.*f" % (dp, n))


def load_passes(paths: Optional[list[str]] = None) -> list[dict]:
    files = paths or sorted(glob.glob(str(RESEARCH_DIR / "*.json")))
    out = []
    for f in files:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            d["_file"] = Path(f).name
            out.append(d)
        except Exception as e:
            print("  skipped %s: %r" % (f, e))
    return out


def has_controls(p: dict) -> bool:
    return any(c.get("family", "").startswith("CONTROL")
               for c in p.get("selected", []))


def newest_per_timeframe(passes: list[dict]) -> list[dict]:
    """One pass per timeframe: the newest that INCLUDES the controls.

    A pass without the random-entry control cannot be checked against the null,
    and a strategy score with nothing to compare it to is a number, not a
    finding. So a control-bearing pass always beats a newer one without them;
    a control-less pass is used only if there is no alternative, and is marked
    so the report can say the comparison is missing.
    """
    best: dict[str, dict] = {}
    for p in passes:
        tf = p.get("timeframe", "?")
        cur = best.get(tf)
        if cur is None:
            best[tf] = p
            continue
        better = ((has_controls(p), p.get("when", ""))
                  > (has_controls(cur), cur.get("when", "")))
        if better:
            best[tf] = p
    for p in best.values():
        p["_has_controls"] = has_controls(p)
    return sorted(best.values(), key=lambda p: p.get("timeframe", ""))


def controls_of(p: dict) -> list[dict]:
    """Every control family's result, selected or not."""
    out = []
    for c in p.get("selected", []):
        if c.get("family", "").startswith("CONTROL"):
            out.append(c)
    return out


def real_candidates(p: dict) -> list[dict]:
    return [c for c in p.get("ranked", [])
            if not c.get("family", "").startswith("CONTROL")]


def control_bar(p: dict) -> Optional[float]:
    """The best out-of-sample score any CONTROL managed.

    This is the number a real strategy has to clear. Beating the other
    strategies in the search is not evidence of anything; beating the coin
    flip on the same bars, with the same costs, is the minimum bar.
    """
    scores = []
    for c in controls_of(p):
        te = c.get("test") or {}
        if te.get("median_score") is not None:
            scores.append(te["median_score"])
    return max(scores) if scores else None


def pooled_ranking(passes: list[dict], top: int,
                   abl: Optional[dict] = None) -> list[dict]:
    """Rank across every timeframe, tagging each candidate with its pass.

    Sorted by, in order: does the ablation say the signal is real, how many
    timeframes it survived on, then the out-of-sample score. Score alone would
    put a one-window position-management artifact above a signal that cleared
    the whole search twice, and that is the wrong way round.
    """
    abl = abl or {}
    xtf = cross_timeframe(passes)
    rows = []
    seen_best: dict[str, dict] = {}
    for p in passes:
        bar = control_bar(p)
        for c in real_candidates(p):
            c = dict(c)
            fam = c["family"]
            c["timeframe"] = p.get("timeframe")
            c["days"] = p.get("days")
            c["control_bar"] = bar
            c["beats_control"] = (
                bar is None or (c["test"]["median_score"] or 0) > bar)
            c["timeframes"] = xtf.get(fam, {})
            c["n_timeframes"] = len(c["timeframes"])
            a = abl.get(fam)
            c["ablation"] = a.get("verdict") if a else None
            c["signal_is_real"] = bool(
                a and (str(a.get("verdict", "")).startswith("signal carries")
                       or str(a.get("verdict", "")).startswith("BETTER without")))
            # one row per family: its best timeframe represents it
            cur = seen_best.get(fam)
            if cur is None or (c["test"]["median_score"] or 0) > (
                    cur["test"]["median_score"] or 0):
                seen_best[fam] = c
    rows = list(seen_best.values())
    rows.sort(key=lambda c: (not c["beats_control"],
                             not c["signal_is_real"],
                             -c["n_timeframes"],
                             -(c["test"]["median_score"] or 0)))
    if rows:
        return rows[:top]

    # Nothing cleared the gates. Rather than print an empty page, show the
    # ones that came CLOSEST, flagged as not qualifying -- seeing how far
    # short they fell is more useful than seeing nothing, and it is the
    # difference between "we found nothing" and "we did not look".
    near = []
    for p in passes:
        bar = control_bar(p)
        for c in p.get("rejected", []):
            if c.get("family", "").startswith("CONTROL"):
                continue
            if not c.get("selected") or not (c.get("test") or {}).get("median_score"):
                continue
            c = dict(c)
            c["timeframe"] = p.get("timeframe")
            c["days"] = p.get("days")
            c["control_bar"] = bar
            c["beats_control"] = (
                bar is None or (c["test"]["median_score"] or 0) > bar)
            c["did_not_qualify"] = True
            near.append(c)
    near.sort(key=lambda c: -(c["test"]["median_score"] or 0))
    return near[:top]



def cross_timeframe(passes: list[dict]) -> dict[str, dict]:
    """Which families survived on more than one bar size.

    Independent agreement across timeframes is the hardest thing in this whole
    exercise to get by luck. A family selected separately on 5-minute and
    15-minute bars, on different windows, has cleared the search twice --
    which is worth more than any single score, because the multiple-comparison
    problem does not compound across independent runs the way it does within
    one.
    """
    out: dict[str, dict] = {}
    for p in passes:
        tf = p.get("timeframe", "?")
        for c in p.get("ranked", []):
            fam = c.get("family", "")
            if fam.startswith("CONTROL"):
                continue
            out.setdefault(fam, {})[tf] = c["test"]["median_score"]
    return out


def cost_sensitivity(winners: list[dict], passes: list[dict],
                     multipliers=(0.5, 1.0, 2.0, 4.0)) -> None:
    """Re-run each winner out of sample at several cost assumptions.

    The slippage model is an estimate, and a strategy taking thousands of small
    trades is far more sensitive to it than one taking dozens. A result that
    only exists at the assumed cost is not a strategy, it is a statement about
    the assumption -- so this puts the number next to it and lets you see.

    Mutates each winner in place, adding {"cost_scan": {mult: score}}.
    """
    import btcode
    import research

    by_tf: dict[str, list[dict]] = {}
    for w in winners:
        by_tf.setdefault(w["timeframe"], []).append(w)

    for tf, group in by_tf.items():
        p = next((x for x in passes if x.get("timeframe") == tf), None)
        if not p:
            continue
        syms = list((p.get("meta") or {}))
        days = p.get("days", 90)
        print("  re-running %d winner(s) on %s bars at %d cost levels..."
              % (len(group), tf, len(multipliers)))
        data = research.fetch(syms, tf, days)
        fam_by_name = {f["name"]: f for f in research.FAMILIES}

        for w in group:
            fam = fam_by_name.get(w["family"])
            if not fam:
                continue
            code = research.build(fam)
            scan: dict[str, Any] = {}
            for m in multipliers:
                per = {}
                for sym, bars in data.items():
                    _tr, te = research.split(bars)
                    slip = round(research.slippage_for(bars) * m, 5)
                    reps = btcode.run_many(
                        te, [{"id": "0", "code": code, "params": w["params"]}],
                        opts={"slippage": slip, "fee_per_share": 0.0,
                              "max_positions": 8, "bar_size": tf},
                        slim=True)
                    r = reps[0]
                    if not r.get("ok"):
                        per[sym] = {"score": None, "total_pl": 0.0, "trades": 0}
                        continue
                    sm = r["summary"]
                    per[sym] = {"score": research.score_one(sm),
                                "total_pl": sm["total_pl"],
                                "trades": sm["total_trades"]}
                agg = research.aggregate(per)
                scan[str(m)] = {"median_score": agg["median_score"],
                                "consistency": agg["consistency"],
                                "sum_pl": agg["sum_pl"],
                                "symbols_scored": agg["symbols_scored"]}
            w["cost_scan"] = scan
            base = scan.get("1.0", {}).get("median_score")
            dbl = scan.get("2.0", {}).get("median_score")
            w["survives_double_cost"] = bool(
                dbl is not None and dbl > 0)
            print("    %-34s cost x1 %s   x2 %s   x4 %s"
                  % (w["family"][:34],
                     num(base), num(dbl),
                     num(scan.get("4.0", {}).get("median_score"))))



def _fmt_params(p: dict) -> str:
    """A readable dict literal, sorted so a diff between two winners is legible."""
    items = ",\n".join("    %r: %r" % (k, v) for k, v in sorted(p.items()))
    return "{\n%s,\n}" % items


def install_winners(winners: list[dict]) -> list[str]:
    """Save each winner as a runnable coded strategy.

    The point of a research pass is not a PDF. Each winner is written to
    strategies/code/ with its tested parameters baked into PARAMS and the
    evidence in the docstring, so it appears in the Backtest page's Code
    dropdown and can be re-run, edited or handed to a ticker without anyone
    retyping a number out of a report.

    Nothing is armed and nothing is applied to a live ticker. This writes
    files.
    """
    import btcode
    import research

    fam_by_name = {f["name"]: f for f in research.FAMILIES}
    saved = []
    for i, w in enumerate(winners, 1):
        fam = fam_by_name.get(w["family"])
        if not fam:
            continue
        te, tr = w["test"], w["train"]
        scan = w.get("cost_scan") or {}
        slug = "found-%d-%s" % (i, research._slugish(w["family"]))

        head = '''"""
%s  --  found by the research pass on %s

%s

WHAT THE BACKTEST SAID (out of sample, on data never used to choose anything)
    score (P/L per $ drawdown, median symbol)   %s
    in-sample score, for comparison             %s
    decay out of sample                         %s
    profitable on                               %d of %d symbols
    trades                                      %s
    summed P/L at 100 shares                    %s
    control (random entry) had to be beaten at  %s
    beats that control                          %s
%s
HOW TO READ THAT
    This was chosen on the FIRST 60%% of the history and measured on the last
    40%%, which chose nothing. It is one out-of-sample window on eight symbols.
    That is enough to take an idea seriously and not enough to trust it. Run
    it disarmed on paper and compare what it actually does with what this says
    it should do before it is allowed near real size.
"""
''' % (
            w["family"],
            datetime.now().astimezone().strftime("%Y-%m-%d"),
            fam.get("note", ""),
            num(te["median_score"]), num(tr["median_score"]),
            "n/a" if w.get("decay") is None else "%.2f" % w["decay"],
            te["symbols_profitable"], te["symbols_scored"],
            format(te["total_trades"], ","),
            signed(te["sum_pl"]),
            num(w.get("control_bar")),
            "yes" if w.get("beats_control") else "NO -- treat as unproven",
            ("    at 2x the assumed slippage             %s\n"
             % num((scan.get("2.0") or {}).get("median_score"))) if scan else "",
        )

        body = research.build(fam)
        # Bake the tested parameters in as an explicit override AFTER the
        # dict rather than by rewriting the lines inside it. Editing the
        # literal by regex silently failed on any line with a trailing
        # comment and left the tested values AHEAD of the defaults, where
        # the defaults won -- which would have shipped five strategies
        # whose settings were not the ones that were measured. A plain
        # update that happens last cannot do that, and you can read it.
        override = ("\n\n# --- the settings this was actually measured with, "
                    "written by the research pass ---\n"
                    "PARAMS.update(%s)\n" % _fmt_params(w["params"]))
        src = head + body + override
        try:
            compile(src, "<check>", "exec")
        except SyntaxError as e:
            print("  could not install %s: %s" % (slug, e))
            continue
        btcode.save(slug, src)
        saved.append(slug)
    return saved



def load_side_studies() -> tuple[dict, dict]:
    """The ablation and validation results, keyed by family name.

    Both are separate scripts producing separate files, so the report works
    whether or not they were run -- it simply says less when they are missing
    rather than pretending the checks happened.
    """
    abl: dict[str, dict] = {}
    val: dict[str, dict] = {}
    for f in glob.glob(str(RESEARCH_DIR / "ablation_*.json")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("rows", []):
            abl[r["family"]] = dict(r, _tf=d.get("timeframe"))
    for f in glob.glob(str(RESEARCH_DIR / "validation_*.json")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        for r in d.get("rows", []):
            val[r["family"]] = dict(r, _tf=d.get("timeframe"),
                                    _from=d.get("earlier_from"),
                                    _to=d.get("earlier_to"),
                                    _bar=d.get("control_bar"))
    return abl, val


def verdict_of(w: dict, abl: dict, val: dict) -> tuple[str, list[str]]:
    """How many independent checks did this actually clear?

    Four things, each capable of killing a result on its own: beating the
    coin-flip control, holding up on a window the search never saw, surviving
    a doubling of the cost assumption, and having a signal that does something
    once the position management is stripped away.
    """
    passed, failed = [], []
    (passed if w.get("beats_control") else failed).append("beats the control")

    v = val.get(w["family"])
    if v is None:
        failed.append("not validated on an earlier window")
    elif v.get("earlier") is None:
        failed.append("too few trades on the earlier window to score")
    elif v["earlier"] > 0 and v.get("consistency", 0) >= 0.6:
        passed.append("held up on an earlier unseen window (%+.3f)" % v["earlier"])
    else:
        failed.append("did not hold up on an earlier window (%s)"
                      % num(v.get("earlier")))

    scan = w.get("cost_scan") or {}
    dbl = (scan.get("2.0") or {}).get("median_score")
    if not scan:
        failed.append("cost sensitivity not measured")
    elif dbl is not None and dbl > 0:
        passed.append("survives double the assumed cost")
    else:
        failed.append("does not survive double the assumed cost")

    n = w.get("n_timeframes", 0)
    if n >= 2:
        passed.append("survived on %d timeframes independently (%s)"
                      % (n, ", ".join("%s %s" % (k, num(v))
                                      for k, v in sorted(w["timeframes"].items()))))
    else:
        failed.append("survived on only one timeframe")

    a = abl.get(w["family"])
    if a is None:
        failed.append("signal not isolated from the execution")
    else:
        vd = str(a.get("verdict", ""))
        if vd.startswith("signal carries") or vd.startswith("BETTER without"):
            passed.append("the signal, not the position management")
        else:
            failed.append("ablation says: %s" % vd)
    return ("%d of 5 checks" % len(passed)), passed + ["NOT: " + f for f in failed]


# ==================================================================== render
def build_pdf(passes: list[dict], winners: list[dict], out: Path,
              top: int = 5, abl: Optional[dict] = None,
              val: Optional[dict] = None) -> Path:
    abl = abl or {}
    val = val or {}
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    st = getSampleStyleSheet()
    S = {
        "H1": ParagraphStyle("H1", parent=st["Title"], fontSize=19, alignment=0,
                             textColor=colors.HexColor(INK), spaceAfter=3,
                             fontName="Helvetica-Bold"),
        "SUB": ParagraphStyle("SUB", parent=st["Normal"], fontSize=9.5,
                              textColor=colors.HexColor(DIM), spaceAfter=14),
        "H2": ParagraphStyle("H2", parent=st["Normal"], fontSize=13,
                             textColor=colors.HexColor(INK),
                             fontName="Helvetica-Bold", spaceBefore=17,
                             spaceAfter=7),
        "H3": ParagraphStyle("H3", parent=st["Normal"], fontSize=10.5,
                             textColor=colors.HexColor(ACC),
                             fontName="Helvetica-Bold", spaceBefore=11,
                             spaceAfter=5),
        "BODY": ParagraphStyle("BODY", parent=st["Normal"], fontSize=9,
                               textColor=colors.HexColor(INK), leading=13.5,
                               spaceAfter=6),
        "NOTE": ParagraphStyle("NOTE", parent=st["Normal"], fontSize=8,
                               textColor=colors.HexColor(DIM), leading=11.5,
                               spaceAfter=4),
        "CELL": ParagraphStyle("CELL", parent=st["Normal"], fontSize=7.6,
                               textColor=colors.HexColor(INK), leading=10),
    }

    def tbl(head, body, widths, align_from=1):
        t = Table([head] + body, colWidths=[w * inch for w in widths],
                  repeatRows=1)
        t.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 7.8),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(DIM)),
            ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(LINE)),
            ("ALIGN", (align_from, 0), (-1, -1), "RIGHT"),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor(BG)]),
        ]))
        return t

    REPORT_DIR.mkdir(exist_ok=True)
    now = datetime.now().astimezone()
    doc = SimpleDocTemplate(str(out), pagesize=letter,
                            leftMargin=0.62 * inch, rightMargin=0.62 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch,
                            title="Strategy research %s" % now.strftime("%Y-%m-%d"))
    F: list[Any] = []
    n_bt = sum(p.get("n_backtests", 0) for p in passes)
    n_var = sum(len(p.get("selected", [])) for p in passes)
    secs = sum(p.get("seconds", 0) for p in passes)

    F.append(Paragraph("Strategy research", S["H1"]))
    F.append(Paragraph(
        "%s &nbsp;&middot;&nbsp; %s backtests across %d timeframes "
        "&nbsp;&middot;&nbsp; %.0f minutes of compute"
        % (now.strftime("%A, %d %B %Y at %H:%M"), format(n_bt, ","),
           len(passes), secs / 60), S["SUB"]))

    # ---------------------------------------------------------- verdict
    F.append(Paragraph("The short version", S["H2"]))
    if not winners:
        F.append(Paragraph(
            "<b>Nothing survived.</b> Of everything tested, no strategy family "
            "was profitable out of sample on a majority of symbols after "
            "costs. That is a real result and the honest thing to do with it "
            "is nothing: there is no top five here worth trading, and picking "
            "the five least-bad would be inventing a recommendation the data "
            "does not support.", S["BODY"]))
    elif all(w.get("did_not_qualify") for w in winners):
        F.append(Paragraph(
            "<b>Nothing qualified.</b> No strategy family was profitable out "
            "of sample on a majority of symbols after costs. The five shown "
            "below are the ones that came CLOSEST, and each is marked with "
            "the gate it failed. None of them is a recommendation. The honest "
            "reading of this pass is that the edge is not in this search "
            "space, and the useful output is the rejection list -- it stops "
            "these ideas being tried again.", S["BODY"]))
    else:
        beat = [w for w in winners if w["beats_control"]]
        F.append(Paragraph(
            "%d strateg%s cleared every gate: chosen on the training window "
            "only, profitable out of sample on the median symbol, consistent "
            "across at least 60%% of symbols, and after costs. "
            "<b>%d of them also beat the random-entry control</b> on the same "
            "bars, which is the number that actually matters."
            % (len(winners), "y" if len(winners) == 1 else "ies", len(beat)),
            S["BODY"]))

    # ---------------------------------------------------------- method
    F.append(Paragraph("How these were chosen (read this first)", S["H2"]))
    F.append(Paragraph(
        "Searching thousands of strategies and shipping the best one is not "
        "research, it is data mining. With enough tries the best result on any "
        "fixed window is mostly luck. Everything below was built to make that "
        "harder, not easier:", S["BODY"]))
    for line in [
        "<b>Chronological split.</b> Each symbol's history is cut in two. "
        "Parameters were chosen using the FIRST 60% only. The last 40% chose "
        "nothing — it is only used to report what the choice turned out to be "
        "worth. There is no overlap and no shuffling.",
        "<b>One winner per family.</b> The best variation per family is picked "
        "on training data, then that single variation is judged out of sample. "
        "The alternative — picking the best out-of-sample result from thousands "
        "— would guarantee a beautiful report and a worthless one.",
        "<b>Median symbol, not best.</b> A strategy is scored on its middle "
        "symbol across the whole universe. One spectacular symbol and five bad "
        "ones is a curve fit, and an average lets it hide.",
        "<b>Controls in the same race.</b> Random-entry and always-long "
        "families were run with identical stops, targets and costs. A strategy "
        "that cannot beat a coin flip has not been shown to do anything.",
        "<b>Costs always on.</b> Slippage is charged on every fill, estimated "
        "from each symbol's own median bar range rather than assumed. Nothing "
        "here is a frictionless backtest.",
        "<b>No look-ahead, structurally.</b> The runner physically refuses to "
        "let a strategy read a bar it could not have seen. Signals are taken "
        "on a bar's close and filled at the NEXT bar's open; a bar touching "
        "both stop and target is resolved as the stop.",
    ]:
        F.append(Paragraph("&bull;&nbsp; " + line, S["BODY"]))

    # ---------------------------------------------------------- universe
    F.append(Paragraph("What was tested", S["H2"]))
    body = []
    for p in passes:
        meta = p.get("meta") or {}
        syms = ", ".join(meta)
        anysym = next(iter(meta.values()), {})
        body.append([p.get("timeframe", "?"), str(p.get("days", "")),
                     str(len(meta)),
                     format(p.get("n_backtests", 0), ","),
                     "%s / %s" % (format(anysym.get("train_bars", 0), ","),
                                  format(anysym.get("test_bars", 0), ",")),
                     Paragraph(syms, S["CELL"])])
    F.append(tbl(["Bars", "Days", "Symbols", "Backtests",
                  "Train/test bars", "Universe"], body,
                 [0.6, 0.5, 0.7, 0.85, 1.15, 3.5]))
    F.append(Paragraph(
        "Eight symbols spanning very different behaviour: two index ETFs, "
        "three large-cap single names, a leveraged ETF, and the two tickers "
        "the live fleet already trades. A rule that only works on one of these "
        "has found that symbol, not an edge.", S["NOTE"]))

    # ---------------------------------------------------------- the winners
    F.append(PageBreak())
    F.append(Paragraph("The top %d" % min(top, len(winners)), S["H2"]))
    if not winners:
        F.append(Paragraph("Nothing qualified. See the survival table below.",
                           S["BODY"]))
    for i, w in enumerate(winners, 1):
        te, tr = w["test"], w["train"]
        head = ("%d. %s &nbsp;<font size=8 color='%s'>%s bars</font>"
                % (i, w["family"], DIM, w["timeframe"]))
        blk = [Paragraph(head, S["H3"])]
        note = (passes[0].get("families") or {}).get(w["family"], "")
        for p in passes:
            note = (p.get("families") or {}).get(w["family"]) or note
        if note:
            blk.append(Paragraph(note, S["NOTE"]))

        if w.get("did_not_qualify"):
            blk.append(Paragraph(
                "<font color='%s'><b>DID NOT QUALIFY.</b> %s. Shown because it "
                "came closest, not because it is recommended.</font>"
                % (RED, (w.get("rejected") or "failed a gate").rstrip(".")),
                S["BODY"]))
        verdict = ("<font color='%s'><b>Beats the random-entry control</b></font>"
                   % GRN) if w["beats_control"] else (
                   "<font color='%s'><b>Does NOT beat the random-entry "
                   "control</b> — treat as unproven</font>" % RED)
        blk.append(Paragraph(verdict, S["BODY"]))

        headline, lines = verdict_of(w, abl, val)
        blk.append(Paragraph("<b>%s passed.</b>" % headline, S["BODY"]))
        for ln in lines:
            colour = RED if ln.startswith("NOT: ") else GRN
            mark = "&#10007;" if ln.startswith("NOT: ") else "&#10003;"
            blk.append(Paragraph(
                "<font color='%s'>%s</font>&nbsp; %s"
                % (colour, mark, ln.replace("NOT: ", "")), S["NOTE"]))
        blk.append(Spacer(1, 4))

        a = abl.get(w["family"])
        if a:
            blk.append(tbl(["Where the result comes from", "Score"], [
                ["The strategy as selected", num(a["full"]["median_score"])],
                ["Random entry, SAME execution parameters",
                 num(a["exec_only"]["median_score"])],
                ["The same signal with averaging-down switched off",
                 num(a["no_adds"]["median_score"])],
            ], [4.4, 1.4]))

        v = val.get(w["family"])
        if v and v.get("earlier") is not None:
            blk.append(Paragraph(
                "On an EARLIER window (%s to %s) that the search never saw, "
                "with the same parameters and no re-tuning, it scored "
                "<b>%s</b> on %d of %d symbols."
                % (str(v.get("_from"))[:10], str(v.get("_to"))[:10],
                   num(v["earlier"]),
                   int(round((v.get("consistency") or 0) * (v.get("symbols") or 0))),
                   v.get("symbols") or 0), S["NOTE"]))

        rows = [
            ["Out-of-sample score (P/L per $ drawdown, median symbol)",
             num(te["median_score"])],
            ["In-sample score, for comparison", num(tr["median_score"])],
            ["Decay out of sample (1.0 = held up perfectly)",
             "n/a" if w.get("decay") is None else "%.2f" % w["decay"]],
            ["Symbols profitable out of sample",
             "%d of %d" % (te["symbols_profitable"], te["symbols_scored"])],
            ["Worst symbol out of sample", num(te["worst_score"])],
            ["Best symbol out of sample", num(te["best_score"])],
            ["Trades out of sample", format(te["total_trades"], ",")],
            ["Summed P/L out of sample, 100 shares a trade",
             signed(te["sum_pl"])],
            ["Control bar it had to clear",
             num(w.get("control_bar")) if w.get("control_bar") is not None
             else "no control scored"],
        ]
        blk.append(tbl(["", "Value"], rows, [4.9, 1.4]))

        ps = ", ".join("%s=%s" % (k, v) for k, v in sorted(w["params"].items())
                       if k not in ("shares",))
        blk.append(Paragraph("<b>Settings:</b> <font size=7.5>%s</font>" % ps,
                             S["NOTE"]))

        scan = w.get("cost_scan")
        if scan:
            srows = []
            for m in ("0.5", "1.0", "2.0", "4.0"):
                d = scan.get(m)
                if not d:
                    continue
                srows.append([
                    ("%sx assumed cost" % m) + (" (as reported)" if m == "1.0" else ""),
                    num(d["median_score"]),
                    "%d%%" % int((d["consistency"] or 0) * 100),
                    signed(d["sum_pl"])])
            blk.append(Paragraph("Cost sensitivity, out of sample:", S["NOTE"]))
            blk.append(tbl(["Slippage", "Score", "Symbols +", "Summed P/L"],
                           srows, [2.2, 1.1, 0.9, 1.3]))
            if not w.get("survives_double_cost", True):
                blk.append(Paragraph(
                    "<font color='%s'><b>Does not survive a doubling of the "
                    "cost assumption.</b> Most of this result is the slippage "
                    "estimate, not the signal.</font>" % RED, S["NOTE"]))

        per = w.get("per_symbol_test") or {}
        prows = []
        for sym, v in per.items():
            prows.append([sym,
                          "n/a" if v.get("score") is None else "%+.3f" % v["score"],
                          signed(v.get("total_pl")),
                          signed(v.get("dd")),
                          str(v.get("trades", 0)),
                          "n/a" if v.get("pf") is None else "%.2f" % v["pf"],
                          "%.0f%%" % (v.get("win") or 0),
                          "n/a" if v.get("sharpe") is None else "%.1f" % v["sharpe"]])
        blk.append(Paragraph("Per symbol, out of sample:", S["NOTE"]))
        blk.append(tbl(["Symbol", "Score", "Total P/L", "Max DD", "Trades",
                        "PF", "Win", "Sharpe"], prows,
                       [0.75, 0.7, 1.0, 1.0, 0.7, 0.6, 0.6, 0.7]))
        blk.append(Spacer(1, 8))
        F.append(KeepTogether(blk))

    # ---------------------------------------------------------- controls
    F.append(PageBreak())
    F.append(Paragraph("What a coin flip scored", S["H2"]))
    F.append(Paragraph(
        "Same execution model, same stops and targets, same costs, same bars. "
        "The only difference is that the entry carries no information. Any "
        "family whose out-of-sample score is not clearly above these has not "
        "been shown to work.", S["BODY"]))
    crows = []
    for p in passes:
        for c in controls_of(p):
            te = c.get("test") or {}
            crows.append([p.get("timeframe", "?"), c["family"],
                          num((c.get("train") or {}).get("median_score")),
                          num(te.get("median_score")),
                          "%d/%d" % (te.get("symbols_profitable", 0),
                                     te.get("symbols_scored", 0)),
                          format(te.get("total_trades", 0), ",")])
    if crows:
        F.append(tbl(["Bars", "Control", "In sample", "Out of sample",
                      "Symbols +", "Trades"], crows,
                     [0.6, 2.4, 1.0, 1.1, 0.8, 0.9]))
    else:
        F.append(Paragraph(
            "<font color='%s'><b>No control ran in these passes.</b> Every "
            "score above is therefore uncompared: there is no measurement of "
            "what an uninformed entry would have scored on the same bars, so "
            "none of it should be acted on.</font>" % RED, S["BODY"]))
    missing = [p.get("timeframe") for p in passes if not p.get("_has_controls")]
    if missing and crows:
        F.append(Paragraph(
            "No control ran on the %s pass, so its candidates have no null to "
            "clear and are shown uncompared." % ", ".join(str(m) for m in missing),
            S["NOTE"]))

    # ---------------------------------------------------------- survival
    F.append(Paragraph("Every family, and what happened to it", S["H2"]))
    F.append(Paragraph(
        "The rejections are the most useful part of this document. They are "
        "ideas that will otherwise be suggested again in a month.", S["BODY"]))
    for p in passes:
        F.append(Paragraph("%s bars, %d days" % (p.get("timeframe"),
                                                 p.get("days", 0)), S["H3"]))
        rows = []
        for c in p.get("ranked", []):
            te = c["test"]
            rows.append([c["family"], "survived",
                         num(te["median_score"]),
                         "%d/%d" % (te["symbols_profitable"], te["symbols_scored"]),
                         Paragraph("", S["CELL"])])
        for c in p.get("rejected", []):
            why = c.get("rejected") or c.get("why") or ""
            te = c.get("test") or {}
            rows.append([c["family"], "rejected",
                         num(te.get("median_score")),
                         "%d/%d" % (te.get("symbols_profitable", 0),
                                    te.get("symbols_scored", 0)),
                         Paragraph(why, S["CELL"])])
        F.append(tbl(["Family", "", "OOS score", "Symbols +", "Why"], rows,
                     [2.1, 0.65, 0.75, 0.7, 3.0]))

    # ---------------------------------------------------------- caveats
    F.append(PageBreak())
    F.append(Paragraph("What this does not tell you", S["H2"]))
    for line in [
        "<b>One out-of-sample window is one sample.</b> These strategies were "
        "tested on a few months of a particular market. That is enough to "
        "reject an idea and not enough to trust one. The right next step for "
        "anything here is paper trading it, disarmed, and comparing what it "
        "actually does to what this said it would.",
        "<b>The costs are an estimate.</b> Slippage was modelled from each "
        "symbol's median bar range. Real fills depend on the spread at the "
        "moment of the order, and a strategy taking hundreds of trades is far "
        "more sensitive to that than one taking dozens. Where two strategies "
        "score similarly, prefer the one that trades less.",
        "<b>No borrow costs or short availability.</b> Short entries are "
        "modelled as freely available. On a thin or hard-to-borrow name they "
        "are not, and a short-biased result on such a symbol may not be "
        "reachable at all.",
        "<b>No overnight gap risk beyond what the bars contain.</b> Positions "
        "held across a session boundary are marked through the gap, but "
        "nothing here models a halt, a news event, or a fill that cannot "
        "happen.",
        "<b>Survivorship in the universe.</b> These eight symbols were chosen "
        "because they are liquid and interesting today. That is a mild "
        "forward-looking bias and it flatters everything equally.",
    ]:
        F.append(Paragraph("&bull;&nbsp; " + line, S["BODY"]))

    F.append(Spacer(1, 10))
    F.append(Paragraph(
        "Every number in this document came from replaying real Alpaca bars "
        "through the same engine the live fleet uses, with look-ahead blocked "
        "at the source. Nothing here has been traded, and nothing has been "
        "armed.", S["NOTE"]))

    doc.build(F)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--files", nargs="*", default=None)
    ap.add_argument("--no-cost-scan", action="store_true")
    ap.add_argument("--no-install", action="store_true")
    a = ap.parse_args(argv)

    passes = newest_per_timeframe(load_passes(a.files))
    if not passes:
        print("No research JSON found in %s" % RESEARCH_DIR)
        return 1
    abl, val = load_side_studies()
    winners = pooled_ranking(passes, a.top, abl)
    if winners and not a.no_cost_scan:
        print("Cost sensitivity check:")
        try:
            cost_sensitivity(winners, passes)
        except Exception as e:
            print("  cost scan skipped: %r" % e)

    installed = []
    if winners and not a.no_install:
        try:
            installed = install_winners(winners)
            print("installed as coded strategies: %s" % ", ".join(installed))
        except Exception as e:
            print("  install skipped: %r" % e)

    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H%M")
    out = REPORT_DIR / ("strategy_research_%s.pdf" % stamp)
    build_pdf(passes, winners, out, a.top, abl, val)

    print("")
    print("passes: %s" % ", ".join("%s(%dd)" % (p["timeframe"], p["days"])
                                   for p in passes))
    print("backtests: %s" % format(sum(p["n_backtests"] for p in passes), ","))
    print("")
    if not winners:
        print("NOTHING SURVIVED out of sample. That is the finding.")
    for i, w in enumerate(winners, 1):
        hl, _ = verdict_of(w, abl, val)
        print("   [%s]" % hl)
        print("%d. %-34s %-6s OOS %+.3f  IS %+.3f  %d/%d symbols  %s  %s"
              % (i, w["family"][:34], w["timeframe"],
                 w["test"]["median_score"], w["train"]["median_score"],
                 w["test"]["symbols_profitable"], w["test"]["symbols_scored"],
                 format(w["test"]["total_trades"], ","),
                 "beats control" if w["beats_control"] else "BELOW CONTROL"))
    print("")
    print("written to %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
