#!/usr/bin/env python3
"""mockai.py -- fixtures for the page assistant, over the REAL assistant.py.

    "I want there to be an ai chatbot for each thing that builds for me ... it
     knows what page I am on for context"                         -- the owner

This file was written against a PROPOSED contract, because `/api/assistant/*`
did not exist when the harness work started. It does now (`assistant.py`, plus
three routes in app.py), so the proposal is gone and this runs the shipping
module -- the same discipline as mockperf.py and mockbank.py. The route
envelopes below are app.py's, key for key:

    GET  /api/assistant        -> {**assistant.status(ctx), account}
    POST /api/assistant/chat   -> assistant.chat(ctx, message=, page=, history=)
    POST /api/assistant/act    -> assistant.act(ctx, proposal_id=, by=)

`assistant.chat` is handed a `hub.Ctx`, which mockserver already builds over a
stub fleet, so every proposal, every diff and every refusal on screen is the
real code's -- including the account check, which takes the account from the
route and never from the body.

------------------------------------------------------------- THE ONE SEAM
A chat panel needs a MODEL, and a harness may not have one, may not spend on
one, and must be deterministic. So exactly two functions are swapped, both
named here, and nothing else:

    assistant.readiness   -> the scenario's answer, so both the "there is a
                             model" and the "there is none" screens can be
                             looked at on a machine where the real answer is
                             whatever it happens to be
    assistant._ask_model  -> a canned reply in the model's own JSON shape

THE MODEL IS STUBBED, THE MODULE IS NOT. `assistant.chat` treats a model reply
as untrusted input and puts every action in it through `_validate_call`,
`preview()` and `PENDING` before it becomes a proposal -- so a canned reply
exercises all of that for real. `assistantrefuse` below proposes two things
the assistant must never do, and the REFUSAL that comes back is the shipping
validator's, not a string typed in this file.

Anything that needs no model needs no seam at all: `/help` and every other
slash command are parsed inside assistant.py and run here untouched.

--------------------------------------------------------------- scenarios
  assistant        a model is reachable. The reply carries one READ and two
                   WRITE proposals, each with the real diff behind it, none of
                   them applied until /act is called with its id
  assistantoff     NO MODEL CONFIGURED. `chat` answers `source: "blocked"` and
                   says the commands still work -- the panel must stay usable
                   and must not read as broken. /help must still answer
  assistantslow    a 3-second answer, for the pending state
  assistantrefuse  the model proposes ARMING and a SIZING change. Both are
                   refused by assistant.py's own validator and the refusals
                   have to render beside the reply rather than vanishing
"""
from __future__ import annotations

import threading
from typing import Any, Optional

#: assistant.py is module state (PENDING, and the two functions swapped below)
#: and this server is threaded. Without the lock two requests for two scenarios
#: read each other's model, which would make a scenario switch look FLAKY
#: rather than wrong -- and flaky is the harder bug to chase. Same reasoning,
#: same shape, as mockserver's _Providers.
LOCK = threading.RLock()

SCENARIOS = ("assistant", "assistantoff", "assistantslow", "assistantrefuse")

READY = {"ready": True, "how": "api_key"}
NOT_READY = {
    "ready": False, "how": "none",
    "problem": "no model is configured: ANTHROPIC_API_KEY is not set in .env "
               "and the Claude Code CLI is not signed in here",
    "fix": "put ANTHROPIC_API_KEY in .env, or sign in to the Claude Code CLI "
           "and trust this folder",
}


def _canned(scenario: str, symbol: str) -> dict:
    """The model's reply, in the JSON shape assistant.py parses.

    `symbol` is the page's, so the answer is about where the owner is standing
    -- the panel's whole claim is that it knows the page, and a fixture that
    always said SPY could not show the claim being kept or broken.
    """
    sym = (symbol or "SPY").upper()
    if scenario == "assistantrefuse":
        return {
            "reply": "Here is what you asked for.",
            "actions": [
                # NOT A TOOL AT ALL. assistant.TOOLS is the whole surface and
                # nothing in it arms, so this is refused by name.
                {"tool": "arm_ticker", "args": {"symbol": sym}},
                # A REAL tool with a refused ARGUMENT: the sizing keys. The
                # refusal text that renders is assistant.py's own.
                {"tool": "change_setting",
                 "args": {"symbol": sym, "settings": {"shares_per_lot": 50}}},
                # and one that is allowed, so the panel has to render a
                # refusal and a proposal side by side
                {"tool": "change_setting",
                 "args": {"symbol": sym, "settings": {"take_profit": 0.18}}},
            ],
        }
    return {
        "reply": "You are on %s. Two changes, and nothing happens until you "
                 "confirm each one." % sym,
        "question": "",
        "actions": [
            {"tool": "list_attachments", "args": {"symbol": sym}},
            {"tool": "attach_strategy",
             "args": {"symbol": sym, "id": "preset:ladder_v3"}},
            {"tool": "change_setting",
             "args": {"symbol": sym, "settings": {"take_profit": 0.18}}},
        ],
    }


class _Model:
    """Swap `readiness` and `_ask_model` for the length of one request."""

    def __init__(self, scenario: str, symbol: str = "") -> None:
        self.scenario = scenario
        self.symbol = symbol

    def __enter__(self):
        import assistant
        LOCK.acquire()
        self.mod = assistant
        self.saved = (assistant.readiness, assistant._ask_model)
        ready = NOT_READY if self.scenario == "assistantoff" else READY
        said = _canned(self.scenario, self.symbol)
        assistant.readiness = lambda: dict(ready)
        assistant._ask_model = lambda prompt, timeout: (dict(said), "")
        return self

    def __exit__(self, *exc):
        self.mod.readiness, self.mod._ask_model = self.saved
        LOCK.release()
        return False


def _sym_of(body: dict) -> str:
    page = (body or {}).get("page")
    if not isinstance(page, dict):
        return ""
    return str(page.get("symbol") or "").upper()


# =================================================================== routes
def status(scenario: str, ctx: Any) -> dict:
    """app.py:assistant_status. `forced` is the harness saying it overrode the
    real readiness -- an unmarked override is how a reviewer comes to believe
    a machine has a key it does not have."""
    import assistant
    real = dict(assistant.readiness())
    with _Model(scenario, "") as m:
        out = {**m.mod.status(ctx), "account": ctx.account_id}
    out["mock"] = {"scenario": scenario, "forced": True, "real_model": real,
                   "note": "the MODEL is stubbed; assistant.py is not. Every "
                           "proposal, diff and refusal below is the shipping "
                           "module's."}
    return out


def chat(scenario: str, ctx: Any, body: dict) -> dict:
    import assistant
    with _Model(scenario, _sym_of(body)) as m:
        return m.mod.chat(ctx, message=str((body or {}).get("message") or ""),
                          page=(body or {}).get("page"),
                          history=(body or {}).get("history"))


def act(scenario: str, ctx: Any, body: dict) -> dict:
    """app.py:assistant_act. NOT stubbed: the proposal is executed for real
    against the stub fleet and mockbank's sandboxed stores, because "confirm
    actually does the thing" is the claim most worth testing and the one a
    canned answer would fake."""
    import assistant
    return assistant.act(ctx, proposal_id=str((body or {}).get("id") or ""),
                         by=str((body or {}).get("by") or "assistant"))


def reset(scenario: str = "") -> None:
    """Forget the pending proposals when the scenario changes.

    A proposal built against the last scenario's account is a proposal whose
    diff describes a book that is no longer there; `act` would refuse it on the
    account check, which is correct but reads as a broken button.
    """
    try:
        import assistant
        with LOCK:
            assistant.PENDING = assistant.Proposals()
    except Exception:
        pass
