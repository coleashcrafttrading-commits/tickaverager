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
   ========================================================================= */
"use strict";
import {
  S, GET, POST, act, ask, toast, el, esc, card, tableHTML, go,
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
    toast(`Saved as <b>${esc(slug)}</b>. Set that slug on a ticker to trade it.`,
          "ok", 8000);
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
    b.onclick = () => act(async () => {
      if (dirty && !(await ask({ title: "Discard changes?",
        body: "The strategy open now has unsaved edits.", ok: "Discard" }))) return;
      const r = await GET("/api/strategies/" + encodeURIComponent(b.dataset.load));
      spec = r.spec; slug = r.slug; dirty = false;
      paintShell(); renderAll();
    });
  });
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
