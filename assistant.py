#!/usr/bin/env python3
"""
assistant.py -- the per-page assistant. It proposes, you confirm, it acts.

    "I want there to be an ai chatbot for each thing that builds for me ... it
     knows what page I am on for context and if im on the strategies builder I
     can tell it to build me a strategy and it builds it on the dashboard for
     me ... i also need a bot that can do anything for me so i can just ask it
     to and it does it."                                        -- the owner

============================ THE CONTRACT ==================================
Five rules. They are the design, not a disclaimer, and each one is pinned by
a check in test_assistant.py.

1. A MODEL REPLY IS DATA, NEVER AN INSTRUCTION. What comes back from the
   model is parsed as JSON and then measured against the schemas in TOOLS
   below. A tool this file does not define does not exist; an argument the
   schema does not name is refused by name rather than passed along. Nothing
   the model returns reaches a shell, a file path, a config key or an order
   body without going through `_validate_call` first.

2. NOTHING RUNS UNTIL THE PERSON CONFIRMS A DIFF. `chat()` NEVER executes a
   write. It returns PROPOSALS: each one a title, a reason, a row-by-row diff
   of what would change from what it is now, and the warnings that change
   carries. Only `act()` executes, only by the id of a proposal this process
   built, and it re-validates from scratch -- so a hand-edited request body is
   exactly as safe as a model-authored one.

3. ARMING, SIZING, CANCELLING AND ORDERING ARE OUT OF SCOPE ENTIRELY. There
   is no tool here that arms, flattens, panics, places, cancels or closes
   anything, and no import of `optexec`, `broker` or `optplaybook`. The
   settings a strategy exposes are split three ways below: FORBIDDEN_SETTINGS
   is refused with the reason named, CONSEQUENTIAL_SETTINGS is allowed but the
   preview says out loud what it will do to the orders already resting at
   Alpaca, and everything else is ordinary. `dry_run` is FORBIDDEN because
   `dry_run=False` IS armed -- that one key is the whole difference between a
   ladder that logs and a ladder that transmits.

4. EVERY EXECUTED ACTION IS AUDITED, with the assistant named as the actor.
   The row goes to `state/audit.jsonl` in agentctl's own shape and through
   agentctl's own path constant, so the dashboard's audit page and
   `agentctl audit` see it beside everything else an agent did. A refusal is
   audited too: "what did it refuse to do" is the more interesting question.

5. NO eval, NO exec, NO SHELL, and NO MODEL-WRITTEN PYTHON. `create_strategy`
   refuses `kind="code"`. A coded strategy is Python that the backtester later
   executes; saving one is one click away from running it, and a blob of
   source cannot be measured against a schema the way a document can. The
   builder page writes those, by hand, on purpose.

=============================== HOW IT WORKS ===============================
`chat(ctx, message=..., page=...)` does one of two things:

  * A message beginning with "/" is a COMMAND. It is parsed here, by this
    file, and never leaves the process. That is why the panel stays useful
    with no model configured at all: `/attach RAM preset:ladder_v3` and
    `/set RAM take_profit=0.15` build the same proposals the model would.
  * Anything else goes to the model through `aiwrite._ask` -- the SAME client
    the indicator builder and the scheduled agents use, resolving
    ANTHROPIC_API_KEY from .env and falling back to the Claude Code CLI.
    There is deliberately no second client and no new dependency here.

The model is given the page context, a FACTS block this file measured (the
tickers that exist, what is attached to them, the bank entries that match the
message, the ladder settings of the ticker on screen) and the tool catalogue.
It is given facts so it cannot invent an id; ids it invents are refused by
`bank.parse_id` anyway, but a refusal the person has to read is a worse
answer than a proposal that is right.

THE PAGE IS THE CONTEXT. "build me a strategy" on the Strategies page means a
bank entry; the same words on a ticker page mean a bank entry AND attaching it
to that ticker. ROOMS below is that map, and it is sent to the model as part
of the prompt rather than being guessed at in the JS.

WHAT THIS FILE OWNS: nothing. It writes through other people's audited entry
points -- `bank.save/copy/delete/attach/detach`, `bank.set_params`,
`hub.set_strategy`, `hub.add_ticker`, `btjobs.submit` -- and keeps only an
in-memory, per-account, expiring table of proposals that have not been
confirmed yet. Restarting the dashboard loses the pending proposals and
nothing else, which is the correct thing to lose.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent

#: How long a proposal the person has not confirmed stays applicable. A diff
#: is a statement about the world as it was when it was computed; confirming
#: an hour-old one would apply it against a world that has moved. `act()`
#: re-reads and re-validates anyway, but an expiry makes the staleness visible
#: instead of silently repairing it.
PROPOSAL_TTL_S = 900
#: Bounded so a chat loop cannot grow the process. Oldest out first.
MAX_PROPOSALS = 200

#: The model gets 90 s, not aiwrite's 180. A chat panel that sits for three
#: minutes reads as broken; the indicator builder is a form submission and can
#: afford the wait.
CHAT_TIMEOUT_S = 90

MAX_MESSAGE = 4000
MAX_REPLY = 4000
MAX_HISTORY = 8
#: The bank is 259 rows / ~257 KB unfiltered (measured). A prompt carrying all
#: of it costs tokens on every message and tells the model less, not more.
MAX_FACT_ENTRIES = 24


class Refusal(Exception):
    """Something the assistant will not do, with the reason in the message.

    Distinct from an error: a Refusal is an ANSWER. It is shown to the person
    as text and audited as a refusal, never logged as a failure.
    """


# ====================================================== settings, three tiers
# Rule 3 in the contract, as data. Every one of these is reachable from
# `change_setting` and from the `settings` of an `attach_strategy` call, so
# both go through `_check_ladder_settings` below and there is one list.
#
# The trap this closes: a "personal ladder preset" is just a settings bundle.
# Refusing `shares_per_lot` on change_setting and then letting a model save a
# preset carrying `shares_per_lot: 10000` and attach it would be the same act
# with one more step in front of it.
FORBIDDEN_SETTINGS: dict = {
    "dry_run": "arming. dry_run=False IS armed -- it is the one key between a "
               "ladder that logs and a ladder that transmits real orders.",
    "autostart": "arming. An armed ladder with autostart starts transmitting "
                 "the next time the dashboard boots, with nobody watching.",
    "shares_per_lot": "sizing. This is how many shares every order is for.",
    "lot_dollars": "sizing. This is the dollar size of every lot.",
    "min_shares": "sizing. It sets the floor on every order.",
    "max_shares": "sizing. It sets the ceiling on every order.",
    "size_mode": "sizing. It decides which of the size settings is used.",
    "risk_dollars": "sizing. It is the dollars a lot is allowed to risk.",
    "max_lots": "sizing. It is how many lots of capital the ladder may commit.",
    "n_target": "sizing. The exposure cap is spread over exactly this many rungs.",
    "f_ladder": "sizing. It is the share of account equity one ladder may hold.",
    "cap_at_rung": "sizing. It truncates the last lot against the exposure cap.",
    "fractional": "sizing. Turning it on makes a fraction of a share a legal "
                  "order quantity, which changes what every size key means.",
    "fractional_sessions": "order timing. It decides the hours in which a "
                           "fractional lot's exit may be placed at all.",
    "daily_loss_limit": "the brake. Widening a loss limit is a risk increase, "
                        "and it must be a person's decision, not a chat one.",
    "symbol": "identity. A ticker IS its symbol -- it keys the ledger, the "
              "config and every client_order_id. Add a ticker instead.",
    "preset": "the attach path. Putting a ladder bundle on a ticker is "
              "`attach_strategy`, so it goes through the bank and is audited "
              "as an attach rather than as an edit of one field.",
    "created": "bookkeeping. Nothing reads it but the record of when the "
               "ticker was made, and rewriting that is falsifying a log.",
}

#: Allowed, but the preview has to SAY what it does. These are the settings
#: whose change reaches orders that are already resting at the broker, or
#: that changes how an open position gets closed. `take_profit` is the
#: clearest case and it is also the single most obvious thing to ask for:
#: `Engine.update_config` cancels every resting take-profit and re-prices it
#: (and `ensure_tps` re-covers in the same call). That is correct behaviour
#: through the one audited entry point -- it is not something to hide.
CONSEQUENTIAL_SETTINGS: dict = {
    "take_profit": "every resting take-profit is cancelled and re-priced at "
                   "the new distance, then immediately re-covered.",
    "allow_extended_hours": "extended-hours eligibility is fixed when an order "
                            "is submitted, so every resting take-profit is "
                            "cancelled and re-placed.",
    "exit_mode": "this changes how an open lot exits (a resting limit versus "
                 "an armed trail).",
    "trail_amount": "this changes where an armed lot's trailing stop sits.",
    "trail_exit_offset": "this changes how far through the bid an armed lot sells.",
    "trail_use_broker_stop": "this decides whether the trailing stop lives at "
                             "Alpaca or only inside this process.",
    "reversal_mode": "'reverse' closes EVERY lot at the market on a 1-hour "
                     "trend flip and re-opens the ladder on the other side.",
    "side_mode": "this decides which side the ladder may take. A short ladder "
                 "needs margin and a borrow and has no stop loss.",
    "use_wind_down": "this turns the staged unwind on or off for open lots.",
    "strategy": "this replaces the ladder's entry and exit rules with an "
                "indicator document.",
    "strategy_entries": "this hands the entry decision to the strategy document.",
    "strategy_exits": "this hands the resting take-profit to the strategy document.",
}

#: A play's numbers are out of scope in full, and not because they are
#: dangerous one at a time. `contracts` and `strikes_below` each set the
#: dollars at risk per entry (10 contracts on a 2-wide spread is $2,000 of
#: max loss); `profit_pct` and `stop_pct` are the thresholds the playbook
#: turns into real closing orders; `target_dte`, `short_delta` and the entry
#: window choose which contract gets ordered. There is no subset of that list
#: which is not sizing or ordering, so the assistant attaches and detaches a
#: play and edits none of its numbers.
PLAY_SETTINGS_WHY = (
    "a tailored play's numbers are all sizing or ordering -- contracts and "
    "strikes_below set the dollars at risk, profit_pct and stop_pct become "
    "real closing orders, and target_dte/short_delta choose the contract that "
    "gets bought or sold. Edit them on the Options page, where the arm state "
    "is on screen beside them."
)


def _check_ladder_settings(settings: dict) -> list:
    """Refuse the forbidden keys by NAME; return the consequential warnings.

    Naming the key and its reason matters more than the refusal: "I cannot
    change that" teaches nothing, and the person then tries it four more ways.
    """
    bad = [(k, why) for k, why in
           ((k, FORBIDDEN_SETTINGS.get(k)) for k in settings) if why]
    if bad:
        raise Refusal(
            "out of scope: " + "; ".join("%s -- %s" % (k, w) for k, w in bad)
            + " Change it on the ticker's own settings pane, where the arm "
              "state is on screen beside it.")
    return ["%s: %s" % (k, CONSEQUENTIAL_SETTINGS[k]) for k in settings
            if k in CONSEQUENTIAL_SETTINGS]


# ======================================================== the rooms of the app
# What "build me a strategy" means depends on where it is asked. This map is
# sent to the model rather than being inferred in the JS, so the answer to
# "which page am I on" has one definition in one file.
ROOMS: dict = {
    "overview": {
        "label": "Portfolio",
        "about": "the account's money across every strategy, its history and "
                 "its performance metrics.",
        "natural": ["list_tickers", "list_attachments", "run_backtest"],
    },
    "ticker": {
        "label": "Ticker",
        "about": "ONE symbol: what is held, which strategies are attached, "
                 "its settings and its chart. `symbol` in the page context is "
                 "the one being looked at, so 'attach the v3 ladder' here "
                 "means attach it to THAT symbol without being told which.",
        "natural": ["get_settings", "change_setting", "attach_strategy",
                    "detach_strategy", "list_strategies", "run_backtest"],
    },
    "strategies": {
        "label": "Strategies",
        "about": "the ONE bank: ladder bundles, indicator documents, 231 "
                 "researched option structures and the tailored plays, "
                 "standard and personal together. 'build me a strategy' here "
                 "means create a personal bank entry.",
        "natural": ["list_strategies", "get_strategy", "create_strategy",
                    "edit_strategy", "copy_strategy", "delete_strategy",
                    "attach_strategy"],
    },
    "research": {
        "label": "Research",
        "about": "the strategy builder, the backtester, the risk profiles and "
                 "the risk bank. 'test that' here means run_backtest.",
        "natural": ["run_backtest", "list_strategies", "create_strategy",
                    "change_strategy_params"],
    },
    "options": {
        "label": "Options",
        "about": "the options board, the chain and the tailored plays. The "
                 "assistant may attach or detach an option structure or a "
                 "play; it may not touch a play's numbers and it may not arm.",
        "natural": ["list_strategies", "attach_strategy", "detach_strategy"],
    },
    "settings": {
        "label": "Settings",
        "about": "the account's own settings and the scheduled agents. The "
                 "assistant changes per-ticker strategy settings only.",
        "natural": ["get_settings", "change_setting"],
    },
    "scanner": {
        "label": "Scanner",
        "about": "finding symbols worth watching. 'watch that' means add_ticker.",
        "natural": ["add_ticker", "list_tickers"],
    },
    "risk": {
        "label": "Risk",
        "about": "live exposure across the account.",
        "natural": ["list_attachments", "get_settings"],
    },
    "add": {
        "label": "Add a ticker",
        "about": "putting a symbol on the watchlist. Adding attaches nothing.",
        "natural": ["add_ticker", "list_strategies", "attach_strategy"],
    },
    "addaccount": {
        "label": "Add an account",
        "about": "connecting another Alpaca key pair. The assistant does not "
                 "handle keys.",
        "natural": [],
    },
}
DEFAULT_ROOM = {"label": "Dashboard", "about": "the trading hub.", "natural": []}

#: Keys the JS may put in `page`. Anything else is dropped without comment --
#: the page context arrives from the browser and is therefore input, not fact.
PAGE_KEYS = ("view", "tab", "symbol", "strategy", "title", "onscreen")


def _clean_text(s: Any, cap: int) -> str:
    """Printable text, capped. Control characters out: a reply is rendered as
    text and a stray escape sequence is the model steering a terminal."""
    txt = str(s if s is not None else "")
    txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", txt)
    return txt[:cap].strip()


def sanitise_page(page: Any) -> dict:
    """The page context, whitelisted. `onscreen` is a flat dict of short
    strings so a view cannot post its whole state into a prompt."""
    src = page if isinstance(page, dict) else {}
    out: dict = {}
    for k in PAGE_KEYS:
        if k not in src:
            continue
        if k == "onscreen":
            row = src.get(k)
            if isinstance(row, dict):
                out[k] = {_clean_text(a, 40): _clean_text(b, 120)
                          for a, b in list(row.items())[:16]}
            continue
        out[k] = _clean_text(src.get(k), 60)
    if out.get("symbol"):
        out["symbol"] = out["symbol"].upper()
    out["view"] = out.get("view") or "overview"
    room = ROOMS.get(out["view"], DEFAULT_ROOM)
    out["room"] = room["label"]
    out["room_about"] = room["about"]
    return out


# ================================================================ the tool set
# Each tool is a NAMED FUNCTION with a schema. `kind` is "read" (runs at once,
# changes nothing, is not audited because nothing happened) or "write" (builds
# a proposal, is executed only by act(), and is always audited).
#
# `args` is {name: (type, required, description)}. The types are the four the
# JSON the model returns can carry, plus "dict" and "list". A value of the
# wrong type is coerced where that is unambiguous (the string "20" for an int)
# and refused where it is not.

TYPES = ("str", "int", "float", "bool", "dict", "list")


def _arg(kind: str, required: bool, desc: str) -> dict:
    if kind not in TYPES:
        raise ValueError("unknown arg type %r" % kind)
    return {"type": kind, "required": bool(required), "desc": desc}


def _coerce(name: str, spec: dict, value: Any) -> Any:
    t = spec["type"]
    if t == "str":
        return _clean_text(value, 4000)
    if t == "bool":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("true", "yes", "on", "1"):
            return True
        if s in ("false", "no", "off", "0"):
            return False
        raise Refusal("%s must be true or false, not %r" % (name, value))
    if t in ("int", "float"):
        try:
            n = float(value)
        except (TypeError, ValueError):
            raise Refusal("%s must be a number, not %r" % (name, value))
        return int(n) if t == "int" else n
    if t == "dict":
        if not isinstance(value, dict):
            raise Refusal("%s must be an object, not %r" % (name, value))
        return dict(value)
    if not isinstance(value, list):
        raise Refusal("%s must be a list, not %r" % (name, value))
    return list(value)


def _validate_call(tool: str, args: Any) -> tuple:
    """THE GATE. A model reply becomes a typed call here or it does not happen.

    Returns (spec, clean_args). Raises Refusal with the reason, which is shown
    to the person -- a silently dropped action is how a chat assistant comes
    to look like it lied about what it did.
    """
    name = _clean_text(tool, 60)
    spec = TOOLS.get(name)
    if spec is None:
        raise Refusal("there is no tool called %r. This assistant can do "
                      "exactly these: %s." % (name, ", ".join(sorted(TOOLS))))
    src = args if isinstance(args, dict) else {}
    unknown = [k for k in src if k not in spec["args"]]
    if unknown:
        raise Refusal("%s does not take %s. It takes: %s."
                      % (name, ", ".join(sorted(unknown)),
                         ", ".join(sorted(spec["args"])) or "nothing"))
    clean: dict = {}
    for key, aspec in spec["args"].items():
        if key not in src or src[key] is None:
            if aspec["required"]:
                raise Refusal("%s needs %s (%s)." % (name, key, aspec["desc"]))
            continue
        clean[key] = _coerce(key, aspec, src[key])
    return spec, clean


# ----------------------------------------------------------------- read tools
def _t_list_strategies(ctx: Any, a: dict) -> dict:
    import bank
    rows = bank.entries(ctx, kind=a.get("kind", ""), origin=a.get("origin", ""),
                        symbol=(a.get("symbol", "") or "").upper(),
                        q=a.get("q", ""),
                        attachable_only=bool(a.get("attachable", False)))
    limit = max(1, min(int(a.get("limit", 40) or 40), 200))
    return {"count": len(rows), "shown": min(limit, len(rows)),
            "entries": [_entry_brief(r) for r in rows[:limit]]}


def _entry_brief(row: dict) -> dict:
    """The columns a chooser needs. The full row carries params_schema and the
    document, which is 257 KB over the whole shelf and belongs in `get`."""
    return {"id": row.get("id"), "name": row.get("name"),
            "kind": row.get("kind"), "origin": row.get("origin"),
            "summary": _clean_text(row.get("summary"), 180),
            "tickers": row.get("tickers"),
            "attachable": bool((row.get("attach") or {}).get("ok")),
            "trades": bool((row.get("trades") or {}).get("ok")),
            "trades_why": (row.get("trades") or {}).get("why") or ""}


def _t_get_strategy(ctx: Any, a: dict) -> dict:
    import bank
    row = bank.get(a["id"], ctx)
    return {"entry": _entry_brief(row),
            "params_schema": row.get("params_schema"),
            "params_reason": row.get("params_reason"),
            "doc": row.get("doc")}


def _t_list_tickers(ctx: Any, a: dict) -> dict:
    import hub
    rows = hub.tickers(ctx)
    out = []
    for r in rows:
        out.append({"symbol": r.get("symbol"),
                    "asset_class": r.get("asset_class"),
                    "strategies": [s.get("id") for s in (r.get("strategies") or [])
                                   if isinstance(s, dict)]})
    return {"count": len(out), "tickers": out}


def _t_list_attachments(ctx: Any, a: dict) -> dict:
    import bank
    return {"attached": bank.attached(ctx, (a.get("symbol") or "").upper())}


def _t_get_settings(ctx: Any, a: dict) -> dict:
    sym = a["symbol"].upper()
    cfg = _ladder_cfg(ctx, sym)
    keys = [str(k) for k in (a.get("keys") or [])] or sorted(cfg)
    shown = {k: cfg[k] for k in keys if k in cfg}
    missing = [k for k in keys if k not in cfg]
    return {"symbol": sym, "settings": shown, "unknown_keys": missing,
            "forbidden": sorted(k for k in shown if k in FORBIDDEN_SETTINGS),
            "note": "Keys listed under `forbidden` are readable and NOT "
                    "changeable from here; see the catalogue for why."}


def _ladder_cfg(ctx: Any, sym: str) -> dict:
    """The ladder's live config for `sym`, or a Refusal that says which.

    Read through `status()` where it exists, because that is the shape every
    other reader uses; `cfg` is the fallback for a stub engine in a test.
    """
    engines = dict(getattr(ctx.fleet, "engines", None) or {})
    eng = engines.get(sym)
    if eng is None:
        raise Refusal(
            "%s has no ladder on this account, so it has no ladder settings. "
            "Attach a ladder strategy first (attach_strategy), or ask about a "
            "symbol in: %s." % (sym, ", ".join(sorted(engines)) or "none"))
    try:
        cfg = dict((eng.status() or {}).get("config") or {})
    except Exception:
        cfg = {}
    return cfg or dict(getattr(eng, "cfg", None) or {})


# ---------------------------------------------------------------- write tools
# Each is a pair: `preview(ctx, args) -> proposal body` and `run(ctx, args)`.
# The preview reads; the run writes, and only through somebody else's audited
# entry point.

def _diff(path: str, before: Any, after: Any, note: str = "") -> dict:
    return {"path": path, "from": before, "to": after, "note": note}


def _p_add_ticker(ctx: Any, a: dict) -> dict:
    import hub
    sym = a["symbol"].upper()
    known = {str(t.get("symbol")) for t in hub.tickers(ctx)}
    if sym in known:
        raise Refusal("%s is already on this account's watchlist." % sym)
    return {"title": "Watch %s" % sym,
            "why": "Adds %s to state/tickers.json. It attaches NOTHING: no "
                   "engine is built, no config is written and no order is "
                   "placed." % sym,
            "diff": [_diff("watchlist", "not watched", "watched")],
            "warnings": [], "store": "state/tickers.json"}


def _r_add_ticker(ctx: Any, a: dict, by: str) -> dict:
    import hub
    return hub.add_ticker(ctx, a["symbol"].upper(), by=by,
                          note=a.get("note", ""))


def _p_attach(ctx: Any, a: dict) -> dict:
    import bank
    sym = a["symbol"].upper()
    row = bank.get(a["id"], ctx)
    settings = dict(a.get("settings") or {})
    warn: list = []
    if row["store"] == "play" and settings:
        raise Refusal("%s is a tailored play and %s"
                      % (row["name"], PLAY_SETTINGS_WHY))
    if row["store"] == "preset" and settings:
        warn += _check_ladder_settings(settings)
    if row["store"] == "option" and settings:
        raise Refusal("a banked option structure is attached as it is written; "
                      "there are no per-ticker numbers to set on one yet.")
    if not row["attach"]["ok"]:
        raise Refusal("%s cannot go on a ticker: %s"
                      % (row["name"], row["attach"]["why"]))
    if not row["trades"]["ok"]:
        warn.append("nothing trades this yet -- %s" % row["trades"]["why"])

    before = bank.attached(ctx, sym)
    have = [r for r in before if r.get("id") == row["id"]]
    if have:
        raise Refusal("%s is already attached to %s." % (row["name"], sym))
    rows = [_diff("%s strategies" % sym,
                  ", ".join(r.get("id") or "" for r in before) or "none",
                  ", ".join([r.get("id") or "" for r in before] + [row["id"]]))]
    # A ticker has ONE engine config, so a second ladder bundle REPLACES the
    # first. bank.attach says so in its result; the person should see it
    # BEFORE confirming, not after.
    if row["kind"] == "ladder":
        prior = [r for r in before if r.get("kind") == "ladder"]
        if prior:
            warn.append("%s already carries %s, and a ticker has exactly one "
                        "engine config -- attaching this REPLACES it."
                        % (sym, prior[0].get("id")))
    for k, v in sorted(settings.items()):
        rows.append(_diff("%s.%s" % (sym, k), "(the strategy's own value)", v))
    return {"title": "Attach %s to %s" % (row["name"], sym),
            "why": "Puts %s (%s, %s) on %s. Attaching NEVER arms: a ladder "
                   "arrives stopped and in dry run, a play is assigned and the "
                   "arm file is untouched."
                   % (row["id"], row["kind"], row["origin"], sym),
            "diff": rows, "warnings": warn}


def _r_attach(ctx: Any, a: dict, by: str) -> dict:
    import bank
    return bank.attach(ctx, a["symbol"].upper(), a["id"],
                       dict(a.get("settings") or {}) or None, by=by)


def _p_detach(ctx: Any, a: dict) -> dict:
    import bank
    sym = a["symbol"].upper()
    row = bank.get(a["id"], ctx)
    before = bank.attached(ctx, sym)
    if not any(r.get("id") == row["id"] for r in before):
        raise Refusal("%s is not attached to %s. Attached: %s."
                      % (row["name"], sym,
                         ", ".join(r.get("id") or "" for r in before) or "nothing"))
    warn = ["Detaching does not sell anything. Open lots and the take-profits "
            "resting against them stay exactly as they are."]
    return {"title": "Detach %s from %s" % (row["name"], sym),
            "why": "Takes %s off %s. Nothing is sold and nothing is cancelled."
                   % (row["id"], sym),
            "diff": [_diff("%s strategies" % sym,
                           ", ".join(r.get("id") or "" for r in before),
                           ", ".join(r.get("id") or "" for r in before
                                     if r.get("id") != row["id"]) or "none")],
            "warnings": warn}


def _r_detach(ctx: Any, a: dict, by: str) -> dict:
    import bank
    return bank.detach(ctx, a["symbol"].upper(), a["id"], by=by)


def _p_change_setting(ctx: Any, a: dict) -> dict:
    sym = a["symbol"].upper()
    settings = dict(a["settings"])
    if not settings:
        raise Refusal("no settings were given to change.")
    # The forbidden keys are refused here; the consequential ones are dropped
    # from the warning list below because each one is already the NOTE on its
    # own diff row, and the same sentence twice on one card reads as two
    # different problems.
    _check_ladder_settings(settings)
    warn: list = []
    cfg = _ladder_cfg(ctx, sym)
    import engine as _eng
    rows, unknown, same = [], [], []
    for k, v in sorted(settings.items()):
        if k not in _eng.TICKER_DEFAULTS:
            unknown.append(k)
            continue
        old = cfg.get(k)
        if old == v:
            same.append(k)
            continue
        rows.append(_diff("%s.%s" % (sym, k), old, v,
                          CONSEQUENTIAL_SETTINGS.get(k, "")))
    if unknown:
        raise Refusal("%s is not a ladder setting. The ladder has %d settings; "
                      "ask for them with `/settings %s` or get_settings."
                      % (", ".join(sorted(unknown)), len(_eng.TICKER_DEFAULTS),
                         sym))
    if not rows:
        raise Refusal("nothing would change -- %s already %s."
                      % (", ".join(same), "have those values"
                         if len(same) > 1 else "has that value"))
    # How many resting exits this touches is knowable, so say the number
    # rather than "some". An open-lot count comes from the ledger, which is
    # ground truth above config for what the ladder owns.
    lots = _open_lots(ctx, sym)
    if lots and any(r["path"].rsplit(".", 1)[-1] in ("take_profit",
                                                     "allow_extended_hours")
                    for r in rows):
        warn.append("%s has %d open lot(s); their resting take-profits are "
                    "cancelled and re-placed by this change." % (sym, lots))
    if cfg.get("preset") not in ("custom", None, ""):
        warn.append("editing a setting by hand stamps %s as `custom`; it is on "
                    "preset `%s` now." % (sym, cfg.get("preset")))
    return {"title": "Change %d setting(s) on %s" % (len(rows), sym),
            "why": "Applied through hub.set_strategy -> Engine.update_config, "
                   "which is the one audited path every guard runs in.",
            "diff": rows, "warnings": warn}


def _open_lots(ctx: Any, sym: str) -> int:
    """Open lots from the engine's ledger, or 0 when it cannot be read.

    Deliberately forgiving: this decorates a warning. A preview that raises
    because a ledger was mid-write would block a legitimate change.
    """
    try:
        eng = dict(getattr(ctx.fleet, "engines", None) or {}).get(sym)
        return len(list(getattr(getattr(eng, "ledger", None), "open_lots", [])))
    except Exception:
        return 0


def _r_change_setting(ctx: Any, a: dict, by: str) -> dict:
    import hub
    sym = a["symbol"].upper()
    want = dict(a["settings"])
    _check_ladder_settings(want)                 # rule 2: re-validated on apply
    before = _ladder_cfg(ctx, sym)
    hub.set_strategy(ctx, sym, "ladder", action="configure", settings=want,
                     by=by)
    after = _ladder_cfg(ctx, sym)
    # THE TRAP. `Engine.update_config` returns the WHOLE config and swallows a
    # rejected value into a WARN event -- so a caller that trusts its return
    # reports a change that did not happen. Comparing intent against the after
    # snapshot is the only way this panel can say a setting did not take.
    landed = {k: after.get(k) for k in want if after.get(k) == want[k]}
    refused = {k: {"asked": want[k], "is": after.get(k)} for k in want
               if k not in landed}
    return {"symbol": sym, "changed": landed, "not_applied": refused,
            "before": {k: before.get(k) for k in want},
            # The keys are in `not_applied`; the note is the EXPLANATION only.
            # Listing them in both places made the panel print the same
            # sentence twice, which reads as two separate problems.
            "note": ("every key landed" if not refused else
                     "the engine kept the previous value and prints the "
                     "reason as a WARN event on the ticker.")}


CREATE_KINDS = ("ladder", "indicator", "option")
#: The document keys each kind may carry. A key outside its kind's list is
#: refused by name: bank.save would drop it silently, and a strategy that
#: quietly lost the rule the person asked for is worse than one that failed.
DOC_KEYS: dict = {
    "ladder": ("summary", "settings"),
    "indicator": ("note", "indicators", "entry", "exit", "target", "stop"),
    "option": ("summary", "legs", "bias", "net", "entry_rules",
               "management_rules", "exit_rules", "assignment_risk"),
}


def _check_doc(kind: str, doc: dict) -> list:
    """Kind-appropriate validation, and the warnings that survive it."""
    extra = sorted(k for k in doc if k not in DOC_KEYS[kind])
    if extra:
        raise Refusal("a %s strategy has no %s. It carries: %s."
                      % (kind, ", ".join(extra), ", ".join(DOC_KEYS[kind])))
    warn: list = []
    if kind == "ladder":
        s = doc.get("settings")
        if not isinstance(s, dict) or not s:
            raise Refusal("a ladder strategy is a bundle of engine settings; "
                          "`settings` is empty.")
        warn += _check_ladder_settings(s)
        import engine as _eng
        bad = sorted(k for k in s if k not in _eng.TICKER_DEFAULTS)
        if bad:
            raise Refusal("%s are not engine settings, so a ladder bundle "
                          "carrying them would never apply." % ", ".join(bad))
    if kind == "indicator":
        # strategy.validate is the builder's own validator and the one the
        # live engine's loader agrees with, so a document that passes here
        # passes there. Running it at PREVIEW time means the person confirms
        # something that will actually save.
        import strategy as _sdoc
        try:
            _sdoc.validate({"name": "preflight", **doc})
        except _sdoc.StrategyError as e:
            raise Refusal("that document does not validate: %s" % e)
    if kind == "option":
        legs = doc.get("legs")
        if not isinstance(legs, list) or not legs:
            raise Refusal("an option structure needs `legs`.")
        warn.append("nothing trades a banked option structure yet -- optengine "
                    "sends the tailored plays only. Attaching one records the "
                    "ticker's chosen options strategy; nothing is ordered.")
        # A short leg with no matching long is level 4 on a level-3 account.
        # bank's own _derive_level refuses it at save/attach; saying so here
        # turns a late refusal into an early one.
        rights = [str(l.get("right") or "").lower() for l in legs
                  if isinstance(l, dict)]
        shorts = [l for l in legs if isinstance(l, dict)
                  and str(l.get("side") or "").lower().startswith("s")]
        for l in shorts:
            r = str(l.get("right") or "").lower()
            if rights.count(r) < 2:
                warn.append("a short %s with no long %s of its own is an "
                            "uncovered short: Alpaca refuses it on this "
                            "level-3 account." % (r or "leg", r or "leg"))
                break
    return warn


def _p_create_strategy(ctx: Any, a: dict) -> dict:
    import bank
    kind = a["kind"].strip().lower()
    if kind == "code":
        raise Refusal(
            "the assistant does not write coded strategies. A coded strategy "
            "is Python the backtester executes, and a blob of source cannot be "
            "measured against a schema the way a document can -- rule 5 at the "
            "top of assistant.py. Write it in the Research builder, where you "
            "read it before it runs.")
    if kind not in CREATE_KINDS:
        raise Refusal("kind must be one of %s." % ", ".join(CREATE_KINDS))
    name = _clean_text(a["name"], 90)
    if not name:
        raise Refusal("a new strategy needs a name.")
    doc = dict(a.get("doc") or {})
    warn = _check_doc(kind, doc)
    clash = [r for r in bank.entries(ctx, kind=kind)
             if str(r.get("name", "")).strip().lower() == name.lower()]
    if clash:
        raise Refusal("there is already a %s strategy called %r (%s). Pick "
                      "another name, or edit that one."
                      % (kind, name, clash[0]["id"]))
    rows = [_diff("name", None, name), _diff("kind", None, kind)]
    for k in DOC_KEYS[kind]:
        if k in doc:
            rows.append(_diff(k, None, doc[k]))
    return {"title": "Create the %s strategy %r" % (kind, name),
            "why": "Saves a PERSONAL entry to the bank. It is attached to "
                   "nothing until you attach it, and nothing is armed.",
            "diff": rows, "warnings": warn}


def _r_create_strategy(ctx: Any, a: dict, by: str) -> dict:
    import bank
    kind = a["kind"].strip().lower()
    doc = dict(a.get("doc") or {})
    return {"entry": _entry_brief(bank.save(
        {"kind": kind, "name": _clean_text(a["name"], 90), **doc}, by=by))}


def _p_edit_strategy(ctx: Any, a: dict) -> dict:
    import bank
    row = bank.get(a["id"], ctx)
    if row["origin"] != "personal":
        raise Refusal("%s is a STANDARD strategy and is not editable -- the "
                      "standard shelf has to keep saying what it always said. "
                      "Copy it first (copy_strategy), then edit the copy."
                      % row["name"])
    if row["store"] == "code":
        raise Refusal("that is a coded strategy (Python). The assistant does "
                      "not edit source -- rule 5 at the top of assistant.py.")
    kind = row["kind"]
    if kind not in DOC_KEYS:
        raise Refusal("%s entries cannot be edited as documents." % kind)
    patch = dict(a["patch"])
    if not patch:
        raise Refusal("nothing to change.")
    name = _clean_text(patch.pop("name", ""), 90)
    warn = _check_doc(kind, patch) if patch else []
    doc = dict(row.get("doc") or {})
    rows = []
    if name and name != row["name"]:
        rows.append(_diff("name", row["name"], name))
    for k, v in sorted(patch.items()):
        if doc.get(k) != v:
            rows.append(_diff(k, doc.get(k), v))
    if not rows:
        raise Refusal("%s already says exactly that." % row["name"])
    return {"title": "Edit %s" % row["name"],
            "why": "Rewrites the personal document in place. Any ticker "
                   "carrying it picks the new version up; nothing is armed.",
            "diff": rows,
            "warnings": warn + ([
                "%s is attached to %s, so this changes how those trade."
                % (row["name"], ", ".join(row["tickers"]))] if row.get("tickers")
                else [])}


def _r_edit_strategy(ctx: Any, a: dict, by: str) -> dict:
    import bank
    row = bank.get(a["id"], ctx)
    patch = dict(a["patch"])
    name = _clean_text(patch.pop("name", ""), 90) or row["name"]
    doc = dict(row.get("doc") or {})
    doc.pop("name", None)
    merged = {k: v for k, v in doc.items() if k in DOC_KEYS[row["kind"]]}
    merged.update(patch)
    return {"entry": _entry_brief(bank.save(
        {"kind": row["kind"], "id": row["id"], "name": name, **merged}, by=by))}


def _p_copy_strategy(ctx: Any, a: dict) -> dict:
    import bank
    row = bank.get(a["id"], ctx)
    name = _clean_text(a.get("name", ""), 90) or ("%s (copy)" % row["name"])
    return {"title": "Copy %s as %r" % (row["name"], name),
            "why": "Duplicates it as a PERSONAL entry you can edit. The "
                   "original is untouched.",
            "diff": [_diff("new entry", None, name),
                     _diff("copied from", None, row["id"])],
            "warnings": []}


def _r_copy_strategy(ctx: Any, a: dict, by: str) -> dict:
    import bank
    return {"entry": _entry_brief(bank.copy(a["id"], a.get("name", ""), by=by))}


def _p_delete_strategy(ctx: Any, a: dict) -> dict:
    import bank
    row = bank.get(a["id"], ctx)
    if row["origin"] != "personal":
        raise Refusal("%s is a STANDARD strategy and cannot be deleted."
                      % row["name"])
    if row.get("tickers"):
        raise Refusal("%s is still attached to %s. Detach it first."
                      % (row["name"], ", ".join(row["tickers"])))
    return {"title": "Delete %s" % row["name"],
            "why": "Removes the personal document from the bank. This cannot "
                   "be undone from here.",
            "diff": [_diff(row["id"], row["name"], None)],
            "warnings": ["Deleting a strategy does not close anything it ever "
                         "opened, and the journal keeps its trades."]}


def _r_delete_strategy(ctx: Any, a: dict, by: str) -> dict:
    import bank
    return bank.delete(a["id"], ctx=ctx)


def _p_change_params(ctx: Any, a: dict) -> dict:
    import bank
    row = bank.get(a["id"], ctx)
    if row["store"] not in ("doc", "code"):
        raise Refusal("change_strategy_params turns the numbers on an "
                      "indicator strategy. For a ladder bundle use "
                      "change_setting on the ticker; for a play, see the "
                      "Options page.")
    knobs = {k["key"]: k for k in (row.get("params_schema") or [])}
    if not knobs:
        raise Refusal("%s declares no numbers to turn. %s"
                      % (row["name"], row.get("params_reason") or ""))
    patch = dict(a["params"])
    bad = sorted(k for k in patch if k not in knobs)
    if bad:
        raise Refusal("%s has no parameter %s. It has: %s."
                      % (row["name"], ", ".join(bad), ", ".join(sorted(knobs))))
    rows = []
    for k, v in sorted(patch.items()):
        old = knobs[k].get("default")
        if old != v:
            rows.append(_diff(k, old, v, knobs[k].get("label") or ""))
    if not rows:
        raise Refusal("%s already has those values." % row["name"])
    return {"title": "Turn %d number(s) on %s" % (len(rows), row["name"]),
            "why": "bank.set_params changes VALUES only -- it can never change "
                   "the shape of a strategy, and it refuses a path that is not "
                   "already a knob on it.",
            "diff": rows,
            "warnings": (["%s is attached to %s, so this changes how those "
                          "trade." % (row["name"], ", ".join(row["tickers"]))]
                         if row.get("tickers") else [])}


def _r_change_params(ctx: Any, a: dict, by: str) -> dict:
    import bank
    store, slug = bank.parse_id(a["id"])
    return bank.set_params(store, slug, dict(a["params"]))


def _p_run_backtest(ctx: Any, a: dict) -> dict:
    sym = a["symbol"].upper()
    days = int(a.get("days", 20) or 20)
    if not 1 <= days <= 365:
        raise Refusal("days must be between 1 and 365; %d was asked for." % days)
    tf = a.get("timeframe", "1Min") or "1Min"
    sweep = dict(a.get("sweep") or {})
    combos = 1
    for k, v in sweep.items():
        if not isinstance(v, list) or not v:
            raise Refusal("sweep[%s] must be a non-empty list of values." % k)
        combos *= len(v)
    rows = [_diff("symbol", None, sym), _diff("days", None, days),
            _diff("timeframe", None, tf)]
    if a.get("strategy"):
        rows.append(_diff("strategy", None, a["strategy"]))
    for k, v in sorted(sweep.items()):
        rows.append(_diff("sweep.%s" % k, None, v))
    warn = ["A backtest places no order and writes no trading state. It reads "
            "historical bars and burns CPU on this box."]
    if combos > 1:
        warn.append("%d combinations will be run." % combos)
    warn.append("Read total_pl, never net_profit alone: a ladder with no stop "
                "loss shows 100% winners because losers are never closed.")
    return {"title": "Backtest %s over %d day(s)" % (sym, days),
            "why": "Queues the job on btjobs and returns its id. Results "
                   "appear on the Research page.",
            "diff": rows, "warnings": warn, "reversible": True}


def _r_run_backtest(ctx: Any, a: dict, by: str) -> dict:
    import btjobs
    spec = {"symbol": a["symbol"].upper(),
            "timeframe": a.get("timeframe", "1Min") or "1Min",
            "days": int(a.get("days", 20) or 20),
            "label": _clean_text(a.get("label", ""), 60) or "assistant",
            "account": getattr(ctx.fleet, "account_id", "default")}
    if a.get("strategy"):
        spec["strategy"] = a["strategy"]
    if a.get("sweep"):
        spec["sweep"] = dict(a["sweep"])
    return {"job": btjobs.submit(ctx.fleet, spec)}


# ------------------------------------------------------------- the catalogue
TOOLS: dict = {
    # ---- read: run at once, change nothing, are not audited ---------------
    "list_strategies": {
        "kind": "read",
        "purpose": "List bank entries. The ONE shelf: ladder bundles, "
                   "indicator documents, option structures and tailored "
                   "plays, standard and personal.",
        "args": {"kind": _arg("str", False, "ladder | indicator | option | "
                                            "option-tailored"),
                 "origin": _arg("str", False, "standard | personal"),
                 "symbol": _arg("str", False, "only what is attached to this "
                                              "ticker"),
                 "q": _arg("str", False, "free text to match"),
                 "attachable": _arg("bool", False, "only entries that can go "
                                                   "on a ticker"),
                 "limit": _arg("int", False, "rows to return (default 40)")},
        "run": _t_list_strategies,
    },
    "get_strategy": {
        "kind": "read",
        "purpose": "One bank entry in full, with its document and its knobs.",
        "args": {"id": _arg("str", True, "a bank id such as preset:ladder_v3")},
        "run": _t_get_strategy,
    },
    "list_tickers": {
        "kind": "read",
        "purpose": "Every ticker on this account and what is attached to it.",
        "args": {},
        "run": _t_list_tickers,
    },
    "list_attachments": {
        "kind": "read",
        "purpose": "What is attached where, each row naming the store the fact "
                   "came from.",
        "args": {"symbol": _arg("str", False, "one ticker, or all of them")},
        "run": _t_list_attachments,
    },
    "get_settings": {
        "kind": "read",
        "purpose": "The live ladder settings of one ticker.",
        "args": {"symbol": _arg("str", True, "the ticker"),
                 "keys": _arg("list", False, "only these keys")},
        "run": _t_get_settings,
    },
    # ---- write: propose a diff, execute only on confirmation, audit -------
    "add_ticker": {
        "kind": "write",
        "purpose": "Put a symbol on the watchlist. Attaches nothing.",
        "args": {"symbol": _arg("str", True, "the symbol"),
                 "note": _arg("str", False, "why it is being watched")},
        "preview": _p_add_ticker, "run": _r_add_ticker,
    },
    "attach_strategy": {
        "kind": "write",
        "purpose": "Put a bank entry on a ticker. Several may be attached to "
                   "one ticker. Attaching NEVER arms.",
        "args": {"symbol": _arg("str", True, "the ticker"),
                 "id": _arg("str", True, "the bank id"),
                 "settings": _arg("dict", False, "per-ticker overrides "
                                                 "(ladder bundles only)")},
        "preview": _p_attach, "run": _r_attach,
    },
    "detach_strategy": {
        "kind": "write",
        "purpose": "Take a bank entry off a ticker. Sells nothing.",
        "args": {"symbol": _arg("str", True, "the ticker"),
                 "id": _arg("str", True, "the bank id")},
        "preview": _p_detach, "run": _r_detach,
    },
    "change_setting": {
        "kind": "write",
        "purpose": "Change ladder settings on one ticker. Sizing and arming "
                   "keys are refused.",
        "args": {"symbol": _arg("str", True, "the ticker"),
                 "settings": _arg("dict", True, "{key: value} of engine "
                                                "settings")},
        "preview": _p_change_setting, "run": _r_change_setting,
    },
    "create_strategy": {
        "kind": "write",
        "purpose": "Build a new PERSONAL bank entry: a ladder bundle, an "
                   "indicator document or an option structure.",
        "args": {"kind": _arg("str", True, "ladder | indicator | option"),
                 "name": _arg("str", True, "what to call it"),
                 "doc": _arg("dict", True, "the body for that kind")},
        "preview": _p_create_strategy, "run": _r_create_strategy,
    },
    "edit_strategy": {
        "kind": "write",
        "purpose": "Change a personal bank entry in place.",
        "args": {"id": _arg("str", True, "the bank id"),
                 "patch": _arg("dict", True, "the keys to change")},
        "preview": _p_edit_strategy, "run": _r_edit_strategy,
    },
    "copy_strategy": {
        "kind": "write",
        "purpose": "Duplicate any entry as a personal one you can edit.",
        "args": {"id": _arg("str", True, "the bank id"),
                 "name": _arg("str", False, "the copy's name")},
        "preview": _p_copy_strategy, "run": _r_copy_strategy,
    },
    "delete_strategy": {
        "kind": "write",
        "purpose": "Delete a personal bank entry. Refused while attached.",
        "args": {"id": _arg("str", True, "the bank id")},
        "preview": _p_delete_strategy, "run": _r_delete_strategy,
    },
    "change_strategy_params": {
        "kind": "write",
        "purpose": "Turn the numbers on an indicator strategy without "
                   "changing its shape.",
        "args": {"id": _arg("str", True, "the bank id"),
                 "params": _arg("dict", True, "{knob: value}")},
        "preview": _p_change_params, "run": _r_change_params,
    },
    "run_backtest": {
        "kind": "write",
        "purpose": "Queue a backtest or a sweep. Places no order.",
        "args": {"symbol": _arg("str", True, "the symbol"),
                 "days": _arg("int", False, "how many days back (default 20)"),
                 "timeframe": _arg("str", False, "1Min by default"),
                 "strategy": _arg("str", False, "a saved strategy slug"),
                 "label": _arg("str", False, "a label for the job"),
                 "sweep": _arg("dict", False, "{param: [values]}")},
        "preview": _p_run_backtest, "run": _r_run_backtest,
    },
}

#: What this assistant will never do, whatever it is asked. Published so the
#: panel can show it: a person trusts a tool that states its limits more than
#: one that refuses on the day.
NEVER: list = [
    "arm or disarm anything, on any account",
    "set a position size, a lot size, a contract count or an exposure cap",
    "place, cancel, close or flatten any order or position",
    "freeze or unfreeze, start, stop or panic a fleet",
    "write, save or run Python",
    "touch broker keys, or any account's credentials",
]


def catalogue() -> dict:
    """The tool set as data, for the panel, the prompt and the tests."""
    return {
        "tools": [{"name": n, "kind": s["kind"], "purpose": s["purpose"],
                   "args": {k: dict(v) for k, v in s["args"].items()}}
                  for n, s in sorted(TOOLS.items())],
        "never": list(NEVER),
        "forbidden_settings": dict(FORBIDDEN_SETTINGS),
        "consequential_settings": dict(CONSEQUENTIAL_SETTINGS),
        "rooms": {k: {"label": v["label"], "about": v["about"],
                      "natural": list(v["natural"])} for k, v in ROOMS.items()},
        "commands": [{"cmd": c, "help": h} for c, (h, _f) in sorted(
            COMMANDS.items())],
    }


# ============================================================ proposals table
class Proposals:
    """Pending proposals, in memory, per account, expiring.

    IN MEMORY ON PURPOSE. A proposal is an offer to change something; an offer
    that survives a restart is an offer nobody is watching any more. Losing
    them costs one re-ask and nothing else.
    """

    def __init__(self, ttl: float = PROPOSAL_TTL_S, cap: int = MAX_PROPOSALS):
        self._lock = threading.Lock()
        self._rows: dict = {}
        self.ttl = float(ttl)
        self.cap = int(cap)

    def put(self, account: str, body: dict) -> str:
        pid = secrets.token_hex(8)
        with self._lock:
            self._sweep()
            if len(self._rows) >= self.cap:
                oldest = min(self._rows, key=lambda k: self._rows[k]["at"])
                self._rows.pop(oldest, None)
            self._rows[pid] = {"account": str(account or ""), "at": time.time(),
                               "body": body}
        return pid

    def take(self, account: str, pid: str) -> dict:
        """Pop one, or raise. An id from another account is NOT FOUND, never
        borrowed -- two accounts' proposals must not be interchangeable."""
        with self._lock:
            self._sweep()
            row = self._rows.get(str(pid or ""))
            if row is None:
                raise Refusal(
                    "that proposal is gone. They expire after %d minutes, and "
                    "the dashboard restarting clears them. Ask again and "
                    "confirm the fresh one." % int(self.ttl // 60))
            if row["account"] != str(account or ""):
                raise Refusal("that proposal belongs to another account.")
            return self._rows.pop(str(pid))["body"]

    def _sweep(self) -> None:
        cut = time.time() - self.ttl
        for k in [k for k, v in self._rows.items() if v["at"] < cut]:
            self._rows.pop(k, None)

    def __len__(self) -> int:
        with self._lock:
            self._sweep()
            return len(self._rows)


PENDING = Proposals()


# ==================================================================== the audit
def _audit(ctx: Any, action: str, detail: dict, ok: bool = True,
           refused: str = "") -> None:
    """One row in state/audit.jsonl, agentctl's shape, actor "assistant".

    Through agentctl's own AUDIT_PATH so a move of that file lands here too.
    agentctl.audit() takes its account from a module global set by the CLI's
    env, which is the wrong account for an HTTP request, so the row is written
    here with the account the request is actually about.
    """
    try:
        import agentctl
        path = Path(agentctl.AUDIT_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "actor": "assistant",
            "account": str(getattr(ctx, "account_id", "") or "default"),
            "action": action,
            "ok": bool(ok),
            "detail": detail,
        }
        if refused:
            row["refused"] = refused
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
            fh.flush()
    except Exception:
        # An audit that cannot be written must not swallow the action's own
        # error, and must not stop a read. It is reported by the caller
        # instead: `audited` in the result is False when this fails.
        raise


def _try_audit(ctx: Any, action: str, detail: dict, ok: bool = True,
               refused: str = "") -> bool:
    try:
        _audit(ctx, action, detail, ok, refused)
        return True
    except Exception:
        return False


# ================================================================== commands
# The no-model path. Every one of these builds the SAME proposal the model
# would, through the same schema -- there is no second way in.
def _split(text: str) -> list:
    """Split on whitespace, honouring "double quotes" so a name can have a
    space in it. Deliberately not shlex: shlex's escaping rules are a surprise
    in a chat box and a backslash in a strategy name is not a thing."""
    out, cur, quoted = [], "", False
    for ch in text:
        if ch == '"':
            quoted = not quoted
            continue
        if ch.isspace() and not quoted:
            if cur:
                out.append(cur)
                cur = ""
            continue
        cur += ch
    if cur:
        out.append(cur)
    return out


def _kv(parts: list) -> dict:
    """key=value pairs -> a dict, with numbers and booleans typed.

    A settings value typed as a string where the engine wants a float is
    rejected by `update_config` without saying why, so the conversion happens
    here where the failure can still be explained.
    """
    out: dict = {}
    for p in parts:
        if "=" not in p:
            raise Refusal("%r is not key=value." % p)
        k, _, v = p.partition("=")
        k, v = k.strip(), v.strip()
        if not k:
            raise Refusal("%r has no key." % p)
        low = v.lower()
        if low in ("true", "on", "yes"):
            out[k] = True
        elif low in ("false", "off", "no"):
            out[k] = False
        else:
            try:
                out[k] = int(v) if re.fullmatch(r"-?\d+", v) else float(v)
            except ValueError:
                out[k] = v
    return out


def _c_help(ctx: Any, args: list, page: dict) -> dict:
    lines = ["Commands run without a model. Anything else is a question for "
             "the model."]
    for c, (h, _f) in sorted(COMMANDS.items()):
        lines.append("%-28s %s" % (c, h))
    lines.append("")
    lines.append("It will never: " + "; ".join(NEVER) + ".")
    return {"reply": "\n".join(lines), "calls": []}


def _c_strategies(ctx: Any, args: list, page: dict) -> dict:
    return {"reply": "", "calls": [("list_strategies",
                                    {"q": " ".join(args), "limit": 20})]}


def _c_tickers(ctx: Any, args: list, page: dict) -> dict:
    return {"reply": "", "calls": [("list_tickers", {})]}


def _c_settings(ctx: Any, args: list, page: dict) -> dict:
    sym = (args[0] if args else page.get("symbol") or "").upper()
    if not sym:
        raise Refusal("which ticker? `/settings RAM`, or open a ticker page.")
    return {"reply": "", "calls": [("get_settings", {"symbol": sym})]}


def _c_attached(ctx: Any, args: list, page: dict) -> dict:
    sym = (args[0] if args else page.get("symbol") or "").upper()
    return {"reply": "", "calls": [("list_attachments", {"symbol": sym})]}


def _c_attach(ctx: Any, args: list, page: dict) -> dict:
    sym, eid = _sym_and_id(args, page)
    return {"reply": "", "calls": [("attach_strategy",
                                    {"symbol": sym, "id": eid})]}


def _c_detach(ctx: Any, args: list, page: dict) -> dict:
    sym, eid = _sym_and_id(args, page)
    return {"reply": "", "calls": [("detach_strategy",
                                    {"symbol": sym, "id": eid})]}


def _sym_and_id(args: list, page: dict) -> tuple:
    """`SYM id` or, on a ticker page, just `id`."""
    if len(args) >= 2:
        return args[0].upper(), args[1]
    if len(args) == 1 and page.get("symbol"):
        return str(page["symbol"]).upper(), args[0]
    raise Refusal("say which ticker and which strategy: "
                  "`/attach RAM preset:ladder_v3`.")


def _c_set(ctx: Any, args: list, page: dict) -> dict:
    if args and "=" not in args[0]:
        sym, rest = args[0].upper(), args[1:]
    else:
        sym, rest = str(page.get("symbol") or "").upper(), args
    if not sym:
        raise Refusal("which ticker? `/set RAM take_profit=0.15`.")
    if not rest:
        raise Refusal("nothing to set. `/set %s take_profit=0.15`." % sym)
    return {"reply": "", "calls": [("change_setting",
                                    {"symbol": sym, "settings": _kv(rest)})]}


def _c_watch(ctx: Any, args: list, page: dict) -> dict:
    if not args:
        raise Refusal("which symbol? `/watch NVDA`.")
    return {"reply": "", "calls": [("add_ticker", {"symbol": args[0].upper()})]}


def _c_copy(ctx: Any, args: list, page: dict) -> dict:
    if not args:
        raise Refusal('which strategy? `/copy option:iron-condor "My condor"`.')
    return {"reply": "", "calls": [("copy_strategy",
                                    {"id": args[0],
                                     "name": " ".join(args[1:])})]}


def _c_backtest(ctx: Any, args: list, page: dict) -> dict:
    sym = (args[0].upper() if args and "=" not in args[0]
           else str(page.get("symbol") or "").upper())
    if not sym:
        raise Refusal("which symbol? `/backtest RAM days=20`.")
    rest = _kv([p for p in args if "=" in p])
    call = {"symbol": sym}
    for k in ("days", "timeframe", "strategy", "label"):
        if k in rest:
            call[k] = rest[k]
    return {"reply": "", "calls": [("run_backtest", call)]}


COMMANDS: dict = {
    "/help": ("what the assistant can do", _c_help),
    "/strategies [text]": ("search the bank", _c_strategies),
    "/tickers": ("every ticker and what is on it", _c_tickers),
    "/settings [SYM]": ("a ticker's ladder settings", _c_settings),
    "/attached [SYM]": ("what is attached where", _c_attached),
    "/attach [SYM] <id>": ("propose attaching a strategy", _c_attach),
    "/detach [SYM] <id>": ("propose detaching one", _c_detach),
    "/set [SYM] k=v ...": ("propose a settings change", _c_set),
    "/watch <SYM>": ("propose watching a symbol", _c_watch),
    "/copy <id> \"name\"": ("propose copying a strategy", _c_copy),
    "/backtest [SYM] k=v": ("propose a backtest", _c_backtest),
}
#: The first word of each key, so "/set" finds "/set [SYM] k=v ...".
_CMD_BY_WORD = {k.split()[0]: v for k, v in COMMANDS.items()}


def run_command(ctx: Any, text: str, page: dict) -> dict:
    word = _split(text)[0] if _split(text) else ""
    entry = _CMD_BY_WORD.get(word.lower())
    if entry is None:
        raise Refusal("there is no command %r. Try /help." % word)
    return entry[1](ctx, _split(text)[1:], page)


# ===================================================================== facts
def _facts(ctx: Any, message: str, page: dict) -> dict:
    """What the model is allowed to treat as true, measured here.

    A model that has to guess an id invents one. Every fact in here came from
    bank, hub or the engine in this request; nothing is remembered between
    messages, because a fact that is a turn old is an opinion.
    """
    out: dict = {"account": getattr(ctx, "label", "") or "default",
                 "tickers": [], "attached": [], "bank": [],
                 "settings": None, "problems": []}
    try:
        import hub
        out["tickers"] = [str(t.get("symbol")) for t in hub.tickers(ctx)][:60]
    except Exception as e:
        out["problems"].append("the ticker list could not be read: %r" % e)
    try:
        import bank
        sym = page.get("symbol") or ""
        out["attached"] = [
            {"symbol": r.get("symbol"), "id": r.get("id"),
             "name": r.get("name"), "kind": r.get("kind")}
            for r in bank.attached(ctx, sym)][:40]
        # The shelf is 259 rows unfiltered. Search it with the words of the
        # message so the prompt carries the dozen entries that could possibly
        # be meant, rather than all of them or none.
        rows = []
        seen = set()
        for term in _terms(message)[:4] + [""]:
            for r in bank.entries(ctx, q=term):
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                rows.append({"id": r["id"], "name": r["name"],
                             "kind": r["kind"], "origin": r["origin"]})
                if len(rows) >= MAX_FACT_ENTRIES:
                    break
            if len(rows) >= MAX_FACT_ENTRIES:
                break
        out["bank"] = rows
    except Exception as e:
        out["problems"].append("the bank could not be read: %r" % e)
    if page.get("symbol"):
        try:
            out["settings"] = _t_get_settings(ctx, {"symbol": page["symbol"]})
        except Refusal as e:
            out["settings"] = {"symbol": page["symbol"], "why": str(e)}
        except Exception as e:
            out["problems"].append("settings could not be read: %r" % e)
    return out


_STOP = {"the", "a", "an", "and", "or", "to", "on", "for", "me", "my", "i",
         "it", "this", "that", "with", "of", "in", "is", "please", "can",
         "you", "build", "make", "put", "set", "add", "want", "give"}


def _terms(message: str) -> list:
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", str(message or "").lower())
    return [w for w in dict.fromkeys(words) if w not in _STOP]


# ================================================================== the model
def readiness() -> dict:
    """Can this dashboard reach a model. aiwrite owns the answer; it resolves
    ANTHROPIC_API_KEY from .env and falls back to the Claude Code CLI, and
    there is deliberately no second client in this file."""
    import aiwrite
    return dict(aiwrite.readiness())


PROMPT = """\
You are the assistant inside a private trading dashboard. You answer in JSON
and nothing else.

You do not trade. You cannot arm anything, size anything, place, cancel or
close any order, and there is no tool here that does. Do not offer to. If the
person asks for one of those, say plainly that it is out of scope and that
they can do it themselves on the page, then help with the part that is in
scope.

Everything you propose is shown to the person as a DIFF and runs only if they
confirm it. So propose the whole change, not a first step, and say in `reply`
what it will do in one or two sentences.

=============================== THE ROOM ==================================
%(room)s

PAGE CONTEXT (what the person is looking at right now):
%(page)s

============================ WHAT IS TRUE ================================
These were measured just now. Use these ids; do not invent one.
%(facts)s

============================== YOUR TOOLS ================================
%(tools)s

Settings you may NOT change, with the reason:
%(forbidden)s

============================== THE HISTORY ===============================
%(history)s

=============================== THE ASK ==================================
%(message)s

============================== YOUR ANSWER ===============================
Return ONE JSON object and nothing else:

{
  "reply":   "one or two sentences to the person, plain text",
  "actions": [{"tool": "attach_strategy",
               "args": {"symbol": "RAM", "id": "preset:ladder_v3"}}],
  "question": ""
}

  * `actions` may be empty -- answering a question is a complete answer.
  * Put a request for missing information in `question` and leave `actions`
    empty rather than guessing a symbol or an id.
  * Never put anything in `reply` that is not true of the actions you listed.
"""


def _prompt(ctx: Any, message: str, page: dict, history: list) -> str:
    room = ROOMS.get(page.get("view", ""), DEFAULT_ROOM)
    cat = catalogue()
    tools = "\n".join(
        "%s(%s)  [%s]\n    %s" % (
            t["name"],
            ", ".join("%s: %s%s" % (k, v["type"], "" if v["required"] else "?")
                      for k, v in sorted(t["args"].items())),
            t["kind"], t["purpose"])
        for t in cat["tools"])
    forbidden = "\n".join("  %s -- %s" % (k, v)
                          for k, v in sorted(FORBIDDEN_SETTINGS.items()))
    hist = "\n".join("%s: %s" % (h["role"], h["text"]) for h in history) \
        or "(this is the first message)"
    return PROMPT % {
        "room": "%s -- %s\nTools that usually make sense here: %s"
                % (room["label"], room["about"],
                   ", ".join(room["natural"]) or "any of them"),
        "page": json.dumps(page, indent=1, default=str)[:1500],
        "facts": json.dumps(_facts(ctx, message, page), indent=1,
                            default=str)[:6000],
        "tools": tools,
        "forbidden": forbidden,
        "history": hist[:3000],
        "message": message,
    }


def _ask_model(prompt: str, timeout: int) -> tuple:
    """(reply_dict, error). aiwrite._ask is the shared client -- the SAME one
    the indicator builder and the scheduled agents use. Adding a second one
    would mean two places to configure a credential and two ways to fail."""
    import aiwrite
    text, err = aiwrite._ask(prompt, timeout)
    if err:
        return None, err
    spec = aiwrite._extract_json(text)
    if not spec:
        return None, ("the model did not return usable JSON. It said: %s"
                      % _clean_text(text, 400))
    return spec, ""


def _sanitise_history(history: Any) -> list:
    rows = []
    for h in (history if isinstance(history, list) else [])[-MAX_HISTORY:]:
        if not isinstance(h, dict):
            continue
        role = "assistant" if str(h.get("role")) == "assistant" else "user"
        text = _clean_text(h.get("text"), 600)
        if text:
            rows.append({"role": role, "text": text})
    return rows


# ==================================================================== chat()
def chat(ctx: Any, *, message: str, page: Any = None, history: Any = None,
         timeout: int = CHAT_TIMEOUT_S) -> dict:
    """One turn. NEVER executes a write -- it returns proposals.

    `source` says where the answer came from: "command" (parsed here, no model
    involved), "model", or "blocked" (no model is configured and the message
    was not a command).
    """
    msg = _clean_text(message, MAX_MESSAGE)
    pg = sanitise_page(page)
    hist = _sanitise_history(history)
    acct = str(getattr(ctx, "account_id", "") or "default")
    out: dict = {"ok": True, "source": "", "reply": "", "question": "",
                 "proposals": [], "refused": [], "reads": [], "page": pg,
                 "model": readiness(), "error": ""}
    if not msg:
        out["reply"] = ("Say what you want done, or type /help for the things "
                        "that work without a model.")
        out["source"] = "command"
        return out

    calls: list = []
    if msg.startswith("/"):
        out["source"] = "command"
        try:
            r = run_command(ctx, msg, pg)
        except Refusal as e:
            out["reply"] = str(e)
            return out
        out["reply"] = r.get("reply", "")
        calls = list(r.get("calls") or [])
    elif not out["model"].get("ready"):
        out["source"] = "blocked"
        out["reply"] = (
            "I cannot reach a model, so I cannot answer that in words -- but "
            "the commands still work with no model at all. Type /help.")
        return out
    else:
        out["source"] = "model"
        said, err = _ask_model(_prompt(ctx, msg, pg, hist), timeout)
        if err:
            out["ok"] = False
            out["error"] = err
            out["reply"] = ("The model did not answer: %s The commands still "
                            "work -- type /help." % err)
            return out
        # RULE 1. From here down `said` is untrusted input, exactly like a form
        # post. Nothing is read off it except through these three keys, and
        # every action it names goes through _validate_call below.
        out["reply"] = _clean_text(said.get("reply"), MAX_REPLY)
        out["question"] = _clean_text(said.get("question"), 500)
        actions = said.get("actions")
        for row in (actions if isinstance(actions, list) else [])[:6]:
            if not isinstance(row, dict):
                out["refused"].append({"tool": "?", "why": "an action that was "
                                                           "not an object"})
                continue
            calls.append((row.get("tool"), row.get("args")))

    for tool, args in calls:
        try:
            spec, clean = _validate_call(tool, args)
        except Refusal as e:
            out["refused"].append({"tool": _clean_text(tool, 60),
                                   "why": str(e)})
            _try_audit(ctx, "assistant_refused",
                       {"tool": _clean_text(tool, 60), "message": msg[:200]},
                       ok=False, refused=str(e))
            continue
        name = _clean_text(tool, 60)
        if spec["kind"] == "read":
            try:
                out["reads"].append({"tool": name, "args": clean,
                                     "result": spec["run"](ctx, clean)})
            except Refusal as e:
                out["refused"].append({"tool": name, "why": str(e)})
            except Exception as e:
                out["refused"].append({"tool": name,
                                       "why": "that read failed: %r" % e})
            continue
        try:
            body = spec["preview"](ctx, clean)
        except Refusal as e:
            out["refused"].append({"tool": name, "why": str(e)})
            _try_audit(ctx, "assistant_refused",
                       {"tool": name, "args": clean}, ok=False,
                       refused=str(e))
            continue
        except Exception as e:
            out["refused"].append({"tool": name,
                                   "why": "that could not be prepared: %r" % e})
            continue
        prop = {"tool": name, "args": clean,
                "title": body.get("title") or name,
                "why": body.get("why") or "",
                "diff": body.get("diff") or [],
                "warnings": body.get("warnings") or [],
                "reversible": bool(body.get("reversible", False)),
                "store": body.get("store") or ""}
        prop["id"] = PENDING.put(acct, prop)
        prop["expires_in_s"] = int(PROPOSAL_TTL_S)
        out["proposals"].append(prop)

    if not out["reply"] and out["proposals"]:
        out["reply"] = "Ready when you are -- look it over and confirm."
    if not out["reply"] and out["reads"]:
        out["reply"] = "Here is what I found."
    if not out["reply"] and out["refused"]:
        out["reply"] = "I could not do that."
    return out


# ===================================================================== act()
def act(ctx: Any, *, proposal_id: str, by: str = "assistant") -> dict:
    """Execute ONE proposal the person confirmed. Re-validated from scratch.

    RULE 2 lives here. `chat()` built the proposal and stored it server-side;
    only its id crosses the wire back, so the thing that runs is byte-identical
    to the diff that was on screen. It is then validated again -- a proposal is
    not a capability, it is a record of an offer.
    """
    acct = str(getattr(ctx, "account_id", "") or "default")
    prop = PENDING.take(acct, proposal_id)
    spec, clean = _validate_call(prop["tool"], prop["args"])
    if spec["kind"] != "write":
        raise Refusal("%s is a read, not an action." % prop["tool"])
    if clean != prop["args"]:
        # Cannot happen through this file, and that is the point of asserting
        # it: a schema that validates differently on the second pass would
        # mean the diff shown was not the call made.
        raise Refusal("that proposal no longer validates the same way. Ask "
                      "again so you can look at a fresh diff.")
    actor = _clean_text(by, 40) or "assistant"
    detail = {"tool": prop["tool"], "args": clean, "title": prop["title"],
              "diff": prop["diff"], "by": actor}
    try:
        result = spec["run"](ctx, clean, "assistant")
    except Refusal as e:
        _try_audit(ctx, "assistant_" + prop["tool"], detail, ok=False,
                   refused=str(e))
        raise
    except Exception as e:
        _try_audit(ctx, "assistant_" + prop["tool"], detail, ok=False,
                   refused="%s: %s" % (e.__class__.__name__, e))
        raise
    audited = _try_audit(ctx, "assistant_" + prop["tool"],
                         {**detail, "result": result}, ok=True)
    return {"ok": True, "tool": prop["tool"], "title": prop["title"],
            "diff": prop["diff"], "result": result, "audited": audited,
            "warning": ("" if audited else
                        "this ran but could NOT be written to "
                        "state/audit.jsonl -- check the disk")}


def status(ctx: Any = None) -> dict:
    """What the panel asks for on open: readiness, the catalogue, the rules."""
    r = readiness()
    return {"ok": True, "ready": bool(r.get("ready")), "model": r,
            "pending": len(PENDING), "ttl_s": PROPOSAL_TTL_S,
            **catalogue()}


if __name__ == "__main__":
    print(json.dumps({"readiness": readiness(),
                      "tools": [t["name"] for t in catalogue()["tools"]],
                      "never": NEVER}, indent=1))
