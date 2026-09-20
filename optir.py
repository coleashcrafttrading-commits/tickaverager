#!/usr/bin/env python3
"""
optir.py -- the typed intermediate representation a strategy document becomes,
and the closed vocabulary it is allowed to speak.

WHAT PROBLEM THIS SOLVES. `options/bank/` holds 231 documents whose rules are
English sentences: "IV rank(252-day) >= 30", "close at 21 DTE regardless of
P/L". They are a library; nothing executes them. The two obvious ways to close
that gap are both rejected in docs/options_design_v2.md section 5.1 -- an LLM
in the order path is nondeterministic and can argue past a risk gate, and 1,400
hand-written predicates drift from the JSON inside a month and then the
dashboard shows a rule the machine is not enforcing.

So: the sentence is compiled OFFLINE into the structure below, the result is
committed to git, and the runtime only ever reads data. This module is the
schema for that data and the validator that decides whether a compiled
strategy is allowed to exist.

THE ONE HARD RULE, and everything else follows from it: THERE IS NO EXPRESSION
LANGUAGE. A predicate is a tree of `all` / `any` / `not` over leaves of
`{fact, op, value}`. No arithmetic, no eval, no exec, not Turing complete. A
sentence that needs arithmetic -- "net credit >= 33% of the wing width" --
becomes a NAMED DERIVED FACT (`credit_over_width`) computed once in optfacts.py
and compared against a constant. The moment the IR grows `"expr"` it has grown
a parser and an evaluator and a whitelist, inside the component that has no
ground truth, and the compiler stops being checkable.

The second hard rule: a predicate may reference ONLY a name in FACTS below. A
sentence that needs something we do not measure cannot compile; its strategy
becomes blocked_on_feature -- browsable, visible, never armable. That turns
"231 strategies" from a vanity number into a ranked measurement backlog.

Read with: optpred.py (the evaluator), opttemplates.py (the shapes),
optcompile.py (the dev tool that produces these files).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent
IR_DIR = ROOT / "options" / "ir"

# Bump when the shape below changes incompatibly. A runtime that reads an IR
# with a version it does not know must refuse it, not best-effort it.
IR_VERSION = 1

# This account. Level 4 is naked; Alpaca rejects those outright, so a document
# needing one can be compiled for display but never armed.
ACCOUNT_LEVEL = 3

# Alpaca: "mleg orders must have at least 2 legs and at most 4 legs".
MIN_LEGS = 2
MAX_LEGS = 4

# draft   -- compiled, may screen and display, may NEVER produce an intent.
# reviewed-- a human read the round-trip diff and signed it.
# armed   -- reviewed AND permitted to allocate. The only status that trades.
STATUSES = ("draft", "reviewed", "armed")


class IRError(Exception):
    pass


# ====================================================================== ops
# Fixed, total, and small enough to read in one breath. `between` is inclusive
# on both ends because every band in the bank is written that way ("delta
# 0.14-0.20" includes 0.20), and picking the other convention would silently
# drop the edge strike the document actually names.
OPS = ("lt", "lte", "gt", "gte", "eq", "ne", "in", "not_in", "between")

# Which ops accept which value shape. Checked by the validator, because
# {"op": "between", "value": 30} is the kind of typo that evaluates to False
# forever and looks like a strategy that simply never triggers.
_OP_ARITY = {
    "lt": "scalar", "lte": "scalar", "gt": "scalar", "gte": "scalar",
    "eq": "any", "ne": "any", "in": "list", "not_in": "list",
    "between": "pair",
}


# ==================================================================== facts
@dataclass(frozen=True)
class FactSpec:
    """One name in the closed vocabulary.

    `scope` is load-bearing, not documentation. A symbol-scoped fact is known
    before any chain is touched, so it may appear in `preconditions`; a
    candidate-scoped fact only exists once a structure has been built, so a
    precondition referencing one is a compiler bug that would otherwise show up
    as "this strategy never fires" months later. The validator enforces it.

    `provider` names the function that must produce it. Where that function
    does not exist yet the fact is still legal to NAME -- that is the whole
    point of the backlog -- but optfacts will hand back a missing value and
    optpred turns missing into False, so the strategy simply does not fire.
    """
    name: str
    unit: str          # ratio | pct | days | usd | bool | count | grade |
                       # enum:a|b|c | price | vol | delta | minutes
    scope: str         # account | symbol | candidate | position
    provider: str      # "module.function" that computes it
    max_age_s: float   # past this the fact is STALE and treated as missing
    computed: bool     # False = handed to us by the broker
    note: str = ""


SCOPES = ("account", "symbol", "candidate", "position")

# Which scopes each IR section may read. A management rule may read symbol
# facts (IV rank fell, so take profit) but an entry precondition may never read
# a position fact -- there is no position yet.
SECTION_SCOPES = {
    "preconditions": ("account", "symbol"),
    "accept": ("account", "symbol", "candidate"),
    "management": ("account", "symbol", "candidate", "position"),
    "exits": ("account", "symbol", "candidate", "position"),
}


def _f(name, unit, scope, provider, max_age_s, computed=True, note=""):
    return FactSpec(name, unit, scope, provider, max_age_s, computed, note)


# The registry. THIS LIST IS THE VOCABULARY -- a fact that is not here cannot
# be written in an IR, and adding one is a deliberate act with a provider
# attached, not a side effect of compiling a new document.
FACTS: dict[str, FactSpec] = {s.name: s for s in [
    # ---------------------------------------------------------- account
    _f("account_equity", "usd", "account", "optfacts.account_facts", 60.0,
       False, "Alpaca equity. Every percent-of-equity gate divides by this."),
    _f("account_option_level", "count", "account", "optfacts.account_facts",
       3600.0, False, "3 today. A document needing 4 can never arm."),
    _f("buying_power_used_pct", "pct", "account", "optfacts.account_facts",
       60.0, True, "Short-premium BP across open structures, as % of equity."),

    # ----------------------------------------------------------- symbol
    _f("spot", "price", "symbol", "optfacts.vector", 15.0, False),
    _f("iv", "vol", "symbol", "optfacts.vector", 60.0, False,
       "ATM implied vol of the front listed expiry."),
    _f("iv_rank", "pct", "symbol", "optfacts.vector", 900.0, True,
       "Default window. Prefer the explicit iv_rank_252 in new IRs."),
    _f("iv_rank_252", "pct", "symbol", "optfacts.vector", 900.0, True,
       "IV rank over 252 sessions. The bank's default when it says 'IV rank'."),
    _f("iv_rank_days", "days", "symbol", "optfacts.vector", 900.0, True,
       "How many sessions of history the rank was actually computed over."),
    _f("iv_rank_quality", "enum:MEASURED|PARTIAL|UNAVAILABLE", "symbol",
       "optfacts.vector", 900.0, True,
       "An IV rank from 40 days of history is not an IV rank. Gate on this."),
    _f("iv_percentile", "pct", "symbol", "optfacts.vector", 900.0, True),
    _f("realized_vol_5", "vol", "symbol", "optfacts.vector", 900.0, True),
    _f("realized_vol_20", "vol", "symbol", "optfacts.vector", 900.0, True),
    _f("rv5_over_rv20", "ratio", "symbol", "optfacts.vector", 900.0, True,
       "A reason to move. Gate B of the cheap-options rule, design 5.6."),
    _f("vrp", "vol", "symbol", "optfacts.vector", 900.0, True,
       "Implied minus realized. Negative means options are cheap for cause."),
    _f("term_slope", "ratio", "symbol", "optfacts.vector", 900.0, True),
    _f("term_shape", "enum:contango|flat|backwardation", "symbol",
       "optfacts.vector", 900.0, True),
    _f("skew", "ratio", "symbol", "optfacts.vector", 900.0, True),
    _f("liquidity_grade", "grade", "symbol", "optfacts.vector", 900.0, True,
       "A|B|C|D|F. Compared with `in`, never with >=; it is not a number."),
    _f("open_interest", "count", "symbol", "optfacts.vector", 3600.0, False,
       "Chain-wide OI. Per-leg OI is the candidate fact min_leg_oi."),
    _f("earnings_in_days", "days", "symbol", "optfacts.vector", 3600.0, False,
       "Sessions to the next confirmed report. MISSING when unknown, which "
       "makes every earnings gate False -- we do not trade blind into one."),
    _f("ex_div_in_days", "days", "symbol", "optfacts.vector", 3600.0, False),
    _f("dte_to_next", "days", "symbol", "optfacts.vector", 3600.0, True,
       "DTE of the nearest listed expiry."),
    _f("regime", "enum:trend_up|trend_down|chop|stress", "symbol",
       "optfacts.vector", 900.0, True,
       "Display label by design 0.1. Legal in an IR but prefer the "
       "components -- a classifier on top of the same facts flaps twice."),
    _f("adx_14", "ratio", "symbol", "optfacts.vector", 900.0, True),
    _f("atr_14", "price", "symbol", "optfacts.vector", 900.0, True),
    _f("atr_20", "price", "symbol", "optfacts.vector", 900.0, True),
    _f("atr_pct_of_spot", "pct", "symbol", "optfacts.vector", 900.0, True),
    _f("close_vs_sma20", "pct", "symbol", "optfacts.vector", 900.0, True,
       "(close / SMA20 - 1) * 100. Positive means above."),
    _f("close_vs_sma50", "pct", "symbol", "optfacts.vector", 900.0, True),
    _f("close_vs_sma200", "pct", "symbol", "optfacts.vector", 900.0, True),
    # An EMA is not an SMA. Several documents say "last close > 20-EMA" and
    # compiling that against close_vs_sma20 would be the compiler quietly
    # trading a different rule than the one written down -- which is the
    # whole failure mode this design exists to prevent. Separate facts.
    _f("close_vs_ema20", "pct", "symbol", "optfacts.vector", 900.0, True,
       "(close / EMA20 - 1) * 100. Positive means above."),
    _f("close_vs_ema50", "pct", "symbol", "optfacts.vector", 900.0, True),
    _f("close_vs_ema200", "pct", "symbol", "optfacts.vector", 900.0, True),
    _f("sma20_vs_sma50", "pct", "symbol", "optfacts.vector", 900.0, True),
    _f("sma50_vs_sma200", "pct", "symbol", "optfacts.vector", 900.0, True),
    _f("adv_20d", "count", "symbol", "optfacts.vector", 3600.0, False,
       "20-day average daily share volume."),
    _f("underlying_price", "price", "symbol", "optfacts.vector", 15.0, False),
    _f("event_in_days", "days", "symbol", "optfacts.vector", 3600.0, False,
       "Nearest scheduled macro event (FOMC/CPI). Same missing-is-False rule."),
    _f("minutes_since_open", "minutes", "symbol", "optfacts.vector", 15.0,
       True, "Entry windows: 0DTE rules refuse the opening range."),
    _f("minutes_to_close", "minutes", "symbol", "optfacts.vector", 15.0, True),

    # -------------------------------------------------------- candidate
    # These exist only once a structure has been constructed from the chain.
    _f("net_credit", "usd", "candidate", "optfacts.candidate_facts", 15.0,
       True, "Per unit, at the mid, positive when we take money in."),
    _f("net_debit", "usd", "candidate", "optfacts.candidate_facts", 15.0,
       True, "Per unit, positive when we pay out."),
    _f("is_credit", "bool", "candidate", "optfacts.candidate_facts", 15.0),
    _f("credit_over_width", "ratio", "candidate", "optfacts.candidate_facts",
       15.0, True, "The '33% of the wing width' family, as one number."),
    _f("debit_over_width", "ratio", "candidate", "optfacts.candidate_facts",
       15.0, True),
    _f("width_pts", "price", "candidate", "optfacts.candidate_facts", 15.0),
    _f("max_loss_usd", "usd", "candidate", "optfacts.candidate_facts", 15.0),
    _f("max_loss_pct_equity", "pct", "candidate", "optfacts.candidate_facts",
       15.0, True),
    _f("max_profit_usd", "usd", "candidate", "optfacts.candidate_facts", 15.0),
    _f("debit_pct_equity", "pct", "candidate", "optfacts.candidate_facts",
       15.0, True),
    _f("credit_abs", "usd", "candidate", "optfacts.candidate_facts", 15.0,
       True, "Absolute dollars of credit per unit, for '>= $0.30' rules."),
    _f("dte", "days", "candidate", "optfacts.candidate_facts", 60.0),
    _f("front_dte", "days", "candidate", "optfacts.candidate_facts", 60.0,
       True, "Calendars and diagonals. The near expiry."),
    _f("back_dte", "days", "candidate", "optfacts.candidate_facts", 60.0),
    _f("dte_gap_days", "days", "candidate", "optfacts.candidate_facts", 60.0,
       True, "back_dte - front_dte. The clause a dropped 'later expiry' "
             "would delete, which is how a diagonal becomes a naked short."),
    _f("short_delta_abs", "delta", "candidate", "optfacts.candidate_facts",
       15.0, True, "Largest |delta| among the short legs."),
    _f("long_delta_abs", "delta", "candidate", "optfacts.candidate_facts",
       15.0, True),
    _f("net_delta_per_unit", "delta", "candidate", "optfacts.candidate_facts",
       15.0, True),
    _f("min_leg_oi", "count", "candidate", "optfacts.candidate_facts", 3600.0),
    _f("min_leg_volume", "count", "candidate", "optfacts.candidate_facts",
       900.0),
    _f("max_leg_spread_pct", "pct", "candidate", "optfacts.candidate_facts",
       15.0, True, "Worst per-leg bid/ask as a percent of that leg's mid."),
    _f("spread_pct_of_credit", "pct", "candidate", "optfacts.candidate_facts",
       15.0, True),
    _f("spread_pct_of_debit", "pct", "candidate", "optfacts.candidate_facts",
       15.0, True),
    _f("iv_diff_back_minus_front", "vol", "candidate",
       "optfacts.candidate_facts", 60.0, True,
       "Vol points. The calendar family's entry edge."),
    _f("short_extrinsic", "usd", "candidate", "optfacts.candidate_facts",
       15.0, True, "Per share, smallest across the short legs."),
    _f("legs_count", "count", "candidate", "optfacts.candidate_facts", 60.0),
    _f("earnings_in_expiry", "bool", "candidate", "optfacts.candidate_facts",
       3600.0, True, "True when a report falls inside the chosen expiry."),
    _f("ex_div_in_expiry", "bool", "candidate", "optfacts.candidate_facts",
       3600.0, True),
    _f("event_in_expiry", "bool", "candidate", "optfacts.candidate_facts",
       3600.0, True, "Scheduled FOMC/CPI inside the chosen expiry."),

    # --------------------------------------------------------- position
    # Deliberately distinct names from the candidate ones. `dte` on a
    # candidate and `dte` on a position are different measurements taken at
    # different times, and one name for both is how a management rule ends up
    # reading the entry snapshot.
    _f("pos_dte", "days", "position", "optfacts.position_facts", 60.0),
    _f("pos_days_held", "days", "position", "optfacts.position_facts", 60.0),
    _f("pl_pct_of_credit", "pct", "position", "optfacts.position_facts", 15.0,
       True, "Realizable P/L as a percent of the credit taken in."),
    _f("pl_pct_of_debit", "pct", "position", "optfacts.position_facts", 15.0),
    _f("pl_pct_of_max_profit", "pct", "position", "optfacts.position_facts",
       15.0, True),
    _f("mark_multiple_of_credit", "ratio", "position",
       "optfacts.position_facts", 15.0, True,
       "What it costs to buy back, over what we sold it for. The 2x stop."),
    _f("pos_short_delta_abs", "delta", "position", "optfacts.position_facts",
       15.0, True),
    _f("pos_net_delta", "delta", "position", "optfacts.position_facts", 15.0),
    _f("pos_short_extrinsic", "usd", "position", "optfacts.position_facts",
       15.0, True, "Per share. Below ~$0.10 an American short is live."),
    _f("pos_short_itm_pct", "pct", "position", "optfacts.position_facts",
       15.0, True, "How far the worst short is in the money, % of spot."),
    _f("pos_dist_to_short_pct", "pct", "position", "optfacts.position_facts",
       15.0, True, "|spot - nearest short strike| / spot * 100. Pin guard."),
    _f("pos_iv_rank", "pct", "position", "optfacts.position_facts", 900.0,
       True, "IV rank now, for 'if IV rank falls below 15, take profit'."),
    _f("pos_minutes_to_close", "minutes", "position",
       "optfacts.position_facts", 15.0, True),
    _f("pos_is_expiration_day", "bool", "position", "optfacts.position_facts",
       60.0, True),
    _f("pos_ex_div_in_days", "days", "position", "optfacts.position_facts",
       3600.0, False),
    _f("pos_leg_untradeable", "bool", "position", "optfacts.position_facts",
       15.0, True, "Any leg with no bid, or a halted underlying."),
]}


def fact(name: str) -> FactSpec:
    spec = FACTS.get(name)
    if spec is None:
        raise IRError("unknown fact %r; add it to optir.FACTS with a "
                      "provider, or the strategy does not compile" % (name,))
    return spec


def facts_in_scope(*scopes: str) -> list[str]:
    return sorted(n for n, s in FACTS.items() if s.scope in scopes)


def enum_values(spec: FactSpec) -> Optional[list[str]]:
    if spec.unit.startswith("enum:"):
        return spec.unit[5:].split("|")
    return None


# ============================================================ typed rules
# The management and exit sections are NOT predicate trees. A predicate answers
# yes/no; a management rule has to say what to DO, and an untyped action is an
# expression language again. So the actions are enumerated, each with its own
# required parameters, and anything the enumeration cannot say is unexpressed[]
# rather than approximated.

# kind -> (required params, optional params)
MANAGE_KINDS = {
    # basis: what the fraction is a fraction OF. Getting this wrong is a real
    # money error -- 50% of the credit and 50% of max profit are the same
    # number on a vertical and very different on a broken-wing fly.
    "profit_target": (("basis", "fraction"), ()),
    "stop_multiple": (("basis", "multiple"), ()),      # buy back at >= Nx
    "stop_fraction": (("basis", "fraction"), ()),      # lose N% of debit
    "time_stop": (("dte",), ("trading_days",)),
    "delta_stop": (("abs_delta",), ("leg",)),
    "touch_stop": (("reference",), ("pct",)),
    "iv_exit": (("iv_rank_below",), ("requires_profit",)),
    "roll": (("trigger", "direction"), ("max_times", "credit_only")),
    "max_units": (("units",), ("scope",)),
}

EXIT_KINDS = {
    # The one that actually protects the account. Every short leg gets one.
    "assignment_guard": (("time_et",), ("scope", "day")),
    "pin_guard": (("time_et", "pct_of_spot"), ()),
    "extrinsic_guard": (("threshold_usd",), ("leg",)),
    "itm_delta_guard": (("abs_delta",), ("leg",)),
    "itm_depth_guard": (("pct_of_width",), ("leg",)),
    "dividend_guard": ((), ("time_et",)),
    "dte_close": (("dte",), ("trading_days",)),
    "unwind_order": (("shorts_first",), ()),
    # "Close, do not exercise, any ITM long leg." Exercising a long is almost
    # always worse than selling it -- it throws away the remaining extrinsic
    # and it delivers 100 shares this account never sized for -- and Alpaca's
    # exercise endpoint has no quantity parameter, so it exercises the whole
    # position. Worth a typed rule of its own so the guard can refuse.
    "close_not_exercise": ((), ("leg",)),
}

BASES = ("credit", "debit", "max_profit", "max_loss", "max_structure_value")


# ================================================================ helpers
def doc_sha(doc: dict) -> str:
    """Content hash of a bank document.

    Why the whole document and not just the rules: a change to `legs` or
    `alpaca_level` invalidates a compile just as surely as a change to a
    sentence, and a hash that covers only what we happened to read is a hash
    that silently permits the edit that matters. If this breaks, the strategy
    auto-disarms and a human recompiles -- design 5.3 gate 2.
    """
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def topology(legs: list[dict]) -> tuple:
    """Canonical (right, action, ratio) triples, sorted.

    Sorted, because the document lists an iron condor's legs bottom-up and a
    template builds it shorts-first; comparing them in file order would fail a
    gate for a difference that does not exist. Ratio is normalised to int
    because the bank writes 100 shares as ratio 100 and 1 contract as 1.
    """
    out = []
    for lg in legs or []:
        out.append((str(lg.get("right", "")).lower(),
                    str(lg.get("action", "")).lower(),
                    int(lg.get("ratio") or 1)))
    return tuple(sorted(out))


def has_share_leg(legs: list[dict]) -> bool:
    return any(str(lg.get("right", "")).lower() == "stock" for lg in legs or [])


def short_option_legs(legs: list[dict]) -> list[dict]:
    return [lg for lg in legs or []
            if str(lg.get("action", "")).lower() == "sell"
            and str(lg.get("right", "")).lower() in ("call", "put")]


_TIME_ET = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


# ============================================================== validator
def validate_predicate(node: Any, section: str, path: str = "pre") -> list[str]:
    """Structural check of one predicate tree. Returns problems, never raises.

    Returns a LIST rather than raising on the first fault because a compiler
    report that shows one error at a time turns 231 documents into 231 round
    trips.
    """
    errs: list[str] = []
    if node is None:
        return errs
    if not isinstance(node, dict):
        return ["%s: predicate node must be an object, got %s"
                % (path, type(node).__name__)]

    combinators = [k for k in ("all", "any", "not") if k in node]
    if combinators and ("fact" in node):
        return ["%s: node is both a combinator (%s) and a leaf (fact=%s)"
                % (path, combinators[0], node.get("fact"))]
    if len(combinators) > 1:
        return ["%s: node has more than one combinator: %s"
                % (path, ", ".join(combinators))]

    if combinators:
        key = combinators[0]
        kids = node[key]
        if key == "not":
            return errs + validate_predicate(kids, section, path + ".not")
        if not isinstance(kids, list) or not kids:
            return ["%s.%s: must be a non-empty list" % (path, key)]
        for i, kid in enumerate(kids):
            errs += validate_predicate(kid, section, "%s.%s[%d]" % (path, key, i))
        return errs

    # ---- leaf
    name = node.get("fact")
    if not name:
        return ["%s: leaf has no `fact`" % path]
    spec = FACTS.get(name)
    if spec is None:
        errs.append("%s: unknown fact %r -- not in optir.FACTS" % (path, name))
    else:
        allowed = SECTION_SCOPES.get(section, SCOPES)
        if spec.scope not in allowed:
            errs.append(
                "%s: fact %r is %s-scoped and cannot be read in `%s` "
                "(allowed: %s)"
                % (path, name, spec.scope, section, ", ".join(allowed)))

    op = node.get("op")
    if op not in OPS:
        errs.append("%s: unknown op %r; the set is %s"
                    % (path, op, ", ".join(OPS)))
        return errs

    value = node.get("value")
    arity = _OP_ARITY[op]
    if arity == "scalar" and not isinstance(value, (int, float)):
        errs.append("%s: op %s needs a number, got %r" % (path, op, value))
    elif arity == "list" and not isinstance(value, list):
        errs.append("%s: op %s needs a list, got %r" % (path, op, value))
    elif arity == "pair":
        ok = (isinstance(value, list) and len(value) == 2
              and all(isinstance(v, (int, float)) for v in value))
        if not ok:
            errs.append("%s: op between needs [lo, hi], got %r" % (path, value))
        elif value[0] > value[1]:
            errs.append("%s: between bounds are inverted: %r" % (path, value))

    if spec is not None:
        choices = enum_values(spec)
        if choices:
            vals = value if isinstance(value, list) else [value]
            bad = [v for v in vals if v not in choices]
            if bad and op in ("eq", "ne", "in", "not_in"):
                errs.append("%s: %r is not a value of %s (%s)"
                            % (path, bad[0], name, "|".join(choices)))
        if spec.unit == "bool" and op not in ("eq", "ne"):
            errs.append("%s: %s is a bool; use eq/ne, not %s"
                        % (path, name, op))
        if spec.unit == "grade" and op in ("lt", "lte", "gt", "gte"):
            errs.append("%s: %s is a letter grade; compare with `in`, "
                        "not %s -- 'B' > 'A' is not what you mean"
                        % (path, name, op))
    if not str(node.get("src") or "").strip():
        errs.append("%s: leaf has no `src` sentence. The round-trip gate "
                    "cannot check a clause with no source." % path)
    return errs


def _validate_typed(rules: Any, kinds: dict, section: str) -> list[str]:
    errs: list[str] = []
    if rules is None:
        return errs
    if not isinstance(rules, list):
        return ["%s: must be a list" % section]
    for i, r in enumerate(rules):
        at = "%s[%d]" % (section, i)
        if not isinstance(r, dict):
            errs.append("%s: must be an object" % at)
            continue
        kind = r.get("kind")
        if kind not in kinds:
            errs.append("%s: unknown kind %r; the set is %s"
                        % (at, kind, ", ".join(sorted(kinds))))
            continue
        required, _optional = kinds[kind]
        for p in required:
            if r.get(p) is None:
                errs.append("%s (%s): missing required parameter %r"
                            % (at, kind, p))
        if r.get("basis") is not None and r["basis"] not in BASES:
            errs.append("%s: basis %r is not one of %s"
                        % (at, r["basis"], ", ".join(BASES)))
        t = r.get("time_et")
        if t is not None and not _TIME_ET.match(str(t)):
            errs.append("%s: time_et %r is not HH:MM in 24h ET" % (at, t))
        if r.get("when") is not None:
            errs += validate_predicate(r["when"], section, at + ".when")
        if not str(r.get("src") or "").strip():
            errs.append("%s: rule has no `src` sentence" % at)
    return errs


def validate(ir: dict, doc: Optional[dict] = None,
             *, level: int = ACCOUNT_LEVEL) -> list[str]:
    """Every structural rule an IR must satisfy. Returns problems, never raises.

    Pass `doc` -- the bank document it was compiled from -- to also run the
    topology and sha checks. Without it you are checking the IR against itself,
    which cannot catch the failure this whole design exists to catch: a
    construction that quietly does not build the structure the document
    describes.
    """
    errs: list[str] = []

    if ir.get("ir_version") != IR_VERSION:
        errs.append("ir_version is %r, this build speaks %d"
                    % (ir.get("ir_version"), IR_VERSION))
    if not str(ir.get("slug") or "").strip():
        errs.append("no slug")
    status = ir.get("status")
    if status not in STATUSES:
        errs.append("status %r is not one of %s"
                    % (status, ", ".join(STATUSES)))

    lvl = ir.get("alpaca_level")
    if not isinstance(lvl, int):
        errs.append("alpaca_level must be an int, got %r" % (lvl,))
    elif lvl > level:
        errs.append("alpaca_level %d exceeds this account's level %d; Alpaca "
                    "rejects it with 'account not eligible to trade uncovered "
                    "option contracts'" % (lvl, level))

    con = ir.get("construction")
    if not isinstance(con, dict):
        errs.append("no construction block")
        con = {}
    legs = con.get("legs") or []
    if not isinstance(legs, list):
        errs.append("construction.legs must be a list")
        legs = []
    if not (MIN_LEGS <= len(legs) <= MAX_LEGS):
        errs.append("construction has %d legs; Alpaca's mleg order takes %d-%d"
                    % (len(legs), MIN_LEGS, MAX_LEGS))
    if has_share_leg(legs):
        errs.append("construction has a share leg; shares do not ride in an "
                    "mleg order and need a separate, reconciled order path")
    if not str(con.get("template") or "").strip():
        errs.append("construction names no template")
    if not isinstance(con.get("params"), dict):
        errs.append("construction.params must be an object (may be empty)")

    for section in ("preconditions", "accept"):
        errs += validate_predicate(ir.get(section), section, section)
    errs += _validate_typed(ir.get("management"), MANAGE_KINDS, "management")
    errs += _validate_typed(ir.get("exits"), EXIT_KINDS, "exits")

    # Provenance. A compile with no source sha cannot be invalidated when the
    # document changes, which defeats gate 2 entirely.
    prov = ir.get("provenance")
    if not isinstance(prov, dict):
        errs.append("no provenance block")
        prov = {}
    if not prov.get("source_doc_sha"):
        errs.append("provenance.source_doc_sha is empty; nothing would "
                    "disarm this strategy when its document is edited")
    if not prov.get("compiled_at"):
        errs.append("provenance.compiled_at is empty")

    if not isinstance(ir.get("safety_gaps"), list):
        errs.append("safety_gaps must be a list (empty is fine, absent is "
                    "not -- absent would read as 'no gaps' when it means "
                    "'never checked')")

    cov = ir.get("coverage")
    if not isinstance(cov, dict):
        errs.append("no coverage block")
    unexp = ir.get("unexpressed")
    if not isinstance(unexp, list):
        errs.append("unexpressed must be a list (empty is fine, absent is not)")
        unexp = []
    for i, u in enumerate(unexp):
        if not isinstance(u, dict) or not u.get("text") or not u.get("reason"):
            errs.append("unexpressed[%d] needs both `text` and `reason`" % i)

    # ---- the status fence. draft may screen and display; only armed trades.
    if status in ("reviewed", "armed"):
        if not prov.get("reviewed_by") or not prov.get("reviewed_at"):
            errs.append("status %s without provenance.reviewed_by/reviewed_at; "
                        "the round-trip diff is signed by a human or it did "
                        "not happen" % status)
    if status == "armed":
        # The document-level check, not a per-sentence one: after compiling,
        # does this strategy still have the protections its own document says
        # it needs? optcompile.safety_gaps() computes them and explains why a
        # per-sentence flag was the wrong instrument.
        gaps = ir.get("safety_gaps") or []
        if gaps:
            errs.append(
                "armed with %d safety gap(s), first: %s -- %s"
                % (len(gaps), gaps[0].get("gap"),
                   str(gaps[0].get("why", ""))[:90]))
        if short_option_legs(legs) and not _has_assignment_guard(ir):
            errs.append("armed with a short leg and no assignment_guard exit; "
                        "nothing with a short leg is ever held to settlement")

    if doc is not None:
        errs += validate_against_doc(ir, doc)
    return errs


def _has_assignment_guard(ir: dict) -> bool:
    return any(isinstance(r, dict) and r.get("kind") == "assignment_guard"
               for r in (ir.get("exits") or []))


def validate_against_doc(ir: dict, doc: dict) -> list[str]:
    """Gate 4, topology, plus the sha. Free to run, because the bank already
    carries structured legs -- this is the single most useful check we get for
    nothing, and it is the one that catches a template wired to the wrong
    shape."""
    errs: list[str] = []
    if ir.get("slug") != doc.get("slug"):
        errs.append("slug mismatch: IR %r vs document %r"
                    % (ir.get("slug"), doc.get("slug")))
    want = topology(doc.get("legs") or [])
    got = topology((ir.get("construction") or {}).get("legs") or [])
    if want != got:
        errs.append("leg topology does not match the document.\n"
                    "      document: %s\n      construction: %s"
                    % (_fmt_topo(want), _fmt_topo(got)))
    sha = (ir.get("provenance") or {}).get("source_doc_sha")
    if sha and sha != doc_sha(doc):
        errs.append("source_doc_sha %s does not match the document on disk "
                    "(%s); the document changed after compiling and this "
                    "strategy must be recompiled and re-reviewed"
                    % (sha, doc_sha(doc)))
    return errs


def _fmt_topo(t: tuple) -> str:
    return ", ".join("%sx %s %s" % (r, a, w) for (w, a, r) in t) or "(none)"


# ============================================================ constructors
def new_ir(slug: str, *, template: str, params: dict, legs: list[dict],
           alpaca_level: int, source_doc_sha: str, compiled_at: str,
           compiler: str = "optcompile") -> dict:
    """An empty, schema-shaped IR. Born `draft` -- always. A compiler that can
    mint a reviewed strategy has removed the human from the one gate the human
    exists for."""
    return {
        "slug": slug,
        "ir_version": IR_VERSION,
        "status": "draft",
        "alpaca_level": int(alpaca_level),
        "construction": {"template": template, "params": dict(params),
                         "legs": list(legs)},
        "preconditions": None,
        "accept": None,
        "management": [],
        "exits": [],
        "limits": {},
        "coverage": {},
        "unexpressed": [],
        "safety_gaps": [],
        "blocked_on": [],
        "provenance": {
            "source_doc_sha": source_doc_sha,
            "compiled_at": compiled_at,
            "compiler": compiler,
            "reviewed_by": None,
            "reviewed_at": None,
        },
    }


def load(slug: str, ir_dir: Optional[Path] = None) -> dict:
    path = (ir_dir or IR_DIR) / ("%s.json" % slug)
    if not path.exists():
        raise IRError("no compiled IR at %s" % path)
    return json.loads(path.read_text(encoding="utf-8"))


def save(ir: dict, ir_dir: Optional[Path] = None) -> Path:
    d = ir_dir or IR_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / ("%s.json" % ir["slug"])
    path.write_text(json.dumps(ir, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def listing(ir_dir: Optional[Path] = None) -> list[dict]:
    d = ir_dir or IR_DIR
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        # Files beginning with an underscore are the compiler's own output
        # (the coverage report), not strategies. Loading one as a strategy
        # would put a row on the dashboard that nothing can trade.
        if p.name.startswith("_"):
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def armable(ir: dict, doc: Optional[dict] = None) -> tuple[bool, str]:
    """May this IR produce an executable intent? The draft fence, in one call.

    optalloc.allocate() asks this and nothing else. Everything upstream of it
    may screen, display and explain a draft strategy in full.
    """
    if ir.get("status") != "armed":
        return False, "status is %r; only `armed` may produce an intent" % (
            ir.get("status"),)
    errs = validate(ir, doc)
    if errs:
        return False, "fails validation: %s" % errs[0]
    return True, ""


if __name__ == "__main__":  # a quick look at the vocabulary
    import collections
    by = collections.Counter(s.scope for s in FACTS.values())
    print("optir v%d: %d facts" % (IR_VERSION, len(FACTS)))
    for scope in SCOPES:
        print("  %-10s %3d" % (scope, by[scope]))
    print("  ops: %s" % ", ".join(OPS))
    print("  manage kinds: %s" % ", ".join(sorted(MANAGE_KINDS)))
    print("  exit kinds:   %s" % ", ".join(sorted(EXIT_KINDS)))
