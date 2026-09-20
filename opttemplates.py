#!/usr/bin/env python3
"""
opttemplates.py -- the structural shapes, one entry per shape, hand-written
once.

The 231 documents in options/bank/ carry 99 distinct leg topologies, but they
collapse onto about thirty SHAPES: a vertical is a vertical whether the
document calls it a bull put spread, a short put vertical or a credit put
spread. A document is a shape plus a parameter choice plus a management rule,
and this module owns the shape.

WHAT A TEMPLATE OWNS, and why each one is here rather than in the IR:

  * leg construction from (chain, params) -- the IR supplies numbers, the code
    supplies structure, because "buy the wing 5 points below the short" is a
    loop over a chain and a loop in a data file is an expression language;
  * the CLOSING ORDER -- shorts first, always, no exception, no template
    override. Selling the long wing first on a four-leg structure leaves a
    naked short for as long as the second order takes to fill, and that is a
    level-4 position on a level-3 account, arrived at by accident;
  * the ASSIGNMENT TOPOLOGY -- which legs a guard has to watch, and whether
    the shape is defined-risk at all;
  * the MAX-LOSS source. Not a second formula: optstructures already computes
    it from the payoff, and a template that re-derived it would be a second
    number to disagree with the first.

WHAT IT DELIBERATELY DOES NOT OWN: pricing, greeks, probability of profit.
`optstructures.build()` does all of that and is tested to arithmetic
identities in test_optstructures.py. This module is a thin, honest layer over
optbacktest's `t_*` builders (which pick strikes from a solved historical
chain) and optstructures' constructors (which price a set of legs). There is
no third implementation here, and adding one would be the bug.

TOPOLOGY IS NECESSARY BUT NOT SUFFICIENT. A put credit spread and a put debit
spread have the identical (right, action, ratio) multiset -- the difference is
which strike is short. So every template also declares `net`, and the match
check below compares both. A compiler that matched on topology alone would
cheerfully compile a bear put spread as a bull put spread and invert the whole
position.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

import optbacktest as BT
import optir
import optstructures as S

# How the order actually reaches Alpaca. It is part of the shape, because it
# decides whether the strategy can exist on this account at all.
#   mleg    one multi-leg option order, 2-4 legs. The only armable path today.
#   single  one option order. Legal, but optir refuses fewer than 2 legs.
#   shares  needs an equity leg placed and reconciled separately. A different
#           order path, a different failure mode, and out of scope for v1.
ORDER_PATHS = ("mleg", "single", "shares")


class TemplateError(Exception):
    pass


@dataclass(frozen=True)
class Template:
    """One shape."""
    name: str
    net: str                      # "credit" | "debit" | "either"
    legs: tuple                   # (right, action, ratio) in BUILD order
    params: tuple                 # parameter names the builder reads
    order_path: str
    defined_risk: bool
    level: int                    # minimum Alpaca options level, DERIVED
                                  # unless a shape overrides it
    multi_expiry: bool = False
    builder: Optional[Callable] = None     # (chain, params) -> [BT.Leg]
    ctor: Optional[str] = None             # optstructures constructor name
    note: str = ""

    @property
    def topology(self) -> tuple:
        return optir.topology([{"right": r, "action": a, "ratio": q}
                               for (r, a, q) in self.legs])

    @property
    def short_legs(self) -> tuple:
        return tuple((r, a, q) for (r, a, q) in self.legs if a == "sell"
                     and r in ("call", "put"))

    def build(self, chain: Any, params: Optional[dict] = None):
        """Pick strikes off a chain. None when the chain cannot supply what
        the shape needs -- that session is skipped and counted, never
        approximated with the nearest thing lying around."""
        if self.builder is None:
            raise TemplateError("template %r has no builder" % self.name)
        return self.builder(chain, dict(params or {}))


# ============================================================ extra shapes
# Written in optbacktest's own idiom -- (rows, p) -> [Leg] or None -- so they
# are indistinguishable from the 24 that already live there, and so a future
# move of these four into that file is a cut and paste with no adaptation.
def _mk(row, action: str, ratio: int = 1) -> "BT.Leg":
    return BT.Leg(occ=row.symbol, strike=row.strike, right=row.right,
                  action=action, ratio=ratio)


def t_put_ratio_spread(rows, p):
    """Buy one nearer put, sell two further. Uncovered below the short strike,
    so level 4 -- listed here because the bank documents it and a shape we
    refuse must still be a shape we can name."""
    hi = BT.pick_by_delta(rows, "put", p.get("delta", 0.45))
    if not hi:
        return None
    lo = BT.pick_by_offset(rows, "put", hi.strike - p.get("width", 5.0))
    return [_mk(hi, "buy"), _mk(lo, "sell", 2)] if lo else None


def t_broken_wing_call_fly(rows, p):
    """The call-side mirror of optbacktest.t_broken_wing_put_fly: the far wing
    is wider, so it opens for a credit and carries no risk below."""
    w, wide = p.get("width", 5.0), p.get("wide", 10.0)
    body = BT.pick_by_delta(rows, "call", p.get("delta", 0.30))
    if not body:
        return None
    near = BT.pick_by_offset(rows, "call", body.strike - w)
    far = BT.pick_by_offset(rows, "call", body.strike + wide)
    if not (near and far):
        return None
    return [_mk(near, "buy"), _mk(body, "sell", 2), _mk(far, "buy")]


def t_put_condor(rows, p):
    d, w = p.get("delta", 0.30), p.get("width", 5.0)
    inner_hi = BT.pick_by_delta(rows, "put", d)
    if not inner_hi:
        return None
    inner_lo = BT.pick_by_offset(rows, "put", inner_hi.strike - w)
    outer_hi = BT.pick_by_offset(rows, "put", inner_hi.strike + w)
    outer_lo = BT.pick_by_offset(rows, "put", inner_hi.strike - 2 * w)
    if not (inner_lo and outer_hi and outer_lo):
        return None
    return [_mk(outer_lo, "buy"), _mk(inner_lo, "sell"),
            _mk(inner_hi, "sell"), _mk(outer_hi, "buy")]


# ---- two-expiry shapes.
# These take a MAPPING {"front": rows, "back": rows} rather than one sequence,
# and that is the point of the separate signature: the single most dangerous
# silent omission in this whole compiler is losing "later expiry" from a
# diagonal, which turns a covered short call into a naked one. Making the
# second expiry a required, differently-shaped argument means the mistake
# cannot be made quietly -- it raises.
def _two(chain) -> tuple:
    if not isinstance(chain, dict) or "front" not in chain or "back" not in chain:
        raise TemplateError(
            "this shape needs {'front': rows, 'back': rows}. A calendar or "
            "diagonal built from one expiry is not the same trade: the short "
            "leg would be uncovered.")
    return chain["front"], chain["back"]


def _calendar(chain, p, right: str):
    front, back = _two(chain)
    near = BT.pick_by_delta(front, right, p.get("delta", 0.50))
    if not near:
        return None
    far = BT.pick_by_offset(back, right, near.strike)
    if not far:
        return None
    return [_mk(near, "sell"), _mk(far, "buy")]


def t_call_calendar(chain, p):
    return _calendar(chain, p, "call")


def t_put_calendar(chain, p):
    return _calendar(chain, p, "put")


def _diagonal(chain, p, right: str):
    front, back = _two(chain)
    near = BT.pick_by_delta(front, right, p.get("short_delta", 0.30))
    if not near:
        return None
    far = BT.pick_by_delta(back, right, p.get("long_delta", 0.60))
    if not far:
        return None
    # The long has to be further out in TIME and, for the short to be covered
    # on the strike axis too, at or beyond the short on a call / at or below on
    # a put. Checked here rather than assumed, because a diagonal that fails
    # this is a naked short wearing a hedge's name.
    if right == "call" and far.strike > near.strike:
        return None
    if right == "put" and far.strike < near.strike:
        return None
    return [_mk(near, "sell"), _mk(far, "buy")]


def t_call_diagonal(chain, p):
    return _diagonal(chain, p, "call")


def t_put_diagonal(chain, p):
    return _diagonal(chain, p, "put")


def t_pmcc(chain, p):
    """Poor man's covered call: a deep-ITM long call in a far expiry standing
    in for 100 shares, against a short OTM call in the front. Same topology as
    a call diagonal; a different trade, because the long delta is ~0.80 and
    the thesis is stock replacement rather than vol."""
    p = dict(p)
    p.setdefault("long_delta", 0.80)
    p.setdefault("short_delta", 0.25)
    return _diagonal(chain, p, "call")


def _fly(rows, p, right: str, long_wings: bool):
    """Symmetric butterfly, either way up. The short (reverse) fly is a real
    shape in the bank, not a curiosity: it is how a document expresses "I want
    a move, and I will pay for it out of a capped credit"."""
    w = p.get("width", 5.0)
    body = BT.pick_by_delta(rows, right, 0.50)
    if not body:
        return None
    up = BT.pick_by_offset(rows, right, body.strike + w)
    dn = BT.pick_by_offset(rows, right, body.strike - w)
    if not (up and dn):
        return None
    if long_wings:
        return [_mk(dn, "buy"), _mk(body, "sell", 2), _mk(up, "buy")]
    return [_mk(dn, "sell"), _mk(body, "buy", 2), _mk(up, "sell")]


def _xmas(rows, p, right: str):
    """1-2-3: one at the body, two one width out, three sold two widths out.
    Written in the bank as a cheaper, more directional butterfly."""
    w = p.get("width", 5.0)
    a = BT.pick_by_delta(rows, right, p.get("delta", 0.50))
    if not a:
        return None
    step = w if right == "call" else -w
    b = BT.pick_by_offset(rows, right, a.strike + step)
    c = BT.pick_by_offset(rows, right, a.strike + 2 * step)
    if not (b and c):
        return None
    return [_mk(a, "buy"), _mk(b, "buy", 2), _mk(c, "sell", 3)]


def _backspread(rows, p, right: str, ratio: int):
    near = BT.pick_by_delta(rows, right, p.get("delta", 0.45))
    if not near:
        return None
    step = p.get("width", 5.0)
    far = BT.pick_by_offset(
        rows, right, near.strike + (step if right == "call" else -step))
    if not far:
        return None
    return [_mk(near, "sell"), _mk(far, "buy", ratio)]


def _ladder(rows, p, right: str):
    """Sell one near, buy two further out at different strikes. Unlike a
    backspread the longs are at DIFFERENT strikes, which is why it is its own
    shape and not a parameter of one."""
    w = p.get("width", 5.0)
    step = w if right == "call" else -w
    a = BT.pick_by_delta(rows, right, p.get("delta", 0.45))
    if not a:
        return None
    b = BT.pick_by_offset(rows, right, a.strike + step)
    c = BT.pick_by_offset(rows, right, a.strike + 2 * step)
    if not (b and c):
        return None
    return [_mk(a, "sell"), _mk(b, "buy"), _mk(c, "buy")]


def _strapstrip(rows, p, calls: int, puts: int):
    c = BT.pick_by_delta(rows, "call", 0.50)
    if not c:
        return None
    pu = BT.pick_by_offset(rows, "put", c.strike)
    if not pu:
        return None
    return [_mk(c, "buy", calls), _mk(pu, "buy", puts)]


def _guts(rows, p, action: str):
    """An ITM strangle: the call is below spot and the put above, so the
    strikes cross. Mostly intrinsic, so the spread cost dominates -- worth its
    own shape precisely so that cost is visible rather than assumed away."""
    d = p.get("delta", 0.70)
    c = BT.pick_by_delta(rows, "call", d)
    pu = BT.pick_by_delta(rows, "put", d)
    if not c or not pu or c.strike >= pu.strike:
        return None
    return [_mk(c, action), _mk(pu, action)]


def _broken_iron(rows, p, fly: bool):
    """An iron condor or fly with DIFFERENT wing widths on the two sides.

    Topologically identical to the balanced version -- the same four legs,
    the same rights, the same actions, the same ratios -- so gate 4 cannot
    tell them apart and would happily let a broken-wing document build a
    balanced structure. That is not a cosmetic difference: the whole point of
    the broken wing is that one tail is wider, which is where the max loss
    lives and which side the guard has to watch. It gets its own shape so the
    name-based match in `match()` can find it.
    """
    wp = p.get("put_width", p.get("width", 5.0))
    wc = p.get("call_width", p.get("wide", 10.0))
    if fly:
        body = BT.pick_by_delta(rows, "call", 0.50)
        if not body:
            return None
        sp = BT.pick_by_offset(rows, "put", body.strike)
        sc = body
    else:
        d = p.get("short_delta", p.get("delta", 0.16))
        sc = BT.pick_by_delta(rows, "call", d)
        sp = BT.pick_by_delta(rows, "put", d)
        if not sc or not sp or sp.strike >= sc.strike:
            return None
    if not sp or not sc:
        return None
    lp = BT.pick_by_offset(rows, "put", sp.strike - wp)
    lc = BT.pick_by_offset(rows, "call", sc.strike + wc)
    if not lp or not lc:
        return None
    return [_mk(sp, "sell"), _mk(lp, "buy"), _mk(sc, "sell"), _mk(lc, "buy")]


def _bt(name: str) -> Callable:
    """Adapt one of optbacktest's builders to the (chain, params) signature."""
    fn = BT.TEMPLATES[name]

    def call(chain, params, _fn=fn):
        return _fn(chain, params)
    call.__name__ = "bt_" + name
    return call


# ================================================================ registry
def derive_level(legs) -> int:
    """The Alpaca options level a shape needs, from its legs alone.

    The rule Alpaca actually applies: a short option is uncovered unless a
    long option of the SAME RIGHT stands behind it. So count per right -- if
    the shorts outnumber the longs anywhere, something is naked and it is
    level 4. No shorts at all is level 2. Everything else is level 3.

    Derived rather than typed in by hand because a hand-typed level is a
    number that drifts from the legs beside it, and the direction it drifts
    is always the same: someone adds a short leg and forgets the level. Four
    shapes override it, each for a stated reason.
    """
    buys: dict = {}
    sells: dict = {}
    shares = 0
    for (right, action, ratio) in legs:
        if right == "stock":
            shares += int(ratio) * (1 if action == "buy" else -1)
            continue
        if right not in ("call", "put"):
            continue
        (sells if action == "sell" else buys)[right] = \
            (sells if action == "sell" else buys).get(right, 0) + int(ratio)
    # 100 long shares cover one short call; 100 short shares cover one short
    # put. That is what makes a covered call level 1 and a naked call level 4
    # with the identical option leg, so it belongs in the same arithmetic
    # rather than in a table of exceptions beside it.
    if shares > 0:
        buys["call"] = buys.get("call", 0) + shares // 100
    elif shares < 0:
        buys["put"] = buys.get("put", 0) + (-shares) // 100
    if not sells:
        return 2
    for right, n in sells.items():
        if n > buys.get(right, 0):
            return 4
    return 3


def _t(name, net, legs, params, *, order_path="mleg", defined_risk=None,
       level=None, multi_expiry=False, builder=None, ctor=None, note=""):
    legs = tuple(legs)
    lvl = derive_level(legs) if level is None else level
    risk = (lvl <= 3) if defined_risk is None else defined_risk
    return Template(name=name, net=net, legs=legs, params=tuple(params),
                    order_path=order_path, defined_risk=risk,
                    level=lvl, multi_expiry=multi_expiry, builder=builder,
                    ctor=ctor, note=note)


_DELTA_W = ("short_delta", "width", "dte")

TEMPLATES: dict[str, Template] = {t.name: t for t in [
    # ---------------------------------------------------- verticals (4)
    _t("put_credit_spread", "credit",
       [("put", "sell", 1), ("put", "buy", 1)], _DELTA_W,
       builder=_bt("put_credit_spread"), ctor="put_credit_spread",
       note="bull put. Short the higher strike."),
    _t("call_credit_spread", "credit",
       [("call", "sell", 1), ("call", "buy", 1)], _DELTA_W,
       builder=_bt("call_credit_spread"), ctor="call_credit_spread",
       note="bear call. Short the lower strike."),
    _t("put_debit_spread", "debit",
       [("put", "buy", 1), ("put", "sell", 1)], _DELTA_W,
       builder=_bt("put_debit_spread"), ctor="put_debit_spread",
       note="bear put. Long the higher strike -- the exact inverse of the "
            "bull put, and the same topology, which is why `net` is checked."),
    _t("call_debit_spread", "debit",
       [("call", "buy", 1), ("call", "sell", 1)], _DELTA_W,
       builder=_bt("call_debit_spread"), ctor="call_debit_spread",
       note="bull call."),

    # ------------------------------------------------- four-leg irons (4)
    _t("iron_condor", "credit",
       [("put", "sell", 1), ("put", "buy", 1),
        ("call", "sell", 1), ("call", "buy", 1)],
       ("short_delta", "width", "dte"),
       builder=_bt("iron_condor"), ctor="iron_condor"),
    _t("reverse_iron_condor", "debit",
       [("put", "buy", 1), ("put", "sell", 1),
        ("call", "buy", 1), ("call", "sell", 1)],
       ("short_delta", "width", "dte"),
       builder=_bt("reverse_iron_condor")),
    _t("iron_butterfly", "credit",
       [("put", "sell", 1), ("put", "buy", 1),
        ("call", "sell", 1), ("call", "buy", 1)],
       ("width", "dte"),
       builder=_bt("iron_butterfly"), ctor="iron_butterfly",
       note="same topology as the condor; the shorts are both ATM."),
    _t("reverse_iron_butterfly", "debit",
       [("put", "buy", 1), ("put", "sell", 1),
        ("call", "buy", 1), ("call", "sell", 1)],
       ("width", "dte"),
       builder=_bt("reverse_iron_butterfly")),

    _t("broken_wing_iron_condor", "credit",
       [("put", "sell", 1), ("put", "buy", 1),
        ("call", "sell", 1), ("call", "buy", 1)],
       ("short_delta", "put_width", "call_width", "dte"),
       builder=lambda c, p: _broken_iron(c, p, False), ctor="iron_condor",
       note="the unbalanced condor. Same topology as the balanced one, so "
            "only the document's own name tells them apart -- and the "
            "difference is which tail carries the max loss."),
    _t("broken_wing_iron_butterfly", "credit",
       [("put", "sell", 1), ("put", "buy", 1),
        ("call", "sell", 1), ("call", "buy", 1)],
       ("put_width", "call_width", "dte"),
       builder=lambda c, p: _broken_iron(c, p, True), ctor="iron_butterfly"),

    # -------------------------------------------------- butterflies (5)
    _t("long_call_butterfly", "debit",
       [("call", "buy", 1), ("call", "sell", 2), ("call", "buy", 1)],
       ("width", "dte"), builder=_bt("long_call_butterfly")),
    _t("long_put_butterfly", "debit",
       [("put", "buy", 1), ("put", "sell", 2), ("put", "buy", 1)],
       ("width", "dte"), builder=_bt("long_put_butterfly")),
    _t("broken_wing_put_fly", "either",
       [("put", "buy", 1), ("put", "sell", 2), ("put", "buy", 1)],
       ("delta", "width", "wide", "dte"),
       builder=_bt("broken_wing_put_fly"), ctor="broken_wing_butterfly"),
    _t("broken_wing_call_fly", "either",
       [("call", "buy", 1), ("call", "sell", 2), ("call", "buy", 1)],
       ("delta", "width", "wide", "dte"),
       builder=t_broken_wing_call_fly, ctor="broken_wing_butterfly"),
    _t("call_condor", "either",
       [("call", "buy", 1), ("call", "sell", 1),
        ("call", "sell", 1), ("call", "buy", 1)],
       ("delta", "width", "dte"), builder=_bt("call_condor")),
    _t("put_condor", "either",
       [("put", "buy", 1), ("put", "sell", 1),
        ("put", "sell", 1), ("put", "buy", 1)],
       ("delta", "width", "dte"), builder=t_put_condor),

    # ------------------------------------------- ratios and backspreads (4)
    _t("call_ratio_spread", "credit",
       [("call", "buy", 1), ("call", "sell", 2)],
       ("delta", "width", "dte"), builder=_bt("call_ratio_spread"),
       note="the extra short call is uncovered above the upper strike."),
    _t("put_ratio_spread", "credit",
       [("put", "buy", 1), ("put", "sell", 2)],
       ("delta", "width", "dte"), builder=t_put_ratio_spread),
    _t("call_backspread", "either",
       [("call", "sell", 1), ("call", "buy", 2)],
       ("delta", "width", "dte"), builder=_bt("call_backspread")),
    _t("put_backspread", "either",
       [("put", "sell", 1), ("put", "buy", 2)],
       ("delta", "width", "dte"), builder=_bt("put_backspread")),

    # ------------------------------------------ strangles and straddles (4)
    _t("long_strangle", "debit",
       [("call", "buy", 1), ("put", "buy", 1)], ("delta", "dte"),
       builder=_bt("long_strangle"), ctor="strangle"),
    _t("short_strangle", "credit",
       [("call", "sell", 1), ("put", "sell", 1)], ("delta", "dte"),
       builder=_bt("short_strangle"), ctor="strangle"),
    _t("long_straddle", "debit",
       [("call", "buy", 1), ("put", "buy", 1)], ("dte",),
       builder=_bt("long_straddle"), ctor="straddle",
       note="same topology as the strangle; both legs on one strike."),
    _t("short_straddle", "credit",
       [("call", "sell", 1), ("put", "sell", 1)], ("dte",),
       builder=_bt("short_straddle"), ctor="straddle"),

    # -------------------------------------------------- jade lizard (1)
    _t("jade_lizard", "credit",
       [("put", "sell", 1), ("call", "sell", 1), ("call", "buy", 1)],
       ("delta", "width", "dte"), builder=_bt("jade_lizard"),
       note="no risk above, all of it below: the naked short put is what "
            "makes this level 4."),

    # ------------------------------------------- two-expiry families (5)
    _t("call_calendar", "debit",
       [("call", "sell", 1), ("call", "buy", 1)],
       ("delta", "front_dte", "back_dte"),
       multi_expiry=True, builder=t_call_calendar, ctor="calendar"),
    _t("put_calendar", "debit",
       [("put", "sell", 1), ("put", "buy", 1)],
       ("delta", "front_dte", "back_dte"),
       multi_expiry=True, builder=t_put_calendar, ctor="calendar"),
    _t("call_diagonal", "either",
       [("call", "sell", 1), ("call", "buy", 1)],
       ("short_delta", "long_delta", "front_dte", "back_dte"),
       multi_expiry=True, builder=t_call_diagonal, ctor="diagonal"),
    _t("put_diagonal", "either",
       [("put", "sell", 1), ("put", "buy", 1)],
       ("short_delta", "long_delta", "front_dte", "back_dte"),
       multi_expiry=True, builder=t_put_diagonal, ctor="diagonal"),
    _t("pmcc", "debit",
       [("call", "sell", 1), ("call", "buy", 1)],
       ("short_delta", "long_delta", "front_dte", "back_dte"),
       multi_expiry=True, builder=t_pmcc, ctor="diagonal",
       note="stock replacement. Topologically a call diagonal; the long is "
            "deep ITM and the management is a covered call's."),

    # ------------------------------------------- single leg, not armable (4)
    _t("long_call", "debit", [("call", "buy", 1)], ("delta", "dte"),
       order_path="single", builder=_bt("long_call"),
       ctor="long_call"),
    _t("long_put", "debit", [("put", "buy", 1)], ("delta", "dte"),
       order_path="single", builder=_bt("long_put"),
       ctor="long_put"),
    _t("short_call", "credit", [("call", "sell", 1)], ("delta", "dte"),
       order_path="single", builder=_bt("short_call")),
    _t("cash_secured_put", "credit", [("put", "sell", 1)], ("delta", "dte"),
       order_path="single", level=3, builder=_bt("short_put"),
       ctor="cash_secured_put",
       note="defined risk only because the underlying stops at zero; the "
            "capital it ties up is the strike, not a spread width."),

    # --------------------------------- shapes the bank uses and the 24 did
    # not cover. Each one exists because a document in options/bank/ has this
    # exact topology and was landing in "no template", which reads on the
    # dashboard as "we have not looked at it" when the truth was "we had no
    # name for it".
    _t("short_call_butterfly", "credit",
       [("call", "sell", 1), ("call", "buy", 2), ("call", "sell", 1)],
       ("width", "dte"), builder=lambda c, p: _fly(c, p, "call", False)),
    _t("short_put_butterfly", "credit",
       [("put", "sell", 1), ("put", "buy", 2), ("put", "sell", 1)],
       ("width", "dte"), builder=lambda c, p: _fly(c, p, "put", False)),
    _t("call_christmas_tree", "debit",
       [("call", "buy", 1), ("call", "buy", 2), ("call", "sell", 3)],
       ("width", "dte"), builder=lambda c, p: _xmas(c, p, "call")),
    _t("put_christmas_tree", "debit",
       [("put", "buy", 1), ("put", "buy", 2), ("put", "sell", 3)],
       ("width", "dte"), builder=lambda c, p: _xmas(c, p, "put")),
    _t("call_backspread_1x3", "either",
       [("call", "sell", 1), ("call", "buy", 3)],
       ("delta", "width", "dte"),
       builder=lambda c, p: _backspread(c, p, "call", 3)),
    _t("put_backspread_1x3", "either",
       [("put", "sell", 1), ("put", "buy", 3)],
       ("delta", "width", "dte"),
       builder=lambda c, p: _backspread(c, p, "put", 3)),
    _t("short_call_ladder", "either",
       [("call", "sell", 1), ("call", "buy", 1), ("call", "buy", 1)],
       ("delta", "width", "dte"),
       builder=lambda c, p: _ladder(c, p, "call")),
    _t("short_put_ladder", "either",
       [("put", "sell", 1), ("put", "buy", 1), ("put", "buy", 1)],
       ("delta", "width", "dte"),
       builder=lambda c, p: _ladder(c, p, "put")),
    _t("strap", "debit",
       [("call", "buy", 2), ("put", "buy", 1)], ("dte",),
       builder=lambda c, p: _strapstrip(c, p, 2, 1),
       note="a straddle weighted to the upside: two calls, one put."),
    _t("strip", "debit",
       [("call", "buy", 1), ("put", "buy", 2)], ("dte",),
       builder=lambda c, p: _strapstrip(c, p, 1, 2)),
    _t("long_guts", "debit",
       [("call", "buy", 1), ("put", "buy", 1)], ("delta", "dte"),
       builder=lambda c, p: _guts(c, p, "buy"),
       note="an ITM strangle. Same topology as the strangle; the strikes "
            "are crossed, so almost all of the premium is intrinsic."),
    _t("short_guts", "credit",
       [("call", "sell", 1), ("put", "sell", 1)], ("delta", "dte"),
       builder=lambda c, p: _guts(c, p, "sell")),

    # ------------------------------------------ share-leg, not armable (3)
    _t("covered_call", "credit",
       [("stock", "buy", 100), ("call", "sell", 1)], ("delta", "dte"),
       order_path="shares", level=1, ctor="covered_call"),
    _t("protective_put", "debit",
       [("stock", "buy", 100), ("put", "buy", 1)], ("delta", "dte"),
       order_path="shares", level=1),
    _t("collar", "either",
       [("stock", "buy", 100), ("put", "buy", 1), ("call", "sell", 1)],
       ("delta", "dte"), order_path="shares", level=1),
]}


def names() -> list[str]:
    return sorted(TEMPLATES)


def get(name: str) -> Template:
    t = TEMPLATES.get(name)
    if t is None:
        raise TemplateError("unknown template %r; the set is %s"
                            % (name, ", ".join(names())))
    return t


def armable_templates() -> list[str]:
    """Shapes this account can actually send: one mleg order, 2-4 option legs,
    level <= 3."""
    return sorted(n for n, t in TEMPLATES.items()
                  if t.order_path == "mleg" and t.level <= optir.ACCOUNT_LEVEL
                  and optir.MIN_LEGS <= len(t.legs) <= optir.MAX_LEGS)


def by_topology(legs: list[dict]) -> list[str]:
    """Every shape whose (right, action, ratio) multiset matches these legs.
    Usually one; two when a credit and a debit version share a topology, which
    is why callers must then discriminate on `net`."""
    want = optir.topology(legs)
    return sorted(n for n, t in TEMPLATES.items() if t.topology == want)


def match(doc: dict) -> tuple[Optional[str], list[str]]:
    """Pick the shape for a bank document. (template_name, notes).

    Topology first, then `net`, then the document's own name as the tie-break
    -- in that order, because topology is machine-checkable and a name is not.
    """
    notes: list[str] = []
    cands = by_topology(doc.get("legs") or [])
    if not cands:
        return None, ["no template has this leg topology: %s"
                      % optir._fmt_topo(optir.topology(doc.get("legs") or []))]
    if len(cands) == 1:
        return cands[0], notes

    net = str(doc.get("net") or "").lower()
    want = "credit" if "credit" in net else ("debit" if "debit" in net else "")
    narrowed = [c for c in cands
                if TEMPLATES[c].net in (want, "either")] if want else cands
    if len(narrowed) == 1:
        return narrowed[0], notes
    if not narrowed:
        narrowed = cands
        notes.append("document net is %r, which no candidate shape claims"
                     % (doc.get("net"),))

    # Name tie-break. Scored on words, so "Short Iron Butterfly" beats
    # "iron_condor" for the shape they share.
    hay = " ".join([str(doc.get("slug") or ""), str(doc.get("name") or ""),
                    " ".join(doc.get("aliases") or [])]).lower()
    # Matched words MINUS unmatched ones. Counting only matches would score
    # `broken_wing_iron_condor` and `iron_condor` equally on a document called
    # "Short Iron Condor" -- both match "iron" and "condor" -- and the tie
    # would be resolved alphabetically, quietly building an unbalanced condor
    # for a balanced document. The words a template claims and the document
    # does NOT say have to count against it.
    best, score = None, -99
    for c in sorted(narrowed, key=lambda n: (len(n.split("_")), n)):
        words = c.split("_")
        s = sum(1 if w in hay else -1 for w in words)
        if s > score:
            best, score = c, s
    if best is not None:
        if len(narrowed) > 1:
            notes.append("topology is shared by %s; chose %s on the "
                         "document's own name" % (", ".join(narrowed), best))
        return best, notes
    notes.append("ambiguous topology %s and the name does not disambiguate"
                 % ", ".join(narrowed))
    return narrowed[0], notes


def check_match(name: str, doc: dict) -> list[str]:
    """Gate 4, per template. Problems, or an empty list."""
    errs = []
    t = get(name)
    want, got = optir.topology(doc.get("legs") or []), t.topology
    if want != got:
        errs.append("template %s builds %s, the document says %s"
                    % (name, optir._fmt_topo(got), optir._fmt_topo(want)))
    net = str(doc.get("net") or "").lower()
    if "credit" in net and t.net == "debit":
        errs.append("template %s opens for a debit, the document says credit"
                    % name)
    if "debit" in net and t.net == "credit":
        errs.append("template %s opens for a credit, the document says debit"
                    % name)
    lvl = doc.get("alpaca_level")
    if isinstance(lvl, int) and t.level > lvl and t.level > optir.ACCOUNT_LEVEL:
        # The dangerous direction only. A document claiming level 3 for a
        # shape whose legs leave a naked short is a document that would be
        # armed on an account that must refuse it, and Alpaca's rejection is
        # not a safety net -- it is a 403 after the intent was formed.
        errs.append("SAFETY: template %s leaves an uncovered short (level %d) "
                    "but the document claims level %d"
                    % (name, t.level, lvl))
    return errs


def level_note(name: str, doc: dict) -> Optional[str]:
    """A level disagreement in the harmless direction: the document is more
    conservative than the shape needs. Worth printing, never worth blocking."""
    t = get(name)
    lvl = doc.get("alpaca_level")
    if isinstance(lvl, int) and t.level != lvl:
        return ("document claims level %d; the shape's legs imply level %d"
                % (lvl, t.level))
    return None


# ======================================================== closing the trade
def closing_order(legs: Sequence) -> list:
    """The order the closing legs must be submitted in: SHORTS FIRST.

    Universal, and deliberately not overridable per template. If a four-leg
    close is ever broken into pieces -- because a leg went no-bid, because the
    mleg was rejected, because a human is unwinding by hand -- buying back the
    shorts first can only ever REDUCE risk. Doing it the other way leaves an
    uncovered short on a level-3 account for as long as the next order takes
    to fill, which is the one position Alpaca will not let us open on purpose
    and the one we can still reach by accident.

    Accepts optbacktest.Leg objects or optstructures leg dicts. Relative order
    within each group is preserved, so a reader can still recognise the
    structure.
    """
    def is_short(lg) -> bool:
        act = getattr(lg, "action", None)
        if act is None and isinstance(lg, dict):
            act = lg.get("side") or lg.get("action")
        return str(act).lower() == "sell"

    return [l for l in legs if is_short(l)] + \
           [l for l in legs if not is_short(l)]


def assignment_legs(name: str) -> list[dict]:
    """Which legs a guard watches, and why. Short options can be assigned to
    us at any time; long options are auto-exercised by the OCC at a cent in
    the money. Both end in stock we never sized for, and they need different
    handling, so they are reported separately rather than merged."""
    t = get(name)
    out = []
    for (right, action, ratio) in t.legs:
        if right not in ("call", "put"):
            continue
        if action == "sell":
            out.append({"right": right, "ratio": ratio, "danger": "assignment",
                        "note": "short %s: in the money at expiry delivers "
                                "100 shares per contract" % right})
        else:
            out.append({"right": right, "ratio": ratio,
                        "danger": "auto_exercise",
                        "note": "long %s: the OCC exercises anything $0.01 in "
                                "the money" % right})
    return out


def needs_assignment_guard(name: str) -> bool:
    return bool(get(name).short_legs)


# ============================================================== structures
def structure(name: str, legs: list[dict], qty: int = 1) -> "S.Structure":
    """Price and measure the built legs.

    Dispatches to the named optstructures constructor when there is one --
    those carry the better labels and the shape-specific assertions -- and
    falls back to the generic engine otherwise. Either way the arithmetic is
    optstructures', never this module's.

    `legs` are optstructures leg dicts in the TEMPLATE'S OWN build order, and
    the constructors below take them positionally in the order their
    signatures document. Getting that order wrong flips a credit spread into a
    debit spread, so test_opttemplates.py checks every constructor path
    against `optstructures.build()` on the same legs.
    """
    t = get(name)
    rows = [lg["row"] for lg in legs]
    sides = [lg["side"] for lg in legs]
    qtys = [lg.get("qty", 1) for lg in legs]

    def r(i):
        return rows[i]

    try:
        if t.ctor == "put_credit_spread":
            return S.put_credit_spread(r(0), r(1), qty)
        if t.ctor == "call_credit_spread":
            return S.call_credit_spread(r(0), r(1), qty)
        if t.ctor == "put_debit_spread":
            return S.put_debit_spread(r(0), r(1), qty)
        if t.ctor == "call_debit_spread":
            return S.call_debit_spread(r(0), r(1), qty)
        if t.ctor == "iron_condor":
            # optstructures takes (short_put, long_put, short_call, long_call)
            # and the template builds in exactly that order.
            return S.iron_condor(r(0), r(1), r(2), r(3), qty)
        if t.ctor == "iron_butterfly":
            return S.iron_butterfly(r(0), r(1), r(2), r(3), qty)
        if t.ctor == "strangle":
            side = "sell" if sides[0] == "sell" else "buy"
            return S.strangle(rows[1], rows[0], qty, side=side)
        if t.ctor == "straddle":
            side = "sell" if sides[0] == "sell" else "buy"
            return S.straddle(rows[1], rows[0], qty, side=side)
        if t.ctor == "calendar":
            return S.calendar(r(0), r(1), qty)
        if t.ctor == "diagonal":
            return S.diagonal(r(0), r(1), qty)
        if t.ctor == "broken_wing_butterfly":
            # Sorted by strike, not taken in build order. The put-side
            # builder walks DOWN from the body (near = body + w, far =
            # body - wide) while the call-side walks up, and the
            # constructor requires strictly increasing strikes. Passing the
            # build order raises on one side and silently mislabels the
            # wings on the other.
            lower, body, upper = sorted(rows, key=lambda r: r["strike"])
            return S.broken_wing_butterfly(lower, body, upper, qty)
        if t.ctor == "cash_secured_put":
            return S.cash_secured_put(r(0), qty)
        if t.ctor == "covered_call":
            return S.covered_call(r(0), qty)
        if t.ctor == "long_call":
            return S.long_call(r(0), qty)
        if t.ctor == "long_put":
            return S.long_put(r(0), qty)
    except (ValueError, TypeError) as exc:
        raise TemplateError("%s: %s" % (name, exc))

    built = [S.leg(rows[i], sides[i], qtys[i] * qty) for i in range(len(rows))]
    return S.build(name, built)


def max_loss(name: str, st: "S.Structure") -> Optional[float]:
    """The shape's worst case, in dollars, or None when it genuinely is not
    known yet.

    Not a formula -- optstructures computed it from the payoff and that is the
    number every gate downstream uses. What this adds is the CONSISTENCY
    CHECK: a template that claims defined risk and produces an unbounded loss
    is a wiring error, and finding it here beats finding it in the fill.
    """
    loss = getattr(st, "max_loss", None)
    if loss is None:
        return None
    t = get(name)
    if t.defined_risk and loss == float("inf"):
        raise TemplateError(
            "%s claims defined risk but the payoff is unbounded -- the legs "
            "were assembled wrong (a wing on the wrong side, or a missing "
            "long)" % name)
    return loss


def summary() -> list[dict]:
    """One row per shape, for the compile report and the dashboard."""
    out = []
    for n in names():
        t = TEMPLATES[n]
        out.append({
            "template": n, "net": t.net, "legs": len(t.legs),
            "order_path": t.order_path, "level": t.level,
            "defined_risk": t.defined_risk, "multi_expiry": t.multi_expiry,
            "short_legs": len(t.short_legs),
            "armable": n in armable_templates(),
            "has_builder": t.builder is not None,
            "note": t.note,
        })
    return out


if __name__ == "__main__":
    print("%d shapes, %d armable on a level-%d account"
          % (len(TEMPLATES), len(armable_templates()), optir.ACCOUNT_LEVEL))
    for row in summary():
        print("  %-24s %-7s %d legs  %-6s L%d %s%s"
              % (row["template"], row["net"], row["legs"], row["order_path"],
                 row["level"], "armable" if row["armable"] else "-",
                 "" if row["has_builder"] else "  (no builder)"))
