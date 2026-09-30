#!/usr/bin/env python3
"""test_bank.py -- the strategy bank: ONE shelf, read, built, tuned, attached.

Sections 1-8 prove the two things the settings panel rests on: that the
numbers it shows are the numbers the strategy actually runs with, and that
saving one can only ever change a value -- never the shape of the strategy,
never a file that would not compile, never a document the backtester would
refuse.

Sections 9-15 prove the shelf itself. The owner had four stores -- ladder
presets in Python, indicator documents, 231 researched option structures and
the two plays he specified himself -- and a ticker dropdown that read none of
them properly. They are one registry now with one id space, `origin` is a
column rather than four rooms, and ONE call attaches any of them to any
ticker, several at a time. Nothing here places an order, nothing here arms,
and no real store is written: the shelf and the fleet are both scratch.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

SCRATCH = Path(tempfile.mkdtemp(prefix="ta_bank_"))
os.environ["TICKAVERAGER_JOURNAL"] = str(SCRATCH / "journal.jsonl")

import btcode                                        # noqa: E402
import strategy as sdoc                              # noqa: E402

# a shelf of our own, so a test can never edit the real strategies
btcode.TEMPLATE_DIR = SCRATCH / "strategies" / "code"
sdoc.STRATEGY_DIR = SCRATCH / "strategies"
btcode.TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)

import bank                                          # noqa: E402

# The personal half of the ladder and option shelves is WRITTEN by save(), so
# it is redirected too. The standard halves (presets.PRESETS, options/bank's
# 231 documents, optplays.PLAYS) are read from the real repo on purpose --
# a unification nobody ran against the real four stores has not been tested.
bank.PERSONAL_DIR = SCRATCH / "personal"

import hub                                           # noqa: E402
import presets                                       # noqa: E402

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


CODE = '''"""Momentum pop -- buys strength, exits on the fade.

A longer note that should not appear as the card's one-line description.
"""
PARAMS = {"ema_n": 200, "ema_on": 1, "streak": 3, "tp": 0.25}


# --- the parameters this strategy actually won with ---
PARAMS.update({
    "ema_on": 0,
    "streak": 2,
})


def on_bar(ctx, i):
    e = ctx.indicator("ema", period=ctx.p.ema_n)
    if ctx.c[i] > e[i]:
        ctx.enter_long(shares=10)
'''

#: A document the OWNER built, so the shelf has a personal one to sort first.
MY_DOC = {
    "name": "My own idea",
    "note": "Something he clicked together himself.",
    "indicators": {"e": {"kind": "ema", "period": 20}},
    "entry": {"gt": ["close", "e.ema"]},
    "exit": {"target_reached": True},
    "target": {"points": 0.25},
}


#: What a research sweep leaves in strategies/code/.
SWEEP_CODE = '''"""A sweep wrote this."""
PARAMS = {"n": 5}
'''


class StubEngine:
    """One ticker's engine, with the ONE behaviour this test turns on:
    `update_config` DROPS a key it does not know, exactly as the real one does
    (`if k not in TICKER_DEFAULTS: continue`). That silent drop is the trap a
    strategy bank can walk a ticker into, so the stub keeps it and records
    every key it swallowed."""

    def __init__(self, sym, cfg):
        self.symbol, self.cfg = sym, dict(cfg)
        self.running = self.halted = False
        self.dropped = []

    def update_config(self, patch):
        import engine as _eng
        for k, v in dict(patch or {}).items():
            if k not in _eng.TICKER_DEFAULTS:
                self.dropped.append(k)
                continue
            self.cfg[k] = v
        return dict(self.cfg)

    def status(self):
        return {"symbol": self.symbol, "state": "idle"}


class StubFleet:
    """`fleet.add_ticker`'s contract, and nothing else. A real Fleet would want
    a broker; what is under test is the bank, not the ladder."""

    account_id, label = "test", "Test"

    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.engines, self.positions, self.account = {}, {}, {}
        self.snap_at = None

    def add_ticker(self, symbol, patch=None, copy_from=""):
        import engine as _eng
        sym = str(symbol).upper()
        base = {**_eng.TICKER_DEFAULTS, **presets.settings(presets.DEFAULT),
                "preset": presets.DEFAULT, "symbol": sym, "dry_run": True}
        e = self.engines[sym] = StubEngine(sym, base)
        if patch:
            e.update_config(patch)
        return e.status()

    def remove_ticker(self, symbol, force=False):
        return {"removed": bool(self.engines.pop(str(symbol).upper(), None))}


DOC = {
    "name": "RSI dip in an uptrend",
    "note": "Buys an oversold close that is still above its trend.",
    "indicators": {"r": {"kind": "rsi", "period": 14},
                   "e": {"kind": "ema", "period": 50},
                   "v": {"kind": "atr", "period": 14}},
    "entry": {"all": [{"lt": ["r.rsi", 30]},
                      {"gt": ["close", "e.ema"]}]},
    "exit": {"any": [{"gt": ["r.rsi", 65]}, {"target_reached": True}]},
    "target": {"atr_mult": 1.5, "indicator": "v.atr"},
    "stop": {"atr_mult": 2.5, "indicator": "v.atr"},
}


def main() -> int:
    btcode.save("momentum-pop", CODE)
    sdoc.save(dict(DOC))
    sdoc.save(dict(MY_DOC))
    # what a research sweep writes: `found-<n>-` is the repo's own marker
    btcode.save("found-9-probe", SWEEP_CODE)

    print("\n1. one shelf, both kinds, each with a line about it")
    rows = bank.listing()
    by = {r["slug"]: r for r in rows}
    check("both kinds listed", sorted(by),
          ["found-9-probe", "momentum-pop", "my-own-idea",
           "rsi-dip-in-an-uptrend"])
    check("a coded strategy's description is its FIRST docstring line",
          by["momentum-pop"]["note"], "Momentum pop -- buys strength, exits on the fade.")
    check("a document's description is its own note",
          by["rsi-dip-in-an-uptrend"]["note"], DOC["note"])
    check("kinds are labelled", (by["momentum-pop"]["kind"], by["rsi-dip-in-an-uptrend"]["kind"]),
          ("code", "doc"))
    check("each says how many knobs it has",
          (by["momentum-pop"]["tunable_count"] > 0, by["rsi-dip-in-an-uptrend"]["tunable_count"] > 0),
          (True, True))

    print("\n2. the panel shows what the strategy RUNS with, not what it first declared")
    # PARAMS says ema_on 1 / streak 3; the update below it says 0 and 2
    d = bank.detail("code", "momentum-pop")
    check("a later PARAMS.update wins, as Python would apply it",
          (d["params"]["ema_on"], d["params"]["streak"]), (0, 2))
    check("values only set once are still there",
          (d["params"]["ema_n"], d["params"]["tp"]), (200, 0.25))
    check("the source rides along for the View card", "def on_bar" in d["source"], True)
    check("and the functions it defines", d["functions"], ["on_bar"])

    print("\n3. a document's knobs: indicator periods, targets, and rule thresholds")
    t = {x["path"]: x for x in bank.tunables("doc", "rsi-dip-in-an-uptrend")}
    check("indicator periods", (t["indicators.r.period"]["value"], t["indicators.e.period"]["value"]),
          (14, 50))
    check("target and stop", (t["target.atr_mult"]["value"], t["stop.atr_mult"]["value"]), (1.5, 2.5))
    check("the 30 in 'r.rsi < 30' is a knob", t["entry.all.0.lt.1"]["value"], 30)
    check("...labelled so it reads as a rule", t["entry.all.0.lt.1"]["label"], "r.rsi < …")
    check("comparing two series gives nothing to turn",
          [k for k in t if k.startswith("entry.all.1")], [])
    check("a flag condition is a rule, not a knob",
          [k for k in t if "target_reached" in k], [])
    check("ints get an integer step", t["indicators.r.period"]["type"], "int")
    check("small floats get a fine one", t["target.atr_mult"]["step"], 0.01)

    print("\n4. saving turns the value where it is actually decided")
    bank.set_params("code", "momentum-pop", {"PARAMS.ema_on": 1, "PARAMS.ema_n": 150})
    src = btcode.load("momentum-pop")
    blocks = bank._param_blocks(src)
    check("ema_on was written to the update block that decides it",
          (blocks[0][1]["ema_on"], blocks[1][1]["ema_on"]), (1, 1))
    check("ema_n, set only in the base, was written there",
          blocks[0][1]["ema_n"], 150)
    check("the effective value is what was asked for",
          bank._code_params(src)["ema_on"], 1)
    check("the file still compiles", bank._compiles(src)[0], True)
    check("nothing outside PARAMS moved", "def on_bar(ctx, i):" in src and
          "Momentum pop -- buys strength" in src, True)
    # These files carry their reasoning in comments beside each parameter.
    # Rebuilding the dict from the parsed values would delete every one of
    # them, because comments are not in the syntax tree.
    check("the comment beside a parameter survives the edit",
          "# --- the parameters this strategy actually won with ---" in src, True)
    check("only the values changed, line for line",
          len([1 for a, b in zip(CODE.splitlines(), src.splitlines()) if a != b]), 2)

    print("\n5. a document saves through its own validator")
    bank.set_params("doc", "rsi-dip-in-an-uptrend",
                    {"indicators.r.period": 21, "entry.all.0.lt.1": 25})
    spec = sdoc.load("rsi-dip-in-an-uptrend")
    check("the period changed", spec["indicators"]["r"]["period"], 21)
    check("the threshold inside the rule changed", spec["entry"]["all"][0]["lt"][1], 25)
    check("the rule's shape did not", spec["entry"]["all"][0]["lt"][0], "r.rsi")
    check("the other condition is untouched", spec["entry"]["all"][1],
          {"gt": ["close", "e.ema"]})

    print("\n6. settings can never change a strategy's SHAPE")
    for patch, why in (
        ({"indicators.r.nonsense": 5}, "a parameter the indicator does not have"),
        ({"indicators.zzz.period": 5}, "an indicator that does not exist"),
        ({"name": "renamed"}, "a field that is not a knob"),
        ({"entry.all.0.lt.0": "close"}, "an operand, which is logic not a value"),
        ({}, "nothing at all"),
    ):
        try:
            bank.set_params("doc", "rsi-dip-in-an-uptrend", patch)
            check(f"refused: {why}", "saved", "refused")
        except bank.BankError:
            check(f"refused: {why}", "refused", "refused")
    check("...and the strategy is unchanged after every refusal",
          sdoc.load("rsi-dip-in-an-uptrend")["indicators"]["r"]["period"], 21)

    print("\n7. a broken write never reaches the shelf")
    good = btcode.load("momentum-pop")
    try:
        bank.set_params("code", "momentum-pop", {"PARAMS.tp": float("nan")})
    except bank.BankError:
        pass
    check("the file on disk still compiles", bank._compiles(btcode.load("momentum-pop"))[0], True)
    check("an unknown kind is refused",
          _raises(lambda: bank.detail("sql", "x"), bank.BankError), True)

    print("\n8. a strategy with no description says so rather than showing nothing")
    btcode.save("bare", "def on_bar(ctx, i):\n    pass\n")
    bare = {r["slug"]: r for r in bank.listing()}["bare"]
    check("no docstring -> empty note, not a crash", bare["note"], "")
    check("no PARAMS -> no knobs, and tuning it is refused",
          (bare["tunable_count"], _raises(
              lambda: bank.set_params("code", "bare", {"PARAMS.x": 1}), bank.BankError)),
          (0, True))


    print("\n9. ONE shelf over every store, with one id space")
    # The four stores the owner had, read through one call. presets, the 231
    # researched option structures and the two tailored plays are the REAL
    # ones; the documents and coded files are this test's own scratch shelf.
    rows = bank.entries()
    by_id = {r["id"]: r for r in rows}
    kinds = Counter(r["kind"] for r in rows)
    check("ids are unique across all four stores", len(by_id), len(rows))
    check("every id is <store>:<slug>",
          all(r["id"] == "%s:%s" % (r["store"], r["slug"]) for r in rows), True)
    check("no store failed to load",
          [r["id"] for r in rows if str(r["slug"]).startswith("store-unreadable")],
          [])
    check("the ladder presets are on it",
          [r["id"] for r in rows if r["kind"] == "ladder"],
          ["preset:basic", "preset:ladder_v3", "preset:ladder_v3_flatten"])
    check("all 231 researched option structures are on it",
          kinds["option"], 231)
    # EVERY tailored play is on the shelf, and this asserts CONTAINMENT rather
    # than an exact roster. Pinning the list meant the suite went red the day a
    # play was added -- which is the one thing this product exists to let the
    # owner do. It now checks the ones that must be there and that nothing
    # arrived without a usable shape.
    _tailored = sorted(r["id"] for r in rows if r["kind"] == "option-tailored")
    for _want in ("play:index-put-credit-spread", "play:swing-atm-hourly",
                  "play:wheel", "play:mabb"):
        check("%s is on the shelf" % _want, _want in _tailored, True)
    check("and every tailored row is usable: a name and editable params",
          all(r.get("name") and r.get("params_schema")
              for r in rows if r["kind"] == "option-tailored"), True)
    check("a document and a coded strategy are BOTH the indicator kind",
          (by_id["doc:rsi-dip-in-an-uptrend"]["kind"],
           by_id["code:momentum-pop"]["kind"]), ("indicator", "indicator"))
    check("every kind is one of the four", sorted(kinds), sorted(bank.BANK_KINDS))
    check("every row carries the whole envelope",
          all(set(r) >= {"id", "name", "kind", "origin", "summary",
                         "params_schema", "tickers", "attach", "trades"}
              for r in rows), True)
    check("`list` is the contract name for the same call",
          bank.list is bank.entries, True)
    # A module-level `list` shadows the builtin at CALL time for every function
    # in bank.py. The runtime isinstance checks go through `_LIST`, and this is
    # what proves it: the walker below hits `isinstance(node, list)` on the
    # rule tree, and a shadowed builtin would make it walk nothing.
    check("...and publishing it did not break the rule walker",
          len(bank.tunables("doc", "rsi-dip-in-an-uptrend")) > 4, True)

    print("\n10. standard versus personal is a FIELD, not four rooms")
    by_id = {r["id"]: r for r in bank.entries()}
    check("a document the repo ships is standard",
          by_id["doc:rsi-dip-in-an-uptrend"]["origin"], "standard")
    check("one the owner built is personal",
          by_id["doc:my-own-idea"]["origin"], "personal")
    # "SuperTrend + EMA stack" is `supertrend---ema-stack` on disk: strategy.py
    # keeps every dash it makes. A tidier slug computed here matched neither of
    # the two built-ins whose names carry punctuation, and served them as the
    # owner's own work.
    check("...including the built-ins whose names carry punctuation",
          (bank._doc_slug("SuperTrend + EMA stack"),
           bank._doc_slug("Ladder (current)")),
          ("supertrend---ema-stack", "ladder--current-"))
    check("a coded strategy a sweep wrote is standard",
          by_id["code:found-9-probe"]["origin"], "standard")
    check("one a person wrote is personal",
          by_id["code:momentum-pop"]["origin"], "personal")
    check("the researched option shelf is standard",
          {r["origin"] for r in rows if r["kind"] == "option"}, {"standard"})
    check("the owner's own plays are personal",
          {r["origin"] for r in rows if r["kind"] == "option-tailored"},
          {"personal"})
    inds = [r["origin"] for r in bank.entries(kind="indicator")]
    first_std = inds.index("standard")
    check("personal entries sort first within a kind, not into their own room",
          ("personal" in inds[:first_std], "personal" in inds[first_std:]),
          (True, False))
    check("...and the 231 standard structures still come back in the same call",
          len(bank.entries(kind="option")), 231)

    print("\n11. an entry that cannot be traded or attached says WHY")
    rows = {r["id"]: r for r in bank.entries()}
    code_row = rows["code:momentum-pop"]
    check("a coded strategy cannot be attached", code_row["attach"]["ok"], False)
    check("...and the reason names the backtester",
          "BACKTESTER" in code_row["attach"]["why"], True)
    lvl4 = [r for r in rows.values()
            if r["kind"] == "option" and not r["attach"]["ok"]]
    check("the level-4 structures are still LISTED, and refused",
          (len(lvl4) > 0, all("level 4" in r["attach"]["why"] for r in lvl4)),
          (True, True))
    check("no option structure claims anything will trade it",
          {r["trades"]["ok"] for r in rows.values() if r["kind"] == "option"},
          {False})
    check("...with the reason on the row, not implied by a blank",
          all("optengine trades the tailored plays only"
              in r["trades"]["why"]
              for r in rows.values() if r["kind"] == "option"), True)
    check("a tailored play IS traded", rows["play:index-put-credit-spread"]
          ["trades"]["ok"], True)
    # An EMPTY knob list is a measurement too: the 231 documents state their
    # rules as prose, and "0 knobs" with no reason leaves the reader guessing
    # whether the strategy has none or whether nobody read them.
    ic = rows["option:iron-condor"]
    check("a structure with no numbers to turn says WHY it has none",
          (ic["params_schema"], "PROSE" in ic["params_reason"]), ([], True))
    check("...and one with knobs carries no excuse",
          rows["play:index-put-credit-spread"]["params_reason"], "")

    print("\n12. building: a personal entry can be created, edited and deleted")
    made = bank.save({"kind": "ladder", "name": "Tight $0.05 ladder",
                      "summary": "half the rung, half the target",
                      "settings": {**presets.settings("basic"),
                                   "add_distance": 0.05, "take_profit": 0.05}},
                     by="test")
    check("a ladder strategy of his own is on the shelf",
          (made["id"], made["kind"], made["origin"]),
          ("preset:tight-0-05-ladder", "ladder", "personal"))
    check("...with a schema the settings pane can render",
          {p["key"] for p in made["params_schema"]} >= {"add_distance",
                                                        "take_profit"}, True)
    check("...and the numbers it was given",
          bank.get(made["id"])["doc"]["settings"]["add_distance"], 0.05)
    check("saving it again edits it in place rather than forking it",
          (bank.save({"id": made["id"], "kind": "ladder",
                      "name": "Tight $0.05 ladder",
                      "settings": {"take_profit": 0.07}})["id"],
           len([r for r in bank.entries() if r["kind"] == "ladder"])),
          (made["id"], 4))
    check("a setting the engine does not have is REFUSED, not dropped silently",
          _raises(lambda: bank.save({"kind": "ladder", "name": "typo",
                                     "settings": {"take_proft": 0.1}}),
                  bank.BankError), True)
    opt = bank.save({"kind": "option", "name": "My put spread",
                     "summary": "the one I actually trade",
                     "legs": [{"right": "put", "action": "sell", "ratio": 1},
                              {"right": "put", "action": "buy", "ratio": 1}],
                     "bias": "bullish", "net": "credit",
                     "entry_rules": ["sell the 0.20 delta"],
                     "management_rules": ["take 50%"],
                     "exit_rules": ["close by 14:30 on expiry"],
                     "assignment_risk": "the short put can be assigned"})
    check("an option structure of his own sits with the researched ones",
          (opt["kind"], opt["origin"], opt["attach"]["ok"]),
          ("option", "personal", True))
    check("a structure this account cannot send is saved and refused, not hidden",
          bank.save({"kind": "option", "name": "Naked call",
                     "summary": "level 4", "legs": [{"right": "call",
                                                     "action": "sell",
                                                     "ratio": 1}],
                     "bias": "bearish", "net": "credit",
                     "entry_rules": ["x"], "management_rules": ["x"],
                     "exit_rules": ["x"], "assignment_risk": "unbounded"}
                    )["attach"]["ok"], False)
    # optbank.permitted() reads `alpaca_level` off the document -- a field the
    # RESEARCH wrote into all 231 of them, and that a hand-built structure
    # arrives without. Without a floor derived from the legs, an uncovered
    # short call would be judged permitted on a level-3 account.
    check("...because a naked short is judged level 4 when nobody said so",
          bank.get("option:naked-call")["doc"]["alpaca_level"], 4)
    check("a short leg the same right buys back is defined risk, not naked",
          bank.get(opt["id"])["doc"]["alpaca_level"], 3)
    check("an option structure with no legs is refused",
          _raises(lambda: bank.save({"kind": "option", "name": "empty",
                                     "summary": "s", "legs": [],
                                     "bias": "neutral", "net": "credit",
                                     "entry_rules": ["x"],
                                     "management_rules": ["x"],
                                     "exit_rules": ["x"],
                                     "assignment_risk": "x"}),
                  bank.BankError), True)
    cop = bank.copy("doc:rsi-dip-in-an-uptrend", "RSI dip, my numbers")
    check("a standard strategy is copied to make it yours",
          (cop["origin"], cop["id"]), ("personal", "doc:rsi-dip--my-numbers"))
    check("...and the copy is a real strategy, not a stub",
          bank.get(cop["id"])["doc"]["indicators"]["r"]["period"], 21)
    check("a STANDARD entry is never overwritten in place",
          _raises(lambda: bank.save({"id": "preset:basic", "kind": "ladder",
                                     "name": "Basic",
                                     "settings": {"take_profit": 9.0}}),
                  bank.BankError), True)
    check("...nor deleted",
          _raises(lambda: bank.delete("preset:basic"), bank.BankError), True)
    check("...and it still says exactly what it always said",
          presets.settings("basic")["take_profit"], 0.10)
    check("a personal entry deletes",
          (bank.delete(cop["id"])["deleted"], cop["id"] in
           {r["id"] for r in bank.entries()}), (cop["id"], False))
    check("a delete with no account context says nobody checked who was using it",
          bank.delete(opt["id"])["attachments_checked"], False)

    print("\n13. attaching: one call, every kind, several per ticker")
    fleet = StubFleet(SCRATCH / "fleet")
    ctx = hub.Ctx(fleet, option_positions=[])
    r = bank.attach(ctx, "ram", "preset:ladder_v3", by="test")
    eng = fleet.engines["RAM"]
    check("a ladder preset reaches the ticker's own engine config",
          (eng.cfg["preset"], eng.cfg["first_entry"]), ("ladder_v3", "with_trend"))
    check("...through update_config, so nothing was dropped", eng.dropped, [])
    check("attaching never arms", (r["armed"], eng.cfg["dry_run"]), (False, True))
    r2 = bank.attach(ctx, "RAM", "preset:basic")
    check("a ticker has ONE engine config, so the second ladder REPLACES the "
          "first -- and says which", r2["replaced"], "preset:ladder_v3")
    r3 = bank.attach(ctx, "RAM", "doc:rsi-dip-in-an-uptrend")
    check("an indicator document goes on the SAME ticker at the same time",
          (eng.cfg["strategy"], eng.cfg["strategy_entries"]),
          ("rsi-dip-in-an-uptrend", True))
    check("...and it did not need a new ladder", r3["created_ladder"], False)
    bank.attach(ctx, "SPY", "play:index-put-credit-spread", {"contracts": 3})
    bank.attach(ctx, "SPY", "option:bear-call-diagonal")
    spy = bank.attached(ctx, "SPY")
    check("an option STRUCTURE attaches exactly like a play does",
          sorted(a["id"] for a in spy),
          ["option:bear-call-diagonal", "play:index-put-credit-spread"])
    check("...and each row names the store the fact came from",
          sorted(a["source"] for a in spy),
          ["bank_attachments.json", "state/options/plays.json"])
    check("a play's own numbers went to optplays, not to a second store",
          json.loads((SCRATCH / "fleet" / "options" / "plays.json")
                     .read_text())["assignments"][0]["params"]["contracts"], 3)
    ram = bank.attached(ctx, "RAM")
    check("RAM carries two strategies at once",
          sorted(a["id"] for a in ram),
          ["doc:rsi-dip-in-an-uptrend", "preset:basic"])
    shelf = {r["id"]: r for r in bank.entries(ctx)}
    check("the shelf now says which tickers each entry is on",
          (shelf["preset:basic"]["tickers"],
           shelf["play:index-put-credit-spread"]["tickers"],
           shelf["option:bear-call-diagonal"]["tickers"]),
          (["RAM"], ["SPY"], ["SPY"]))
    check("without an account, `tickers` is None and says nobody looked",
          (bank.entries()[0]["tickers"],
           bool(bank.entries()[0]["tickers_reason"])), (None, True))
    bank.attach(ctx, "ZZZ", made["id"])
    check("a personal entry cannot be deleted while a ticker is using it",
          _raises(lambda: bank.delete(made["id"], ctx=ctx), bank.BankError), True)
    bank.detach(ctx, "ZZZ", made["id"])
    check("...and it deletes once nothing is using it",
          bank.delete(made["id"], ctx=ctx)["deleted"], made["id"])
    bank.detach(ctx, "RAM", "doc:rsi-dip-in-an-uptrend")
    check("detaching a document leaves the ladder running on its own rules",
          (eng.cfg["strategy"], "RAM" in fleet.engines), ("", True))
    bank.detach(ctx, "SPY", "option:bear-call-diagonal")
    check("detaching a structure removes it and nothing else",
          [a["id"] for a in bank.attached(ctx, "SPY")],
          ["play:index-put-credit-spread"])
    bank.detach(ctx, "RAM", "preset:basic")
    check("detaching the ladder strategy removes the ladder",
          "RAM" in fleet.engines, False)

    print("\n14. when the ticker and the bank disagree, the row says so")
    fleet.add_ticker("XYZ")
    fleet.engines["XYZ"].cfg["strategy"] = "a-document-that-was-deleted"
    row = [a for a in bank.attached(ctx, "XYZ")
           if a["id"] == "doc:a-document-that-was-deleted"][0]
    check("a ticker naming a strategy that is not on the shelf is flagged",
          (row["kind"], "disagree" in row["why"]), (None, True))
    check("...and nothing claims it trades", row["trades"]["ok"], False)
    fleet.engines["XYZ"].cfg["strategy"] = ""
    fleet.engines["XYZ"].cfg["preset"] = "custom"
    row = [a for a in bank.attached(ctx, "XYZ") if a["kind"] == "ladder"][0]
    check("a hand-edited ladder is shown as itself, not as 'no strategy'",
          (row["id"], row["name"]), ("", "Custom ladder settings"))

    print("\n15. every preset key is a key the engine actually has")
    # engine.update_config SILENTLY DROPS an unknown key. A preset carrying a
    # misspelt setting would apply, stamp the ticker, and trade as if that line
    # had never been written.
    import engine as _eng
    for pid in presets.PRESETS:
        bad = sorted(k for k in presets.settings(pid)
                     if k not in _eng.TICKER_DEFAULTS)
        check("%s applies cleanly" % pid, bad, [])
    check("`preset` and `strategy` are real config keys, so the stamp sticks",
          [k for k in ("preset", "strategy", "strategy_entries",
                       "strategy_exits") if k not in _eng.TICKER_DEFAULTS], [])

    shutil.rmtree(SCRATCH, ignore_errors=True)
    print("\n" + ("ALL CHECKS PASSED" if not FAIL else f"{FAIL} CHECK(S) FAILED"))
    return 1 if FAIL else 0


def _raises(fn, exc) -> bool:
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
