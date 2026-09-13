/* ============================================================================
   Research -> Builder -- build a strategy by clicking, not by writing JSON.

   This was its own rail entry. It is a research tool: it produces a document
   that the backtester runs and the engine can be pointed at, and it touches
   no live money, so it belongs beside the backtester rather than in a slot
   of its own two clicks away from it. "Backtest it" now hands the saved slug
   to the Backtest tab instead of jumping to an empty ladder run.

   A strategy is a document, and the document is the single source of truth: the
   builder edits it, the JSON pane shows it, the backtester runs it and the
   engine trades it. There is no second representation to drift out of sync.

   Rules are a tree. A group (all / any / none) holds conditions and other
   groups, which is exactly the shape strategy.py evaluates, so what you see is
   literally what runs.

   Beside it, in the rail, is the STRATEGY BANK: one shelf holding both kinds
   of strategy -- the documents built here by clicking, and the coded ones
   Claude writes, which used to land in a folder the dashboard never showed.
   The bank is next to the builder rather than on a page of its own because
   the three things you do with a strategy -- look at it, turn its numbers,
   change its shape -- are one train of thought, and walking between rooms in
   the middle of it is how people end up tuning the wrong strategy.
   ========================================================================= */
"use strict";
import {
  S, GET, POST, act, ask, toast, el, esc, card, tableHTML, go, dur,
} from "../core.js";
import { preset as btPreset } from "./backtest.js";

let CAT = {};              // indicator catalogue from the server
let LIST = [];             // saved strategies
let spec = null;           // the document being edited
let slug = "";             // what it was loaded from ("" = new)
let dirty = false;
let vErr = "";
let showJSON = false;

/* Price fields plus every indicator output, as "ref" strings. */
function refs() {
  const out = [
    ["close", "close"], ["open", "open"], ["high", "high"],
    ["low", "low"], ["volume", "volume"],
  ];
  for (const [name, d] of Object.entries(spec.indicators || {})) {
    const c = CAT[d.kind];
    for (const o of (c ? c.outputs : ["value"])) {
      out.push([`${name}.${o}`, `${name}.${o}`]);
    }
  }
  return out;
}

const OPS = [
  ["gt", "is above"], ["lt", "is below"],
  ["gte", "is at or above"], ["lte", "is at or below"], ["eq", "equals"],
  ["cross_above", "crosses above"], ["cross_below", "crosses below"],
  ["rising", "is rising"], ["falling", "is falling"],
  ["between", "is between"],
  ["target_reached", "the target is reached"],
  ["stop_hit", "the stop is hit"],
];
const ONE_ARG = new Set(["rising", "falling"]);
const NO_ARG = new Set(["target_reached", "stop_hit"]);
const GROUPS = [["all", "ALL of these are true"], ["any", "ANY of these is true"],
                ["not", "NONE of these is true"]];

const BLANK = () => ({
  name: "New strategy",
  indicators: {},
  entry: { all: [{ lt: ["close", "open"] }] },
  exit: { any: [{ target_reached: true }] },
  target: { points: 0.10 },
  stop: {},
});

/* ------------------------------------------------------------- rule model */
function kindOf(node) {
  if (node === true || node === false || node == null) return "const";
  const k = Object.keys(node)[0];
  if (k === "all" || k === "any" || k === "not") return "group";
  return "cond";
}

/* Address a node by a path of indices, e.g. [0,2] = child 2 of child 0. */
function at(root, path) {
  let n = root;
  for (const i of path) n = n[Object.keys(n)[0]][i];
  return n;
}
function parentOf(root, path) {
  return path.length ? at(root, path.slice(0, -1)) : null;
}

function setRoot(which, node) { spec[which] = node; dirty = true; }

/* ------------------------------------------------------------------ views */
export const BUILDER = {
  async mount() {
    el("view").innerHTML = `<div class="faint">Loading the indicator catalogue…</div>`;
    try {
      const r = await GET("/api/strategies");
      CAT = r.indicators || {};
      LIST = r.strategies || [];
    } catch (e) {
      el("view").innerHTML = `<div class="note bad">Could not load strategies:
        ${esc(e.message)}</div>`;
      return;
    }
    if (!spec) spec = BLANK();
    paintShell();
    renderAll();
    loadBank();          // the shelf fills itself in; the builder never waits
  },
};

function paintShell() {
  el("view").innerHTML = `
    <div class="grid main">
      <div>
        ${card("Strategy", `
          <label class="f"><span>Name</span>
            <input id="stName" value="${esc(spec.name || "")}"></label>
          <div class="hint">Saved under a slug made from this name. Renaming
            saves a <b>copy</b>; the old slug keeps running wherever it is set.</div>
          <div id="stValid"></div>`)}

        ${card("Indicators", `<div id="stInds"></div>
          <div class="row-btns" style="margin-top:12px">
            <select id="stIndKind" style="width:auto"></select>
            <button class="btn sm" id="stIndAdd">Add indicator</button>
          </div>
          <div class="tip">Every indicator returns a full series aligned to the
            bars, with <b>no value</b> — never a zero — where it has not formed
            yet. A rule that references an unformed value is false, so a
            strategy can never trade on a warm-up artefact.</div>`,
          "", { flush: false })}

        ${card("Entry — open a lot when", `<div id="stEntry"></div>`)}
        ${card("Exit — close a lot when", `<div id="stExit"></div>`)}

        ${card("Target and stop", `
          <div class="f2">
            <label class="f"><span>Target</span>
              <select id="stTgtMode">
                <option value="points">a fixed $/share</option>
                <option value="percent">a % of entry</option>
                <option value="atr_mult">a multiple of an indicator (ATR)</option>
                <option value="none">none — exit rules only</option>
              </select></label>
            <label class="f"><span>Value</span>
              <input id="stTgtVal" type="number" step="0.01"></label>
            <label class="f"><span>Stop</span>
              <select id="stStpMode">
                <option value="none">none</option>
                <option value="points">a fixed $/share</option>
                <option value="percent">a % of entry</option>
                <option value="atr_mult">a multiple of an indicator (ATR)</option>
              </select></label>
            <label class="f"><span>Value</span>
              <input id="stStpVal" type="number" step="0.01"></label>
          </div>
          <div id="stAtrRow"></div>
          <div class="tip"><b>A stop is not optional in a backtest.</b> Without
            one the backtester holds every losing trade to the end of the window
            and reports a win rate that cannot happen live. Where a bar touches
            both, the stop is taken first — 1-minute bars cannot say which came
            first, and assuming the good one is how a backtest lies.</div>`)}

        ${card("Document", `
          <div class="row-btns" style="margin-bottom:10px">
            <button class="btn sm" id="stToggleJSON">Show JSON</button>
            <button class="btn sm" id="stApplyJSON" style="display:none">Apply JSON</button>
          </div>
          <div id="stJSON" style="display:none"></div>`)}
      </div>

      <div>
        ${card("Strategy Bank", `
          <div class="bk-lede">Every strategy there is, on one shelf: the
            documents built here by clicking, and the Python ones Claude
            writes. <b>View</b> opens the whole thing; <b>Settings</b> turns
            its numbers without touching its shape.</div>
          <div id="stBank" class="bk-list"></div>`,
          `<span id="stBankN"></span>
           <button class="btn sm" id="stBankRefresh">Refresh</button>`,
          { cls: "hero bk", id: "stBankCard" })}

        ${card("Actions", `
          <div class="row-btns">
            <button class="btn primary sm" id="stSave">Save</button>
            <button class="btn sm" id="stTest">Backtest it</button>
            <button class="btn sm" id="stNew">New</button>
          </div>
          <div class="tip" id="stSaveNote">Saving validates first. A strategy
            that will not validate is never written, so the engine can never
            load a broken one.</div>`)}
        ${card("Saved", `<div id="stList"></div>`, "", { flush: true })}
        ${card("Reference", `
          <div class="tip" style="margin-top:0">
            <b>References</b> — <code>close open high low volume</code>, an
            indicator output as <code>name.output</code>, or a plain number.<br><br>
            <b>Everything runs on closed bars.</b> A signal computed on a bar's
            close is filled at the <i>next</i> bar's open, both here and in the
            backtester. That one rule is the difference between a backtest and
            a fantasy.<br><br>
            <b>To trade one</b>, set a ticker's <i>Strategy slug</i> in its
            settings and switch on <i>Strategy decides entries</i> or
            <i>exits</i>. Both are off by default.
          </div>`)}
      </div>
    </div>`;

  el("stIndKind").innerHTML = Object.keys(CAT).map(
    (k) => `<option value="${k}">${k}</option>`).join("");

  el("stName").oninput = () => { spec.name = el("stName").value; dirty = true; };
  el("stIndAdd").onclick = addIndicator;
  el("stBankRefresh").onclick = () => { bankLoaded = false; renderBank(); loadBank(); };
  el("stSave").onclick = save;
  el("stNew").onclick = () => act(async () => {
    if (dirty && !(await ask({ title: "Discard changes?",
      body: "This strategy has unsaved edits.", ok: "Discard" }))) return;
    spec = BLANK(); slug = ""; dirty = false; vErr = "";
    paintShell(); renderAll();
  });
  /* Save first, then hand the SLUG to the backtester in strategy mode. This
     used to save and navigate, leaving the Backtest tab in ladder mode on
     whatever symbol happened to be first -- it carried nothing at all. */
  el("stTest").onclick = () => act(async () => {
    if (!slug || dirty) await save();
    if (!slug) return;
    btPreset({
      mode: "strategy", strategy: slug, label: spec.name || slug,
      from: "the strategy builder",
      note: `Running the saved document "${slug}". Pick a symbol and a window, `
          + `then Run backtest.`,
    });
    go({ kind: "research", tab: "backtest" });
  });
  el("stToggleJSON").onclick = () => {
    showJSON = !showJSON;
    el("stJSON").style.display = showJSON ? "" : "none";
    el("stApplyJSON").style.display = showJSON ? "" : "none";
    el("stToggleJSON").textContent = showJSON ? "Hide JSON" : "Show JSON";
    renderJSON();
  };
  el("stApplyJSON").onclick = () => act(async () => {
    let next;
    try { next = JSON.parse(el("stRaw").value); }
    catch (e) { toast("That is not valid JSON: " + esc(e.message), "err"); return; }
    spec = next; dirty = true;
    paintShell(); renderAll();
    toast("Applied. Nothing is saved until you press Save.", "ok");
  });

  for (const id of ["stTgtMode", "stTgtVal", "stStpMode", "stStpVal"]) {
    el(id).onchange = readTargets;
    el(id).oninput = readTargets;
  }
  writeTargets();
}

function renderAll() {
  renderIndicators();
  renderRules("entry", "stEntry");
  renderRules("exit", "stExit");
  renderList();
  renderBank();          // paintShell() throws the rail away; the shelf is redrawn
  renderJSON();
  validate();
}

/* ------------------------------------------------------------- indicators */
function addIndicator() {
  const kind = el("stIndKind").value;
  let n = kind, i = 2;
  while (spec.indicators[n]) n = kind + i++;
  spec.indicators[n] = { kind, ...JSON.parse(JSON.stringify(CAT[kind].params || {})) };
  dirty = true;
  // the rule dropdowns are built from the indicator list, so they have to be
  // rebuilt too -- otherwise the new outputs are simply not offered, and
  // picking one silently writes an empty reference
  renderAll();
}

function renderIndicators() {
  const host = el("stInds");
  const rows = Object.entries(spec.indicators || {});
  if (!rows.length) {
    host.innerHTML = `<div class="faint">None. A strategy with no indicators is
      legal — it can still trade off the bar itself (close below open, and so on).</div>`;
    return;
  }
  host.innerHTML = rows.map(([name, d]) => {
    const c = CAT[d.kind] || { params: {}, outputs: [] };
    const ps = Object.keys(c.params || {}).map((p) => `
      <label class="f" style="margin:0">
        <span style="min-width:0">${p}</span>
        <input data-ip="${esc(name)}" data-pk="${p}" type="number" step="any"
               value="${esc(d[p] ?? c.params[p])}" style="width:88px"></label>`).join("");
    return `<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;
                padding:9px 0;border-bottom:1px solid var(--hairline)">
      <input data-iname="${esc(name)}" value="${esc(name)}" style="width:110px">
      <span class="pill acc">${esc(d.kind)}</span>
      ${ps}
      <span class="faint" style="font-size:11px;flex:1">→ ${
        (c.outputs || []).map((o) => `${esc(name)}.${o}`).join(", ")}</span>
      <button class="btn sm" data-idel="${esc(name)}">×</button>
    </div>`;
  }).join("");

  host.querySelectorAll("[data-ip]").forEach((i) => {
    i.onchange = () => {
      const v = Number(i.value);
      spec.indicators[i.dataset.ip][i.dataset.pk] = Number.isFinite(v) ? v : i.value;
      dirty = true; renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-iname]").forEach((i) => {
    i.onchange = () => {
      const from = i.dataset.iname, to = i.value.trim();
      if (!to || to === from) { i.value = from; return; }
      if (spec.indicators[to]) { toast("That name is already used.", "err"); i.value = from; return; }
      const d = spec.indicators[from];
      delete spec.indicators[from];
      spec.indicators[to] = d;
      // rewrite every rule that referenced the old name, so a rename can never
      // silently leave a rule pointing at an indicator that no longer exists
      const fix = (s) => (typeof s === "string" && s.startsWith(from + "."))
        ? to + s.slice(from.length) : s;
      walk(spec.entry, fix); walk(spec.exit, fix);
      if (spec.target?.indicator) spec.target.indicator = fix(spec.target.indicator);
      if (spec.stop?.indicator) spec.stop.indicator = fix(spec.stop.indicator);
      dirty = true; renderAll();
    };
  });
  host.querySelectorAll("[data-idel]").forEach((b) => {
    b.onclick = () => {
      delete spec.indicators[b.dataset.idel];
      dirty = true; renderAll();
    };
  });
}

/* Rewrite every operand string in a rule tree. */
function walk(node, fn) {
  if (!node || typeof node !== "object") return;
  for (const [k, v] of Object.entries(node)) {
    if (Array.isArray(v)) {
      if (k === "all" || k === "any" || k === "not") v.forEach((c) => walk(c, fn));
      else node[k] = v.map(fn);
    } else if (typeof v === "string") node[k] = fn(v);
    else walk(v, fn);
  }
}

/* ------------------------------------------------------------------ rules */
function renderRules(which, hostId) {
  const host = el(hostId);
  let node = spec[which];
  if (node === undefined || node === null) node = false;
  if (kindOf(node) !== "group") {
    // normalise a bare condition or constant into a group so it can be edited
    node = { all: node === false || node === true ? [] : [node] };
    setRoot(which, node);
  }
  host.innerHTML = groupHTML(node, [], which);
  wire(host, which);
}

function groupHTML(node, path, which) {
  const g = Object.keys(node)[0];
  const kids = node[g] || [];
  const p = path.join(".");
  return `<div class="rule-g" data-path="${p}">
    <div class="rule-h">
      <select data-gsel="${p}" data-w="${which}" style="width:auto">
        ${GROUPS.map(([k, l]) => `<option value="${k}"${k === g ? " selected" : ""}>${l}</option>`).join("")}
      </select>
      <span style="flex:1"></span>
      <button class="btn sm" data-addc="${p}" data-w="${which}">+ condition</button>
      <button class="btn sm" data-addg="${p}" data-w="${which}">+ group</button>
      ${path.length ? `<button class="btn sm" data-del="${p}" data-w="${which}">×</button>` : ""}
    </div>
    ${kids.length
      ? kids.map((k, i) => kindOf(k) === "group"
          ? groupHTML(k, path.concat(i), which)
          : condHTML(k, path.concat(i), which)).join("")
      : `<div class="faint" style="padding:6px 2px">Empty. Add a condition —
          the validator rejects an empty group rather than guess what it meant.</div>`}
  </div>`;
}

function condHTML(node, path, which) {
  const op = Object.keys(node)[0];
  const raw = node[op];
  const args = Array.isArray(raw) ? raw : [raw];
  const p = path.join(".");
  const opt = refs().map(([v, l]) =>
    `<option value="${esc(v)}">${esc(l)}</option>`).join("");

  const operand = (i, v) => {
    const isRef = typeof v === "string";
    return `<span class="opnd">
      <select data-otype="${p}:${i}" data-w="${which}" style="width:auto">
        <option value="ref"${isRef ? " selected" : ""}>value</option>
        <option value="num"${isRef ? "" : " selected"}>number</option>
      </select>
      ${isRef
        ? `<select data-oref="${p}:${i}" data-w="${which}" style="width:auto">${opt}</select>`
        : `<input data-onum="${p}:${i}" data-w="${which}" type="number" step="any"
                  value="${esc(v)}" style="width:96px">`}
    </span>`;
  };

  const verb = `<select data-op="${p}" data-w="${which}" style="width:auto">
      ${OPS.map(([k, l]) => `<option value="${k}"${k === op ? " selected" : ""}>${l}</option>`).join("")}
    </select>`;

  // the row reads as a sentence -- subject, verb, object -- so the left operand
  // is rendered BEFORE the verb wherever the condition has one
  let body;
  if (NO_ARG.has(op)) body = verb;
  else if (ONE_ARG.has(op)) body = operand(0, args[0]) + verb;
  else if (op === "between") {
    body = operand(0, args[0]) + verb + operand(1, args[1])
         + `<span class="faint">and</span>` + operand(2, args[2]);
  } else body = operand(0, args[0]) + verb + operand(1, args[1]);

  return `<div class="rule-c" data-path="${p}">
    ${body}
    <span style="flex:1"></span>
    <button class="btn sm" data-del="${p}" data-w="${which}">×</button>
  </div>`;
}

function wire(host, which) {
  const root = () => spec[which];
  const toPath = (s) => (s === "" ? [] : s.split(".").map(Number));

  host.querySelectorAll("[data-gsel]").forEach((s) => {
    s.onchange = () => {
      const n = at(root(), toPath(s.dataset.gsel));
      const old = Object.keys(n)[0];
      const kids = n[old];
      delete n[old];
      n[s.value] = kids;
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-addc]").forEach((b) => {
    b.onclick = () => {
      const n = at(root(), toPath(b.dataset.addc));
      n[Object.keys(n)[0]].push({ gt: ["close", "open"] });
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-addg]").forEach((b) => {
    b.onclick = () => {
      const n = at(root(), toPath(b.dataset.addg));
      n[Object.keys(n)[0]].push({ any: [] });
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => {
      const path = toPath(b.dataset.del);
      const par = parentOf(root(), path);
      par[Object.keys(par)[0]].splice(path[path.length - 1], 1);
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });
  host.querySelectorAll("[data-op]").forEach((s) => {
    s.onchange = () => {
      const n = at(root(), toPath(s.dataset.op));
      const old = Object.keys(n)[0];
      const args = Array.isArray(n[old]) ? n[old] : [n[old]];
      delete n[old];
      const op = s.value;
      n[op] = NO_ARG.has(op) ? true
        : ONE_ARG.has(op) ? (args[0] ?? "close")
        : op === "between" ? [args[0] ?? "close", args[1] ?? 0, args[2] ?? 100]
        : [args[0] ?? "close", args[1] ?? "open"];
      dirty = true; renderRules(which, host.id); renderJSON(); validate();
    };
  });

  const setArg = (key, value) => {
    const [ps, is] = key.split(":");
    if (value === "" || value == null) return;   // never write an empty reference
    const n = at(root(), toPath(ps));
    const op = Object.keys(n)[0];
    if (Array.isArray(n[op])) n[op][Number(is)] = value;
    else n[op] = value;
    dirty = true; renderJSON(); validate();
  };
  host.querySelectorAll("[data-oref]").forEach((s) => {
    // a select cannot carry a "selected" attribute we did not know about, so
    // the current value is set here rather than in the markup
    const [ps, is] = s.dataset.oref.split(":");
    const n = at(root(), toPath(ps));
    const op = Object.keys(n)[0];
    const cur = Array.isArray(n[op]) ? n[op][Number(is)] : n[op];
    if (cur != null) {
      s.value = String(cur);
      if (s.value !== String(cur)) {
        // a reference to an indicator that no longer exists: show it rather
        // than quietly snapping to the first option
        s.insertAdjacentHTML("afterbegin",
          `<option value="${esc(cur)}" selected>${esc(cur)} (missing)</option>`);
        s.value = String(cur);
      }
    }
    s.onchange = () => setArg(s.dataset.oref, s.value);
  });
  host.querySelectorAll("[data-onum]").forEach((i) => {
    i.onchange = () => setArg(i.dataset.onum, Number(i.value));
  });
  host.querySelectorAll("[data-otype]").forEach((s) => {
    s.onchange = () => {
      setArg(s.dataset.otype, s.value === "ref" ? "close" : 0);
      renderRules(which, host.id);
    };
  });
}

/* ------------------------------------------------------- target and stop */
function modeOf(d) {
  if (!d || !Object.keys(d).length) return "none";
  for (const k of ["points", "percent", "atr_mult"]) if (d[k] != null) return k;
  return "none";
}

function writeTargets() {
  const tm = modeOf(spec.target), sm = modeOf(spec.stop);
  el("stTgtMode").value = tm;
  el("stTgtVal").value = tm === "none" ? "" : spec.target[tm];
  el("stStpMode").value = sm;
  el("stStpVal").value = sm === "none" ? "" : spec.stop[sm];
  const need = tm === "atr_mult" || sm === "atr_mult";
  el("stAtrRow").innerHTML = need ? `
    <label class="f"><span>ATR indicator to use</span>
      <select id="stAtrRef">${
        Object.entries(spec.indicators || {})
          .filter(([, d]) => (CAT[d.kind]?.outputs || []).length)
          .flatMap(([n, d]) => (CAT[d.kind]?.outputs || []).map((o) => `${n}.${o}`))
          .map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join("")
        || `<option value="">— add an ATR indicator first —</option>`}
      </select></label>
    <div class="hint">A multiple of a volatility measure, so the same strategy
      means the same thing on a $12 stock and a $500 one.</div>` : "";
  if (need) {
    const r = el("stAtrRef");
    const cur = spec.target?.indicator || spec.stop?.indicator;
    if (cur) r.value = cur;
    r.onchange = () => {
      if (modeOf(spec.target) === "atr_mult") spec.target.indicator = r.value;
      if (modeOf(spec.stop) === "atr_mult") spec.stop.indicator = r.value;
      dirty = true; renderJSON(); validate();
    };
  }
}

function readTargets() {
  const build = (mode, val, prev) => {
    if (mode === "none") return {};
    const d = { [mode]: Number(val) || 0 };
    if (mode === "atr_mult") d.indicator = (el("stAtrRef") || {}).value || prev?.indicator || "";
    return d;
  };
  spec.target = build(el("stTgtMode").value, el("stTgtVal").value, spec.target);
  spec.stop = build(el("stStpMode").value, el("stStpVal").value, spec.stop);
  dirty = true;
  writeTargets(); renderJSON(); validate();
}

/* ------------------------------------------------------------------- I/O */
async function validate() {
  try {
    const r = await POST("/api/strategies/validate", spec);
    vErr = r.ok ? "" : (r.error || "invalid");
  } catch (e) { vErr = e.message; }
  const host = el("stValid");
  if (!host) return;
  host.innerHTML = vErr
    ? `<div class="note bad" style="margin-top:4px"><b>Will not run:</b> ${esc(vErr)}</div>`
    : `<div class="note good" style="margin-top:4px">Valid — this will run.</div>`;
  const b = el("stSave");
  if (b) b.disabled = !!vErr;
}

async function save() {
  return act(async () => {
    const r = await POST("/api/strategies", spec);
    slug = r.slug;
    dirty = false;
    const l = await GET("/api/strategies");
    LIST = l.strategies || [];
    renderList();
    // a strategy that was just built must be on the shelf without a reload:
    // the bank is where it is looked at and tuned from now on
    loadBank();
    toast(`Saved as <b>${esc(slug)}</b>. It is on the shelf in the Strategy Bank. `
          + `Set that slug on a ticker to trade it.`, "ok", 8000);
  });
}

function renderList() {
  const host = el("stList");
  if (!host) return;
  host.innerHTML = tableHTML(["Strategy", "Slug", ""],
    LIST.map((s) => `<tr>
      <td style="text-align:left">${esc(s.name || s.slug)}${
        s.builtin ? ` <span class="pill">built-in</span>` : ""}</td>
      <td class="faint mono" style="text-align:left">${esc(s.slug)}</td>
      <td><button class="btn sm" data-load="${esc(s.slug)}">Open</button></td>
    </tr>`), "None saved yet.");
  host.querySelectorAll("[data-load]").forEach((b) => {
    b.onclick = () => act(() => openSlug(b.dataset.load));
  });
}

/* Load a saved document into the builder. ONE path: the Saved list and the
   bank's "Load into the builder" both come through here, so a change to what
   loading means cannot land on one of them and not the other.
   Returns false when the operator kept their unsaved work. */
async function openSlug(want) {
  if (dirty && !(await ask({ title: "Discard changes?",
    body: "The strategy open now has unsaved edits.", ok: "Discard" }))) return false;
  const r = await GET("/api/strategies/" + encodeURIComponent(want));
  spec = r.spec;
  slug = r.slug || want;        // the slug is what it was loaded from, always
  dirty = false;
  paintShell(); renderAll();
  return true;
}

function renderJSON() {
  const host = el("stJSON");
  if (!host || !showJSON) return;
  const keep = document.activeElement && document.activeElement.id === "stRaw";
  if (keep) return;                        // never fight the operator's cursor
  host.innerHTML = `<textarea id="stRaw" rows="18" spellcheck="false"
    style="font-family:var(--mono);font-size:12px">${
      esc(JSON.stringify(spec, null, 2))}</textarea>`;
}

/* ==========================================================================
   THE STRATEGY BANK

   Two kinds of strategy share this shelf and the pane never pretends they are
   the same thing: a "clicked" one is a document (indicators and a rule tree,
   editable in the builder below), a "python" one is a file with an `on_bar`,
   which is what every strategy Claude writes is. Both are listed newest
   first, both open, and both can have their numbers turned -- but only their
   NUMBERS. `bank.py` refuses a path that is not already a knob on that
   strategy, so a settings panel can never quietly restructure something you
   later have to explain. Adding an indicator or rewriting a rule stays in the
   builder, where it is visibly a change of shape.
   ========================================================================= */
let BANK = [];           // GET /api/bank
let bankErr = "";
let bankLoaded = false;
let CUR = null;          // the detail open in the sheet
let sheet = null;        // the overlay itself, or null

const kindWord = (k) => (k === "doc" ? "clicked" : "python");
const kindWhat = (k) => (k === "doc"
  ? "a document: indicators and rules, built by clicking"
  : "real Python with an on_bar, run in a process of its own");

/* The bank is a SHARED library -- the same shelf whichever account is
   selected, exactly like /api/strategies and /api/code. core.js's api() only
   leaves a path unprefixed when it is listed in SHARED_API, and this view may
   not edit core.js, so these three calls go straight out rather than being
   rewritten to /api/a/<acct>/bank, which the server does not serve. Same
   contract as core's req(): a deadline, and the server's own `detail` as the
   error message. */
async function bankReq(method, path, body) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), method === "GET" ? 15000 : 30000);
  let r;
  try {
    r = await fetch(path, {
      method,
      headers: body !== undefined ? { "content-type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store",
      signal: ctl.signal,
    });
  } catch (e) {
    throw new Error(e.name === "AbortError"
      ? `${method} ${path} did not answer in time`
      : (e.message || String(e)));
  } finally {
    clearTimeout(t);
  }
  const txt = await r.text();
  if (!r.ok) {
    let m = txt;
    try { m = JSON.parse(txt).detail || txt; } catch (e) { /* plain text */ }
    const err = new Error(m || r.statusText);
    err.status = r.status;
    throw err;
  }
  return txt ? JSON.parse(txt) : {};
}

const bankPath = (kind, slug, tail = "") =>
  `/api/bank/${encodeURIComponent(kind)}/${encodeURIComponent(slug)}${tail}`;

async function loadBank() {
  try {
    const r = await bankReq("GET", "/api/bank");
    BANK = r.strategies || [];
    bankErr = "";
  } catch (e) {
    bankErr = e.message;
  }
  bankLoaded = true;
  renderBank();
}

/* ------------------------------------------------------------- the shelf */
function renderBank() {
  const host = el("stBank");
  if (!host) return;
  const n = el("stBankN");
  if (n) n.textContent = bankLoaded && !bankErr
    ? `${BANK.length} on the shelf` : "";

  if (!bankLoaded) {
    host.innerHTML = `<div class="bk-faint">Reading the shelf…</div>`;
    return;
  }
  if (bankErr) {
    host.innerHTML = `<div class="note bad" style="margin:0">
      <b>Could not read the bank.</b> ${esc(bankErr)}</div>`;
    return;
  }
  if (!BANK.length) {
    host.innerHTML = `<div class="bk-faint">Nothing on the shelf yet. Build one
      below and save it, or ask Claude for one — every strategy it writes
      lands here.</div>`;
    return;
  }
  host.innerHTML = BANK.map(bankRow).join("");
  host.querySelectorAll("[data-bkv]").forEach((b) => {
    b.onclick = () => openView(b.dataset.bkk, b.dataset.bkv);
  });
  host.querySelectorAll("[data-bks]").forEach((b) => {
    b.onclick = () => openSettings(b.dataset.bkk, b.dataset.bks);
  });
}

function bankRow(s) {
  const k = s.kind === "doc" ? "doc" : "code";
  const n = Number(s.tunable_count) || 0;
  return `<div class="bk-row">
    <div class="bk-row-t">
      <span class="bk-name">${esc(s.name || s.slug)}</span>
      <span class="pill${k === "doc" ? " acc" : ""}" title="${esc(kindWhat(k))}"
        >${kindWord(k)}</span>
    </div>
    ${s.note
      ? `<div class="bk-note">${esc(s.note)}</div>`
      : `<div class="bk-note bk-none">No description — nobody will know what
           this is in six months.</div>`}
    ${s.error ? `<div class="bk-err">Will not load: ${esc(s.error)}</div>` : ""}
    <div class="bk-meta" title="${esc(changedFull(s.modified))}">
      ${n ? `${n} setting${n === 1 ? "" : "s"}` : "nothing to tune"}
      <span class="bk-dot">·</span>${esc(changedText(s.modified))}</div>
    <div class="row-btns bk-acts">
      <button class="btn sm" data-bkk="${esc(k)}" data-bkv="${esc(s.slug)}">View</button>
      <button class="btn sm" data-bkk="${esc(k)}" data-bks="${esc(s.slug)}">Settings</button>
    </div>
  </div>`;
}

function changedText(ts) {
  const t = Number(ts) || 0;
  if (!t) return "changed — date unknown";
  const ago = Date.now() / 1000 - t;
  return ago < 45 ? "changed just now" : `changed ${dur(ago)} ago`;
}
function changedFull(ts) {
  const t = Number(ts) || 0;
  return t ? new Date(t * 1000).toLocaleString() : "no date on the file";
}

/* ------------------------------------------------------------- the sheet */
function onSheetKey(e) { if (e.key === "Escape") closeSheet(); }

function closeSheet() {
  if (sheet) sheet.remove();
  sheet = null;
  CUR = null;
  document.removeEventListener("keydown", onSheetKey);
}

/* One overlay, repainted. View and Settings are two faces of the same card,
   so switching between them must not feel like leaving and arriving. */
function paintSheet(o) {
  if (!sheet) {
    sheet = document.createElement("div");
    sheet.className = "veil bk-veil";
    document.body.appendChild(sheet);
    sheet.addEventListener("click", (e) => { if (e.target === sheet) closeSheet(); });
    document.addEventListener("keydown", onSheetKey);
  }
  sheet.innerHTML = `<div class="bk-sheet" role="dialog" aria-modal="true">
    <div class="bk-sheet-h">
      <div class="bk-sheet-t">
        <span class="bk-sheet-n">${esc(o.title || "")}</span>
        ${o.kind ? `<span class="pill${o.kind === "doc" ? " acc" : ""}"
          title="${esc(kindWhat(o.kind))}">${kindWord(o.kind)}</span>` : ""}
        ${o.sub ? `<div class="bk-sheet-s">${o.sub}</div>` : ""}
      </div>
      <button class="btn sm" id="bkX">Close</button>
    </div>
    <div class="bk-sheet-b" id="bkBody">${o.body || ""}</div>
    ${o.foot ? `<div class="bk-sheet-f">${o.foot}</div>` : ""}
  </div>`;
  el("bkX").onclick = closeSheet;
}

function sheetError(kind, slug, msg) {
  paintSheet({
    title: slug, kind,
    body: `<div class="note bad" style="margin:0"><b>Could not open it.</b>
      ${esc(msg)}</div>`,
    foot: `<button class="btn" data-bkclose>Close</button>`,
  });
  wireFoot();
}

function wireFoot() {
  if (!sheet) return;
  sheet.querySelectorAll("[data-bkclose]").forEach((b) => { b.onclick = closeSheet; });
}

/* ------------------------------------------------------------------ VIEW */
async function openView(kind, slug) {
  paintSheet({ title: slug, kind, body: `<div class="bk-faint">Reading it…</div>` });
  let d;
  try { d = await bankReq("GET", bankPath(kind, slug)); }
  catch (e) { sheetError(kind, slug, e.message); return; }
  CUR = d;
  renderView();
}

function renderView() {
  const d = CUR;
  const body = d.kind === "doc" ? viewDoc(d) : viewCode(d);
  paintSheet({
    title: d.name || d.slug, kind: d.kind,
    sub: `<span class="mono">${esc(d.slug)}</span>`,
    body,
    foot: `${d.kind === "doc"
        ? `<button class="btn" id="bkLoad">Load into the builder</button>` : ""}
      <button class="btn" id="bkTest">Backtest it</button>
      <button class="btn" id="bkToSet">Settings</button>
      <span style="flex:1"></span>
      <button class="btn" data-bkclose>Close</button>`,
  });
  wireFoot();
  if (el("bkLoad")) {
    el("bkLoad").onclick = () => act(async () => {
      const slugWanted = d.slug;
      if (!(await openSlug(slugWanted))) return;   // unsaved work was kept
      closeSheet();
      toast(`<b>${esc(d.name || slugWanted)}</b> is open in the builder. `
          + `Saving writes it back to the same slug.`, "ok");
    });
  }
  el("bkTest").onclick = () => toBacktest(d);
  el("bkToSet").onclick = () => openSettings(d.kind, d.slug);
}

const bkSec = (t, body) => `<section class="bk-sec"><h4>${t}</h4>${body}</section>`;

function noteBlock(note) {
  return note
    ? `<div class="bk-lead">${esc(note)}</div>`
    : `<div class="bk-lead bk-none">No description. Nothing in the file says
        what this is for.</div>`;
}

function viewDoc(d) {
  return `${noteBlock(d.note)}
    ${bkSec("Indicators", indsHTML(d.indicators))}
    ${bkSec("Entry — open a lot when", ruleHTML(d.entry))}
    ${bkSec("Exit — close a lot when", ruleHTML(d.exit))}
    <div class="bk-two">
      ${bkSec("Target", `<div class="bk-rule">${
        limitText(d.target, "none — the exit rules decide")}</div>`)}
      ${bkSec("Stop", `<div class="bk-rule">${limitText(d.stop,
        `<span class="bk-none">none — nothing here closes a losing lot</span>`)}</div>`)}
    </div>
    ${d.sizing && Object.keys(d.sizing).length
      ? bkSec("Sizing", kvHTML(d.sizing)) : ""}
    <details class="bk-det"><summary>The document as JSON</summary>
      <pre class="bk-pre">${esc(d.source || "")}</pre></details>`;
}

function viewCode(d) {
  const params = Object.entries(d.params || {});
  return `${noteBlock(d.note)}
    ${bkSec("What the file says about itself", d.doc
      ? `<pre class="bk-pre bk-doc">${esc(d.doc)}</pre>`
      : `<div class="bk-none">No docstring at all — this file explains
          nothing about itself.</div>`)}
    ${bkSec("Parameters it actually runs with", params.length
      ? `${tableHTML(["Parameter", "Value"], params.map(([k, v]) =>
          `<tr><td style="text-align:left" class="mono">${esc(k)}</td>
               <td style="text-align:left" class="num">${esc(fmtVal(v))}</td></tr>`))}
         <div class="bk-foot">The EFFECTIVE values: where a later
           <code>PARAMS.update</code> overrides an earlier <code>PARAMS</code>,
           these are the ones that win.</div>`
      : `<div class="bk-none">It declares no <code>PARAMS</code>, so there is
          nothing to turn from outside the file.</div>`)}
    ${bkSec("Functions it defines", pillsHTML(d.functions,
      "none — this file defines no functions, which means it has no on_bar"))}
    ${bkSec("Indicators it calls", pillsHTML(d.indicators,
      "none — it works straight off the bars"))}
    ${bkSec("Source", `<pre class="bk-pre bk-src">${esc(d.source || "")}</pre>`)}`;
}

const fmtVal = (v) => (typeof v === "boolean" ? (v ? "on" : "off")
  : v === null || v === undefined ? "—" : String(v));

function pillsHTML(list, none) {
  const a = (list || []).filter(Boolean);
  if (!a.length) return `<div class="bk-none">${none}</div>`;
  return `<div class="bk-pills">${a.map((x) =>
    `<span class="pill mono">${esc(x)}</span>`).join("")}</div>`;
}

function kvHTML(o) {
  return `<div class="bk-kv">${Object.entries(o || {}).map(([k, v]) =>
    `<div><span class="bk-faint">${esc(k)}</span>
      <b class="num">${esc(fmtVal(v))}</b></div>`).join("")}</div>`;
}

function indsHTML(inds) {
  const rows = Object.entries(inds || {});
  if (!rows.length) {
    return `<div class="bk-faint">None. It trades off the bar itself — a close
      below an open, and so on.</div>`;
  }
  return `<div class="bk-inds">${rows.map(([name, c]) => {
    const ps = Object.entries(c || {}).filter(([k]) => k !== "kind")
      .map(([k, v]) => `${k} ${fmtVal(v)}`).join(" · ");
    return `<div class="bk-ind">
      <span class="mono bk-ind-n">${esc(name)}</span>
      <span class="pill acc">${esc((c || {}).kind || "?")}</span>
      <span class="bk-faint">${esc(ps || "no parameters")}</span></div>`;
  }).join("")}</div>`;
}

/* ---- rules, as a sentence rather than a JSON dump ----------------------
   The tree is what strategy.py evaluates. Rendering it in the same words the
   builder's dropdowns use means the card and the editor describe the rule
   identically; an operator should never have to translate between them. */
const RULE_OP = {
  gt: "is above", lt: "is below", gte: "is at or above", lte: "is at or below",
  eq: "equals", ne: "is not", cross_above: "crosses above",
  cross_below: "crosses below", rising: "is rising", falling: "is falling",
};
const RULE_FLAG = { target_reached: "the target is reached",
                    stop_hit: "the stop is hit" };
const RULE_GRP = { all: "ALL of these are true", any: "ANY of these is true",
                   not: "NONE of these is true", none: "NONE of these is true" };

const operandHTML = (v) => (typeof v === "string"
  ? `<code>${esc(v)}</code>` : `<b class="num">${esc(fmtVal(v))}</b>`);

function ruleHTML(node) {
  if (node === null || node === undefined) {
    return `<div class="bk-none">Nothing set — this side never fires.</div>`;
  }
  if (node === true) return `<div class="bk-rule">always</div>`;
  if (node === false) return `<div class="bk-rule">never</div>`;
  if (typeof node !== "object") return `<div class="bk-rule">${esc(String(node))}</div>`;
  const k = Object.keys(node)[0];
  if (RULE_GRP[k]) {
    const kids = Array.isArray(node[k]) ? node[k] : [node[k]];
    if (!kids.length) {
      return `<div class="bk-none">An empty group. The validator rejects this
        rather than guess what it meant.</div>`;
    }
    return `<div class="bk-grp"><div class="bk-grp-h">${RULE_GRP[k]}</div>
      <ul class="bk-grp-l">${kids.map((c) =>
        `<li>${ruleHTML(c)}</li>`).join("")}</ul></div>`;
  }
  return `<div class="bk-rule">${condText(node)}</div>`;
}

function condText(node) {
  const op = Object.keys(node)[0];
  const raw = node[op];
  const a = Array.isArray(raw) ? raw : [raw];
  if (RULE_FLAG[op]) return RULE_FLAG[op];
  if (op === "between") {
    return `${operandHTML(a[0])} is between ${operandHTML(a[1])}
            and ${operandHTML(a[2])}`;
  }
  if (op === "rising" || op === "falling") {
    return `${operandHTML(a[0])} ${RULE_OP[op]}`;
  }
  if (RULE_OP[op]) return `${operandHTML(a[0])} ${RULE_OP[op]} ${operandHTML(a[1])}`;
  // an operator this page has not met: show it rather than drop the rule
  return `<code>${esc(op)}</code> ${esc(JSON.stringify(raw))}`;
}

function limitText(d, none) {
  if (!d || !Object.keys(d).length) return none;
  if (d.points != null) return `$${Number(d.points).toFixed(2)} per share`;
  if (d.percent != null) return `${esc(d.percent)}% of the entry price`;
  if (d.atr_mult != null) {
    return `${esc(d.atr_mult)} × <code>${esc(d.indicator || "an ATR")}</code>`;
  }
  return `<code>${esc(JSON.stringify(d))}</code>`;
}

/* -------------------------------------------------------------- SETTINGS */
async function openSettings(kind, slug) {
  paintSheet({ title: slug, kind, body: `<div class="bk-faint">Reading it…</div>` });
  let d;
  try { d = await bankReq("GET", bankPath(kind, slug)); }
  catch (e) { sheetError(kind, slug, e.message); return; }
  CUR = d;
  renderSettings();
}

function renderSettings() {
  const d = CUR;
  const ts = d.tunables || [];
  paintSheet({
    title: d.name || d.slug, kind: d.kind,
    sub: `<span class="mono">${esc(d.slug)}</span> · settings`,
    body: `<div id="bkSaveNote"></div>
      <div class="bk-copy"><b>Values only.</b> This panel changes numbers a
        strategy already has. Adding an indicator, or rewriting a rule, changes
        what the strategy <i>is</i> — that happens in the builder, and the
        server refuses it from here.</div>
      ${ts.length ? knobsHTML(ts) : nothingToTune(d)}`,
    foot: `${ts.length
        ? `<button class="btn primary" id="bkSave" disabled>Save</button>` : ""}
      <button class="btn" id="bkToView">View details</button>
      <span style="flex:1"></span>
      <button class="btn" data-bkclose>Close</button>`,
  });
  wireFoot();
  el("bkToView").onclick = () => openView(d.kind, d.slug);
  if (el("bkSave")) el("bkSave").onclick = saveKnobs;
  wireKnobs();
}

function nothingToTune(d) {
  return `<div class="note warn" style="margin:0"><b>Nothing to turn here.</b>
    ${d.kind === "code"
      ? `This file declares no <code>PARAMS</code>, so every number in it is
         written into the code itself.`
      : `Every part of this strategy is shape — an indicator, a reference, a
         rule — and none of it is a number this panel may change.`}
    Open it in the builder to change what it does.</div>`;
}

/* Grouped in the order the server sent them: `group` is the server's own
   heading ("rsi (rsi)", "Target", "Entry rules"), so a knob never appears
   under a heading this page invented for it. */
function knobsHTML(ts) {
  const order = [];
  const by = new Map();
  for (const t of ts) {
    const g = t.group || "Settings";
    if (!by.has(g)) { by.set(g, []); order.push(g); }
    by.get(g).push(t);
  }
  let i = 0;
  return order.map((g) => `<section class="bk-sec">
    <h4>${esc(g)}</h4>
    <div class="bk-knobs">${by.get(g).map((t) => knobHTML(t, i++)).join("")}</div>
  </section>`).join("");
}

function knobHTML(t, i) {
  const id = `bkK${i}`;
  const common = `id="${id}" data-bkp="${esc(t.path)}" data-bkt="${esc(t.type || "text")}"
    data-bkv="${esc(JSON.stringify(t.value === undefined ? null : t.value))}"`;
  let control;
  if (t.type === "bool") {
    control = `<label class="bk-chk"><input type="checkbox" ${common}
      ${t.value ? "checked" : ""}><span>${t.value ? "on" : "off"}</span></label>`;
  } else if (t.type === "int" || t.type === "float") {
    const step = t.step != null ? t.step : (t.type === "int" ? 1 : "any");
    control = `<input type="number" ${common} value="${esc(t.value)}"
      step="${esc(step)}"${t.min != null ? ` min="${esc(t.min)}"` : ""}${
      t.max != null ? ` max="${esc(t.max)}"` : ""} inputmode="decimal">`;
  } else {
    control = `<input type="text" ${common} value="${esc(fmtVal(t.value))}" readonly>`;
  }
  return `<div class="bk-k">
    <label class="bk-k-l" for="${id}">${esc(t.label || t.path)}
      <span class="bk-k-p mono">${esc(t.path)}</span></label>
    <div class="bk-k-c">${control}
      ${t.type === "text" && t.hint ? `<div class="bk-hint">${esc(t.hint)}</div>` : ""}
      ${(t.type === "int" || t.type === "float") && t.max != null
        ? `<div class="bk-hint">${esc(t.min)} – ${esc(t.max)}</div>` : ""}
    </div>
  </div>`;
}

const knobEls = () => (sheet ? [...sheet.querySelectorAll("[data-bkp]")] : []);

/* Only what actually moved. Sending the whole panel back would rewrite every
   value in the file on every save, and a save that touches things nobody
   changed is a save nobody can review. */
function changedPaths() {
  const out = {};
  for (const i of knobEls()) {
    const type = i.dataset.bkt;
    if (type === "text") continue;                 // read-only: never sent
    let was;
    try { was = JSON.parse(i.dataset.bkv); } catch (e) { continue; }
    if (type === "bool") {
      if (i.checked !== !!was) out[i.dataset.bkp] = i.checked;
      continue;
    }
    if (i.value === "") continue;                  // an empty box is not a zero
    const v = Number(i.value);
    if (!Number.isFinite(v) || v === Number(was)) continue;
    out[i.dataset.bkp] = v;
  }
  return out;
}

function wireKnobs() {
  for (const i of knobEls()) {
    i.oninput = refreshDirty;
    i.onchange = refreshDirty;
  }
  refreshDirty();
}

function refreshDirty() {
  const p = changedPaths();
  const n = Object.keys(p).length;
  const b = el("bkSave");
  if (b) {
    b.disabled = !n;
    b.textContent = n ? `Save ${n} change${n === 1 ? "" : "s"}` : "Save";
  }
  for (const i of knobEls()) {
    if (i.dataset.bkt === "bool") {
      const w = i.parentElement.querySelector("span");
      if (w) w.textContent = i.checked ? "on" : "off";
    }
    const row = i.closest(".bk-k");
    if (row) row.classList.toggle("on", p[i.dataset.bkp] !== undefined);
  }
}

async function saveKnobs() {
  const patch = changedPaths();
  const n = Object.keys(patch).length;
  if (!n) return;
  const b = el("bkSave");
  if (b) { b.disabled = true; b.textContent = "Saving…"; }
  try {
    const r = await bankReq("POST", bankPath(CUR.kind, CUR.slug, "/params"),
                            { params: patch });
    CUR.tunables = r.tunables || CUR.tunables;
    if (r.params) CUR.params = r.params;
    // repaint from what came BACK, never from what was sent: the server is
    // the only thing that knows what the file now says
    renderSettings();
    el("bkSaveNote").innerHTML = `<div class="note good">
      <b>Saved.</b> ${n} value${n === 1 ? "" : "s"} written to
      <span class="mono">${esc(CUR.slug)}</span>. Its shape is untouched.</div>`;
    loadBank();                           // the shelf's "changed" line moved
  } catch (e) {
    // the server's own words, verbatim: it is the only thing that knows why
    const note = el("bkSaveNote");
    if (note) {
      note.innerHTML = `<div class="note bad"><b>Not saved.</b>
        ${esc(e.message)}</div>`;
      note.scrollIntoView({ block: "nearest" });
    }
    refreshDirty();                       // the edits are still on screen
  }
}

/* --------------------------------------------------------- to the tester */
function toBacktest(d) {
  const name = d.name || d.slug;
  if (d.kind === "doc") {
    btPreset({
      mode: "strategy", strategy: d.slug, label: name,
      from: "the Strategy Bank",
      note: `Running the saved document "${d.slug}". Pick a symbol and a `
          + `window, then Run backtest.`,
    });
  } else {
    btPreset({
      mode: "code", label: name, from: "the Strategy Bank",
      note: `Loading the coded strategy "${d.slug}" into the editor. Pick a `
          + `symbol and a window, then Run backtest.`,
    });
    selectCodeLater(d.slug);
  }
  closeSheet();
  go({ kind: "research", tab: "backtest" });
}

/* The Backtest tab fills its code-file list asynchronously and preset() has
   no field for it, so the file is chosen once the select exists. If it never
   does, this gives up quietly -- the note above the run box still says which
   strategy was meant, so nobody runs the wrong one thinking it is this one. */
function selectCodeLater(want, tries = 12) {
  const sel = el("btCodeSel");
  if (sel && [...sel.options].some((o) => o.value === want)) {
    sel.value = want;
    sel.dispatchEvent(new Event("change"));
    return;
  }
  if (tries > 0) setTimeout(() => selectCodeLater(want, tries - 1), 150);
}
