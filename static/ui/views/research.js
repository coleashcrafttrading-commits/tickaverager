/* ============================================================================
   Research -- one tab, three rooms.

   The indicator builder, the strategy tester and the backtester were three
   separate rail entries doing three parts of the same job. They are one entry
   now, with tabs in the topbar rather than more panels on the page, so the
   sidebar stays short and each room still opens on its own.

   The two existing views are not reimplemented here: this dispatches to their
   own mount() and paint(), so there is exactly one strategy tester and one
   backtester in the codebase.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, toast, el, esc, card, tableHTML, go,
} from "../core.js";
import { CATALOG, cols } from "../ind.js";

const TABS = [
  ["indicators", "Indicator builder"],
  ["tester", "Strategy tester"],
  ["backtest", "Backtest"],
];

VIEWS.research = {
  title: () => "Research",
  sub: (ov, v) => ({
    indicators: "describe an indicator in English and put it on the chart",
    tester: "run a strategy on any symbol and see it on the chart",
    backtest: "sweep parameters over real history",
  }[v.tab || "indicators"]),
  tabs: TABS,

  mount(v) {
    const t = v.tab || "indicators";
    if (t === "tester") return VIEWS.tester.mount(v);
    if (t === "backtest") return VIEWS.backtest.mount(v);
    return mountBuilder();
  },

  paint(v) {
    const t = v.tab || "indicators";
    if (t === "tester" && VIEWS.tester.paint) return VIEWS.tester.paint(v);
    if (t === "backtest" && VIEWS.backtest.paint) return VIEWS.backtest.paint(v);
  },
};

/* ------------------------------------------------------------- the builder */
let custom = [];
let ready = null;
let last = null;

const EXAMPLES = [
  "An EMA of the typical price, but the period shortens when volatility rises",
  "Distance from the session VWAP measured in ATRs, as an oscillator",
  "A cumulative delta proxy: volume signed by whether the bar closed up or down",
  "Bollinger bandwidth percentile over the last 200 bars",
  "The slope of a 50-bar linear regression, normalised by its standard error",
];

async function mountBuilder() {
  el("view").innerHTML = `
    <div class="grid main">
      <div>
        ${card("Describe it", `
          <div id="aiReady"></div>
          <label class="f"><span>What should it do?</span>
            <textarea id="aiDesc" rows="4" placeholder="Plain English. Be specific about the maths where it matters — an ambiguous description gets an arbitrary reading of it."></textarea></label>
          <div class="hint">Try one of these:</div>
          <div class="row-btns" id="aiEx" style="margin-bottom:12px"></div>
          <div class="row-btns">
            <button class="btn primary" id="aiGo">Build it</button>
            <span class="faint" id="aiStatus"></span>
          </div>`)}
        ${card("Result", `<div id="aiOut"><div class="empty">Nothing built yet.</div></div>`)}
      </div>
      <div>
        ${card("Your indicators", `<div id="aiList"></div>`, "", { flush: true })}
        ${card("How it works", `
          <div class="tip" style="margin-top:0">
            You get <b>two</b> things from one description. The
            <b>JavaScript</b> runs on this dashboard's own chart, so you can see
            it immediately — that is the point. The <b>Pine Script</b> is a
            convenience for taking the same idea to TradingView; nothing here
            executes it, so it is untested and labelled as such.<br><br>
            Generated indicators are checked against real bars before they are
            offered: one that throws, returns the wrong length, or reads a
            future bar is rejected rather than quietly drawing nonsense.
          </div>`)}
      </div>
    </div>`;

  el("aiEx").innerHTML = EXAMPLES.map((e, i) =>
    `<button class="btn sm" data-ex="${i}">${esc(e.slice(0, 34))}…</button>`).join("");
  el("aiEx").querySelectorAll("[data-ex]").forEach((b) => {
    b.onclick = () => { el("aiDesc").value = EXAMPLES[+b.dataset.ex]; };
  });
  el("aiGo").onclick = build;

  await refresh();
}

async function refresh() {
  try {
    const r = await GET("/api/indicators/custom");
    custom = r.indicators || [];
    ready = r.ready || {};
  } catch (e) { custom = []; ready = { ready: false, problem: e.message }; }
  renderReady();
  renderList();
}

function renderReady() {
  const h = el("aiReady");
  if (!h) return;
  if (ready && ready.ready) {
    h.innerHTML = `<div class="note good" style="margin-top:0">Connected via
      <b>${esc(ready.how === "api_key" ? "an API key" : "the Claude Code CLI")}</b>.</div>`;
    return;
  }
  h.innerHTML = `<div class="note warn" style="margin-top:0">
    <b>The builder cannot reach a model yet.</b> ${esc((ready && ready.problem) || "")}
    <div class="tip">${esc((ready && ready.fix) || "")}</div>
    <div class="tip">Either works: put <code>ANTHROPIC_API_KEY=sk-ant-…</code> in
      <code>.env</code> and restart the dashboard, or open a terminal in the bot
      folder and run <code>claude</code> once to accept the trust prompt and
      sign in. Everything else on this page works meanwhile.</div></div>`;
}

async function build() {
  const desc = (el("aiDesc").value || "").trim();
  if (!desc) { toast("Describe the indicator first.", "err"); return; }
  const b = el("aiGo");
  b.disabled = true;
  el("aiStatus").textContent = "asking… this takes up to a minute";
  try {
    const r = await POST("/api/indicators/ai", { description: desc });
    if (!r.ok) {
      el("aiOut").innerHTML = `<div class="note bad"><b>${esc(r.error || "failed")}</b>
        ${r.fix ? `<div class="tip">${esc(r.fix)}</div>` : ""}
        ${r.raw ? `<pre class="mono" style="white-space:pre-wrap;font-size:11px">${esc(String(r.raw).slice(0, 900))}</pre>` : ""}</div>`;
      return;
    }
    last = r.indicator;
    renderResult(last);
    await refresh();
    toast(`Built <b>${esc(last.name)}</b>.`, "ok");
  } catch (e) {
    el("aiOut").innerHTML = `<div class="note bad">${esc(e.message)}</div>`;
  } finally {
    b.disabled = false;
    el("aiStatus").textContent = "";
  }
}

function renderResult(ind) {
  const check = verify(ind);
  el("aiOut").innerHTML = `
    <h4 style="margin-top:0">${esc(ind.name)}</h4>
    <p class="sub">${esc(ind.note || "")}</p>
    ${ind.warning ? `<div class="note warn">${esc(ind.warning)}</div>` : ""}
    <div class="note ${check.ok ? "good" : "bad"}">
      <b>${check.ok ? "Runs on real bars." : "Rejected."}</b> ${esc(check.msg)}</div>
    <div class="row-btns" style="margin:10px 0">
      <button class="btn sm primary" id="aiPlot"${check.ok ? "" : " disabled"}>Put it on the chart</button>
      <button class="btn sm" id="aiPine">Copy Pine Script</button>
    </div>
    <div class="f2">
      <div><h4>Parameters</h4><div class="scroll">${tableHTML(["Name", "Default"],
        Object.entries(ind.params || {}).map(([k, v]) =>
          [`<code>${esc(k)}</code>`, esc(v)]), "None.")}</div></div>
      <div><h4>Draws</h4><p class="sub">${ind.panel
        ? "in its own pane below the price" : "over the price candles"}</p></div>
    </div>
    <h4>JavaScript (this is what runs here)</h4>
    <pre class="mono" style="white-space:pre-wrap;font-size:11px;max-height:220px;overflow:auto">${esc(ind.js)}</pre>
    <h4>Pine Script <span class="pill warn">untested here</span></h4>
    <pre class="mono" style="white-space:pre-wrap;font-size:11px;max-height:200px;overflow:auto">${esc(ind.pine || "not provided")}</pre>`;

  el("aiPine").onclick = () => {
    navigator.clipboard.writeText(ind.pine || "").then(
      () => toast("Pine Script copied.", "ok"),
      () => toast("Could not copy.", "err"));
  };
  const p = el("aiPlot");
  if (p) p.onclick = () => { install(ind); go({ kind: "research", tab: "tester" }); };
}

/* An indicator is offered only once it has RUN. A generated function that
   throws, returns the wrong length, or reads a future bar would otherwise draw
   silent nonsense over real prices.

   The look-ahead test is the one that matters and the one that cannot be done
   by reading the code: run the indicator over the whole series, then over the
   series with the last 40 bars removed, and compare where they overlap. An
   honest indicator gives bar 150 the same value whether or not bar 151 exists.
   One that peeks changes its mind, and that difference is the proof. */
export function verify(ind) {
  const n = 260;
  const bars = { o: [], h: [], l: [], c: [], v: [], day: [] };
  let px = 100;
  for (let i = 0; i < n; i++) {
    const o = px;
    px = Math.max(1, px + Math.sin(i / 7) * 0.6 + (i % 11 - 5) * 0.05);
    bars.o.push(o); bars.c.push(px);
    bars.h.push(Math.max(o, px) + 0.2); bars.l.push(Math.min(o, px) - 0.2);
    bars.v.push(1000 + i); bars.day.push("2026-01-" + String(1 + (i / 60 | 0)).padStart(2, "0"));
  }
  let fn;
  try { fn = new Function("b", "p", ind.js); }
  catch (e) { return { ok: false, msg: "it does not compile: " + e.message }; }
  let out;
  try { out = fn(bars, { ...(ind.params || {}) }); }
  catch (e) { return { ok: false, msg: "it threw on real-shaped bars: " + e.message }; }
  if (!out || typeof out !== "object") {
    return { ok: false, msg: "it returned no series object" };
  }
  const names = Object.keys(out);
  if (!names.length) return { ok: false, msg: "it returned no series" };
  for (const k of names) {
    const v = out[k];
    if (!Array.isArray(v) || v.length !== n) {
      return { ok: false, msg: `series "${k}" is not an array of ${n} values` };
    }
    if (v.some((x) => typeof x === "number" && !isFinite(x))) {
      return { ok: false, msg: `series "${k}" contains NaN or Infinity` };
    }
  }
  const formed = names.map((k) => out[k].filter((x) => x != null).length);
  if (!formed.some((f) => f > 5)) {
    return { ok: false, msg: "it never produced a value on 260 bars" };
  }

  // ---- look-ahead: recompute on a truncated series and compare the overlap --
  const cut = n - 40;
  const shortBars = {};
  for (const k of Object.keys(bars)) shortBars[k] = bars[k].slice(0, cut);
  let sout;
  try { sout = fn(shortBars, { ...(ind.params || {}) }); }
  catch (e) { return { ok: false, msg: "it threw on a shorter series: " + e.message }; }
  for (const k of names) {
    const full = out[k], part = (sout || {})[k];
    if (!Array.isArray(part)) continue;
    // EVERY bar of the overlap, including the last. There is no "settling"
    // to make allowances for: a correct indicator's value at bar i depends
    // only on bars up to i, so it must be identical whether or not bar i+1
    // exists. Excusing the final few bars excused exactly the region where a
    // one-bar peek shows itself, and a function returning b.c[i+1] passed.
    for (let i = 0; i < cut; i++) {
      const a = full[i], c = part[i];
      if (a == null && c == null) continue;
      if (a == null || c == null || Math.abs(a - c) > Math.abs(a) * 1e-9 + 1e-9) {
        return { ok: false,
                 msg: `series "${k}" changes at bar ${i} when later bars are `
                    + `removed (${a} vs ${c}) -- it is reading the future` };
      }
    }
  }

  return { ok: true,
           msg: `${names.length} series (${names.join(", ")}), `
              + `${Math.max(...formed)} of ${n} bars formed, and no look-ahead.` };
}

/* Add it to the chart's live catalogue for this session. */
function install(ind) {
  try {
    const fn = new Function("b", "p", ind.js);
    CATALOG[ind.key] = {
      label: ind.name, params: { ...(ind.params || {}) },
      panel: !!ind.panel, run: (b, p) => fn(b, p), custom: true,
    };
    toast(`${esc(ind.name)} is now in the chart's Indicators list.`, "ok", 7000);
  } catch (e) {
    toast("Could not install: " + esc(e.message), "err");
  }
}

/* Everything saved gets installed on load, so the picker has them all. */
export function installAll(list) {
  for (const ind of list || []) {
    try {
      const fn = new Function("b", "p", ind.js);
      CATALOG[ind.key] = {
        label: ind.name, params: { ...(ind.params || {}) },
        panel: !!ind.panel, run: (b, p) => fn(b, p), custom: true,
      };
    } catch (e) { /* a bad one is skipped, never fatal */ }
  }
}

function renderList() {
  const h = el("aiList");
  if (!h) return;
  installAll(custom);
  h.innerHTML = tableHTML(["Indicator", "Draws", ""],
    custom.map((c) => [
      `<b>${esc(c.name)}</b><br><span class="faint" style="font-size:11px">${esc((c.note || "").slice(0, 70))}</span>`,
      c.panel ? "own pane" : "on price",
      `<button class="btn sm" data-use="${esc(c.key)}">Show</button>
       <button class="btn sm" data-del="${esc(c.key)}">×</button>`,
    ]), "None yet. Describe one on the left.");

  h.querySelectorAll("[data-use]").forEach((b) => {
    b.onclick = () => {
      const ind = custom.find((c) => c.key === b.dataset.use);
      if (ind) { install(ind); renderResult(ind); }
    };
  });
  h.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => act(async () => {
      await DEL("/api/indicators/custom/" + encodeURIComponent(b.dataset.del));
      delete CATALOG[b.dataset.del];
      await refresh();
    });
  });
}
