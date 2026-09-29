#!/usr/bin/env python3
"""
bank.py -- one shelf for every strategy, whatever it is made of.

Two kinds of strategy already existed and neither knew about the other: a
DOCUMENT (`strategies/<slug>.json`, built by clicking indicators and rules
together) and CODE (`strategies/code/<slug>.py`, real Python with an `on_bar`).
They had separate listings, separate APIs and separate pages, so a strategy
Claude wrote landed somewhere the dashboard never showed.

This module is the shelf they share. It answers three questions:

    listing()              what is on the shelf, with a line about each
    detail(kind, slug)     everything about one of them
    tunables(kind, slug)   which numbers a human may turn, and their bounds

and one verb, `set_params`, which turns those numbers without touching the
strategy's SHAPE. That split is deliberate: changing 30 to 25 is a tweak and
belongs in a panel you can open in two clicks, while adding an indicator or
rewriting a rule changes what the strategy IS and belongs in the builder that
was made for it. A settings panel that can silently restructure a strategy is
how you end up unable to say what you are running.

Nothing here places an order or touches a live ladder. Attaching a strategy to
a ticker stays an explicit act on that ticker's own page.
"""
from __future__ import annotations

import ast
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

import btcode
import strategy as sdoc

ROOT = Path(__file__).resolve().parent

#: THE BUILTIN, captured before this module publishes a function called
#: `list` (the registry contract the hub is written against says `list()`).
#: A module-level name shadows the builtin at CALL time, not at import time,
#: so every `isinstance(x, list)` below would silently start asking "is this
#: a bank listing function" and answer False for every real list. Runtime
#: uses in this file therefore go through `_LIST`. Annotations are safe --
#: `from __future__ import annotations` makes them strings.
_LIST = list

KINDS = ("doc", "code")


class BankError(Exception):
    pass


# ===================================================================== listing
def listing() -> list[dict]:
    """Every strategy on the shelf, newest first within each kind.

    `note` is the one-line description the card shows: a document's own note,
    or the first line of a coded strategy's docstring. A strategy with neither
    says so rather than showing a blank -- an unlabelled strategy is one
    nobody will dare attach six months from now.
    """
    out: list[dict] = []
    for d in sdoc.listing():
        p = sdoc.STRATEGY_DIR / f"{d['slug']}.json"
        out.append({
            "kind": "doc",
            "slug": d["slug"],
            "name": d.get("name") or d["slug"],
            "note": d.get("note") or "",
            "indicators": d.get("indicators") or [],
            "error": d.get("error"),
            "modified": p.stat().st_mtime if p.exists() else 0,
            "size": p.stat().st_size if p.exists() else 0,
        })
    for c in btcode.listing():
        out.append({
            "kind": "code",
            "slug": c["slug"],
            "name": _title_from_slug(c["slug"]),
            "note": c.get("note") or "",
            "indicators": [],
            "error": None,
            "modified": c.get("modified") or 0,
            "size": c.get("size") or 0,
        })
    for x in out:
        x["tunable_count"] = len(_tunables_safe(x["kind"], x["slug"]))
    return sorted(out, key=lambda x: (-float(x["modified"] or 0), x["slug"]))


def _title_from_slug(slug: str) -> str:
    s = re.sub(r"^found-\d+-", "", str(slug)).replace("-", " ").replace("_", " ")
    return s[:1].upper() + s[1:] if s else str(slug)


# ====================================================================== detail
def detail(kind: str, slug: str) -> dict:
    """Everything about one strategy: what it is, what it says, what turns."""
    kind = _kind(kind)
    if kind == "doc":
        spec = sdoc.load(slug)
        return {
            "kind": "doc", "slug": slug,
            "name": spec.get("name") or slug,
            "note": spec.get("note") or "",
            "spec": spec,
            "indicators": spec.get("indicators") or {},
            "entry": spec.get("entry"),
            "exit": spec.get("exit"),
            "target": spec.get("target"),
            "stop": spec.get("stop"),
            "sizing": spec.get("sizing"),
            "source": json.dumps(spec, indent=2),
            "tunables": tunables(kind, slug),
        }
    src = btcode.load(slug)
    return {
        "kind": "code", "slug": slug,
        "name": _title_from_slug(slug),
        "note": _docstring(src),
        "doc": _docstring(src, whole=True),
        "source": src,
        "params": _code_params(src),
        "functions": _functions(src),
        "indicators": sorted(set(re.findall(r"""indicator\(\s*["']([a-z_]+)["']""", src))),
        "tunables": tunables(kind, slug),
    }


def _docstring(src: str, whole: bool = False) -> str:
    try:
        d = ast.get_docstring(ast.parse(src)) or ""
    except SyntaxError:
        return ""
    return d.strip() if whole else (d.strip().splitlines() or [""])[0]


def _functions(src: str) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]


def _param_blocks(src: str) -> list[tuple[Any, dict]]:
    """Every literal that sets PARAMS, in source order, with its AST node.

    A strategy may declare PARAMS and then override some of them:

        PARAMS = {"ema_n": 200, "ema_on": 1}
        # --- the parameters this strategy actually won with ---
        PARAMS.update({"ema_on": 0})

    Reading only the first assignment reports ema_on as 1 while the strategy
    runs with 0 -- a settings panel showing a number the code does not use,
    and worse, writes to it that change nothing. So both forms are collected
    and later ones win, exactly as Python would apply them.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[tuple[Any, dict]] = []
    for node in ast.walk(tree):
        lit = None
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "PARAMS" for t in node.targets):
            lit = node.value
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "update" and isinstance(node.func.value, ast.Name)
              and node.func.value.id == "PARAMS" and len(node.args) == 1):
            lit = node.args[0]
        if lit is None:
            continue
        try:
            v = ast.literal_eval(lit)
        except (ValueError, SyntaxError):
            continue
        if isinstance(v, dict):
            out.append((lit, dict(v)))
    out.sort(key=lambda nv: (getattr(nv[0], "lineno", 0), getattr(nv[0], "col_offset", 0)))
    return out


def _code_params(src: str) -> dict:
    """The EFFECTIVE parameters: every block merged in source order."""
    merged: dict = {}
    for _, d in _param_blocks(src):
        merged.update(d)
    return merged


# ==================================================================== tunables
def _tunables_safe(kind: str, slug: str) -> list[dict]:
    try:
        return tunables(kind, slug)
    except Exception:
        return []


def tunables(kind: str, slug: str) -> list[dict]:
    """The numbers a human may turn, each addressed by a dotted path.

    A tunable is a NUMBER that already exists in the strategy. Nothing here
    invents a knob the strategy does not have, and nothing here can add or
    remove an indicator, a rule or a branch -- `set_params` writes values back
    to these exact paths and refuses anything else.
    """
    kind = _kind(kind)
    out: list[dict] = []
    if kind == "code":
        for k, v in _code_params(btcode.load(slug)).items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                out.append(_knob(f"PARAMS.{k}", k, v, group="Parameters"))
            else:
                out.append(_knob(f"PARAMS.{k}", k, v, group="Parameters"))
        return out

    spec = sdoc.load(slug)
    for name, cfg in (spec.get("indicators") or {}).items():
        for p, v in (cfg or {}).items():
            # numbers and on/off switches only. A STRING input (a source, a
            # mode) is left to the builder: `set_params` re-validates the
            # shape but cannot know that "typical" is not a price source, so
            # a free-text box here would write a strategy that only fails
            # later, live, when the indicator is asked to compute.
            if p == "kind" or not isinstance(v, (int, float, bool)):
                continue
            out.append(_knob(f"indicators.{name}.{p}", p, v,
                             group=f"{name} ({cfg.get('kind', '?')})"))
    for side in ("target", "stop"):
        for p, v in (spec.get(side) or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(_knob(f"{side}.{p}", p, v, group=side.title()))
    for which in ("entry", "exit"):
        _walk_rules(spec.get(which), f"{which}", which.title() + " rules", out)
    for p, v in (spec.get("sizing") or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(_knob(f"sizing.{p}", p, v, group="Sizing"))
    return out


OPS = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "=", "ne": "!=",
       "cross_above": "crosses above", "cross_below": "crosses below"}


def _walk_rules(node: Any, path: str, group: str, out: list) -> None:
    """Numeric thresholds inside the entry/exit tree -- the 32 in
    `{"lt": ["rsi.rsi", 32]}`. These are the numbers people nudge most.

    A condition is one key (the operator) over a two-element operand list, so
    the knob is operand 1 and only when it is a NUMBER: `{"gt": ["close",
    "slow.ema"]}` compares two series and has nothing to turn, and a flag like
    `{"target_reached": true}` is a rule, not a value.
    """
    if isinstance(node, _LIST):
        for i, k in enumerate(node):
            _walk_rules(k, f"{path}.{i}", group, out)
        return
    if not isinstance(node, dict):
        return
    for key in ("all", "any", "none", "not"):
        if key in node:
            _walk_rules(node[key], f"{path}.{key}", group, out)
            return
    for op, operands in node.items():
        if not isinstance(operands, _LIST) or len(operands) != 2:
            continue
        rhs = operands[1]
        if isinstance(rhs, bool) or not isinstance(rhs, (int, float)):
            continue
        out.append(_knob(f"{path}.{op}.1", f"{operands[0]} {OPS.get(op, op)} …",
                         rhs, group=group))


def _knob(path: str, label: str, value: Any, group: str = "") -> dict:
    """One knob, with bounds a slider can use without inventing a range.

    The bounds come from the value's own magnitude rather than a guess about
    what the number means: a period of 14 gets an integer range, a 0.30 target
    gets a fine one. `hint` is what the panel shows when the value is not a
    number at all -- those are shown read-only rather than hidden, because a
    knob you cannot see is a knob you forget exists.
    """
    d: dict = {"path": path, "label": label, "value": value, "group": group}
    if isinstance(value, bool):
        d["type"] = "bool"
    elif isinstance(value, int):
        d.update(type="int", step=1, min=0, max=max(4, int(abs(value) * 4) or 100))
    elif isinstance(value, float):
        mag = abs(value) or 1.0
        step = 0.01 if mag < 5 else 0.1 if mag < 100 else 1.0
        d.update(type="float", step=step, min=0.0, max=round(mag * 4, 4))
    else:
        d.update(type="text", hint="not a number -- edit this one in the builder")
    return d


# ================================================================== set_params
def set_params(kind: str, slug: str, patch: dict) -> dict:
    """Write new VALUES to existing paths. Never adds, removes or restructures.

    Every path is checked against the strategy's own tunables first, so a
    patch cannot invent a field, and a document is re-validated before it is
    saved -- a settings panel must not be able to write a strategy the
    backtester will later refuse to run.
    """
    kind = _kind(kind)
    if not isinstance(patch, dict) or not patch:
        raise BankError("nothing to change")
    known = {t["path"]: t for t in tunables(kind, slug)}
    unknown = [p for p in patch if p not in known]
    if unknown:
        raise BankError(
            f"{', '.join(sorted(unknown))} is not a setting on this strategy. "
            f"Adding or removing one is a change of shape -- do that in the builder.")

    if kind == "code":
        src = btcode.load(slug)
        changed = {path.split(".", 1)[1]: _coerce(known[path], v)
                   for path, v in patch.items()}
        new = _rewrite_params(src, changed)
        ok, err = _compiles(new)
        if not ok:
            raise BankError(f"that change would not compile: {err}")
        btcode.save(slug, new)
        # read back, so the caller sees the EFFECTIVE values rather than what
        # it hoped it wrote -- the two differ if a later block still wins
        return {"ok": True, "kind": kind, "slug": slug,
                "params": _code_params(btcode.load(slug)),
                "tunables": tunables(kind, slug)}

    spec = sdoc.load(slug)
    for path, v in patch.items():
        _assign(spec, path, _coerce(known[path], v))
    sdoc.validate(spec)                     # never save a strategy that will not run
    sdoc.save(spec)
    return {"ok": True, "kind": kind, "slug": slug, "tunables": tunables(kind, slug)}


def _coerce(knob: dict, v: Any) -> Any:
    t = knob.get("type")
    if t == "bool":
        return bool(v)
    if t == "int":
        return int(round(float(v)))
    if t == "float":
        return round(float(v), 6)
    return v


def _assign(spec: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    node: Any = spec
    for p in parts[:-1]:
        node = node[int(p)] if isinstance(node, _LIST) else node[p]
    last = parts[-1]
    if isinstance(node, _LIST):
        node[int(last)] = value
    else:
        node[last] = value


def _rewrite_params(src: str, changed: dict) -> str:
    """Replace individual VALUES in place, touching nothing else in the file.

    Deliberately not "re-emit the dict from the parsed values": these files
    carry their reasoning in comments beside each parameter --

        "exit_on_zero": 0,       # 1 = close when the signal returns to 0.
                                 #     Many published rules are stated as a
                                 #     CONDITION TO BE IN...

    -- and rebuilding the literal from an AST throws every one of them away,
    because comments are not in the tree. A panel that quietly deletes the
    documentation for the number it just changed is worse than one that
    refuses to change it. So only the span of the value being changed is
    replaced: comments, key order, quote style, alignment and blank lines all
    survive byte for byte.

    A key set twice (a base PARAMS and a later `PARAMS.update`) is changed
    where it is LAST set, or the edit would be silently overridden by the line
    below it. Edits are applied back-to-front so earlier offsets stay valid.
    """
    blocks = _param_blocks(src)
    if not blocks:
        raise BankError("this strategy declares no PARAMS, so it has nothing to tune")

    lines = src.splitlines(keepends=True)

    def off(line: int, col: int) -> int:
        return sum(len(x) for x in lines[:line - 1]) + col

    # the span of each key's VALUE, in the last block that sets it
    spans: dict[str, tuple[int, int]] = {}
    for node, d in blocks:                      # source order: later wins
        for k_node, v_node in zip(node.keys, node.values):
            try:
                key = ast.literal_eval(k_node)
            except (ValueError, SyntaxError):
                continue
            if not isinstance(key, str):
                continue
            spans[key] = (off(v_node.lineno, v_node.col_offset),
                          off(getattr(v_node, "end_lineno", v_node.lineno),
                              getattr(v_node, "end_col_offset", 0)))

    missing = [k for k in changed if k not in spans]
    if missing:
        raise BankError(
            f"{', '.join(sorted(missing))} is not declared in this strategy's PARAMS")

    out = src
    for k, v in sorted(changed.items(), key=lambda kv: spans[kv[0]][0], reverse=True):
        start, end = spans[k]
        out = out[:start] + repr(v) + out[end:]
    return out


def _compiles(src: str) -> tuple[bool, str]:
    try:
        compile(src, "<strategy>", "exec")
        return True, ""
    except SyntaxError as e:
        return False, f"line {e.lineno}: {e.msg}"


def _kind(kind: str) -> str:
    k = str(kind or "").lower()
    if k not in KINDS:
        raise BankError(f"unknown kind {kind!r} -- expected one of {', '.join(KINDS)}")
    return k


# ============================================================== THE ONE BANK
# Four stores existed, each with its own listing, and the ticker dropdown read
# none of them properly -- a strategy on the shelf did not appear when you
# opened the dropdown on a ticker.
#
#   presets.PRESETS         ladder bundles, in Python
#   strategies/*.json       indicator DOCUMENTS the builder writes
#   strategies/code/*.py    coded strategies (the backtester's, not the engine's)
#   options/bank/*.json     231 researched option STRUCTURES
#   optplays.PLAYS          the two plays the owner specified himself
#
# One shelf now, one id space, one `origin` column. "Standard versus personal"
# is a FIELD, not four rooms: a personal iron condor sits next to the
# researched one and both come back from the same call.
#
# STORE is where the bytes live; KIND is what the thing IS, and the two are
# deliberately not one to one. A clicked document and a coded strategy are both
# `indicator` to anyone choosing one; a shipped preset and one the owner built
# are both `ladder`.
#
#     store       kind              writable?
#     preset      ladder            personal ones only (personal/ladder/)
#     doc         indicator         yes (strategies/, strategy.save)
#     code        indicator         yes (strategies/code/, btcode.save)
#     option      option            personal ones only (personal/option/)
#     play        option-tailored   no -- they are dataclasses in optplays.py
#
# An id is "<store>:<slug>". Prefixed because a slug alone is NOT unique:
# `supertrend-spy-1min` could be a document or a coded file, and a bare slug in
# a dropdown that silently picks the wrong one is how you attach a strategy
# nobody chose.

STORES = ("preset", "doc", "code", "option", "play")
BANK_KINDS = ("ladder", "indicator", "option", "option-tailored")
ORIGINS = ("standard", "personal")

STORE_KIND = {"preset": "ladder", "doc": "indicator", "code": "indicator",
              "option": "option", "play": "option-tailored"}

#: Personal ladder bundles and personal option structures: the two kinds whose
#: STANDARD shelf cannot hold them.
#:
#: WHY NOT beside the standard ones. `optbank.ingest()` rewrites
#: `options/bank/` from research output, so a personal structure filed there is
#: one re-ingest away from being silently replaced by a same-slug research
#: document -- the owner's own work overwritten by a batch job, with no error.
#: `presets.PRESETS` is Python and cannot be written at all. Documents and
#: coded strategies keep their existing homes: they already have an audited
#: writer and a builder page that writes them, and moving live files to make a
#: table symmetrical is how you lose them.
PERSONAL_DIR = ROOT / "personal"
PERSONAL_SUB = {"preset": "ladder", "option": "option"}


def make_id(store: str, slug: str) -> str:
    return "%s:%s" % (_store(store), str(slug))


def parse_id(entry_id: str) -> tuple:
    """"preset:basic" -> ("preset", "basic").

    A bare slug is accepted and resolved, because the chat assistant and the
    CLI will both be handed one -- but only when it names exactly ONE entry.
    An ambiguous slug raises and NAMES both candidates rather than picking; the
    whole reason ids are prefixed is that guessing here attaches a strategy
    nobody chose.
    """
    raw = str(entry_id or "").strip()
    if not raw:
        raise BankError("no strategy id given")
    if ":" in raw:
        store, _, slug = raw.partition(":")
        return _store(store), slug.strip()
    hits = [r["id"] for r in _shelf() if r["slug"] == raw]
    if len(hits) == 1:
        return parse_id(hits[0])
    if not hits:
        raise BankError("no strategy on the shelf is called %r" % raw)
    raise BankError("%r names %d strategies (%s) -- say which one"
                    % (raw, len(hits), ", ".join(sorted(hits))))


def _store(store: str) -> str:
    s = str(store or "").strip().lower()
    if s not in STORES:
        raise BankError("unknown store %r -- expected one of %s"
                        % (store, ", ".join(STORES)))
    return s


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:60]
    return s or "unnamed"


def _personal_dir(store: str) -> Path:
    sub = PERSONAL_SUB.get(_store(store))
    if sub is None:
        raise BankError("the %s store has no personal shelf" % store)
    return PERSONAL_DIR / sub


def _personal_docs(store: str) -> dict:
    """slug -> document, for the personal half of a store. Never raises."""
    out: dict = {}
    d = _personal_dir(store)
    if not d.exists():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # A corrupt personal file must not vanish from the shelf: a
            # strategy you cannot see is one you cannot fix.
            out[p.stem] = {"slug": p.stem, "name": p.stem, "summary": "",
                           "_error": "unreadable: %s" % e}
            continue
        if isinstance(doc, dict):
            doc.setdefault("slug", p.stem)
            out[str(doc["slug"])] = doc
    return out


# ------------------------------------------------------------ params schema
def _param(key: str, default: Any, *, label: str = "", group: str = "",
           choices: Any = None, lo: Any = None, hi: Any = None,
           step: Any = None, note: str = "") -> dict:
    """One knob, as the settings pane and the chat assistant both read it.

    A superset of hub's `settings_schema` row ({key, default, type}), so
    anything already written against that shape keeps working.
    """
    if isinstance(default, bool):
        typ = "bool"
    elif isinstance(default, int):
        typ = "int"
    elif isinstance(default, float):
        typ = "float"
    else:
        typ = "enum" if choices else "text"
    d = {"key": key, "label": label or key, "type": typ, "default": default,
         "group": group}
    if choices:
        d["choices"] = _LIST(choices)
    if lo is not None:
        d["min"] = lo
    if hi is not None:
        d["max"] = hi
    if step is not None:
        d["step"] = step
    if note:
        d["note"] = note
    return d


#: Enum settings, mirrored from `engine.Engine.update_config`. ENGINE IS THE
#: ENFORCER: it rejects a value outside its own list whatever this table says,
#: so a stale entry here mis-renders a dropdown and can never write a bad
#: config. Copied rather than imported because engine.py is a live trading path
#: and nothing in this round touches it.
_LADDER_CHOICES = {
    "side_mode": ("auto", "long", "short", "both"),
    "exit_mode": ("limit", "trail"),
    "first_entry": ("red_bar", "immediate", "with_trend"),
    "bias_source": ("rd", "1h"),
    "entry_ma": ("vwap", "ema"),
    "add_trigger": ("touch", "close"),
    "add_anchor": ("last_fill", "last_open"),
    "reversal_mode": ("off", "flatten", "reverse"),
    "fractional": ("off", "on"),
}

#: The bounds `optplays.Assignments._validate` enforces. Same note as above:
#: optplays validates on write regardless, so a wrong bound here is a slider
#: that stops early, never a trade nobody meant.
_PLAY_BOUNDS = {
    "contracts": (1, 100, 1),
    "short_delta": (0.01, 0.49, 0.01),
    "strikes_below": (1, 20, 1),
    "target_dte": (1, 400, 1),
    "profit_pct": (0.05, 5.0, 0.05),
    "stop_pct": (0.05, 5.0, 0.05),
}
_PLAY_CHOICES = {"direction": ("both", "calls", "puts")}


def _ladder_schema(settings: dict) -> list:
    out = []
    for k, v in sorted((settings or {}).items()):
        if k == "preset":
            continue
        out.append(_param(k, v, group="Ladder", choices=_LADDER_CHOICES.get(k)))
    return out


def _play_schema(play: Any) -> list:
    out = []
    params = play.defaults() if hasattr(play, "defaults") else dict(play or {})
    editable = _LIST(getattr(play, "editable", None) or sorted(params))
    for k in editable:
        if k not in params:
            continue
        lo, hi, step = _PLAY_BOUNDS.get(k, (None, None, None))
        out.append(_param(k, params[k], group="Play",
                          choices=_PLAY_CHOICES.get(k), lo=lo, hi=hi, step=step))
    return out


def _tunable_schema(kind: str, slug: str) -> list:
    """A document's or coded strategy's knobs, in the params_schema shape.

    `key` is the dotted PATH `set_params` already accepts, so the pane that
    renders this posts it straight back without inventing a second vocabulary.
    """
    out = []
    for t in _tunables_safe(kind, slug):
        d = {"key": t["path"], "label": t.get("label") or t["path"],
             "type": t.get("type") or "text", "default": t.get("value"),
             "group": t.get("group") or ""}
        for f in ("min", "max", "step", "hint"):
            if f in t:
                d[f] = t[f]
        out.append(d)
    return out


# --------------------------------------------------------------- shelf rows
def _gate(ok: bool, why: str = "") -> dict:
    return {"ok": bool(ok), "why": why if not ok else ""}


_CODE_WHY = ("a coded strategy runs in the BACKTESTER only -- the live engine "
             "loads documents (strategies/*.json), so there is nothing on a "
             "ticker for this to drive yet")
_OPTION_TRADE_WHY = ("no engine sends a banked structure: optengine trades the "
                     "tailored plays only. Attaching one records the ticker's "
                     "chosen options strategy; nothing is ordered")


def _row(store: str, slug: str, name: str, summary: str, origin: str, *,
         schema: Any = None, attach: Any = None, trades: Any = None,
         tags: Any = None, error: Any = None, modified: float = 0.0,
         schema_why: str = "") -> dict:
    store = _store(store)
    return {
        "id": make_id(store, slug),
        "store": store,
        "slug": str(slug),
        "kind": STORE_KIND[store],
        "origin": str(origin),
        "name": str(name or slug),
        "summary": str(summary or ""),
        "params_schema": _LIST(schema or []),
        # An EMPTY schema is a measurement too. Without this the settings pane
        # shows "0 knobs" beside a strategy and leaves the reader to guess
        # whether it has none or whether nobody read them.
        "params_reason": schema_why or (
            "" if schema else "this strategy declares no numbers to turn"),
        # filled by entries() when it is given an account context. None means
        # NOBODY LOOKED, which is not the same as "attached to nothing"
        "tickers": None,
        "tickers_reason": "no account was given, so attachments were not read",
        "attach": attach or _gate(True),
        "trades": trades or _gate(True),
        "tags": dict(tags or {}),
        "error": error,
        "modified": float(modified or 0.0),
    }


def _preset_rows() -> list:
    import presets
    out = []
    for pid, p in presets.PRESETS.items():
        out.append(_row("preset", pid, p.get("label") or pid,
                        p.get("description") or "", "standard",
                        schema=_ladder_schema(p.get("settings") or {}),
                        tags={"default": pid == presets.DEFAULT}))
    for slug, doc in _personal_docs("preset").items():
        out.append(_row("preset", slug, doc.get("name") or slug,
                        doc.get("summary") or "", "personal",
                        schema=_ladder_schema(doc.get("settings") or {}),
                        error=doc.get("_error"),
                        modified=doc.get("updated_at") or 0.0))
    return out


def _doc_slug(name: str) -> str:
    """`strategy.save()`'s own file-naming rule, character for character.

    NOT `_slugify`. strategy.py keeps every dash it makes, so "Ladder
    (current)" is `ladder--current-` on disk and "SuperTrend + EMA stack" is
    `supertrend---ema-stack`. A tidier slug computed here matched neither, and
    the two built-in documents whose names contain punctuation were being
    served as the owner's own work.
    """
    return "".join(ch if ch.isalnum() or ch in "-_" else "-"
                   for ch in str(name or "").lower())[:60] or "unnamed"


#: What the research sweeps write into `strategies/code/`. `_title_from_slug`
#: already strips `found-<n>-`, so the prefix is the repo's own marker for
#: "this file came out of a sweep, not out of a person".
_GENERATED_CODE = ("found-", "study-")


def _builtin_doc_slugs() -> set:
    """The documents `strategy.install_builtins()` ships.

    Origin is DERIVED, not stored: a document on disk carries no flag saying
    who wrote it, and stamping the six already there would be a guess written
    down as a fact. Anything the repo does not ship is the owner's.
    """
    return {_doc_slug(s.get("name") or "") for s in getattr(sdoc, "BUILTIN", [])}


def _doc_rows() -> list:
    builtin = _builtin_doc_slugs()
    out = []
    for d in sdoc.listing():
        slug = d["slug"]
        p = sdoc.STRATEGY_DIR / ("%s.json" % slug)
        out.append(_row("doc", slug, d.get("name") or slug, d.get("note") or "",
                        "standard" if slug in builtin else "personal",
                        schema=_tunable_schema("doc", slug),
                        error=d.get("error"),
                        tags={"indicators": _LIST(d.get("indicators") or [])},
                        modified=p.stat().st_mtime if p.exists() else 0.0))
    return out


def _code_rows() -> list:
    out = []
    for c in btcode.listing():
        slug = c["slug"]
        generated = str(slug).startswith(_GENERATED_CODE)
        out.append(_row("code", slug, _title_from_slug(slug), c.get("note") or "",
                        "standard" if generated else "personal",
                        schema=_tunable_schema("code", slug),
                        attach=_gate(False, _CODE_WHY),
                        trades=_gate(False, _CODE_WHY),
                        modified=c.get("modified") or 0.0))
    return out


_OPTION_PARAMS_WHY = (
    "a researched structure states its rules as PROSE -- strike_rule, dte_rule, "
    "entry_rules -- so there is no number to turn until it is compiled. Read "
    "the document; the numbers are in the rules")


def _option_rows() -> list:
    import optbank
    out, standard = [], set()
    for r in optbank.listing(include_forbidden=True):
        slug = r["slug"]
        standard.add(slug)
        out.append(_row("option", slug, r.get("name") or slug,
                        r.get("summary") or "", "standard",
                        attach=_gate(r.get("permitted", True),
                                     r.get("blocked_because") or ""),
                        trades=_gate(False, _OPTION_TRADE_WHY),
                        schema_why=_OPTION_PARAMS_WHY,
                        tags={"legs": r.get("legs"), "bias": r.get("bias"),
                              "net": r.get("net"), "zero_dte": r.get("zero_dte"),
                              "permitted": r.get("permitted"),
                              "alpaca_level": r.get("alpaca_level")}))
    for slug, doc in _personal_docs("option").items():
        ok, why = True, ""
        if not doc.get("_error"):
            try:
                ok, why = optbank.permitted(doc)
            except Exception as e:                      # a half-written doc
                ok, why = False, "this structure could not be screened: %r" % e
        out.append(_row("option", slug, doc.get("name") or slug,
                        doc.get("summary") or "", "personal",
                        attach=_gate(ok, why),
                        trades=_gate(False, _OPTION_TRADE_WHY),
                        schema_why=_OPTION_PARAMS_WHY,
                        error=doc.get("_error") or (
                            "a STANDARD structure has this slug too; the "
                            "personal one is what this bank serves"
                            if slug in standard else None),
                        tags={"legs": len(doc.get("legs") or []),
                              "bias": doc.get("bias"), "net": doc.get("net"),
                              "permitted": ok},
                        modified=doc.get("updated_at") or 0.0))
    return out


def _play_rows() -> list:
    import optplays
    out = []
    for pid, p in optplays.PLAYS.items():
        out.append(_row("play", pid, p.label, p.summary, "personal",
                        schema=_play_schema(p),
                        tags={"legs_desc": p.legs_desc,
                              "suggested": _LIST(p.suggested or ())}))
    return out


_READERS = (_preset_rows, _doc_rows, _code_rows, _option_rows, _play_rows)


def _shelf() -> list:
    """Every row from every store. One bad store must not blank the shelf."""
    out: list = []
    for reader in _READERS:
        try:
            out.extend(reader() or [])
        except Exception as e:                  # noqa: BLE001 -- see docstring
            out.append(_row(
                "doc", "store-unreadable-%s" % getattr(reader, "__name__", "?"),
                "a store could not be read", "", "standard",
                error="%s failed: %r" % (getattr(reader, "__name__", "?"), e),
                attach=_gate(False, "this row is an error, not a strategy")))
    return out


def entries(ctx: Any = None, *, kind: str = "", origin: str = "",
            symbol: str = "", q: str = "", attachable_only: bool = False
            ) -> list:
    """THE registry: every strategy, whatever it is made of.

        [{id, name, kind, origin, summary, params_schema, tickers, ...}]

    `ctx` is a `hub.Ctx` (anything with `.fleet` and `.state_dir`). Given one,
    `tickers` is the list of symbols the entry is attached to; without one it
    is None and `tickers_reason` says nobody looked -- an empty list would
    claim the entry is attached to nothing, which is a measurement.

    `symbol` filters to the entries attached to that ticker; `q` matches the
    name, the slug and the summary.
    """
    rows = _shelf()
    if ctx is not None:
        by_id: dict = {}
        for a in attached(ctx):
            if a.get("id"):
                by_id.setdefault(a["id"], []).append(a["symbol"])
        for r in rows:
            r["tickers"] = sorted(set(by_id.get(r["id"], [])))
            r["tickers_reason"] = None
    kind = str(kind or "").strip().lower()
    origin = str(origin or "").strip().lower()
    sym = str(symbol or "").strip().upper()
    needle = str(q or "").strip().lower()
    out = []
    for r in rows:
        if kind and r["kind"] != kind:
            continue
        if origin and r["origin"] != origin:
            continue
        if attachable_only and not r["attach"]["ok"]:
            continue
        if sym:
            if r["tickers"] is None:
                raise BankError("filtering by ticker needs an account context")
            if sym not in r["tickers"]:
                continue
        if needle and needle not in (
                "%s %s %s" % (r["name"], r["slug"], r["summary"])).lower():
            continue
        out.append(r)
    # Personal first within a kind, then by name: the owner's own work is what
    # he is looking for, and it is outnumbered 231 to 2 by the researched shelf.
    order = {k: i for i, k in enumerate(BANK_KINDS)}
    return sorted(out, key=lambda r: (order.get(r["kind"], 9),
                                      0 if r["origin"] == "personal" else 1,
                                      r["name"].lower()))


def get(entry_id: str, ctx: Any = None) -> dict:
    """One entry, with the raw document it came from under `doc`."""
    store, slug = parse_id(entry_id)
    eid = make_id(store, slug)
    for r in entries(ctx):
        if r["id"] != eid:
            continue
        row = dict(r)
        row["doc"] = _document(store, slug)
        if store in ("doc", "code"):
            try:
                row["detail"] = detail(store, slug)
            except Exception as e:
                row["detail"] = None
                row["error"] = row.get("error") or repr(e)
        return row
    raise BankError("no strategy with id %r" % eid)


def _document(store: str, slug: str) -> dict:
    """The raw document behind an entry, whatever store it came from.

    A PERSONAL document wins over a standard one with the same slug, and
    `_option_rows` flags the collision, so the shelf and this never disagree
    about which file a given id means.
    """
    store = _store(store)
    if store == "preset":
        import presets
        if slug in presets.PRESETS:
            p = presets.PRESETS[slug]
            return {"slug": slug, "name": p.get("label"),
                    "summary": p.get("description"),
                    "settings": dict(p.get("settings") or {})}
        doc = _personal_docs("preset").get(slug)
        if doc is None:
            raise BankError("no ladder strategy called %r" % slug)
        return doc
    if store == "doc":
        return sdoc.load(slug)
    if store == "code":
        return {"slug": slug, "source": btcode.load(slug)}
    if store == "option":
        doc = _personal_docs("option").get(slug)
        if doc is not None:
            return doc
        import optbank
        return optbank.load(slug)
    import optplays
    return optplays.play(slug).as_dict()


# ================================================================== building
def _atomic_write(path: Path, payload: Any) -> None:
    """Temp file then os.replace.

    Learned the hard way elsewhere in this repo: 617 of 1,584 cached files were
    corrupt because a writer was interrupted mid-write. A half-written strategy
    is a strategy that disappears from the shelf the next time it is read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".bank-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


#: kind -> the store a NEW personal entry of that kind is written to. `code`
#: and `doc` are both the `indicator` kind, so a doc carrying `source` is
#: routed to the coded shelf and everything else to the document one.
_KIND_STORE = {"ladder": "preset", "indicator": "doc", "option": "option",
               "code": "code", "doc": "doc", "preset": "preset",
               "option-tailored": "play"}


def save(doc: dict, *, by: str = "") -> dict:
    """Create or replace a PERSONAL entry. Returns the saved row, as `get`.

    THE SIGNATURE THE CHAT ASSISTANT CALLS. One dict, one call, four kinds:

        {"kind": "ladder",    "name": ..., "summary": ...,
         "settings": {engine config keys}}             -> personal/ladder/
        {"kind": "indicator", "name": ..., "note": ...,
         "indicators": {...}, "entry": {...}, "exit": {...},
         "target": {...}, "stop": {...}}               -> strategies/
        {"kind": "code",      "name": ..., "source": "python"}
                                                       -> strategies/code/
        {"kind": "option",    "name": ..., "summary": ..., "legs": [...],
         "bias": ..., "net": "credit"|"debit", "entry_rules": [...],
         "management_rules": [...], "exit_rules": [...],
         "assignment_risk": ...}                       -> personal/option/

    `id` (or `slug`) updates that entry in place; without one the slug is
    derived from the name. A STANDARD entry is never overwritten -- `copy()`
    first, which is also what "I want the standard ones AND the ones I made"
    means in practice.
    """
    if not isinstance(doc, dict) or not doc:
        raise BankError("nothing to save")
    doc = dict(doc)
    kind = str(doc.pop("kind", "") or "").strip().lower()
    store = str(doc.pop("store", "") or "").strip().lower()
    if not store:
        # `indicator` covers both halves of that kind, so the body decides:
        # something carrying `source` is Python and belongs on the coded shelf.
        if kind in ("indicator", "") and doc.get("source"):
            store = "code"
        else:
            store = _KIND_STORE.get(kind, "")
    if not store:
        raise BankError("say what kind this is: one of %s"
                        % ", ".join(BANK_KINDS))
    store = _store(store)
    if store == "play":
        raise BankError(
            "the tailored plays are Python (optplays.PLAYS), not documents -- "
            "they are edited in optplays.py and their per-ticker numbers are "
            "edited by attaching one with settings")

    slug = str(doc.pop("slug", "") or "").strip()
    if doc.get("id"):
        st, slug = parse_id(doc.pop("id"))
        if st != store:
            raise BankError("that id is a %s entry, not a %s one" % (st, store))
    name = str(doc.get("name") or "").strip()
    if not slug:
        if not name:
            raise BankError("a new strategy needs a name")
        # a document keeps strategy.py's naming, so the builder page and this
        # write the same file for the same name
        slug = _doc_slug(name) if store == "doc" else _slugify(name)
    if not name:
        name = slug

    existing = {r["slug"]: r for r in _shelf() if r["store"] == store}
    prev = existing.get(slug)
    if prev is not None and prev["origin"] == "standard":
        raise BankError(
            "%s is a STANDARD strategy and is not editable. Copy it to a "
            "personal one first (copy(%r, \"a new name\")) -- the standard "
            "shelf has to keep saying what it always said."
            % (prev["name"], make_id(store, slug)))

    if store == "doc":
        _save_document(slug, {**doc, "name": name})
    elif store == "code":
        _save_code(slug, doc)
    else:
        _save_personal(store, slug, {**doc, "name": name}, by=by)
    return get(make_id(store, slug))


def _save_document(slug: str, spec: dict) -> None:
    """An indicator document, through its own validator.

    NOT `strategy.save()`: that derives the file name from the strategy's NAME,
    so renaming one through this path would leave the old file on the shelf and
    the ticker pointing at it -- one strategy, two documents, and the live
    engine still loading the stale one. The slug is the identity here and only
    `copy()` mints a new one.
    """
    spec = {k: v for k, v in spec.items() if k not in ("kind", "id")}
    sdoc.validate(spec)
    sdoc.STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(sdoc.STRATEGY_DIR / ("%s.json" % slug), spec)


def _save_code(slug: str, doc: dict) -> None:
    src = str(doc.get("source") or "")
    if not src.strip():
        raise BankError("a coded strategy needs its `source`")
    ok, err = _compiles(src)
    if not ok:
        raise BankError("that code would not compile: %s" % err)
    btcode.save(slug, src)


_LADDER_REQUIRED = ("settings",)


def _save_personal(store: str, slug: str, doc: dict, *, by: str = "") -> None:
    doc = dict(doc)
    doc["slug"] = slug
    doc["origin"] = "personal"
    doc["kind"] = STORE_KIND[store]
    doc["updated_at"] = time.time()
    doc["updated_by"] = str(by or "")
    if store == "preset":
        _check_ladder_settings(doc.get("settings"))
    else:
        _check_option_doc(doc)
    _atomic_write(_personal_dir(store) / ("%s.json" % slug), doc)


def _check_ladder_settings(settings: Any) -> None:
    """Every key must be one the engine actually has.

    `engine.update_config` SILENTLY DROPS a key it does not know (`if k not in
    TICKER_DEFAULTS: continue`), so a ladder strategy with a misspelt setting
    would save, attach, and trade as if that line had never been written. This
    is the only place that misspelling can still be reported.
    """
    if not isinstance(settings, dict) or not settings:
        raise BankError("a ladder strategy needs a `settings` object")
    import engine
    unknown = sorted(k for k in settings if k not in engine.TICKER_DEFAULTS)
    if unknown:
        raise BankError(
            "the engine has no setting called %s -- it would be dropped in "
            "silence, so this is refused instead" % ", ".join(unknown))


def _check_option_doc(doc: dict) -> None:
    import optbank
    missing = [k for k in optbank._REQUIRED if k not in ("slug",) and not doc.get(k)]
    if missing:
        raise BankError("an option structure needs %s" % ", ".join(missing))
    legs = doc.get("legs") or []
    if not isinstance(legs, _LIST) or not legs:
        raise BankError("an option structure needs at least one leg")
    if len(legs) > optbank.MAX_LEGS:
        raise BankError("%d legs; Alpaca refuses an mleg order outside 2-%d"
                        % (len(legs), optbank.MAX_LEGS))
    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            raise BankError("leg %d is not an object" % (i + 1))
        right = str(leg.get("right", "")).lower()
        # one vocabulary on disk: optbank's own word for the share leg is
        # "stock", and permitted() tests for that exact string
        if right in ("share", "shares", "equity"):
            right = leg["right"] = "stock"
        if right not in ("call", "put", "stock"):
            raise BankError("leg %d: `right` must be call, put or stock"
                            % (i + 1))
        if str(leg.get("action", "")).lower() not in ("buy", "sell"):
            raise BankError("leg %d: `action` must be buy or sell" % (i + 1))
    doc["bias_note"] = str(doc.get("bias") or "")
    doc["bias"] = optbank.normalize_bias(doc["bias_note"])
    if doc.get("alpaca_level") is None:
        doc["alpaca_level"] = _derive_level(legs)
    # Recorded, never enforced by hiding it: this account is level 3 and a
    # level-4 structure is still worth having written down. `attach()` is what
    # refuses it.
    doc["permitted"], doc["blocked_because"] = optbank.permitted(doc)


def _derive_level(legs: list) -> int:
    """The options level a hand-built structure needs, when it does not say.

    `optbank.permitted()` reads `alpaca_level` off the document -- a field the
    RESEARCH wrote into all 231 of them. A structure the owner builds here has
    nobody to write it, so an uncovered short call would arrive with the field
    missing, be judged permitted, and be attached to a ticker on an account
    Alpaca refuses it on ("403 account not eligible to trade uncovered option
    contracts").

    A FLOOR, and only from what is unambiguous: a short option leg is covered
    when the same right is also bought (any vertical, diagonal or butterfly is
    defined-risk) or when a stock leg carries it. Anything else is naked and
    needs level 4. A document that states its own level keeps it -- this never
    overrides research.
    """
    bought = {str(L.get("right", "")).lower() for L in legs
              if str(L.get("action", "")).lower() == "buy"}
    shorts = [L for L in legs
              if str(L.get("action", "")).lower() == "sell"
              and str(L.get("right", "")).lower() in ("call", "put")]
    for L in shorts:
        right = str(L.get("right", "")).lower()
        if right in bought or "stock" in bought:
            continue
        return 4
    return 3 if shorts else 2


def copy(entry_id: str, name: str = "", *, by: str = "") -> dict:
    """Duplicate any entry as a PERSONAL one you can edit.

    This is how a standard strategy becomes yours: the 231 researched
    structures and the shipped presets stay exactly as they are, and the copy
    carries `copied_from` so six months from now it still says where it came
    from.
    """
    store, slug = parse_id(entry_id)
    src = get(make_id(store, slug))
    doc = dict(src["doc"] or {})
    new_name = str(name or "").strip() or ("%s (mine)" % src["name"])
    if store == "play":
        # A play is Python; its editable numbers are a ladder-free bundle, so
        # the honest copy is an OPTION document describing what it does rather
        # than a fake play the options engine would never look up.
        raise BankError(
            "a tailored play cannot be copied into a document -- it is code "
            "(optplays.PLAYS). Attach it with your own settings instead; every "
            "number on it is editable per ticker.")
    if store == "preset":
        body = {"kind": "ladder", "name": new_name,
                "summary": src["summary"], "settings": doc.get("settings") or {}}
    elif store == "doc":
        body = {k: v for k, v in doc.items() if k != "name"}
        body.update({"kind": "indicator", "name": new_name})
    elif store == "code":
        body = {"kind": "code", "name": new_name, "source": doc.get("source")}
    else:
        body = {k: v for k, v in doc.items()
                if k not in ("name", "slug", "origin", "kind")}
        body.update({"kind": "option", "name": new_name})
    out = save(body, by=by)
    tgt = parse_id(out["id"])
    if tgt[0] in ("preset", "option"):
        p = _personal_dir(tgt[0]) / ("%s.json" % tgt[1])
        d = json.loads(p.read_text(encoding="utf-8"))
        d["copied_from"] = src["id"]
        _atomic_write(p, d)
    return get(out["id"])


def delete(entry_id: str, *, ctx: Any = None, force: bool = False) -> dict:
    """Remove a PERSONAL entry. Refused while a ticker is using it.

    `ctx` is how that last sentence is checked. Without one the delete still
    happens and the result says attachments were not checked, because silently
    implying "nothing was using it" is worse than saying nobody looked.
    """
    store, slug = parse_id(entry_id)
    eid = make_id(store, slug)
    row = get(eid)
    if row["origin"] != "personal":
        raise BankError("%s is a STANDARD strategy; it is part of the machine "
                        "and is not deletable." % row["name"])
    users: list = []
    checked = ctx is not None
    if checked:
        users = [a["symbol"] for a in attached(ctx) if a.get("id") == eid]
        if users and not force:
            raise BankError("%s is attached to %s. Detach it first, or pass "
                            "force." % (row["name"], ", ".join(sorted(users))))
    if store == "doc":
        (sdoc.STRATEGY_DIR / ("%s.json" % slug)).unlink(missing_ok=True)
    elif store == "code":
        btcode.delete(slug)
    elif store in ("preset", "option"):
        (_personal_dir(store) / ("%s.json" % slug)).unlink(missing_ok=True)
    else:
        raise BankError("a tailored play lives in optplays.py and is not "
                        "deletable from here")
    return {"ok": True, "deleted": eid, "name": row["name"],
            "was_attached_to": sorted(users),
            "attachments_checked": checked,
            "note": None if checked else
            "no account context was given, so nothing checked whether a "
            "ticker was still using it"}


# =============================================================== attachments
ATTACH_FILE = "bank_attachments.json"


class Attachments:
    """The bank's OWN attachment file -- and ONLY for what no other store can
    hold: an option STRUCTURE chosen for a ticker.

    Every other attachment is a fact that already has exactly one owner, and
    mirroring it here is how two screens start disagreeing:

        ladder preset, indicator document -> the ticker's engine config
        tailored play                     -> state/options/plays.json

    Those are read back out of their own stores by `attached()` and are never
    written here. One writer per fact, or the dashboard gets to choose which
    lie to show.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._rows: dict = {}
        self.load()

    @staticmethod
    def key(symbol: str, entry_id: str) -> str:
        return "%s:%s" % (str(symbol).upper(), entry_id)

    def load(self) -> None:
        self._rows = {}
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            # Same rule as optplays.Assignments: a corrupt file must not read
            # as an empty one to a caller that would then report "nothing is
            # attached" and be believed.
            raise BankError("%s is unreadable (%s) -- refusing to treat a "
                            "corrupt attachment file as 'nothing attached'"
                            % (self.path, e))
        for r in (d.get("attachments") or []):
            sym = str(r.get("symbol", "")).upper()
            eid = str(r.get("id", ""))
            if sym and eid:
                self._rows[self.key(sym, eid)] = {
                    "symbol": sym, "id": eid,
                    "settings": dict(r.get("settings") or {}),
                    "enabled": bool(r.get("enabled", True)),
                    "added": str(r.get("added", "")),
                    "added_by": str(r.get("added_by", "")),
                    "note": str(r.get("note", ""))}

    def save(self) -> None:
        _atomic_write(self.path, {
            "updated": time.time(),
            "attachments": sorted(self._rows.values(),
                                  key=lambda r: (r["symbol"], r["id"])),
        })

    def all(self) -> list:
        return sorted(self._rows.values(), key=lambda r: (r["symbol"], r["id"]))

    def put(self, symbol: str, entry_id: str, settings: Any = None, *,
            enabled: bool = True, by: str = "", note: str = "") -> dict:
        sym = str(symbol).upper().strip()
        k = self.key(sym, entry_id)
        cur = self._rows.get(k) or {
            "symbol": sym, "id": entry_id, "settings": {}, "enabled": True,
            "added": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "added_by": by,
            "note": note}
        cur["settings"].update(dict(settings or {}))
        cur["enabled"] = bool(enabled)
        if note:
            cur["note"] = note
        self._rows[k] = cur
        self.save()
        return dict(cur)

    def remove(self, symbol: str, entry_id: str) -> bool:
        k = self.key(symbol, entry_id)
        existed = k in self._rows
        self._rows.pop(k, None)
        if existed:
            self.save()
        return existed


def _state_dir(ctx: Any) -> Path:
    return Path(getattr(ctx, "state_dir", None) or ".")


def _store_file(ctx: Any) -> Attachments:
    return Attachments(_state_dir(ctx) / ATTACH_FILE)


def _engines(ctx: Any) -> dict:
    return dict(getattr(getattr(ctx, "fleet", None), "engines", None) or {})


def _plays_store(ctx: Any):
    import optplays
    return optplays.Assignments(_state_dir(ctx) / "options" / "plays.json")


def _attach_row(index: dict, symbol: str, entry_id: str, *, source: str,
                enabled: bool = True, settings: Any = None,
                state: str = "", note: str = "") -> dict:
    """One attachment, and what it is actually going to do.

    `source` names the FILE the fact came from, because ground truth here is
    ordered (Alpaca > ledgers > journal > config) and a reader has to be able
    to see which store said this.
    """
    row = index.get(entry_id)
    known = row is not None
    return {
        "symbol": str(symbol).upper(),
        "id": entry_id,
        "kind": row["kind"] if known else None,
        "origin": row["origin"] if known else None,
        "name": row["name"] if known else "%s (not on the shelf)" % entry_id,
        "enabled": bool(enabled),
        "settings": dict(settings or {}),
        "state": state,
        "source": source,
        "trades": row["trades"] if known else _gate(
            False, "nothing on the shelf has this id"),
        "why": "" if known else (
            "%s names %r, but no strategy with that id is on the shelf. The "
            "ticker and the bank disagree." % (source, entry_id)),
    }


def attached(ctx: Any, symbol: str = "") -> list:
    """Every strategy attached to every ticker (or to one), from its OWN store.

    MULTIPLE per ticker, by construction: the ladder preset, an indicator
    document driving it, any number of tailored plays and any number of option
    structures all come back as peer rows for the same symbol.
    """
    index = {r["id"]: r for r in _shelf()}
    want = str(symbol or "").strip().upper()
    out: list = []

    import hub
    for sym, eng in _engines(ctx).items():
        sym = str(sym).upper()
        if want and sym != want:
            continue
        cfg = dict(getattr(eng, "cfg", None) or {})
        state = hub.LadderStrategy.engine_state(eng)
        pid = str(cfg.get("preset") or "").strip()
        if pid and pid != "custom":
            out.append(_attach_row(index, sym, make_id("preset", pid),
                                   source="config.json (the ticker's engine)",
                                   state=state,
                                   settings={k: cfg.get(k) for k in
                                             _preset_keys(index, pid)}))
        else:
            # Not an id: the ladder is on settings nobody named. Shown rather
            # than omitted, or the ticker page says "no strategy" about a
            # ladder that is running.
            out.append({"symbol": sym, "id": "", "kind": "ladder",
                        "origin": "personal", "name": "Custom ladder settings",
                        "enabled": True, "settings": {}, "state": state,
                        "source": "config.json (the ticker's engine)",
                        "trades": _gate(True),
                        "why": "these settings were edited by hand, so they "
                               "match no strategy on the shelf"})
        slug = str(cfg.get("strategy") or "").strip()
        if slug:
            out.append(_attach_row(
                index, sym, make_id("doc", slug),
                source="config.json (the ticker's `strategy`)", state=state,
                enabled=bool(cfg.get("strategy_entries")
                             or cfg.get("strategy_exits")),
                settings={"strategy_entries": bool(cfg.get("strategy_entries")),
                          "strategy_exits": bool(cfg.get("strategy_exits"))}))

    try:
        for a in _plays_store(ctx).all():
            if want and a.symbol != want:
                continue
            out.append(_attach_row(index, a.symbol, make_id("play", a.play),
                                   source="state/options/plays.json",
                                   enabled=bool(a.enabled),
                                   settings=dict(a.params or {}),
                                   state="live" if a.enabled else "idle"))
    except Exception as e:                       # noqa: BLE001
        out.append({"symbol": want or "*", "id": "", "kind": "option-tailored",
                    "origin": None, "name": "the play assignments could not "
                    "be read", "enabled": False, "settings": {}, "state": "",
                    "source": "state/options/plays.json",
                    "trades": _gate(False, "unreadable"), "why": repr(e)})

    for r in _store_file(ctx).all():
        if want and r["symbol"] != want:
            continue
        out.append(_attach_row(index, r["symbol"], r["id"],
                               source=ATTACH_FILE, enabled=r["enabled"],
                               settings=r["settings"]))
    return sorted(out, key=lambda r: (r["symbol"], r["kind"] or "", r["id"]))


def _preset_keys(index: dict, pid: str) -> list:
    row = index.get(make_id("preset", pid))
    return [p["key"] for p in (row or {}).get("params_schema") or []]


ATTACH_NOTE = ("Attaching never arms. A ladder arrives stopped and in dry run; "
               "a play is assigned and the arm file is untouched.")


def attach(ctx: Any, symbol: str, entry_id: str, settings: Any = None, *,
           by: str = "", enabled: bool = True, force: bool = False) -> dict:
    """Put ANY bank entry on a ticker. One call, every kind.

        attach(ctx, "RAM", "preset:ladder_v3")
        attach(ctx, "RAM", "doc:rsi-dip-in-an-uptrend")
        attach(ctx, "SPY", "play:index-put-credit-spread", {"contracts": 5})
        attach(ctx, "SPY", "option:iron-condor")

    MULTIPLE per ticker: a ladder, an indicator document and any number of
    options entries live together. The two ladder-shaped kinds are the
    exception and they say so -- a ticker has exactly ONE engine config, so
    attaching a second ladder strategy REPLACES the first and the result names
    what it replaced rather than doing it quietly.

    Every branch delegates to that subsystem's own audited entry point
    (`hub.set_strategy` for the ladder, `optplays.Assignments` for a play), so
    nothing here becomes a second way to start a strategy. NOTHING HERE PLACES
    AN ORDER AND NOTHING HERE ARMS.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise BankError("a symbol is required")
    store, slug = parse_id(entry_id)
    eid = make_id(store, slug)
    row = get(eid)
    if not row["attach"]["ok"] and not force:
        raise BankError("%s cannot go on a ticker: %s"
                        % (row["name"], row["attach"]["why"]))
    over = dict(settings or {})
    was = [a for a in attached(ctx, sym)]
    out: dict = {"ok": True, "symbol": sym, "id": eid, "kind": row["kind"],
                 "origin": row["origin"], "name": row["name"],
                 "action": "attach", "armed": False, "note": ATTACH_NOTE,
                 "trades": row["trades"], "replaced": None}

    if store == "preset":
        out.update(_attach_ladder(ctx, sym, slug, row, over, by, was))
    elif store == "doc":
        out.update(_attach_document(ctx, sym, slug, over, by, was))
    elif store == "play":
        import hub
        out["detail"] = hub.set_strategy(ctx, sym, slug, action="attach",
                                         settings={**over, "enabled": enabled},
                                         by=by or "bank")
        out["store_written"] = "state/options/plays.json"
    elif store == "option":
        out["detail"] = _store_file(ctx).put(sym, eid, over, enabled=enabled,
                                             by=by or "bank")
        out["store_written"] = ATTACH_FILE
    else:
        raise BankError("%s entries cannot be attached" % store)
    return out


def _attach_ladder(ctx: Any, sym: str, slug: str, row: dict, over: dict,
                   by: str, was: list) -> dict:
    """A ladder bundle, through `hub.set_strategy` and so through
    `Engine.update_config`, where every guard and re-cover rule runs."""
    import hub
    doc = row["doc"] or {}
    base = dict(doc.get("settings") or {})
    if not base:
        raise BankError("%s has no settings to apply" % row["name"])
    unknown = sorted(k for k in over if k not in base)
    if unknown:
        # An override of a key the strategy does not set is almost always a
        # typo, and the engine would drop it without a word.
        import engine
        bad = [k for k in unknown if k not in engine.TICKER_DEFAULTS]
        if bad:
            raise BankError("the engine has no setting called %s"
                            % ", ".join(bad))
    patch = {**base, **over, "preset": slug}
    prev = next((a["id"] for a in was
                 if a["kind"] == "ladder" and a["id"]), None)
    res = hub.set_strategy(ctx, sym, "ladder", action="attach",
                           settings=patch, by=by or "bank")
    if res.get("already"):
        res = hub.set_strategy(ctx, sym, "ladder", action="configure",
                               settings=patch, by=by or "bank")
    return {"detail": res, "store_written": "config.json (the ticker's engine)",
            "replaced": prev if prev and prev != make_id("preset", slug) else None}


def _attach_document(ctx: Any, sym: str, slug: str, over: dict, by: str,
                     was: list) -> dict:
    """An indicator document, which DRIVES a ladder rather than replacing one.

    So the ticker needs an engine to carry it. If there is none, one is created
    on the default preset -- stopped and in dry run, like every ladder this
    module makes -- and the result says so, because a call that quietly builds
    an Engine is the exact surprise hub.py was written to end.
    """
    import hub
    created = False
    if sym not in _engines(ctx):
        import presets
        hub.set_strategy(ctx, sym, "ladder", action="attach",
                         settings={**presets.settings(presets.DEFAULT),
                                   "preset": presets.DEFAULT},
                         by=by or "bank")
        created = True
    patch = {"strategy": slug,
             "strategy_entries": bool(over.get("strategy_entries", True)),
             "strategy_exits": bool(over.get("strategy_exits", False))}
    for k, v in over.items():
        if k not in patch:
            patch[k] = v
    prev = next((a["id"] for a in was
                 if a["kind"] == "indicator" and a["id"]), None)
    res = hub.set_strategy(ctx, sym, "ladder", action="configure",
                           settings=patch, by=by or "bank")
    return {"detail": res, "created_ladder": created,
            "store_written": "config.json (the ticker's `strategy`)",
            "replaced": prev if prev and prev != make_id("doc", slug) else None,
            "carrier_note": (
                "a ladder was created on %s to carry this document; it is "
                "stopped and in dry run" % sym) if created else ""}


def detach(ctx: Any, symbol: str, entry_id: str, *, by: str = "",
           force: bool = False) -> dict:
    """Take one bank entry off one ticker. The inverse of `attach`, per kind.

    Detaching a LADDER strategy removes the ladder from the ticker, because on
    a ticker the ladder IS its settings; detaching an indicator document leaves
    the ladder running on its own rules, which is what it did before the
    document was attached.
    """
    sym = str(symbol or "").strip().upper()
    store, slug = parse_id(entry_id)
    eid = make_id(store, slug)
    out = {"ok": True, "symbol": sym, "id": eid, "action": "detach"}
    if store == "preset":
        import hub
        out["detail"] = hub.set_strategy(ctx, sym, "ladder", action="detach",
                                         by=by or "bank", force=force)
        out["store_written"] = "config.json (the ticker's engine)"
    elif store == "doc":
        import hub
        out["detail"] = hub.set_strategy(
            ctx, sym, "ladder", action="configure", by=by or "bank",
            settings={"strategy": "", "strategy_entries": False,
                      "strategy_exits": False})
        out["store_written"] = "config.json (the ticker's `strategy`)"
        out["note"] = ("the ladder stays on %s and goes back to its own entry "
                       "and exit rules" % sym)
    elif store == "play":
        import hub
        out["detail"] = hub.set_strategy(ctx, sym, slug, action="detach",
                                         by=by or "bank")
        out["store_written"] = "state/options/plays.json"
    elif store == "option":
        out["removed"] = _store_file(ctx).remove(sym, eid)
        out["store_written"] = ATTACH_FILE
    else:
        raise BankError("%s entries cannot be attached" % store)
    return out


# The registry contract the hub is written against says `list()`, so this
# module publishes one. A module-level `list` shadows the builtin at CALL time
# for every function in this file, which is why the runtime `isinstance(x,
# list)` checks above go through `_LIST` -- the builtin captured at the top,
# before this line existed. Adding a bare `isinstance(x, list)` anywhere in
# this module from here on is a bug that answers False for every real list.
list = entries                                                  # noqa: A001
