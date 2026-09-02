#!/usr/bin/env python3
"""
btstats.py -- turn a list of trades into a performance report and the curves
that go with it.

Every backtest in this project -- the ladder replay, a declarative strategy, a
hand-written coded strategy -- ends up here, so all three are judged by exactly
the same arithmetic and can be compared honestly against each other.

A TRADE is one round trip:

    {"entry_i": 12, "entry_t": "...", "entry": 10.00, "shares": 100,
     "exit_i": 40, "exit_t": "...", "exit": 10.20, "side": "long",
     "why": "target", "fees": 0.0}

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No "win rate" is reported without the profit factor and the open drawdown next
to it. A ladder with no stop loss closes ~100% winners by construction and is
still capable of losing everything it holds; a win rate on its own is the most
misleading number a backtest can print, so it never travels alone.

Sharpe and Sortino are computed on DAILY equity, not per bar and not per
trade. A 1-minute ladder is flat for most minutes, so its per-bar return series
is mostly exact zeros -- annualising that by sqrt(98,280) is how a backtest ends
up claiming a Sharpe of 30. Both come back as null rather than as a number
whenever the window is too short or too one-sided to support one.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Optional

# bar size -> bars per trading year, for annualising
BARS_PER_YEAR = {
    "1Min": 252 * 390, "2Min": 252 * 195, "3Min": 252 * 130,
    "5Min": 252 * 78, "10Min": 252 * 39, "15Min": 252 * 26,
    "30Min": 252 * 13, "1Hour": 252 * 7, "2Hour": 252 * 4,
    "4Hour": 252 * 2, "1Day": 252,
}


def _ts(v: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def pnl_of(t: dict) -> float:
    """Signed P/L of one round trip, after its own fees."""
    d = -1.0 if t.get("side") == "short" else 1.0
    return ((float(t["exit"]) - float(t["entry"])) * float(t["shares"]) * d
            - float(t.get("fees") or 0.0))


def equity_curve(trades: list[dict], bars: list[dict],
                 starting_equity: float = 0.0,
                 notional: float = 0.0) -> dict:
    """Equity, drawdown and exposure, sampled once per bar.

    Realized P/L is booked on the bar a trade EXITS, and open positions are
    marked to market on every bar in between. Booking everything at the exit
    would hide exactly the drawdown this curve exists to show -- a strategy
    that goes $40k underwater before recovering looks flat if you only plot
    closed trades.
    """
    n = len(bars)
    eq = [starting_equity] * n
    dd = [0.0] * n
    exposed = [0] * n
    if not n:
        return {"equity": eq, "drawdown": dd, "t": [], "exposure_pct": 0.0,
                "max_drawdown": 0.0, "max_drawdown_pct": 0.0,
                "final_equity": starting_equity}

    closed_by_bar = [0.0] * n
    for t in trades:
        i = int(t.get("exit_i", n - 1))
        if 0 <= i < n:
            closed_by_bar[i] += pnl_of(t)

    # open exposure per bar, so the curve includes unrealised swing
    opens: list[list] = [[] for _ in range(n)]
    for t in trades:
        a = max(0, int(t.get("entry_i", 0)))
        b = min(n - 1, int(t.get("exit_i", n - 1)))
        for i in range(a, b):
            opens[i].append(t)

    running = starting_equity
    peak = starting_equity
    max_dd = 0.0
    max_dd_pct = 0.0
    for i in range(n):
        running += closed_by_bar[i]
        c = float(bars[i]["c"])
        unreal = 0.0
        for t in opens[i]:
            d = -1.0 if t.get("side") == "short" else 1.0
            unreal += (c - float(t["entry"])) * float(t["shares"]) * d
        exposed[i] = len(opens[i])
        e = running + unreal
        eq[i] = round(e, 2)
        peak = max(peak, e)
        drop = e - peak
        dd[i] = round(drop, 2)
        if drop < max_dd:
            max_dd = drop
            # as a percentage of the CAPITAL AT RISK, not of the curve's own
            # peak. With no starting equity the curve begins at zero, and a
            # percentage of a peak near zero is a number in the thousands.
            base = abs(starting_equity) or abs(notional) or abs(peak) or 1.0
            max_dd_pct = drop / base * 100

    return {
        "equity": eq,
        "drawdown": dd,
        "t": [b["t"] for b in bars],
        "close": [float(b["c"]) for b in bars],
        "exposure_pct": round(100 * sum(1 for x in exposed if x) / n, 1),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "final_equity": round(eq[-1], 2),
    }


def _streaks(vals: list[float]) -> tuple[int, int]:
    """Longest run of wins and of losses."""
    best_w = best_l = cur_w = cur_l = 0
    for v in vals:
        if v > 0:
            cur_w += 1
            cur_l = 0
        else:
            cur_l += 1
            cur_w = 0
        best_w = max(best_w, cur_w)
        best_l = max(best_l, cur_l)
    return best_w, best_l


def _sharpe(eq: list[float], stamps: list, starting: float) -> tuple[float, float]:
    """Sharpe and Sortino from DAILY equity changes, annualised by sqrt(252).

    Deliberately not per-bar. A ladder is flat for most minutes of the day, so
    its per-minute return series is thousands of exact zeros with a few small
    positives -- almost no variance, a positive mean, and a Sharpe of 30 once
    it is multiplied by sqrt(98,280). That number is an artifact of the
    sampling rate, not a property of the strategy. Resampling to calendar days
    is what every serious tool does and is the only version worth printing.

    Returns are measured against a fixed notional (the starting equity, or the
    peak capital the run actually used) because a curve that begins at zero
    has no denominator of its own.
    """
    base = abs(starting) or 1.0
    if len(eq) != len(stamps) or len(eq) < 2:
        return 0.0, 0.0

    # last equity value of each calendar day
    daily: list[float] = []
    cur_day = None
    for e, t in zip(eq, stamps):
        d = str(t)[:10]
        if d != cur_day:
            cur_day = d
            daily.append(e)
        else:
            daily[-1] = e
    if len(daily) < 4:
        # fewer than four days is not enough to say anything about volatility,
        # and a number invented from two points is worse than no number
        return 0.0, 0.0

    rets = [(daily[i] - daily[i - 1]) / base for i in range(1, len(daily))]
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / len(rets))
    down = [r for r in rets if r < 0]
    k = math.sqrt(252)
    sharpe = round(m / sd * k, 2) if sd else None
    # Sortino divides by DOWNSIDE deviation. A ladder with no stop can go a
    # whole window without a single down day, and dividing by an almost-zero
    # denominator produces a Sortino in the hundreds -- a number that means
    # "there were no down days", not "this is a superb strategy". Say the
    # former by refusing to print the latter.
    if len(down) < 3:
        sortino = None
    else:
        dsd = math.sqrt(sum(r * r for r in down) / len(rets))
        sortino = round(m / dsd * k, 2) if dsd else None
    return sharpe, sortino


def report(trades: list[dict], bars: list[dict], *,
           bar_size: str = "1Min",
           starting_equity: float = 0.0,
           open_positions: Optional[list[dict]] = None) -> dict:
    """The whole performance picture: summary, curves and the trade list."""
    trades = list(trades or [])
    open_positions = list(open_positions or [])
    n = len(bars)
    last = float(bars[-1]["c"]) if bars else 0.0

    pnls = [pnl_of(t) for t in trades]
    for t, p in zip(trades, pnls):
        t["pnl"] = round(p, 2)
    run_tot = 0.0
    for t in trades:
        run_tot += t["pnl"]
        t["cum_pnl"] = round(run_tot, 2)

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)          # positive number
    net = gross_win - gross_loss

    open_pl = 0.0
    for p in open_positions:
        d = -1.0 if p.get("side") == "short" else 1.0
        open_pl += (last - float(p["entry"])) * float(p["shares"]) * d

    # notional actually put at risk -- the honest denominator for a return
    peak_cap = 0.0
    if trades or open_positions:
        by_bar = [0.0] * max(1, n)
        for t in trades:
            a = max(0, int(t.get("entry_i", 0)))
            b = min(n - 1, int(t.get("exit_i", n - 1)))
            for i in range(a, b + 1):
                by_bar[i] += float(t["entry"]) * float(t["shares"])
        for p in open_positions:
            a = max(0, int(p.get("entry_i", 0)))
            for i in range(a, n):
                by_bar[i] += float(p["entry"]) * float(p["shares"])
        peak_cap = max(by_bar) if by_bar else 0.0

    # Two different jobs, kept apart on purpose:
    #   the curve STARTS at whatever equity the run started with -- zero by
    #     default, which makes it a cumulative P/L curve, the thing a results
    #     chart is actually asking about;
    #   the return and risk ratios DIVIDE BY the capital the run put at risk,
    #     because a curve that begins at zero has no denominator of its own.
    # Conflating them started the curve at peak capital and put an axis on the
    # chart that had nothing to do with the P/L it was drawing.
    denom = starting_equity or peak_cap
    curve = equity_curve(trades, bars, starting_equity=starting_equity,
                         notional=peak_cap)
    sharpe, sortino = _sharpe(curve["equity"], curve["t"], denom)
    win_streak, loss_streak = _streaks(pnls)

    holds = [int(t.get("exit_i", 0)) - int(t.get("entry_i", 0)) for t in trades]
    span_days = 0.0
    if n >= 2:
        a, b = _ts(bars[0]["t"]), _ts(bars[-1]["t"])
        if a and b:
            span_days = max(0.01, (b - a).total_seconds() / 86400)

    buy_hold = 0.0
    if n >= 2 and float(bars[0]["c"]):
        buy_hold = round((last / float(bars[0]["c"]) - 1) * 100, 2)

    exits: dict[str, int] = {}
    for t in trades:
        k = str(t.get("why") or "?")
        exits[k] = exits.get(k, 0) + 1

    summary = {
        # --- headline ---
        "net_profit": round(net, 2),
        "open_pl": round(open_pl, 2),
        "total_pl": round(net + open_pl, 2),
        "gross_profit": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        # profit factor is the number that survives a strategy with no stop:
        # it cannot be flattered by never closing a loser
        # None, never float("inf"): Infinity is not valid JSON, and the run
        # that produces it -- no losing trades at all -- is exactly the one a
        # results page must still be able to render.
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown": curve["max_drawdown"],
        "max_drawdown_pct": curve["max_drawdown_pct"],

        # --- trade counts ---
        "total_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "open_at_end": len(open_positions),

        # --- per trade ---
        "avg_trade": round(net / len(trades), 2) if trades else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "win_loss_ratio": round((gross_win / len(wins)) / (gross_loss / len(losses)), 2)
                          if wins and losses else 0.0,
        "largest_win": round(max(wins), 2) if wins else 0.0,
        "largest_loss": round(min(losses), 2) if losses else 0.0,
        "expectancy": round(net / len(trades), 2) if trades else 0.0,
        "max_consecutive_wins": win_streak,
        "max_consecutive_losses": loss_streak,
        "avg_bars_in_trade": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "max_bars_in_trade": max(holds) if holds else 0,

        # --- capital and risk ---
        "peak_capital": round(peak_cap, 2),
        "return_on_peak_capital_pct":
            round(100 * (net + open_pl) / peak_cap, 3) if peak_cap else 0.0,
        "starting_equity": round(starting_equity, 2),
        "exposure_pct": curve["exposure_pct"],
        "sharpe": sharpe,
        "sortino": sortino,

        # --- context ---
        "bars": n,
        "bar_size": bar_size,
        "span_days": round(span_days, 2),
        "trades_per_day": round(len(trades) / span_days, 2) if span_days else 0.0,
        "buy_hold_pct": buy_hold,
        "exits": exits,
    }

    # The one honesty line the summary always carries. A ladder with no stop
    # closes only winners by construction; saying so next to the win rate is
    # the difference between a report and a sales pitch.
    if summary["win_rate"] >= 95 and not summary["losing_trades"]:
        summary["caveat"] = (
            "Every closed trade is a winner. That is what a strategy with no "
            "stop loss always looks like -- losers are simply never closed. "
            "Read max_drawdown and open_pl, not the win rate.")
    elif summary["profit_factor"] is None and summary["total_trades"]:
        summary["caveat"] = ("No losing trades in this window, so there is no "
                             "profit factor to report. That is a property of "
                             "the window, not proof of an edge.")
    else:
        summary["caveat"] = ""

    return {
        "summary": summary,
        "curve": curve,
        "trades": trades,
        "open_positions": open_positions,
    }


def compact(rep: dict) -> dict:
    """The summary plus a thinned curve -- what a sweep row carries.

    A 20-day 1-minute sweep of 200 combinations would otherwise ship about
    1.5 million equity points to the browser, which is how a results page
    stops rendering.
    """
    c = rep["curve"]
    eq = c["equity"]
    step = max(1, len(eq) // 240)
    return {
        **rep["summary"],
        "spark": [eq[i] for i in range(0, len(eq), step)],
    }
