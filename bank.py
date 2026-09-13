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
import re
from pathlib import Path
from typing import Any, Optional

import btcode
import strategy as sdoc

ROOT = Path(__file__).resolve().parent
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
            if p == "kind" or not isinstance(v, (int, float)) or isinstance(v, bool):
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
    if isinstance(node, list):
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
        if not isinstance(operands, list) or len(operands) != 2:
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
        node = node[int(p)] if isinstance(node, list) else node[p]
    last = parts[-1]
    if isinstance(node, list):
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
