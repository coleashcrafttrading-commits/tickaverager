#!/usr/bin/env python3
"""
optpred.py -- the evaluator for an optir predicate tree, and the renderer that
turns one back into English.

Pure. No I/O, no clock of its own, no imports beyond the standard library and
optir. It is the only thing in the options stack that decides whether an entry
condition holds, so it is deliberately the most boring module in the repo:
given the same tree and the same facts it returns the same answer forever, and
a test can enumerate its behaviour exhaustively.

THE RULE THAT MATTERS: A MISSING FACT MAKES THE PREDICATE FALSE, NEVER TRUE,
AND NEVER ASSUMED. `earnings_in_days` is missing because we could not reach the
calendar, not because there are no earnings -- and a system that reads absence
as "no earnings" sells premium into a print. So absence is a third value,
UNKNOWN, propagated through the tree by Kleene logic and collapsed to False at
the top with the fact's name in the reason.

Why three values and not just False: under plain two-valued logic
`{"not": {"fact": "earnings_in_expiry", "op": "eq", "value": true}}` with the
fact missing evaluates to TRUE -- the negation of a false-because-unknown --
and that is exactly the trade we must not place. `not UNKNOWN` is UNKNOWN here.

Staleness is missing-ness. A fact older than its FactSpec.max_age_s is not a
fact, it is a memory, and the caller passing `ages` gets it treated as absent
with a reason that says so.
"""
from __future__ import annotations

from typing import Any, Optional

import optir

# Three-valued logic. Kept as plain strings so a trace can be JSON-dumped into
# the dashboard without a custom encoder.
TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"

# Missing values. None is the house spelling of "unformed"; the others turn up
# when a provider hands back a float it could not compute.
_MISSING = (None,)


class PredError(Exception):
    pass


def _is_missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:      # NaN, from a division by zero
        return True
    return False


# ================================================================== compare
def _cmp(op: str, value: Any, target: Any) -> Optional[bool]:
    """One leaf comparison. None means the comparison itself is not meaningful
    (a string against a number), which is UNKNOWN, not False -- a type error in
    a fact provider must surface as unusable, not as a silently skipped gate.
    """
    try:
        if op == "in":
            return value in target
        if op == "not_in":
            return value not in target
        if op == "eq":
            return _eq(value, target)
        if op == "ne":
            return not _eq(value, target)
        if op == "between":
            lo, hi = target
            return float(lo) <= float(value) <= float(hi)
        v, t = float(value), float(target)
        if op == "lt":
            return v < t
        if op == "lte":
            return v <= t
        if op == "gt":
            return v > t
        if op == "gte":
            return v >= t
    except (TypeError, ValueError):
        return None
    raise PredError("unknown op %r" % (op,))


def _eq(value: Any, target: Any) -> bool:
    """Equality that does not lie about bools.

    In Python `True == 1` and `False == 0`, so a bool fact compared against 0
    would pass. Every bool gate in the bank is written as "no earnings inside
    the expiry", and having that satisfied by a provider returning 0.0 for
    "unknown" is precisely the failure this module exists to prevent.
    """
    if isinstance(target, bool) or isinstance(value, bool):
        return bool(value) is bool(target) and \
            isinstance(value, bool) == isinstance(target, bool)
    if isinstance(target, str) or isinstance(value, str):
        return str(value) == str(target)
    try:
        return float(value) == float(target)
    except (TypeError, ValueError):
        return value == target


# ================================================================ evaluate
def _leaf(node: dict, facts: dict, ages: Optional[dict],
          now: Optional[float]) -> tuple[str, Optional[str]]:
    name = node.get("fact")
    op = node.get("op")
    target = node.get("value")
    spec = optir.FACTS.get(name)
    if spec is None:
        # Cannot happen through the validator, but this module is also called
        # on hand-edited JSON, and a typo must not become an unguarded pass.
        return UNKNOWN, "fact %r is not in the registry" % (name,)

    if name not in facts:
        return UNKNOWN, "fact %r was not supplied" % (name,)
    value = facts[name]
    if _is_missing(value):
        return UNKNOWN, "fact %r is unformed (no value could be computed)" % (
            name,)

    if ages is not None and now is not None and name in ages:
        age = now - float(ages[name])
        if age > spec.max_age_s:
            return UNKNOWN, ("fact %r is stale: %.0fs old, budget %.0fs"
                             % (name, age, spec.max_age_s))

    got = _cmp(op, value, target)
    if got is None:
        return UNKNOWN, ("fact %r has value %r, which cannot be compared with "
                         "%s %r" % (name, value, op, target))
    if got:
        return TRUE, None
    return FALSE, "%s is %s, needs %s %s" % (
        name, _fmt_value(value), _OP_WORDS[op], _fmt_value(target))


def _walk(node: Any, facts: dict, ages: Optional[dict], now: Optional[float],
          reasons: list, unknowns: list) -> str:
    if node is None:
        # An absent section is vacuously satisfied. A strategy with no
        # preconditions is a strategy whose preconditions all live in
        # `accept`, not a strategy that never trades.
        return TRUE
    if not isinstance(node, dict):
        unknowns.append("malformed predicate node: %r" % (node,))
        return UNKNOWN

    if "all" in node:
        states = [_walk(k, facts, ages, now, reasons, unknowns)
                  for k in node["all"]]
        if FALSE in states:
            return FALSE
        return UNKNOWN if UNKNOWN in states else TRUE

    if "any" in node:
        # Sub-reasons of a satisfied `any` are noise -- "close is below the
        # 20-EMA" is not a failure when the other branch passed -- so they are
        # collected into a scratch list and only kept if the whole node fails.
        sub_r: list = []
        sub_u: list = []
        states = [_walk(k, facts, ages, now, sub_r, sub_u) for k in node["any"]]
        if TRUE in states:
            return TRUE
        reasons.extend(sub_r)
        unknowns.extend(sub_u)
        return UNKNOWN if UNKNOWN in states else FALSE

    if "not" in node:
        sub_r: list = []
        sub_u: list = []
        inner = _walk(node["not"], facts, ages, now, sub_r, sub_u)
        if inner == UNKNOWN:
            unknowns.extend(sub_u or ["negated clause is unknown"])
            return UNKNOWN
        if inner == TRUE:
            reasons.append("NOT(%s) but it holds" % render(node["not"]))
            return FALSE
        return TRUE

    state, why = _leaf(node, facts, ages, now)
    if state == FALSE and why:
        reasons.append(why)
    elif state == UNKNOWN and why:
        unknowns.append(why)
    return state


def evaluate(tree: Any, facts: dict, *, ages: Optional[dict] = None,
             now: Optional[float] = None) -> tuple[bool, list[str]]:
    """Does this predicate hold against these facts? (ok, reasons-it-did-not).

    `facts` maps a registry name to a value; a name that is absent, None or
    NaN is MISSING. `ages` optionally maps a name to the epoch second it was
    measured; with `now` supplied, anything past its FactSpec budget is
    treated as missing.

    On success `reasons` is empty. On failure it names every clause that
    blocked, and unknown facts are named as unknown -- the dashboard must be
    able to tell "we looked and it said no" from "we could not look", because
    the fix for the second one is a fact provider, not a different strategy.
    """
    reasons: list[str] = []
    unknowns: list[str] = []
    state = _walk(tree, facts, ages, now, reasons, unknowns)
    if state == TRUE:
        return True, []
    out = list(reasons) + ["UNUSABLE: " + u for u in unknowns]
    if not out:
        out = ["predicate is false"]
    return False, out


def explain(tree: Any, facts: dict, *, ages: Optional[dict] = None,
            now: Optional[float] = None) -> dict:
    """The same walk, as a tree a dashboard can draw: every node with its
    state, its English and, for a leaf, the value we actually saw."""
    if tree is None:
        return {"node": "empty", "state": TRUE, "text": "(no conditions)"}
    for key in ("all", "any"):
        if key in tree:
            kids = [explain(k, facts, ages=ages, now=now) for k in tree[key]]
            states = [k["state"] for k in kids]
            if key == "all":
                st = FALSE if FALSE in states else (
                    UNKNOWN if UNKNOWN in states else TRUE)
            else:
                st = TRUE if TRUE in states else (
                    UNKNOWN if UNKNOWN in states else FALSE)
            return {"node": key, "state": st, "text": render(tree),
                    "children": kids}
    if "not" in tree:
        kid = explain(tree["not"], facts, ages=ages, now=now)
        st = {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}[kid["state"]]
        return {"node": "not", "state": st, "text": render(tree),
                "children": [kid]}
    state, why = _leaf(tree, facts, ages, now)
    return {"node": "leaf", "state": state, "text": render(tree),
            "fact": tree.get("fact"),
            "value": facts.get(tree.get("fact")),
            "src": tree.get("src"), "why": why}


def facts_used(tree: Any) -> list[str]:
    """Every registry name this tree reads. The allocator uses it to ask
    optfacts for exactly what is needed and no more."""
    out: list[str] = []
    _collect(tree, out)
    seen: set = set()
    uniq: list[str] = []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def _collect(node: Any, out: list) -> None:
    if not isinstance(node, dict):
        return
    for key in ("all", "any"):
        if key in node:
            for k in node[key]:
                _collect(k, out)
            return
    if "not" in node:
        _collect(node["not"], out)
        return
    if node.get("fact"):
        out.append(node["fact"])


# ================================================================== render
# Gate 2 of the compiler. A renderer that reads well but drops a clause
# defeats the gate it exists for, so every branch here prints something for
# every node -- there is no "and some other conditions".
_OP_WORDS = {
    "lt": "below", "lte": "at most", "gt": "above", "gte": "at least",
    "eq": "equal to", "ne": "not equal to", "in": "one of",
    "not_in": "none of", "between": "between",
}

# English for the facts whose names do not read as English on their own. A
# fact absent from this table renders as its name with the underscores taken
# out, which is honest and slightly ugly -- that is the right trade for a gate
# a human reads.
_PHRASES = {
    "iv_rank_252": "IV rank (252-day)",
    "iv_rank": "IV rank",
    "iv_percentile": "IV percentile",
    "iv_rank_quality": "IV rank quality",
    "rv5_over_rv20": "5-day realized vol over 20-day",
    "vrp": "variance risk premium (implied minus realized)",
    "adx_14": "ADX(14)",
    "atr_14": "ATR(14)",
    "atr_20": "ATR(20)",
    "atr_pct_of_spot": "ATR as a percent of spot",
    "close_vs_ema20": "close versus the 20-day EMA, in percent",
    "close_vs_ema50": "close versus the 50-day EMA, in percent",
    "close_vs_ema200": "close versus the 200-day EMA, in percent",
    "close_vs_sma20": "close versus the 20-day SMA, in percent",
    "close_vs_sma50": "close versus the 50-day SMA, in percent",
    "close_vs_sma200": "close versus the 200-day SMA, in percent",
    "sma20_vs_sma50": "20-day SMA versus the 50-day, in percent",
    "sma50_vs_sma200": "50-day SMA versus the 200-day, in percent",
    "adv_20d": "20-day average daily share volume",
    "credit_over_width": "net credit as a fraction of the width",
    "debit_over_width": "net debit as a fraction of the width",
    "max_loss_pct_equity": "max loss as a percent of equity",
    "debit_pct_equity": "debit as a percent of equity",
    "min_leg_oi": "the thinnest leg's open interest",
    "min_leg_volume": "the thinnest leg's day volume",
    "max_leg_spread_pct": "the widest leg's bid-ask as a percent of its mid",
    "spread_pct_of_credit": "the structure's bid-ask as a percent of credit",
    "spread_pct_of_debit": "the structure's bid-ask as a percent of debit",
    "short_delta_abs": "the short strike's absolute delta",
    "long_delta_abs": "the long strike's absolute delta",
    "net_delta_per_unit": "net delta per unit",
    "iv_diff_back_minus_front": "back-month IV minus front-month IV, in vol "
                                "points",
    "dte_gap_days": "the gap between the back and front expiries, in days",
    "short_extrinsic": "the short leg's extrinsic value per share",
    "earnings_in_days": "sessions to the next earnings report",
    "ex_div_in_days": "sessions to the next ex-dividend date",
    "event_in_days": "sessions to the next scheduled macro event",
    "earnings_in_expiry": "an earnings report inside the expiry",
    "ex_div_in_expiry": "an ex-dividend date inside the expiry",
    "event_in_expiry": "a scheduled macro event inside the expiry",
    "pos_dte": "days to expiry",
    "pos_days_held": "days held",
    "pl_pct_of_credit": "P/L as a percent of the credit",
    "pl_pct_of_debit": "P/L as a percent of the debit",
    "pl_pct_of_max_profit": "P/L as a percent of max profit",
    "mark_multiple_of_credit": "the buy-back cost as a multiple of the credit",
    "pos_short_delta_abs": "the short leg's absolute delta",
    "pos_short_extrinsic": "the short leg's extrinsic value per share",
    "pos_short_itm_pct": "how far the short is in the money, percent of spot",
    "pos_dist_to_short_pct": "distance from spot to the short strike, percent",
    "pos_iv_rank": "IV rank now",
    "pos_leg_untradeable": "a leg with no bid or a halted underlying",
    "account_equity": "account equity",
    "account_option_level": "the account's Alpaca options level",
    "liquidity_grade": "liquidity grade",
    "term_shape": "term structure shape",
    "minutes_since_open": "minutes since the open",
    "minutes_to_close": "minutes to the close",
}


def phrase(name: str) -> str:
    return _PHRASES.get(name, name.replace("_", " "))


def _negate(text: str) -> str:
    """"no an earnings report inside the expiry" is how a round-trip diff
    stops being readable, and an unreadable diff is an unread diff."""
    for art in ("a ", "an ", "the "):
        if text.startswith(art):
            return "no " + text[len(art):]
    return "no " + text


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return " and ".join(_fmt_value(x) for x in v)
    if isinstance(v, float):
        return ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v)


def render(node: Any, *, top: bool = True) -> str:
    """Turn a predicate back into English. The other half of gate 2."""
    if node is None:
        return "(no conditions)"
    if not isinstance(node, dict):
        return "(malformed: %r)" % (node,)
    for key, word in (("all", " AND "), ("any", " OR ")):
        if key in node:
            parts = [render(k, top=False) for k in node[key]]
            body = word.join(parts)
            return body if top or len(parts) == 1 else "(%s)" % body
    if "not" in node:
        return "NOT (%s)" % render(node["not"], top=False)
    name = node.get("fact", "?")
    op = node.get("op", "?")
    val = node.get("value")
    if optir.FACTS.get(name) and optir.FACTS[name].unit == "bool":
        want = bool(val) if op == "eq" else (not val)
        return phrase(name) if want else _negate(phrase(name))
    return "%s %s %s" % (phrase(name), _OP_WORDS.get(op, op), _fmt_value(val))


def render_rule(rule: dict) -> str:
    """English for one typed management or exit rule. Same gate, other half of
    the document."""
    k = rule.get("kind")
    if k == "profit_target":
        return "take profit at %s%% of the %s" % (
            _fmt_value(100.0 * float(rule["fraction"])), rule["basis"])
    if k == "stop_multiple":
        return "stop out when it can only be bought back for %sx the %s" % (
            _fmt_value(rule["multiple"]), rule["basis"])
    if k == "stop_fraction":
        return "stop out at a loss of %s%% of the %s" % (
            _fmt_value(100.0 * float(rule["fraction"])), rule["basis"])
    if k == "time_stop":
        if rule.get("dte") is not None:
            return "close at %s DTE regardless of P/L" % _fmt_value(rule["dte"])
        return "close after %s trading days held" % _fmt_value(
            rule.get("trading_days"))
    if k == "delta_stop":
        return "close when the %s leg's absolute delta reaches %s" % (
            rule.get("leg", "short"), _fmt_value(rule["abs_delta"]))
    if k == "touch_stop":
        return "close if the underlying touches %s" % rule["reference"]
    if k == "iv_exit":
        tail = " while profitable" if rule.get("requires_profit") else ""
        return "close when IV rank falls below %s%s" % (
            _fmt_value(rule["iv_rank_below"]), tail)
    if k == "roll":
        return "roll %s when %s" % (rule["direction"], rule["trigger"])
    if k == "max_units":
        return "at most %s units per %s" % (
            _fmt_value(rule["units"]), rule.get("scope", "underlying"))
    if k == "assignment_guard":
        return ("close the %s no later than %s ET on expiration day, "
                "regardless of P/L" % (rule.get("scope", "structure"),
                                       rule["time_et"]))
    if k == "pin_guard":
        return ("close at %s ET if the underlying is within %s%% of a short "
                "strike" % (rule["time_et"], _fmt_value(rule["pct_of_spot"])))
    if k == "extrinsic_guard":
        return ("close the short leg when its extrinsic value falls below "
                "$%s per share" % _fmt_value(rule["threshold_usd"]))
    if k == "itm_delta_guard":
        return "close the short leg when its absolute delta reaches %s" % (
            _fmt_value(rule["abs_delta"]))
    if k == "itm_depth_guard":
        return ("close when a short goes in the money by more than %s of the "
                "width" % _fmt_value(rule["pct_of_width"]))
    if k == "dividend_guard":
        t = rule.get("time_et")
        when = (" by %s ET" % t) if t else ""
        return ("close or roll the short call before the ex-dividend "
                "date%s" % when)
    if k == "dte_close":
        if rule.get("dte") is not None:
            return "close no later than %s DTE" % _fmt_value(rule["dte"])
        return "close no later than %s trading days before expiry" % (
            _fmt_value(rule.get("trading_days")))
    if k == "unwind_order":
        return ("unwind shorts before longs" if rule.get("shorts_first")
                else "unwind longs before shorts")
    if k == "close_not_exercise":
        return "close an in-the-money long leg rather than exercising it"
    return "(no rendering for kind %r)" % (k,)


if __name__ == "__main__":
    tree = {"all": [
        {"fact": "iv_rank_252", "op": "gte", "value": 30, "src": "x"},
        {"any": [{"fact": "close_vs_sma20", "op": "gt", "value": 0, "src": "y"},
                 {"fact": "adx_14", "op": "lt", "value": 20, "src": "z"}]},
        {"fact": "earnings_in_expiry", "op": "eq", "value": False, "src": "w"},
    ]}
    print(render(tree))
    print(evaluate(tree, {"iv_rank_252": 41.0, "close_vs_sma20": 1.2,
                          "earnings_in_expiry": False}))
    print(evaluate(tree, {"iv_rank_252": 41.0, "close_vs_sma20": 1.2}))
