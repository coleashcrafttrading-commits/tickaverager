#!/usr/bin/env python3
"""
optcompile.py -- the DEV TOOL that turns a bank document's English into an
optir IR. Run by a human, output committed to git.

    .venv/Scripts/python optcompile.py --slug iron-condor --show
    .venv/Scripts/python optcompile.py --all --report
    .venv/Scripts/python optcompile.py --slug iron-condor --review
    .venv/Scripts/python optcompile.py --slug iron-condor --sign "cole"

NO RUNNING PROCESS MAY IMPORT THIS MODULE. That is the whole point of the
design: the translation from a sentence to a decision happens once, offline,
where a human can read the diff, and the runtime only ever loads JSON. If this
file ever appears in an import chain under optd.py, the order path has grown a
nondeterministic component and the backtest has stopped predicting production.

HOW THE TRANSLATION ACTUALLY WORKS, and the judgement call a reviewer should
challenge first. Design 5.3 says "an LLM does the translation". What is
implemented here is a DETERMINISTIC CLAUSE MATCHER: ~60 patterns over the
phrasings the bank actually uses, each emitting a typed IR fragment. Two
reasons, and the reviewer is entitled to disagree with both:

  1. It is reproducible. Re-running it on an unchanged document produces a
     byte-identical IR, so `git diff` after a document edit shows exactly what
     changed in the machine's understanding. An LLM pass re-run produces a
     diff on every line and the signal is lost in it.
  2. It cannot hallucinate a number. A matcher either finds "0.33" in the
     sentence or it does not fire. The failure mode of a pattern matcher is a
     MISS, which gate 3 reports; the failure mode of a generative translator
     is a plausible INVENTION, which no gate here can see.

The cost is real: a phrasing nobody anticipated is reported as unexpressed
rather than understood, so coverage is lower than an LLM pass would claim.
That is the trade this file makes on purpose -- an honest 60% beats a
confident 95% when the 5% is a missing stop.

THE FOUR GATES (design 5.3):
  a SCHEMA     optir.validate -- names, ops, legs, level, provenance.
  b ROUND-TRIP every compiled clause is rendered back into English and diffed
               against the sentence it came from. The diff is number-aware:
               any figure in the source that does not survive into the IR is a
               failure, because SILENT OMISSION is what schema checking cannot
               see. Dropping "later expiry" from a diagonal un-covers a short
               call and every other check still passes.
  c COVERAGE   every sentence in entry_rules / management_rules / exit_rules
               is compiled, partially compiled, or listed in unexpressed[]
               with a reason. No silent drops. A strategy that looks armed and
               is missing its stop is the failure that matters most.
  d TOPOLOGY   the construction's legs match the document's own legs[].
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import optbank
import optir
import optpred
import opttemplates

ROOT = Path(__file__).resolve().parent
REPORT_PATH = optir.IR_DIR / "_coverage_report.json"

NUM = r"(\d+(?:\.\d+)?)"


# ================================================================== clauses
@dataclass
class Clause:
    """One fragment of IR produced from one span of one sentence."""
    section: str                  # preconditions | accept | management | exits
    payload: dict                 # a predicate leaf, or a typed rule
    span: tuple
    matcher: str

    def rendered(self) -> str:
        if self.section in ("preconditions", "accept"):
            return optpred.render(self.payload)
        return optpred.render_rule(self.payload)


def _leaf(section, fact, op, value, src, matcher, span, note=None):
    p = {"fact": fact, "op": op, "value": value, "src": src}
    if note:
        p["note"] = note
    return Clause(section, p, span, matcher)


def _rule(section, kind, src, matcher, span, **params):
    p = {"kind": kind, "src": src}
    p.update(params)
    return Clause(section, p, span, matcher)


# ================================================================ matchers
# Each entry is (name, compiled regex, handler). The handler gets the match
# and the ORIGINAL sentence, and returns a list of Clauses. Patterns are
# matched against a lowercased copy; spans index the original, which is the
# same length because lowering never changes length for the ASCII the bank
# uses.
MATCHERS: list[tuple] = []


def matcher(name: str, pattern: str):
    def deco(fn):
        MATCHERS.append((name, re.compile(pattern, re.I), fn))
        return fn
    return deco


def _op_word(word: str) -> Optional[str]:
    w = word.strip().lower()
    return {">=": "gte", "=>": "gte", ">": "gt", "<=": "lte", "=<": "lte",
            "<": "lt", "at least": "gte", "above": "gt", "over": "gt",
            "below": "lt", "under": "lt", "at most": "lte",
            "no more than": "lte", "no less than": "gte",
            "minimum": "gte", "min": "gte", "max": "lte",
            "maximum": "lte"}.get(w)


_CMP = (r"(>=|<=|=>|=<|>|<|at least|at most|no more than|"
        r"no less than|above|below|over|under)")


# ----------------------------------------------------------- volatility
@matcher("iv_rank_between", r"iv\s*rank[^.;]{0,20}?between\s+" + NUM +
         r"\s*(?:and|-|to)\s*" + NUM)
def _m(m, s):
    return [_leaf("preconditions", "iv_rank_252", "between",
                  [float(m.group(1)), float(m.group(2))], s,
                  "iv_rank_between", m.span())]


@matcher("iv_rank", r"\b(?:iv\s*rank|ivr)\b(?:\s*\([^)]*\))?\s*" + _CMP +
         r"\s*" + NUM)
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("preconditions", "iv_rank_252", op, float(m.group(2)), s,
                  "iv_rank", m.span())] if op else []


@matcher("iv_pct", r"iv\s*percentile\s*" + _CMP + r"\s*" + NUM)
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("preconditions", "iv_percentile", op, float(m.group(2)), s,
                  "iv_pct", m.span())] if op else []


@matcher("term_contango", r"term structure in contango|contango")
def _m(m, s):
    return [_leaf("preconditions", "term_shape", "eq", "contango", s,
                  "term_contango", m.span())]


@matcher("vrp", r"\bvrp\b\s*" + _CMP + r"\s*(-?\d+(?:\.\d+)?)")
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("preconditions", "vrp", op, float(m.group(2)), s, "vrp",
                  m.span())] if op else []


# ------------------------------------------------------------- trend
@matcher("account_level", r"(?:alpaca\s+)?(?:options?\s+)?level\s*" + _CMP +
                          r"\s*(\d)\b")
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("preconditions", "account_option_level", op,
                  float(m.group(2)), s, "account_level", m.span())] if op else []


@matcher("account_level_bare", r"alpaca\s+(?:options?\s+)?level\s+(\d)\b")
def _m(m, s):
    return [_leaf("preconditions", "account_option_level", "gte",
                  float(m.group(1)), s, "account_level_bare", m.span())]


@matcher("adx", r"adx\s*\(?\s*14\s*\)?\s*" + _CMP + r"\s*" + NUM)
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("preconditions", "adx_14", op, float(m.group(2)), s, "adx",
                  m.span())] if op else []


@matcher("sma_cross", r"(\d+)-day sma\s*(?:<|below)\s*(?:the\s+)?(\d+)-day sma")
def _m(m, s):
    a, b = m.group(1), m.group(2)
    if (a, b) in (("20", "50"), ("50", "200")):
        return [_leaf("preconditions", "sma%s_vs_sma%s" % (a, b), "lt", 0, s,
                      "sma_cross", m.span())]
    return []


@matcher("spot_vs_ma", r"(?:spot|price|last close|close)\s*(<|>|above|below)"
                       r"\s*(?:the\s+)?(\d+)[- ](?:day[- ])?(sma|ema)")
def _m(m, s):
    win, kind = m.group(2), m.group(3).lower()
    if win not in ("20", "50", "200"):
        return []
    op = "lt" if m.group(1).lower() in ("<", "below") else "gt"
    # sma and ema are different facts on purpose; see optir.FACTS.
    return [_leaf("preconditions", "close_vs_%s%s" % (kind, win), op, 0, s,
                  "spot_vs_ma", m.span())]


# ------------------------------------------------------------- events
@matcher("no_earnings", r"no (?:entry with )?earnings"
                        r"(?:[^.;]{0,40}?)?(?:inside|before|through|"
                        r"in)\s+the\s+(?:chosen\s+)?(?:expiry|expiration|"
                        r"cycle|front cycle|window)")
def _m(m, s):
    out = [_leaf("accept", "earnings_in_expiry", "eq", False, s,
                 "no_earnings", m.span())]
    low = s.lower()
    if "ex-dividend" in low or "ex dividend" in low:
        out.append(_leaf("accept", "ex_div_in_expiry", "eq", False, s,
                         "no_earnings", m.span()))
    if "fomc" in low or "cpi" in low:
        out.append(_leaf("accept", "event_in_expiry", "eq", False, s,
                         "no_earnings", m.span()))
    return out


@matcher("no_earnings_short", r"no earnings\b(?!\s*(?:inside|before|through))")
def _m(m, s):
    return [_leaf("accept", "earnings_in_expiry", "eq", False, s,
                  "no_earnings_short", m.span())]


@matcher("earnings_days", r"earnings\s*(?:at least|>=|more than)\s*" + NUM +
                          r"\s*(?:days|sessions)")
def _m(m, s):
    return [_leaf("preconditions", "earnings_in_days", "gt", float(m.group(1)),
                  s, "earnings_days", m.span())]


# --------------------------------------------------------------- dte
@matcher("dte_between", r"\bdte\b\s*(?:between\s*)?" + NUM +
                        r"\s*(?:-|to|and)\s*" + NUM)
def _m(m, s):
    return [_leaf("accept", "dte", "between",
                  [float(m.group(1)), float(m.group(2))], s, "dte_between",
                  m.span())]


@matcher("dte_between2", r"\b" + NUM + r"\s*(?:-|to)\s*" + NUM + r"\s*dte\b")
def _m(m, s):
    return [_leaf("accept", "dte", "between",
                  [float(m.group(1)), float(m.group(2))], s, "dte_between2",
                  m.span())]


@matcher("dte_cmp", r"\bdte\b\s*" + _CMP + r"\s*" + NUM)
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "dte", op, float(m.group(2)), s, "dte_cmp",
                  m.span())] if op else []


@matcher("front_back_dte", r"front\s*" + NUM + r"\s*(?:-|to)\s*" + NUM +
                           r"\s*dte,?\s*back\s*" + NUM + r"\s*(?:-|to)\s*" +
                           NUM + r"\s*dte")
def _m(m, s):
    g = [float(x) for x in m.groups()]
    return [_leaf("accept", "front_dte", "between", [g[0], g[1]], s,
                  "front_back_dte", m.span()),
            _leaf("accept", "back_dte", "between", [g[2], g[3]], s,
                  "front_back_dte", m.span()),
            # The clause that must never be lost. A diagonal whose "back" is
            # not actually later is a naked short call.
            _leaf("accept", "dte_gap_days", "gt", 0, s, "front_back_dte",
                  m.span(), note="implied by front < back")]


@matcher("later_expiry", r"(?:short|front|near)\s+expiry\s+(?:strictly\s+)?"
                         r"before\s+the\s+(?:long|back|far)\s+expiry|"
                         r"long(?:er)?\s+(?:leg|call|put)?\s*in\s+a\s+later\s+"
                         r"expiry|later\s+expir(?:y|ation)")
def _m(m, s):
    return [_leaf("accept", "dte_gap_days", "gt", 0, s, "later_expiry",
                  m.span(),
                  note="SAFETY: without this the short leg is uncovered")]


@matcher("dte_gap", r"gap\s*" + NUM + r"\s*(?:-|to)\s*" + NUM + r"\s*days")
def _m(m, s):
    return [_leaf("accept", "dte_gap_days", "between",
                  [float(m.group(1)), float(m.group(2))], s, "dte_gap",
                  m.span())]


# ------------------------------------------------------------- deltas
@matcher("delta_band", r"(?:\|?delta\|?|deltas)\s*(?:of\s*)?(?:between\s*)?"
                       r"-?" + NUM + r"\s*(?:-|to|and)\s*-?" + NUM)
def _m(m, s):
    lo, hi = sorted((float(m.group(1)), float(m.group(2))))
    if hi > 1.0:                      # "delta 30-45" is not a delta band here
        return []
    return [_leaf("accept", "short_delta_abs", "between", [lo, hi], s,
                  "delta_band", m.span())]


@matcher("delta_band2", r"-?" + NUM + r"\s*(?:-|to)\s*-?" + NUM +
                        r"\s*delta\b")
def _m(m, s):
    lo, hi = sorted((float(m.group(1)), float(m.group(2))))
    if hi > 1.0:
        return []
    return [_leaf("accept", "short_delta_abs", "between", [lo, hi], s,
                  "delta_band2", m.span())]


@matcher("delta_cmp", r"(?:short\s+strikes?\s+)?\|?delta\|?\s*" + _CMP +
                      r"\s*-?" + NUM)
def _m(m, s):
    op = _op_word(m.group(1))
    v = float(m.group(2))
    if not op or v > 1.0:
        return []
    return [_leaf("accept", "short_delta_abs", op, v, s, "delta_cmp",
                  m.span())]


# ------------------------------------------------------ credit and risk
# "the wing width W", "spread width", "strike width", "W x 100" -- five
# spellings of one quantity across the bank, and a gate that only knew one of
# them silently dropped the credit floor on four documents in five.
_WIDTH = r"(?:the\s*)?(?:wing|spread|strike)?\s*(?:width|w\b)"


@matcher("credit_over_width", r"(?:net\s+)?credit\s*" + _CMP + r"\s*" + NUM +
                              r"\s*%\s*of\s*" + _WIDTH)
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "credit_over_width", op, float(m.group(2)) / 100.0,
                  s, "credit_over_width", m.span())] if op else []


@matcher("debit_over_width", r"(?:net\s+|total\s+)?debit\s*" + _CMP +
                             r"\s*" + NUM + r"\s*%\s*of\s*" + _WIDTH)
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "debit_over_width", op, float(m.group(2)) / 100.0,
                  s, "debit_over_width", m.span())] if op else []


@matcher("credit_abs", r"(?:net\s+)?credit\s*" + _CMP + r"\s*\$" + NUM)
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "credit_abs", op, float(m.group(2)), s,
                  "credit_abs", m.span())] if op else []


@matcher("net_credit_required", r"open for a net credit|must remain a credit|"
                                r"reject debit fills")
def _m(m, s):
    return [_leaf("accept", "is_credit", "eq", True, s,
                  "net_credit_required", m.span())]


@matcher("max_loss_equity", r"max(?:imum)?\s+loss[^.;]{0,60}?" + _CMP +
                            r"\s*" + NUM + r"\s*%\s*of\s*(?:account\s*)?"
                            r"equity")
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "max_loss_pct_equity", op, float(m.group(2)), s,
                  "max_loss_equity", m.span())] if op else []


@matcher("debit_equity", r"(?:net\s+|total\s+)?debit\s*" + _CMP + r"\s*" +
                         NUM + r"\s*%\s*of\s*(?:account\s*)?equity")
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "debit_pct_equity", op, float(m.group(2)), s,
                  "debit_equity", m.span())] if op else []


# --------------------------------------------------------- liquidity
@matcher("oi", r"\b(?:oi|open interest)\b\s*" + _CMP + r"\s*([\d,]+)")
def _m(m, s):
    op = _op_word(m.group(1))
    v = float(m.group(2).replace(",", ""))
    return [_leaf("accept", "min_leg_oi", op, v, s, "oi", m.span())] if op else []


@matcher("vol", r"\b(?:day\s+)?volume\b\s*" + _CMP + r"\s*([\d,]+)")
def _m(m, s):
    op = _op_word(m.group(1))
    v = float(m.group(2).replace(",", ""))
    if v > 1e6:                      # that is share ADV, not option volume
        return []
    return [_leaf("accept", "min_leg_volume", op, v, s, "vol",
                  m.span())] if op else []


@matcher("spread_pct_mid", r"(?:bid[-/ ]ask|spread)[^.;]{0,30}?" + _CMP +
                           r"\s*" + NUM + r"\s*%\s*of\s*(?:leg\s*)?(?:the\s*)?"
                           r"mid")
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "max_leg_spread_pct", op, float(m.group(2)), s,
                  "spread_pct_mid", m.span())] if op else []


@matcher("spread_pct_credit", r"bid[-/ ]ask\s*" + _CMP + r"\s*" + NUM +
                              r"\s*%\s*of\s*the\s*credit")
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "spread_pct_of_credit", op, float(m.group(2)), s,
                  "spread_pct_credit", m.span())] if op else []


@matcher("spread_pct_debit", r"bid[-/ ]ask\s*" + _CMP + r"\s*" + NUM +
                             r"\s*%\s*of\s*the\s*debit")
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "spread_pct_of_debit", op, float(m.group(2)), s,
                  "spread_pct_debit", m.span())] if op else []


@matcher("underlying_price", r"underlying price\s*" + _CMP + r"\s*\$?" + NUM)
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("preconditions", "spot", op, float(m.group(2)), s,
                  "underlying_price", m.span())] if op else []


@matcher("adv", r"adv\s*" + _CMP + r"\s*([\d,]+)")
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("preconditions", "adv_20d", op,
                  float(m.group(2).replace(",", "")), s, "adv",
                  m.span())] if op else []


@matcher("iv_diff", r"back_?iv\s*-\s*front_?iv\s*" + _CMP + r"\s*" + NUM +
                    r"\s*vol")
def _m(m, s):
    op = _op_word(m.group(1))
    return [_leaf("accept", "iv_diff_back_minus_front", op,
                  float(m.group(2)), s, "iv_diff", m.span())] if op else []


# =========================================================== management
_BASIS = r"(credit|debit|max profit|max structure value|max-loss amount|" \
         r"max loss|total credit|total debit|structure value)"


def _basis_of(word: str) -> str:
    w = word.lower()
    if "max profit" in w:
        return "max_profit"
    if "max-loss" in w or "max loss" in w:
        return "max_loss"
    if "structure value" in w:
        return "max_structure_value"
    return "credit" if "credit" in w else "debit"


@matcher("profit_target", r"(?:take profit|profit target|close|buy to close)"
                          r"[^.;]{0,30}?(?:at|of)?\s*\+?" + NUM +
                          r"\s*%\s*of\s*(?:the\s*)?(?:net\s*)?"
                          r"(?:total\s*)?(?:theoretical\s*)?" + _BASIS)
def _m(m, s):
    return [_rule("management", "profit_target", s, "profit_target", m.span(),
                  basis=_basis_of(m.group(2)),
                  fraction=float(m.group(1)) / 100.0)]


@matcher("profit_target_pct", r"(?:take profit|profit target)\s*(?:at\s*)?\+"
                              + NUM + r"\s*%(?!\s*of)")
def _m(m, s):
    # "+50%" with no basis stated. The bank means the structure's own net:
    # credit for a credit structure, debit for a debit one. That is a
    # judgement, so it is recorded as one -- the note travels into the IR.
    return [_rule("management", "profit_target", s, "profit_target_pct",
                  m.span(), basis="credit",
                  fraction=float(m.group(1)) / 100.0,
                  note="basis not stated in the sentence; assumed the "
                       "structure's own net. Reviewer: confirm.")]


@matcher("stop_fraction", r"stop\s*(?:out\s*)?(?:at|of)?\s*-" + NUM +
                          r"\s*%\s*(?:of\s*(?:the\s*)?(?:total\s*)?" +
                          _BASIS + r")?")
def _m(m, s):
    basis = _basis_of(m.group(2)) if m.group(2) else "debit"
    return [_rule("management", "stop_fraction", s, "stop_fraction", m.span(),
                  basis=basis, fraction=float(m.group(1)) / 100.0)]


@matcher("stop_multiple", r"(?:>=\s*)?" + NUM + r"\s*x\s*(?:the\s*)?"
                          r"(credit|debit)\s*(?:received)?")
def _m(m, s):
    low = s.lower()
    if not any(w in low for w in ("stop", "close if", "buy it back",
                                  "bought back", "loss")):
        return []
    return [_rule("management", "stop_multiple", s, "stop_multiple", m.span(),
                  basis=m.group(2).lower(), multiple=float(m.group(1)))]


@matcher("time_stop", r"(?:time stop|close|exit)[^.;]{0,30}?(?:at|:)\s*" +
                      NUM + r"\s*dte")
def _m(m, s):
    return [_rule("management", "time_stop", s, "time_stop", m.span(),
                  dte=float(m.group(1)))]


@matcher("time_stop2", r"time stop\s*(?:at|:)?\s*" + NUM + r"\s*dte")
def _m(m, s):
    return [_rule("management", "time_stop", s, "time_stop2", m.span(),
                  dte=float(m.group(1)))]


@matcher("max_units", r"max(?:imum)?\s*" + NUM + r"\s*units?\s*per\s*"
                      r"(underlying|symbol|account)")
def _m(m, s):
    return [_rule("management", "max_units", s, "max_units", m.span(),
                  units=float(m.group(1)), scope=m.group(2).lower())]


@matcher("delta_stop", r"(?:short\s+(?:put|call)\s+)?\|?delta\|?\s*"
                       r"(?:reaches|hits|>=|exceeds)\s*-?" + NUM)
def _m(m, s):
    v = float(m.group(1))
    low = s.lower()
    if v > 1.0:
        return []
    if not any(w in low for w in ("stop", "close", "roll", "guard", "exit")):
        return []
    section = "exits" if "guard" in low or "itm" in low else "management"
    kind = "itm_delta_guard" if section == "exits" else "delta_stop"
    return [_rule(section, kind, s, "delta_stop", m.span(), abs_delta=v)]


@matcher("iv_exit", r"iv rank (?:falls|drops)\s*below\s*" + NUM)
def _m(m, s):
    return [_rule("management", "iv_exit", s, "iv_exit", m.span(),
                  iv_rank_below=float(m.group(1)),
                  requires_profit="profitable" in s.lower())]


# A positive-signed stop: "loss stop at 200% of max loss", "hard stop at 50%
# loss of the debit". The bank writes the same instruction with and without
# the minus sign, and reading only the signed form lost the stop on 14
# documents -- the single most expensive class of miss in this compiler.
@matcher("stop_fraction_pos", r"(?:loss stop|hard stop|stop)\s*(?:out\s*)?"
                              r"(?:at|of|:)\s*" + NUM + r"\s*%\s*"
                              r"(?:loss\s*)?of\s*(?:the\s*)?(?:net\s*)?"
                              r"(?:total\s*)?(?:theoretical\s*)?" + _BASIS)
def _m(m, s):
    return [_rule("management", "stop_fraction", s, "stop_fraction_pos",
                  m.span(), basis=_basis_of(m.group(2)),
                  fraction=float(m.group(1)) / 100.0)]


@matcher("max_units2", r"maximum\s*" + NUM + r"\s*units?\b")
def _m(m, s):
    return [_rule("management", "max_units", s, "max_units2", m.span(),
                  units=float(m.group(1)), scope="underlying")]


@matcher("touch_stop", r"touch stop[^.;]{0,60}?(?:trades through|touches)\s*"
                       r"([^,.;]{3,40})")
def _m(m, s):
    return [_rule("management", "touch_stop", s, "touch_stop", m.span(),
                  reference=m.group(1).strip())]


# "close any ITM short leg by 7 DTE unconditionally", "close the entire
# structure no later than 2 trading days before expiry".
@matcher("dte_close2", r"clos\w+[^.;]{0,50}?\b(?:by|at)\s*" + NUM +
                       r"\s*dte\b")
def _m(m, s):
    return [_rule("exits", "dte_close", s, "dte_close2", m.span(),
                  dte=float(m.group(1)))]


@matcher("dte_close_days2", r"clos\w+[^.;]{0,50}?\b(?:by|within|no later "
                            r"than)\s*" + NUM + r"\s*trading days")
def _m(m, s):
    return [_rule("exits", "dte_close", s, "dte_close_days2", m.span(),
                  dte=float(m.group(1)), trading_days=float(m.group(1)))]


# The ex-dividend family, in every spelling the bank uses. A short call whose
# extrinsic is below the dividend WILL be exercised the night before the
# ex-date; this is the one guard that fires outside expiration week.
@matcher("dividend_guard2", r"(?:clos\w+|roll)[^.;]{0,90}?ex-?div")
def _m(m, s):
    t = re.search(_TIME, s, re.I)
    tt = None
    if t:
        tt = t.group(1)
        if len(tt.split(":")[0]) == 1:
            tt = "0" + tt
    return [_rule("exits", "dividend_guard", s, "dividend_guard2", m.span(),
                  **({"time_et": tt} if tt else {}))]


# ================================================================= exits
_TIME = r"(\d{1,2}:\d{2})\s*(?:et|pm et|p\.m\. et)?"


@matcher("assignment_guard", r"(?:flat(?:ten)? by|"
                             r"force[- ]close[^.;]{0,40}?by|"
                             r"(?:submit\s+a\s+)?clos\w+[^.;]{0,60}?"
                             r"(?:no later than|\bby\b))\s*(?:the\s+)?"
                             + _TIME)
def _m(m, s):
    t = m.group(1)
    if len(t.split(":")[0]) == 1:
        t = "0" + t
    low = s.lower()
    scope = "short_legs" if "short" in low and "structure" not in low \
        else "structure"
    return [_rule("exits", "assignment_guard", s, "assignment_guard", m.span(),
                  time_et=t, scope=scope, day="expiration")]


@matcher("assignment_guard2", r"(?:mandatory assignment guard|hard rule|"
                              r"assignment guard)[^.;]{0,60}?" + _TIME)
def _m(m, s):
    t = m.group(1)
    if len(t.split(":")[0]) == 1:
        t = "0" + t
    return [_rule("exits", "assignment_guard", s, "assignment_guard2",
                  m.span(), time_et=t, scope="structure", day="expiration")]


@matcher("pin_guard", r"pin guard[^.;]{0,80}?" + NUM + r"\s*%")
def _m(m, s):
    t = re.search(_TIME, s, re.I)
    tt = t.group(1) if t else "15:30"
    if len(tt.split(":")[0]) == 1:
        tt = "0" + tt
    return [_rule("exits", "pin_guard", s, "pin_guard", m.span(),
                  time_et=tt, pct_of_spot=float(m.group(1)))]


@matcher("extrinsic_guard", r"extrinsic(?:\s+value)?\s*(?:<=|<|below|under)"
                            r"\s*\$?" + NUM)
def _m(m, s):
    return [_rule("exits", "extrinsic_guard", s, "extrinsic_guard", m.span(),
                  threshold_usd=float(m.group(1)))]


@matcher("itm_depth", r"itm by (?:more than\s*)?" + NUM +
                      r"\s*(?:x|\*)\s*w\b")
def _m(m, s):
    return [_rule("exits", "itm_depth_guard", s, "itm_depth", m.span(),
                  pct_of_width=float(m.group(1)))]


@matcher("dividend_guard", r"dividend guard|before (?:the\s+)?ex-dividend|"
                           r"ex-dividend date whenever|before every "
                           r"ex-dividend")
def _m(m, s):
    return [_rule("exits", "dividend_guard", s, "dividend_guard", m.span())]


@matcher("dte_close", r"close[^.;]{0,40}?no later than\s*" + NUM + r"\s*dte")
def _m(m, s):
    return [_rule("exits", "dte_close", s, "dte_close", m.span(),
                  dte=float(m.group(1)))]


@matcher("dte_close_days", r"close[^.;]{0,50}?no later than\s*" + NUM +
                           r"\s*trading days")
def _m(m, s):
    return [_rule("exits", "dte_close", s, "dte_close_days", m.span(),
                  dte=float(m.group(1)), trading_days=float(m.group(1)))]


@matcher("close_not_exercise", r"clos\w+,? do not exercise|"
                               r"close(?:,| rather than)[^.;]{0,30}?"
                               r"(?:rather than|instead of) (?:exercis|"
                               r"allowing assignment)")
def _m(m, s):
    return [_rule("exits", "close_not_exercise", s, "close_not_exercise",
                  m.span())]


# Every spelling of "shorts first" in the bank. It is one instruction and it
# is the one that keeps a partial unwind from creating a naked short, so it
# is worth matching generously rather than precisely.
@matcher("unwind_order", r"unwind shorts before longs|buy back the short"
                         r"(?:s)? before selling (?:the|any) long|"
                         r"short risk first|never (?:close|sell) the long"
                         r"[^.;]{0,40}?while the short|"
                         r"close the short(?:s)? (?:leg\s+)?first")
def _m(m, s):
    return [_rule("exits", "unwind_order", s, "unwind_order", m.span(),
                  shorts_first=True)]


# ======================================================= sentence triage
# A sentence that no matcher understood still has to be ACCOUNTED FOR. These
# patterns do not compile anything; they explain, in the report, why not. A
# reason of "unclassified" is a request for a new matcher, and the report
# counts them so the backlog is visible instead of implied.
_REASONS = [
    # First, the sentences that say there is deliberately nothing to enforce.
    # Reading "no short option legs, therefore no assignment risk" as a
    # MISSING assignment guard is how a safety flag becomes noise.
    (r"no profit target or stop is specified|not applicable|"
     r"no short option legs|no assignment risk|there is nothing to|"
     r"^n/a\b",
     "the document states there is nothing to enforce here"),
    (r"same width|equal wings|balanced wings|identical (?:atm )?strike|"
     r"all three (?:strikes|contracts)|same expiry|wings",
     "structural: the template enforces this shape, so there is nothing for "
     "a predicate to check"),
    (r"\bunless\b|\bexcept\b|\bonly if\b",
     "conditional exception clause: the IR has no way to attach an 'unless' "
     "to a compiled predicate without an expression language"),
    (r"\broll\b",
     "rolling is not implemented in v1 (design 5.4)"),
    (r"mleg|limit order|improve by|market order|natural mid|pay up to|"
     r"single .*order|fill|slippage",
     "order mechanics: owned by optexec, not by the IR"),
    (r"level 4|uncovered|not eligible|blocked on this account",
     "level 4 on this account; the strategy cannot be sent at all"),
    (r"implied move|expected move",
     "no fact: implied_move_multiple"),
    (r"\bskew\b|\bterm structure\b|\bvix\b",
     "no fact for this volatility-surface condition yet"),
    (r"correlat",
     "portfolio-level constraint: belongs to optalloc, not a per-strategy IR"),
    (r"catalyst|news|thesis|discretion|judgement|judgment",
     "discretionary: no machine-readable condition in the sentence"),
    (r"halt|untradeable|no bid",
     "degraded-market handling: owned by optguard"),
    (r"assignment|assigned|exercise",
     "assignment prose: the machine-readable part is the guard, which is "
     "compiled separately where the sentence gives a time or a threshold"),
    (r"\bstop\b|\bguard\b|\bclose\b|\bexit\b",
     "an exit or stop this compiler could not express"),
]


def classify_unexpressed(sentence: str, section: str,
                         verdict: str) -> tuple[str, bool]:
    """Why a sentence did not fully compile, and whether that is dangerous.

    SAFETY-CRITICAL is deliberately narrow, because a flag that fires on
    everything is a flag nobody reads. It means: this sentence produced
    NOTHING, and it lives where losing it costs money. A partially compiled
    sentence is not flagged -- something of it is enforced, and gate 2 already
    refuses to let it arm until a human has read the leftover in the diff.
    """
    low = sentence.lower()
    reason = "unclassified: no matcher recognised this phrasing"
    for pat, text in _REASONS:
        if text.startswith("an exit or stop") and section == "entry_rules":
            # "Directional gate: last CLOSE > 20-EMA" is an entry rule that
            # happens to contain the word "close". Calling it a missing exit
            # sends a reader looking for a stop that was never there.
            continue
        if re.search(pat, low):
            reason = text
            break

    return reason, (verdict == "unexpressed" and section == "exit_rules"
                    and not reason.startswith(
                        ("the document states", "order mechanics",
                         "structural", "level 4", "portfolio-level")))


# ------------------------------------------------------- safety, per DOCUMENT
# The flag that decides whether a strategy may ever arm is computed over the
# WHOLE document, not sentence by sentence, and that is the correction that
# matters most in this file.
#
# A per-sentence flag fires on "Assignment guard: the short put and the short
# call are both assignable at any time" -- which is a description, not an
# instruction, and whose actionable half ("flat by 15:45") is compiled from
# the sentence above it. Four hundred flags like that is a flag nobody reads,
# and a safety check nobody reads is worse than none, because it looks like
# cover.
#
# The question that actually matters is: after compiling, does this strategy
# still have the protections its own document says it needs?
def safety_gaps(ir: dict, doc: dict) -> list[dict]:
    gaps: list[dict] = []
    legs = doc.get("legs") or []
    shorts = optir.short_option_legs(legs)
    kinds = {r.get("kind") for r in (ir.get("exits") or [])}
    mkinds = {r.get("kind") for r in (ir.get("management") or [])}
    text = " ".join(
        (doc.get("management_rules") or []) + (doc.get("exit_rules") or [])
    ).lower()

    if shorts and "assignment_guard" not in kinds:
        gaps.append({
            "gap": "no_assignment_guard",
            "why": "the structure has %d short leg(s) and no compiled "
                   "assignment guard. These are AMERICAN options on shares: "
                   "a short that finishes in the money delivers 100 shares "
                   "per contract the account never sized for."
                   % len(shorts)})

    wants_stop = bool(re.search(r"\bstop\b|\bloss stop\b|\bhard stop\b", text))
    has_stop = bool({"stop_multiple", "stop_fraction", "delta_stop",
                     "touch_stop"} & mkinds)
    if wants_stop and not has_stop:
        gaps.append({
            "gap": "stop_named_but_not_compiled",
            "why": "the document names a stop and none compiled. A strategy "
                   "that looks armed and is missing its stop is the failure "
                   "this whole pipeline exists to prevent."})

    short_calls = [l for l in shorts
                   if str(l.get("right", "")).lower() == "call"]
    if short_calls and "dividend" in text and "dividend_guard" not in kinds:
        gaps.append({
            "gap": "no_dividend_guard",
            "why": "a short call plus a dividend rule in the document, and "
                   "no compiled guard. A short call with less extrinsic than "
                   "the dividend is exercised the night before the ex-date."})

    if shorts and "unwind_order" not in kinds:
        # Not fatal on its own -- opttemplates.closing_order() enforces
        # shorts-first for every shape whether or not a document says so --
        # so this is recorded and NOT counted as a gap. Left here as the
        # comment that stops someone adding it as one.
        pass
    return gaps


# ============================================================ round-trip
# Numbers inside parentheses and after "e.g." are ILLUSTRATIONS ("e.g. >= $1.65
# on a $5 wing"), not separate conditions, so they are excluded from the
# accounting. This is a real weakening of gate 2 and it is stated here rather
# than buried: a genuine condition written only inside a parenthesis would be
# missed by the number check. It would still be caught by the residual-text
# check below, which does not strip anything.
_PARENS = re.compile(r"\([^)]*\)")
_EG = re.compile(r"\b(?:e\.g\.|for example|i\.e\.)[^;.]*", re.I)
# Thousands separators are part of the number. Without this, "2,000,000
# shares" reads as three missing figures and every liquidity rule in the bank
# fails gate 2 for a reason that is punctuation.
_NUMS = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Numbers that are part of a fact's own name rather than a threshold.
_NAME_NUMS = {"252", "14", "20", "50", "200", "100", "5", "10"}


def numbers_in(sentence: str) -> list[str]:
    s = _EG.sub("", _PARENS.sub("", sentence))
    return [n.replace(",", "").rstrip(".") for n in _NUMS.findall(s)]


def numbers_out(clauses: list[Clause]) -> set:
    """Every figure that survived into the IR, in both its written forms --
    0.5 and 50 for a half, because the sentence says "50%" and the IR stores
    0.5, and a gate that could not see through that would fail every profit
    target in the bank."""
    out: set = set()

    def add(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        for cand in (f, f * 100.0, f / 100.0):
            out.add(_canon(cand))

    for c in clauses:
        for key in ("value", "fraction", "multiple", "dte", "trading_days",
                    "abs_delta", "units", "pct_of_spot", "threshold_usd",
                    "pct_of_width", "iv_rank_below"):
            v = c.payload.get(key)
            if isinstance(v, list):
                for x in v:
                    add(x)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                add(v)
        t = c.payload.get("time_et")
        if t:
            for part in str(t).split(":"):
                add(part)
                out.add(_canon(float(part.lstrip("0") or "0")))
    return out


def _canon(x: float) -> str:
    return ("%.6f" % float(x)).rstrip("0").rstrip(".")


def round_trip(sentence: str, clauses: list[Clause]) -> dict:
    """Gate 2, for one sentence. Renders what was compiled and reports every
    figure in the source that did not survive."""
    got = numbers_out(clauses)
    missing = []
    for n in numbers_in(sentence):
        try:
            f = float(n)
        except ValueError:
            continue
        if _canon(f) in got:
            continue
        if n in _NAME_NUMS and clauses:
            continue                  # "ADX(14)", "252-day": part of a name
        missing.append(n)
    residual = _residual(sentence, clauses)
    ored = _or_between(sentence, clauses)
    return {
        "source": sentence,
        "rendered": _render_all(clauses),
        "missing_numbers": missing,
        "residual": residual,
        "or_risk": ored,
        "ok": not missing and not residual and not ored and bool(clauses),
    }


def _render_all(clauses: list[Clause]) -> str:
    """Predicate leaves are ANDed; typed rules are independent triggers. Using
    one joiner for both would render an exit list as a conjunction, and a
    reviewer skimming the diff would sign off on "close at 21 DTE AND at 50%
    of credit" as though both had to hold."""
    if not clauses:
        return "(nothing)"
    pre = [c for c in clauses if c.section in ("preconditions", "accept")]
    rules = [c for c in clauses if c not in pre]
    parts = []
    if pre:
        parts.append(" AND ".join(c.rendered() for c in pre))
    if rules:
        parts.append("; ".join(c.rendered() for c in rules))
    return "; ".join(parts)


_OR = re.compile(r"\b(?:or|either)\b", re.I)


def _or_between(sentence: str, clauses: list[Clause]) -> str:
    """Does the sentence say OR where the IR will say AND?

    This is a silent-omission cousin and it is worth its own check. A
    predicate section is a conjunction, so two clauses lifted from "IV rank >=
    30 OR IV percentile >= 40" would be ANDed. For an ENTRY gate that is
    merely stricter than intended -- fewer trades, never a riskier one -- so
    the clauses are kept and the sentence is reported as partial rather than
    dropped. Reported, though: a strategy that never fires because of this
    looks identical to a strategy the market never offered.
    """
    pre = [c for c in clauses if c.section in ("preconditions", "accept")]
    if len(pre) < 2:
        return ""
    spans = sorted((c.span for c in pre), key=lambda s: s[0])
    for (a_lo, a_hi), (b_lo, _b_hi) in zip(spans, spans[1:]):
        gap = sentence[a_hi:b_lo]
        if _OR.search(gap):
            return gap.strip()
    return ""


_QUALIFIERS = re.compile(
    r"\bunless\b|\bexcept\b|\bonly if\b|\botherwise\b|\bbut\b|\bwhichever\b|"
    r"\bprefer\b|\bideally\b|\broll\b|\bnever\b", re.I)


def _residual(sentence: str, clauses: list[Clause]) -> str:
    """What is left of the sentence once every matched span is cut out --
    but only the part that still looks like a CONDITION. Prose glue is not a
    dropped clause, and reporting it as one would make gate 2 cry wolf until
    nobody reads it."""
    if not clauses:
        return ""
    spans = sorted((c.span for c in clauses), key=lambda s: s[0])
    keep, cursor = [], 0
    for lo, hi in spans:
        if lo > cursor:
            keep.append(sentence[cursor:lo])
        cursor = max(cursor, hi)
    keep.append(sentence[cursor:])
    left = " ".join(keep)
    q = _QUALIFIERS.search(left)
    if q:
        return left[max(0, q.start() - 20):q.start() + 80].strip()
    return ""


# =============================================================== compile
def _dedupe(clauses: list[Clause]) -> list[Clause]:
    """Two matchers can legitimately fire on one sentence ("DTE 30-60" hits
    both dte_between and dte_between2). The narrower span wins, and an exact
    duplicate payload is dropped."""
    out: list[Clause] = []
    seen: set = set()
    for c in sorted(clauses, key=lambda c: (c.span[1] - c.span[0])):
        key = (c.section, json.dumps(
            {k: v for k, v in c.payload.items() if k != "src"},
            sort_keys=True, default=str))
        if key in seen:
            continue
        # A clause entirely inside another clause's span, on the same fact, is
        # the same condition read twice.
        covered = any(o.span[0] <= c.span[0] and c.span[1] <= o.span[1]
                      and o.payload.get("fact") == c.payload.get("fact")
                      and o.payload.get("fact") is not None for o in out)
        if covered:
            continue
        seen.add(key)
        out.append(c)
    return sorted(out, key=lambda c: c.span[0])


# Everything after one of these words belongs to an EXCEPTION or a
# PREFERENCE, not to the rule, and nothing from it may enter the IR.
#
# This is not tidiness. Two real misreadings from the bank, both caught by
# reading a round-trip diff:
#
#   "IV rank >= 30 over 252 days; prefer IV rank > 50 for full size."
#       compiled to `>= 30 AND > 50`, silently raising the entry bar to 50.
#
#   "Earnings: no entry with earnings inside the expiry UNLESS IV rank > 60
#    and the short strike is beyond 1.25 * the implied move."
#       compiled to `IV rank > 60` ALONE -- the prohibition dropped and the
#       exception kept, which is the exact inversion of the sentence. That
#       one would have sold premium into an earnings print.
#
# The IR has no way to attach a condition to a condition, so the only honest
# handling is to take nothing from the clause and report the sentence as
# partial. Being unable to express something must cost coverage, never
# correctness.
_QUALIFIED_FROM = re.compile(
    r"\b(?:unless|except|otherwise|prefer(?:ably)?|ideally|"
    r"rather than|instead of)\b", re.I)


def _drop_qualified(sentence: str, clauses: list[Clause]) -> list[Clause]:
    q = _QUALIFIED_FROM.search(sentence)
    if not q:
        return clauses
    cut = q.start()
    return [c for c in clauses if c.span[0] < cut]


def compile_sentence(sentence: str) -> list[Clause]:
    low = sentence.lower()
    found: list[Clause] = []
    for name, rx, fn in MATCHERS:
        for m in rx.finditer(low):
            try:
                found.extend(fn(m, sentence))
            except Exception as exc:                  # a bad matcher must not
                found.append(Clause("preconditions",                  # kill
                                    {"fact": None, "op": None,        # the run
                                     "value": None, "src": sentence,
                                     "error": "%s: %s" % (name, exc)},
                                    m.span(), name))
    return _dedupe(_drop_qualified(sentence, found))


@dataclass
class Result:
    slug: str
    ir: Optional[dict] = None
    status: str = "unknown"
    gates: dict = field(default_factory=dict)
    sentences: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """May a human be asked to sign this?

        Round-trip is deliberately NOT in this list. Gate 2's output IS the
        review -- the diff is the thing the human reads -- so requiring it to
        be green before a review could happen would mean only the documents
        that needed no review could be reviewed. What must be green first is
        everything a human cannot check by reading: the schema, the leg
        topology, the accounting that no sentence was silently dropped, and
        the absence of a missing guard or stop.
        """
        return (all(self.gates.get(g, {}).get("ok") for g in
                    ("schema", "coverage", "topology"))
                and not (self.ir or {}).get("safety_gaps"))

    @property
    def clean(self) -> bool:
        """Nothing at all left for a human to adjudicate."""
        return self.ok and bool(self.gates.get("round_trip", {}).get("ok"))


_SECTION_OF = {"entry_rules": ("preconditions", "accept"),
               "management_rules": ("management",),
               "exit_rules": ("exits",)}


def compile_doc(doc: dict, *, now: Optional[str] = None) -> Result:
    slug = doc.get("slug") or optbank.slugify(doc.get("name") or "unnamed")
    res = Result(slug=slug)
    legs = doc.get("legs") or []

    # ---- can this shape exist on this account at all? Answered BEFORE any
    # sentence is read, because a strategy that cannot be sent should not
    # consume review attention, and because "we compiled 231" would be a lie.
    tmpl, notes = opttemplates.match(doc)
    res.notes.extend(notes)
    if tmpl is None:
        res.status = "no_template"
        res.blocked.append("no structural template has this leg topology")
    else:
        t = opttemplates.get(tmpl)
        if t.order_path == "shares":
            res.blocked.append("needs a share leg: an equity order placed and "
                               "reconciled separately, a different order path")
        if t.order_path == "single":
            res.blocked.append("single-leg: optir requires 2-4 legs, because "
                               "the mleg path is the only one built")
    # The level is the HIGHER of what the document claims and what the legs
    # actually imply. Three documents in the bank claim level 3 for a shape
    # whose short leg has nothing behind it; believing the document would form
    # an intent that Alpaca answers with a 403, and a 403 is not a safety net.
    doc_lvl = doc.get("alpaca_level")
    leg_lvl = opttemplates.derive_level(
        [(str(l.get("right", "")).lower(), str(l.get("action", "")).lower(),
          int(l.get("ratio") or 1)) for l in legs]) if legs else 4
    if tmpl and opttemplates.get(tmpl).order_path == "shares":
        leg_lvl = 1               # the shares ARE the cover
    if tmpl and tmpl == "cash_secured_put":
        leg_lvl = 3               # cash secures it; see the template's note
    lvl = max(int(doc_lvl) if isinstance(doc_lvl, int) else 4, leg_lvl)
    if isinstance(doc_lvl, int) and leg_lvl > doc_lvl:
        res.notes.append("document claims level %d; its legs imply level %d"
                         % (doc_lvl, leg_lvl))
    if lvl > optir.ACCOUNT_LEVEL:
        res.blocked.append("needs Alpaca options level %d; this account is "
                           "level %d" % (lvl, optir.ACCOUNT_LEVEL))
    if not (optir.MIN_LEGS <= len(legs) <= optir.MAX_LEGS):
        res.blocked.append("%d legs; Alpaca's mleg order takes 2-4"
                           % len(legs))
    if optir.has_share_leg(legs):
        if not any("share leg" in b for b in res.blocked):
            res.blocked.append("has a share leg")

    # ---- read every sentence, whatever the verdict above. Coverage is
    # measured on all 231 so the backlog is real; only ARMING is gated.
    all_clauses: list[Clause] = []
    for key in ("entry_rules", "management_rules", "exit_rules"):
        for sentence in doc.get(key) or []:
            clauses = compile_sentence(sentence)
            allowed = _SECTION_OF[key]
            kept, misplaced = [], []
            for c in clauses:
                if key == "entry_rules":
                    # "close at 21 DTE" filed under entry_rules is a
                    # management rule in the wrong list. Keep it -- losing a
                    # stop to a filing error is exactly the failure this gate
                    # exists for -- and record that it moved.
                    kept.append(c)
                    if c.section not in allowed:
                        misplaced.append(c)
                elif c.section in allowed:
                    kept.append(c)
                else:
                    # An ENTRY predicate lifted out of an exit sentence is a
                    # re-reading of the same threshold ("delta >= 0.85" in a
                    # close rule is not an entry condition). Dropping it is
                    # not a silent drop: the typed exit rule beside it carries
                    # the same number, and gate 2 still sees the figure.
                    misplaced.append(c)
            rt = round_trip(sentence, kept)
            if kept:
                verdict = "compiled" if rt["ok"] else "partial"
            else:
                verdict = "unexpressed"
            reason, critical = ("", False)
            if verdict != "compiled":
                reason, critical = classify_unexpressed(sentence, key,
                                                        verdict)
            res.sentences.append({
                "section": key, "text": sentence, "verdict": verdict,
                "rendered": rt["rendered"],
                "missing_numbers": rt["missing_numbers"],
                "residual": rt["residual"],
                "or_risk": rt["or_risk"],
                "reason": reason, "safety_critical": critical,
                "matchers": sorted({c.matcher for c in kept}),
                "misplaced": [c.section for c in misplaced],
            })
            all_clauses.extend(kept)

    # ---- assemble the IR
    stamp = now or dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds")
    ir = optir.new_ir(
        slug, template=tmpl or "unknown",
        params=_params_from(all_clauses, tmpl),
        legs=[{"right": l.get("right"), "action": l.get("action"),
               "ratio": int(l.get("ratio") or 1)} for l in legs],
        alpaca_level=int(lvl),
        source_doc_sha=optir.doc_sha(doc), compiled_at=stamp)

    pre = [c.payload for c in all_clauses if c.section == "preconditions"]
    acc = [c.payload for c in all_clauses if c.section == "accept"]
    ir["preconditions"] = {"all": _uniq(pre)} if pre else None
    ir["accept"] = {"all": _uniq(acc)} if acc else None
    ir["management"] = _uniq([c.payload for c in all_clauses
                              if c.section == "management"])
    ir["exits"] = _uniq([c.payload for c in all_clauses
                         if c.section == "exits"])
    ir["blocked_on"] = list(res.blocked)
    ir["unexpressed"] = [
        {"text": s["text"], "reason": s["reason"],
         "safety_critical": s["safety_critical"], "section": s["section"],
         "partial": s["verdict"] == "partial",
         "left_over": s["residual"] or None,
         "missing_numbers": s["missing_numbers"] or None}
        for s in res.sentences if s["verdict"] != "compiled"]
    ir["coverage"] = _coverage(res.sentences)
    ir["limits"] = _limits(ir["management"])
    ir["safety_gaps"] = safety_gaps(ir, doc)

    # ---- the four gates
    res.gates["schema"] = _gate_schema(ir, doc)
    res.gates["round_trip"] = _gate_round_trip(res.sentences)
    res.gates["coverage"] = _gate_coverage(res.sentences, ir)
    res.gates["topology"] = _gate_topology(ir, doc, tmpl)

    res.ir = ir
    res.status = _status(res)
    return res


def _uniq(items: list[dict]) -> list[dict]:
    out, seen = [], set()
    for p in items:
        key = json.dumps({k: v for k, v in p.items() if k not in ("src",)},
                         sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _params_from(clauses: list[Clause], tmpl: Optional[str]) -> dict:
    """The numbers the builder needs, lifted out of the compiled clauses.

    Only ever a midpoint of a band the document itself stated. Nothing is
    invented: a shape whose parameter has no sentence gets no default here,
    and the builder's own default applies -- visibly, in the template, where
    it is one line to read rather than a number buried in a data file.
    """
    params: dict = {}
    for c in clauses:
        f = c.payload.get("fact")
        v = c.payload.get("value")
        if f == "short_delta_abs" and isinstance(v, list):
            params.setdefault("short_delta", round(sum(v) / 2.0, 4))
        elif f == "short_delta_abs" and isinstance(v, (int, float)):
            params.setdefault("short_delta", float(v))
        elif f == "dte" and isinstance(v, list):
            params.setdefault("dte", int(sum(v) / 2))
        elif f == "front_dte" and isinstance(v, list):
            params.setdefault("front_dte", int(sum(v) / 2))
        elif f == "back_dte" and isinstance(v, list):
            params.setdefault("back_dte", int(sum(v) / 2))
    return params


def _limits(manage: list[dict]) -> dict:
    for r in manage:
        if r.get("kind") == "max_units":
            return {"max_units_per_underlying": r.get("units"),
                    "src": r.get("src")}
    return {}


def _coverage(sentences: list[dict]) -> dict:
    out: dict = {}
    for key in ("entry_rules", "management_rules", "exit_rules"):
        rows = [s for s in sentences if s["section"] == key]
        out[key] = {
            "total": len(rows),
            "compiled": sum(1 for s in rows if s["verdict"] == "compiled"),
            "partial": sum(1 for s in rows if s["verdict"] == "partial"),
            "unexpressed": sum(1 for s in rows
                               if s["verdict"] == "unexpressed"),
        }
    out["safety_critical_uncompiled"] = sum(
        1 for s in sentences if s["safety_critical"])
    return out


# ------------------------------------------------------------- the gates
def _gate_schema(ir: dict, doc: dict) -> dict:
    errs = optir.validate(ir, doc)
    return {"ok": not errs, "errors": errs}


def _gate_round_trip(sentences: list[dict]) -> dict:
    bad = [s for s in sentences
           if s["verdict"] == "partial" or s["missing_numbers"]]
    return {
        "ok": not bad,
        "checked": sum(1 for s in sentences if s["verdict"] != "unexpressed"),
        "failures": [{"text": s["text"], "rendered": s["rendered"],
                      "missing_numbers": s["missing_numbers"],
                      "residual": s["residual"],
                      "or_risk": s.get("or_risk", "")} for s in bad],
    }


def _gate_coverage(sentences: list[dict], ir: dict) -> dict:
    """Gate 3. Passes when every sentence is accounted for -- compiled,
    partially compiled, or listed with a reason. It does NOT pass or fail on
    the safety gaps; those decide arming, which is a separate question from
    whether anything was silently dropped."""
    unaccounted = [s["text"] for s in sentences
                   if s["verdict"] != "compiled" and not s["reason"]]
    return {
        "ok": not unaccounted,
        "unaccounted": unaccounted,
        "safety_gaps": ir.get("safety_gaps") or [],
        "uncompiled_exits": [s["text"] for s in sentences
                             if s["safety_critical"]],
        "totals": ir["coverage"],
    }


def _gate_topology(ir: dict, doc: dict, tmpl: Optional[str]) -> dict:
    errs = list(optir.validate_against_doc(ir, doc))
    notes = []
    if tmpl:
        errs += opttemplates.check_match(tmpl, doc)
        n = opttemplates.level_note(tmpl, doc)
        if n:
            notes.append(n)
    else:
        errs.append("no template matched")
    return {"ok": not errs, "errors": errs, "notes": notes}


def _status(res: Result) -> str:
    """One word for the dashboard. The order matters: a level-4 strategy is
    reported as level-4 even if its sentences compiled perfectly, because the
    number the owner needs is how many will actually run."""
    # Order: the ORDER PATH first, then the leg count, then the level. A
    # covered call is blocked because shares are a different order path, and
    # reporting it as "needs level 4" would send someone to ask Alpaca for an
    # approval that would not help.
    for b in res.blocked:
        if "share leg" in b:
            return "blocked_share_leg"
    for b in res.blocked:
        if "single-leg" in b or "2-4" in b:
            return "blocked_leg_count"
    for b in res.blocked:
        if "level" in b:
            return "blocked_level_4"
    if res.status == "no_template":
        return "no_template"
    if not res.gates.get("topology", {}).get("ok"):
        return "topology_fail"
    if not res.gates.get("schema", {}).get("ok"):
        return "schema_fail"
    if res.ir and res.ir.get("safety_gaps"):
        return "partial_unsafe"
    if res.gates.get("round_trip", {}).get("ok") and \
            not (res.ir or {}).get("unexpressed"):
        return "clean"
    return "partial"


# ============================================================== reporting
# Fields that move on every run but say nothing about what the machine
# understood. compiled_at is a clock; reviewed_by/reviewed_at/status are a
# HUMAN's marks, and a recompile that changed nothing must not erase them.
_VOLATILE = ("compiled_at", "reviewed_by", "reviewed_at")


def _substance(ir: dict) -> str:
    """The part of an IR whose change should show up in `git diff`."""
    trimmed = dict(ir)
    trimmed.pop("status", None)
    prov = dict(trimmed.get("provenance") or {})
    for k in _VOLATILE:
        prov.pop(k, None)
    trimmed["provenance"] = prov
    return json.dumps(trimmed, sort_keys=True, ensure_ascii=False)


def save_stable(ir: dict, ir_dir: Optional[Path] = None) -> bool:
    """Write an IR only when its SUBSTANCE changed. Returns True if written.

    The trap this closes: optir.save() stamps a fresh compiled_at, so a plain
    re-run rewrote all 86 files and `git diff` showed 86 changed files with
    nothing in them. That is not cosmetic -- the entire reason this compiler
    is a deterministic matcher rather than an LLM (see the module docstring)
    is that the diff after a document edit shows exactly what the machine's
    understanding did. A diff that is always 86 files long carries no signal,
    and a reviewer stops reading it.

    Leaving the old file in place also PRESERVES A HUMAN'S SIGNATURE. If the
    substance is unchanged the review of it still stands. If anything did
    change -- including source_doc_sha, which covers the whole document --
    the new draft lands and the signature is gone, which is the correct way
    round: an edited document must be re-read before it can be armed again.
    """
    d = ir_dir or optir.IR_DIR
    path = d / ("%s.json" % ir["slug"])
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            old = None                      # unreadable: rewrite it
        if old is not None and _substance(old) == _substance(ir):
            return False
    optir.save(ir, ir_dir)
    return True


def compile_all(write: bool = True, ir_dir: Optional[Path] = None) -> dict:
    docs = []
    for path in sorted((optbank.BANK_DIR).glob("*.json")):
        docs.append(json.loads(path.read_text(encoding="utf-8")))
    results = [compile_doc(d) for d in docs]

    written = changed = 0
    if write:
        (ir_dir or optir.IR_DIR).mkdir(parents=True, exist_ok=True)
        for r in results:
            # Only IRs that are structurally valid reach options/ir/. A
            # directory of files that fail their own schema is a directory
            # nobody can trust, and the blocked ones are fully described in
            # the report instead.
            if r.gates["schema"]["ok"] and r.gates["topology"]["ok"]:
                written += 1
                if save_stable(r.ir, ir_dir):
                    changed += 1

    report = summarise(results)
    report["written"] = written
    report["changed"] = changed
    if write:
        optir.IR_DIR.mkdir(parents=True, exist_ok=True)
        # Same rule for the report: generated_at alone is not a change.
        new = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        old_txt = REPORT_PATH.read_text(encoding="utf-8") \
            if REPORT_PATH.exists() else ""
        if _report_substance(old_txt) != _report_substance(new):
            REPORT_PATH.write_text(new, encoding="utf-8")
    return report


def _report_substance(text: str) -> str:
    try:
        d = json.loads(text)
    except Exception:
        return text
    d.pop("generated_at", None)
    return json.dumps(d, sort_keys=True, ensure_ascii=False)


def summarise(results: list) -> dict:
    by_status: dict = {}
    for r in results:
        by_status.setdefault(r.status, []).append(r.slug)

    sent_tot = sent_ok = sent_part = sent_no = 0
    crit = 0
    reasons: dict = {}
    for r in results:
        for s in r.sentences:
            sent_tot += 1
            if s["verdict"] == "compiled":
                sent_ok += 1
            elif s["verdict"] == "partial":
                sent_part += 1
            else:
                sent_no += 1
            if s["safety_critical"]:
                crit += 1
            if s["verdict"] != "compiled" and s["reason"]:
                reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1

    # Two different questions, and conflating them is how "231 strategies"
    # became a number nobody could act on.
    #   SENDABLE  -- could this shape reach Alpaca on this account at all?
    #                A property of the legs, not of the prose.
    #   RUNNABLE  -- did its prose compile into something the machine can act
    #                on, with every remaining sentence named?
    sendable = [r.slug for r in results
                if r.status in ("clean", "partial", "partial_unsafe",
                                "topology_fail", "schema_fail")]
    runnable = [r.slug for r in results if r.ok]
    armable = [r.slug for r in results if r.clean]
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds"),
        "documents": len(results),
        "by_status": {k: sorted(v) for k, v in sorted(by_status.items())},
        "status_counts": {k: len(v) for k, v in sorted(by_status.items())},
        "sentences": {"total": sent_tot, "compiled": sent_ok,
                      "partial": sent_part, "unexpressed": sent_no,
                      "safety_critical_uncompiled": crit},
        "unexpressed_reasons": dict(sorted(reasons.items(),
                                           key=lambda kv: -kv[1])),
        "sendable": sorted(sendable),
        "runnable": sorted(runnable),
        "clean": sorted(armable),
        "per_document": [
            {"slug": r.slug, "status": r.status,
             "template": (r.ir or {}).get("construction", {}).get("template"),
             "level": (r.ir or {}).get("alpaca_level"),
             "blocked_on": r.blocked,
             "coverage": (r.ir or {}).get("coverage"),
             "gates": {k: v["ok"] for k, v in r.gates.items()},
             "gate_errors": {k: (v.get("errors") or v.get("failures") or [])
                             for k, v in r.gates.items() if not v["ok"]},
             "notes": r.notes}
            for r in sorted(results, key=lambda r: r.slug)],
    }


def print_report(rep: dict) -> None:
    print("=" * 74)
    print("COMPILE REPORT  %s" % rep["generated_at"])
    print("=" * 74)
    print("%d documents in options/bank/, %d IR files written to options/ir/"
          % (rep["documents"], rep.get("written", 0)))
    print("")
    print("STATUS                 COUNT   what it means")
    meaning = {
        "clean": "compiled, every sentence accounted, gates green",
        "partial": "compiled, but sentences remain unexpressed",
        "partial_unsafe": "an EXIT or STOP did not compile -- never arm",
        "blocked_level_4": "needs Alpaca level 4; cannot trade here",
        "blocked_share_leg": "needs a share leg; separate order path",
        "blocked_leg_count": "1 leg, or more than 4; mleg path only",
        "no_template": "no structural shape matches its legs",
        "topology_fail": "the template does not build the document's legs",
        "schema_fail": "the IR does not satisfy optir.validate",
    }
    for k, n in sorted(rep["status_counts"].items(), key=lambda kv: -kv[1]):
        print("  %-20s %4d   %s" % (k, n, meaning.get(k, "")))
    print("")
    s = rep["sentences"]
    print("SENTENCES  %d total: %d compiled, %d partial, %d unexpressed"
          % (s["total"], s["compiled"], s["partial"], s["unexpressed"]))
    print("           %d uncompiled sentences are SAFETY-CRITICAL"
          % s["safety_critical_uncompiled"])
    print("")
    print("WHY SENTENCES DID NOT COMPILE (top reasons)")
    for reason, n in list(rep["unexpressed_reasons"].items())[:12]:
        print("  %4d  %s" % (n, reason[:64]))
    print("")
    print("THE REAL NUMBERS")
    print("  %3d documents in the bank" % rep["documents"])
    print("  %3d could be SENT on this account at all (level <= 3, 2-4 "
          "option legs," % len(rep["sendable"]))
    print("      no share leg). Everything else is blocked by its own shape, "
          "not by us.")
    print("  %3d of those are REVIEWABLE: schema, topology and coverage are "
          "green, no" % len(rep["runnable"]))
    print("      missing guard or stop. A human reads the round-trip diff "
          "and signs.")
    print("  %3d of those are CLEAN: the diff has nothing in it to "
          "adjudicate either." % len(rep["clean"]))
    unsafe = rep["status_counts"].get("partial_unsafe", 0)
    print("  %3d are SENDABLE but an exit or stop did not compile. They may "
          "screen and" % unsafe)
    print("      display; optir.armable() refuses them. That gap is the "
          "backlog, and it is")
    print("      the honest reason the runnable number is not the sendable "
          "number.")


def show(res: Result, diff: bool = False) -> None:
    print("=" * 74)
    print("%s  ->  template %s   status %s"
          % (res.slug, (res.ir or {}).get("construction", {}).get("template"),
             res.status))
    print("=" * 74)
    for b in res.blocked:
        print("  BLOCKED: %s" % b)
    for n in res.notes:
        print("  note: %s" % n)
    for g, v in res.gates.items():
        mark = "ok  " if v["ok"] else "FAIL"
        print("  gate %-11s %s" % (g, mark))
        if not v["ok"]:
            for e in (v.get("errors") or [])[:6]:
                print("        %s" % e)
            for f in (v.get("failures") or [])[:6]:
                print("        sentence: %s" % f["text"][:70])
                print("        compiled: %s" % f["rendered"][:70])
                if f["missing_numbers"]:
                    print("        MISSING FIGURES: %s"
                          % ", ".join(f["missing_numbers"]))
                if f["residual"]:
                    print("        LEFT OVER: %s" % f["residual"][:70])
    if diff:
        print("")
        print("ROUND-TRIP DIFF -- read every pair. The compiler is trusted "
              "only as far as this reads true.")
        for s in res.sentences:
            flag = {"compiled": "  ", "partial": "~ ",
                    "unexpressed": "X "}[s["verdict"]]
            print("%s%s" % (flag, s["text"]))
            if s["verdict"] == "unexpressed":
                print("    -> NOT COMPILED: %s%s"
                      % (s["reason"], "  [SAFETY-CRITICAL]"
                         if s["safety_critical"] else ""))
            else:
                print("    -> %s" % s["rendered"])
                if s["missing_numbers"]:
                    print("       MISSING FIGURES: %s"
                          % ", ".join(s["missing_numbers"]))
                if s["residual"]:
                    print("       LEFT OVER: %s" % s["residual"])


# ==================================================================== cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--slug")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--review", action="store_true",
                    help="print the full round-trip diff for a human")
    ap.add_argument("--sign", metavar="REVIEWER",
                    help="record that a human read the diff and accepted it")
    ap.add_argument("--arm", action="store_true",
                    help="promote a reviewed IR to armed")
    ap.add_argument("--no-write", action="store_true")
    a = ap.parse_args(argv)

    if a.all:
        rep = compile_all(write=not a.no_write)
        print_report(rep)
        return 0

    if not a.slug:
        ap.error("--slug or --all")

    doc = optbank.load(a.slug)
    res = compile_doc(doc)
    show(res, diff=a.review or a.show)

    if a.sign:
        if not res.ok:
            print("\nREFUSED: gates are not green; nothing to sign.")
            return 2
        ir = res.ir
        ir["provenance"]["reviewed_by"] = a.sign
        ir["provenance"]["reviewed_at"] = dt.datetime.now(
            dt.timezone.utc).isoformat(timespec="seconds")
        ir["status"] = "reviewed"
        errs = optir.validate(ir, doc)
        if errs:
            print("\nREFUSED: %s" % errs[0])
            return 2
        print("\nsigned by %s -> %s" % (a.sign, optir.save(ir)))
        return 0

    if a.arm:
        ir = optir.load(a.slug)
        if ir.get("status") != "reviewed":
            print("REFUSED: status is %r; sign the round-trip diff first"
                  % ir.get("status"))
            return 2
        ir["status"] = "armed"
        errs = optir.validate(ir, doc)
        if errs:
            ir["status"] = "reviewed"
            print("REFUSED: %s" % errs[0])
            return 2
        print("armed -> %s" % optir.save(ir))
        return 0

    if not a.no_write and res.gates["schema"]["ok"] and \
            res.gates["topology"]["ok"]:
        print("\nwrote %s" % optir.save(res.ir))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
