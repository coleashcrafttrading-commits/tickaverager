#!/usr/bin/env python3
"""
test_optwatch.py -- the options watchlist, offline.

No network, no credentials, no touching the real state directory: every check
runs against a temporary state dir and a temporary config, so this suite says
the same thing on a fresh clone as it does on the VM.

What is actually being defended here:

  * A malformed row is a REFUSAL, not a default. The watchlist is hand-edited
    and a typo that reads as a value (cadence "fast" quietly becoming 300s) is
    worse than one that stops the load.
  * A row with no `why` cannot exist. Six months from now the sentence is the
    only defensible reason to keep or drop a name.
  * The share-fleet overlap is detected from the lot LEDGERS as well as from
    config.tickers, because a ledger outlives its config entry and shares with
    a ledger and no config row are still shares.
  * Disabling is not removing.

    .venv/Scripts/python test_optwatch.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import optwatch as W

FAIL = 0


def check(name, got, want) -> None:
    global FAIL
    ok = got == want
    if not ok:
        FAIL += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}: got {got!r}, want {want!r}")


def raises(name, fn, needle="") -> None:
    global FAIL
    try:
        fn()
    except Exception as exc:                                   # noqa: BLE001
        ok = needle.lower() in str(exc).lower()
        if not ok:
            FAIL += 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: raised {type(exc).__name__}"
              f" {str(exc)[:90]!r}, wanted {needle!r} in it")
        return
    FAIL += 1
    print(f"  FAIL  {name}: nothing raised, wanted {needle!r}")


class Scratch:
    """A throwaway state dir, a throwaway config, a throwaway watchlist."""

    def __init__(self, tickers=(), ledgers=()):
        self.dir = Path(tempfile.mkdtemp(prefix="ta_optwatch_"))
        self.state = self.dir / "state"
        self.state.mkdir()
        self.path = self.state / "options_watchlist.json"
        self.config_path = self.dir / "config.json"
        self.config_path.write_text(
            json.dumps({"tickers": {t: {} for t in tickers}}), encoding="utf-8")
        for sym in ledgers:
            (self.state / ("lots_%s.json" % sym)).write_text("[]",
                                                             encoding="utf-8")

    @property
    def kw(self) -> dict:
        return {"path": self.path, "state_dir": self.state,
                "config_path": self.config_path}

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def main() -> int:
    print("\n1. the seed board is honest about itself")
    rows = W.default_rows(now="2026-09-19T00:00:00+00:00")
    check("every seeded row carries a reason",
          all(len(r.why) > 20 for r in rows), True)
    check("every seeded row is stamped with when",
          all(r.added_at == "2026-09-19T00:00:00+00:00" for r in rows), True)
    check("every seeded tier has a cadence",
          all(r.cadence_s == W.TIER_CADENCE_S[r.tier] for r in rows), True)
    check("no duplicate symbols in the seed",
          len({r.symbol for r in rows}), len(rows))
    check("the seed avoids a name the fleet already trades",
          [r.symbol for r in W.default_rows(blocked=["SPY"])].count("SPY"), 0)

    print("\n2. first use seeds the file; the second read is the same board")
    s = Scratch()
    try:
        check("the file does not exist yet", s.path.exists(), False)
        first = W.load(**s.kw, config={})
        check("the file exists now", s.path.exists(), True)
        check("seeded the whole default set", len(first), len(W.SEED))
        second = W.load(**s.kw, config={})
        same = [r.to_dict() for r in second] == [r.to_dict() for r in first]
        check("a second load is byte-for-byte the same board", same, True)
        check("seed=False on a missing file returns an empty board, no write",
              W.load(path=s.state / "nope.json", state_dir=s.state,
                     config_path=s.config_path, config={}, seed=False), [])
    finally:
        s.close()

    print("\n3. every field survives the round trip")
    s = Scratch()
    try:
        row = W.WatchRow(symbol="XLE", tier="C", cadence_s=1234.0,
                         enabled=False, mode_weight=0.25, slot_budget=3,
                         why="energy sector ETF, penny-wide, low correlation "
                             "to the index names already on the board",
                         added_at="2026-01-02T03:04:05+00:00",
                         notes="hand added")
        W.save([row], s.path)
        back = W.load(**s.kw, config={})[0]
        check("symbol", back.symbol, "XLE")
        check("tier", back.tier, "C")
        check("cadence survives a non-default value", back.cadence_s, 1234.0)
        check("enabled=False survives", back.enabled, False)
        check("mode_weight", back.mode_weight, 0.25)
        check("slot_budget", back.slot_budget, 3)
        check("added_at", back.added_at, "2026-01-02T03:04:05+00:00")
        check("notes", back.notes, "hand added")
        check("the derived conflict flag is NOT persisted",
              "shares_conflict" in json.loads(s.path.read_text())["rows"][0],
              False)
    finally:
        s.close()

    print("\n4. a malformed row stops the load instead of becoming a default")
    s = Scratch()
    try:
        def write(rows):
            s.path.write_text(json.dumps({"rows": rows}), encoding="utf-8")

        good = {"symbol": "SPY", "why": "the deepest option market there is"}
        write([{**good, "tier": "Z"}])
        raises("an unknown tier", lambda: W.load(**s.kw, config={}), "tier")
        write([{**good, "cadence_s": "fast"}])
        raises("a cadence that is not a number",
               lambda: W.load(**s.kw, config={}), "cadence_s")
        write([{**good, "cadence_s": 0}])
        raises("a zero cadence", lambda: W.load(**s.kw, config={}), "positive")
        write([{**good, "mode_weight": 1.5}])
        raises("a mode_weight outside 0..1",
               lambda: W.load(**s.kw, config={}), "mode_weight")
        write([{"symbol": "SPY"}])
        raises("a row with no reason", lambda: W.load(**s.kw, config={}),
               "needs a `why`")
        write([good, good])
        raises("the same symbol twice", lambda: W.load(**s.kw, config={}),
               "appears twice")
        s.path.write_text("{not json", encoding="utf-8")
        raises("a file that is not JSON", lambda: W.load(**s.kw, config={}),
               "not valid json")
        # The design doc sketches a bare list; we write an object. Both read.
        s.path.write_text(json.dumps([good]), encoding="utf-8")
        check("a bare list of rows still loads",
              [r.symbol for r in W.load(**s.kw, config={})], ["SPY"])
    finally:
        s.close()

    print("\n5. add, remove, disable, enable")
    s = Scratch()
    try:
        W.save([], s.path)
        W.add("SPY", "the deepest listed option market", tier="A", **s.kw,
              config={})
        check("added", [r.symbol for r in W.load(**s.kw, config={})], ["SPY"])
        check("tier A took tier A's cadence",
              W.load(**s.kw, config={})[0].cadence_s, W.TIER_CADENCE_S["A"])
        raises("adding it twice", lambda: W.add("SPY", "again", **s.kw,
                                                config={}), "already")
        raises("adding with no reason",
               lambda: W.add("QQQ", "  ", **s.kw, config={}), "needs a reason")
        raises("adding a symbol that is not a symbol",
               lambda: W.add("not a ticker", "x", **s.kw, config={}),
               "not a usable equity symbol")

        W.disable("SPY", **s.kw, config={})
        after = W.load(**s.kw, config={})
        check("disable keeps the row", len(after), 1)
        check("disable clears enabled", after[0].enabled, False)
        check("disable keeps the reason", bool(after[0].why), True)
        check("symbols() skips a disabled row by default",
              W.symbols(path=s.path, state_dir=s.state,
                        config_path=s.config_path, config={}), [])
        check("symbols(enabled_only=False) does not",
              W.symbols(path=s.path, state_dir=s.state,
                        config_path=s.config_path, config={},
                        enabled_only=False), ["SPY"])
        W.enable("SPY", **s.kw, config={})
        check("enable puts it back",
              W.load(**s.kw, config={})[0].enabled, True)

        W.remove("SPY", **s.kw, config={})
        check("removed", W.load(**s.kw, config={}), [])
        raises("removing what is not there",
               lambda: W.remove("SPY", **s.kw, config={}), "not on the")
    finally:
        s.close()

    print("\n6. the share fleet overlap is flagged, and refused when it counts")
    s = Scratch(tickers=("RAM", "MSTX"))
    try:
        W.save([W.WatchRow(symbol="RAM", why="deliberately overlapping, to "
                                             "prove the warning renders",
                           added_at="2026-09-19T00:00:00+00:00"),
                W.WatchRow(symbol="SPY", why="the deepest option market",
                           added_at="2026-09-19T00:00:00+00:00")], s.path)
        rows = W.load(**s.kw)
        by = {r.symbol: r for r in rows}
        check("the overlapping row is flagged",
              by["RAM"].shares_conflict, True)
        check("the flag names config.tickers",
              "config.tickers" in (by["RAM"].conflict_reason or ""), True)
        check("the clean row is not flagged", by["SPY"].shares_conflict, False)
        check("increment 2 loads anyway -- the board must be able to SHOW it",
              len(rows), 2)
        raises("assert_disjoint refuses",
               lambda: W.assert_disjoint(rows, state_dir=s.state,
                                         config_path=s.config_path),
               "overlaps the share fleet")
        raises("load(strict=True) is the same refusal in one call",
               lambda: W.load(**s.kw, strict=True), "overlaps the share fleet")
        raises("add() refuses an overlapping name by default",
               lambda: W.add("MSTX", "because I can", **s.kw), "share fleet")
        W.add("MSTX", "watching a name the ladder holds, deliberately",
              allow_share_conflict=True, **s.kw)
        check("...but the escape hatch works and keeps the warning",
              {r.symbol for r in W.load(**s.kw) if r.shares_conflict},
              {"RAM", "MSTX"})
        b = W.board(**s.kw)
        check("board() says the board is not disjoint", b["disjoint"], False)
        check("board() lists the conflicts once at the top",
              sorted(b["conflicts"]), ["MSTX", "RAM"])
    finally:
        s.close()

    print("\n7. a lot ledger counts even when config has forgotten the symbol")
    s = Scratch(tickers=(), ledgers=("NVDA",))
    try:
        fleet = W.share_fleet_symbols({}, state_dir=s.state,
                                      config_path=s.config_path)
        check("the ledger is found", "NVDA" in fleet, True)
        check("and the reason says which half found it",
              "lot ledger" in fleet["NVDA"], True)
        W.save([W.WatchRow(symbol="NVDA", why="overlaps a stale ledger",
                           added_at="x")], s.path)
        check("a stale-ledger symbol is flagged like a config one",
              W.load(**s.kw, config={})[0].shares_conflict, True)
        check("the seed skips it too",
              "NVDA" in [r.symbol for r in W.default_rows(blocked=fleet)],
              False)
    finally:
        s.close()

    print("\n8. the default path is not reachable from a named one")
    s = Scratch()
    try:
        missing = s.state / "definitely_absent.json"
        check("a named path that does not exist does not fall back to the "
              "real watchlist", W._candidate_paths(missing), [missing])
        check("the default path does carry the design doc's alternative",
              W._candidate_paths(W.WATCHLIST),
              [W.WATCHLIST, W.LEGACY_WATCHLIST])
    finally:
        s.close()

    print("\n9. this module cannot reach a broker or place an order")
    src = Path(W.__file__).read_text(encoding="utf-8")
    body = src.split('"""', 2)[-1]
    for needle in ("import broker", "import requests", "urllib",
                   "OptionTrader", "submit(", "alpaca"):
        check("no %r in the body" % needle, needle in body, False)

    print()
    if FAIL:
        print("FAILED: %d check(s)" % FAIL)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
