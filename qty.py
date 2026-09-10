"""qty.py -- share quantities. ONE definition for the whole repo (engine,
broker, journal, fleet, app, agentctl, report, backtest).

Alpaca: a qty carries up to 9 decimals; fractional orders are accepted with
time_in_force=day only; there are no fractional short sales and no
fractional trailing stops.

The rules, in one place:
  qty(v)    parses anything to a float rounded to 9 dp (sign preserved)
  qnum(v)   the STORAGE and CALL-SITE form: an int when the value is whole,
            else the 9-dp float -- json.dumps(qnum(100.0)) is '100', which is
            what keeps a whole-share ledger, journal and order byte-identical
  qstr(v)   the WIRE form, applied once in broker.Alpaca.submit; never
            str(float) (str(0.1 + 0.2) is '0.30000000000000004')
  qfloor    the ONLY directional rounding, used on sizing, never on fills
  qsame/qzero/qwhole  every comparison of two quantities goes through these

No imports beyond decimal: journal.py is free of engine imports at module
scope and broker.py cannot import engine (engine imports broker).
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

QTY_DP = 9            # Alpaca's precision
QTY_EPS = 1e-6        # every comparison of two quantities uses this
MIN_QTY = 0.001       # house floor for a fractional lot (asset min_order_size overrides upward)
MIN_NOTIONAL = 1.0    # a fractional lot under $1 is refused (rehearsal item R4)


def qty(v) -> float:
    """Parse anything (None, '', '0.010000000', 100, -0.5) to a float rounded
    to 9 dp. Sign preserved. Unparseable -> 0.0."""
    if v is None or v == "":
        return 0.0
    try:
        return round(float(v), QTY_DP)
    except (TypeError, ValueError):
        return 0.0


def qwhole(v) -> bool:
    q = qty(v)
    return abs(q - round(q)) < QTY_EPS


def qnum(v):
    """STORAGE and CALL-SITE form: int when whole, else the 9-dp float.
    json.dumps(qnum(100.0)) == '100'; qnum(0.01) == 0.01."""
    q = qty(v)
    return int(round(q)) if qwhole(q) else q


def qsame(a, b) -> bool:
    return abs(qty(a) - qty(b)) < QTY_EPS


def qzero(v) -> bool:
    return abs(qty(v)) < QTY_EPS


def qfloor(v, step: float = 1.0) -> float:
    """The ONLY directional rounding. step 1.0 is EXACTLY today's int()
    truncation on the raw value (int(28.999999999999996) is 28 today and
    must stay 28). Any other step rounds DOWN to a multiple of the step after
    stripping binary noise (12 dp, three beyond the wire precision, so
    0.7 - 0.4 = 0.29999999999999993 floors to 0.29 at a 0.01 step and not
    to 0.28, while 1500 / 759 = 1.9762845849... still floors to 1.976284584
    at 1e-9). Used on sizing only, never on fills."""
    if step >= 1.0:
        return float(int(v))
    d = Decimal(str(round(float(v), QTY_DP + 3))) / Decimal(str(step))
    return float(d.to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))


def qstr(v) -> str:
    """Wire form: '100', '0.01', '0.3' for 0.30000000000000004, never more
    than 9 decimals and never a trailing zero."""
    s = f"{qty(v):.{QTY_DP}f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"
