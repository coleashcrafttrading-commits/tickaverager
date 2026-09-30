/* ============================================================================
   Backtest -- the room where an idea is tested, whatever kind of idea it is.

   FOUR SUBJECTS, ONE REPORT.

     Ladder    replays the live engine's own decision functions
     Strategy  replays a saved indicator document
     Python    runs real code you or an agent wrote
     Options   the structures, graded across two markets, in Options lab

   The ladder is ONE of these and it is listed first only because it is the
   one currently running live -- it is the baseline a new idea has to beat,
   not the subject this page is built around. That distinction is the whole
   point of the rewrite: this used to be "the ladder backtester, with two
   other modes bolted beside it", and the report was laid out accordingly.

   ------------------------------------------------------- the honesty rules
   CLAUDE.md's "Reading a backtest" section is a list of ways this project has
   already been misled by its own results page. They are not tips in a sidebar
   here; they are btread.js, applied to every row of every sweep:

     * TOTAL P/L is the headline. The repo's own example is +$2,190 realised
       against -$2,577 open, which is -$387 actual. A page that draws the
       first number large and the second one small IS the lie, so realised and
       open are drawn the SAME SIZE and the total is shown as their sum.
     * A 100% win rate is what no stop loss looks like. Said next to the win
       rate, in red, every time it happens.
     * profit_factor is null, never infinity. sortino is null under three down
       days. max_lots_held at the cap means the CAP bound the result.
     * Ranking is P/L per dollar of drawdown, never profit, and anything under
       ten closed trades is listed but never ranked.
     * A number nobody measured renders as a dash with its reason. Never 0.
   ========================================================================= */
"use strict";
import {
  S, GET, POST, DEL, act, ask, toast, el, esc, card, tableHTML,
  money, money0, sgn, go,
} from "../core.js";
import {
  measured, decompose, score, rank, caveats, worstLevel, benchmarks,
  tradeSeries, parseSweep, combos, sweepKinds,
} from "../btread.js";
import { ResultChart } from "../resultchart.js";

let poll = null;
let STRATS = [];
let CODEFILES = [];
let TEMPLATE = "";
let mode = "ladder";
let lastJob = null;      // the finished job status
let detail = null;       // the full report for the selected row
let selRow = 0;
let chart = null;        // the ResultChart instance
let chartMetric = "equity";
let resTab = "summary";
let compare = [];        // row indexes picked for the side-by-side
let forAcct = "";        // jobs and results are per account; strategies are shared

const WINDOWS = [["5", "1w"], ["20", "1m"], ["60", "3m"], ["120", "6m"],
                 ["250", "1y"]];

const PRESETS = {
  "Take profit": { take_profit: "0.05,0.10,0.20,0.30,0.40,0.50" },
  "Add distance": { add_distance: "0.05,0.10,0.15,0.20,0.30,0.40" },
  "TP x add": { take_profit: "0.10,0.20,0.30,0.40", add_distance: "0.10,0.20,0.30,0.40" },
  "Exit style": { exit_mode: "limit,trail", trail_amount: "0.03,0.05,0.10" },
  "Lot size": { shares_per_lot: "25,50,100,200" },
};

const MODES = [
  ["ladder", "Ladder", "the strategy live on this ticker today"],
  ["strategy", "Strategy document", "a saved indicator document"],
  ["code", "Python", "real code, sandboxed, no credentials"],
];

/* ------------------------------------------------------ dash-honest output
   core.js's money() and sgn() coerce null to 0, which is right on a page
   where every field always arrives and wrong here: a sweep row whose
   profit_factor is null must not render "$0.00" beside one whose loss really
   was zero. Same formatting, same colours, one question asked first. */
const DASH = `<span class="faint">&mdash;</span>`;
const mny = (v, dp = 2) => (measured(v) ? money(v, dp) : DASH);
const pl = (v, dp = 2) => (measured(v) ? sgn(v, dp) : DASH);
const n2 = (v, dp = 2) => (measured(v) ? Number(v).toFixed(dp) : DASH);
const pc = (v, dp = 1) => (measured(v)
  ? `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(dp)}%` : DASH);
const cnt = (v) => (measured(v) ? Number(v).toLocaleString() : DASH);

/* ------------------------------------------------------- arriving loaded */
let pending = null;
export function preset(p) { pending = p || null; }

function applyPending() {
  if (!pending) return;
  const p = pending;
  pending = null;
  if (p.mode && MODES.some((m) => m[0] === p.mode)) {
    mode = p.mode;
    paintModes();
    paintModeBox();
  }
  if (p.symbol && el("btSym")) el("btSym").value = p.symbol;
  if (p.label && el("btLabel")) el("btLabel").value = p.label;
  if (p.sweep && el("btSweep")) {
    el("btSweep").value = Object.entries(p.sweep)
      .map(([k, v]) => `${k}=${v}`).join("\n");
    const d = el("btSweepBox");
    if (d) d.open = true;
    updateCount();
  }
  if (p.strategy) {
    const set = () => { const s = el("btStrat"); if (s) s.value = p.strategy; };
    set();
    setTimeout(set, 300);              // the list may still be loading
  }
  if (p.from) {
    /* The EMPTY STATE of the result panel, not a banner over the page: the
       panel is what is blank, so the sentence belongs inside it. */
    el("btResult").innerHTML = `<div class="empty">Loaded from
      ${esc(p.from)}. ${esc(p.note
        || "Nothing has run yet -- check the numbers and press Run.")}</div>`;
  }
}

export const BACKTEST = {
  mount() {
    ensureStyle();
    if (forAcct !== S.account) {
      clearInterval(poll);
      lastJob = null; detail = null; selRow = 0; compare = [];
      forAcct = S.account;
    }
    const syms = (S.ov && S.ov.tickers || []).map((t) => t.symbol);
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${card("Run a test", runFormHTML(syms))}
          <div id="btResult"></div>
        </div>
        <div>
          ${card("Recent runs", `<div id="btJobs"></div>`, "", { flush: true })}
          ${card("What the numbers mean", readingHTML())}
        </div>
      </div>`;

    el("btModes").onclick = (e) => {
      const b = e.target.closest(".bt-mode");
      if (!b) return;
      mode = b.dataset.m;
      paintModes();
      paintModeBox();
    };
    el("btWin").onclick = (e) => {
      const b = e.target.closest("[data-d]");
      if (!b) return;
      el("btDays").value = b.dataset.d;
      paintWindows();
    };
    el("btDays").addEventListener("input", paintWindows);
    el("view").querySelectorAll(".bt-preset").forEach((b) => {
      b.onclick = () => {
        el("btSweep").value = Object.entries(PRESETS[b.dataset.p])
          .map(([k, v]) => `${k}=${v}`).join("\n");
        updateCount();
      };
    });
    el("btSweep").addEventListener("input", updateCount);
    el("btRun").onclick = () => act(runIt);
    el("btOptLab").onclick = () => go({ kind: "options", tab: "backtest" });

    paintModes();
    paintWindows();
    paintModeBox();
    updateCount();
    loadStrategies();
    loadCode();
    loadJobs();
    if (lastJob) renderJob(lastJob);
    applyPending();
  },

  paint() { /* results are pushed by the watcher, never by the fleet poll */ },
};

/* ==================================================================== form */
function runFormHTML(syms) {
  return `
    <div class="bt-modes" id="btModes">
      ${MODES.map(([k, label, sub]) => `
        <button class="bt-mode" data-m="${k}">
          <b>${label}</b><span>${sub}</span>
        </button>`).join("")}
    </div>

    <div class="bt-row">
      <label class="f bt-sym"><span>Symbol</span>
        <input id="btSym" value="${esc(syms[0] || "RAM")}" placeholder="RAM"
          list="btSyms"></label>
      <datalist id="btSyms">${syms.map((s) =>
        `<option value="${esc(s)}">`).join("")}</datalist>
      <label class="f bt-tf"><span>Bars</span>
        <select id="btTf">${["1Min", "5Min", "15Min", "1Hour", "1Day"].map((t) =>
          `<option${t === "1Min" ? " selected" : ""}>${t}</option>`).join("")}
        </select></label>
      <label class="f bt-days"><span>Window (days)</span>
        <input id="btDays" type="number" value="30" min="1" max="2000"></label>
      <span class="rc-chips" id="btWin">${WINDOWS.map(([d, l]) =>
        `<button class="rc-chip" data-d="${d}">${l}</button>`).join("")}</span>
      <label class="f bt-label"><span>Label</span>
        <input id="btLabel" placeholder="optional"></label>
    </div>

    <div id="btModeBox"></div>

    <details class="bt-sweep" id="btSweepBox">
      <summary>Sweep parameters <span class="faint">-- run every
        combination and compare them</span></summary>
      <textarea id="btSweep" rows="4"
        placeholder="take_profit=0.05,0.10,0.20&#10;p.rsi_period=7,14,21"></textarea>
      <div id="btCount" class="bt-count"></div>
      <div class="row-btns">${Object.keys(PRESETS).map((p) =>
        `<button class="btn sm bt-preset" data-p="${esc(p)}">${esc(p)}</button>`
        ).join("")}</div>
      <div class="hint">One <code>param=a,b,c</code> per line. A sweep may mix
        every kind of parameter at once: <code>take_profit</code> (a run
        setting), <code>target.points</code> and <code>stop.atr_mult</code> (a
        strategy document), <code>ind.rsi.period</code> (an indicator inside
        it), <code>p.oversold</code> (a PARAMS value in code).</div>
    </details>

    <div class="bt-go">
      <button class="btn primary" id="btRun">Run</button>
      <button class="btn sm" id="btOptLab" title="the graded option sweeps">
        Testing an option structure?</button>
    </div>`;
}

function paintModes() {
  const host = el("btModes");
  if (!host) return;
  host.querySelectorAll(".bt-mode").forEach((b) =>
    b.classList.toggle("on", b.dataset.m === mode));
}

function paintWindows() {
  const host = el("btWin");
  if (!host) return;
  const cur = String(Number(el("btDays").value) || 0);
  host.querySelectorAll("[data-d]").forEach((b) =>
    b.classList.toggle("on", b.dataset.d === cur));
}

function readingHTML() {
  return `<div class="tip" style="margin-top:0">
    <b>Total P/L</b> is realised plus whatever the run was still holding at the
    end, marked to market. It is the only number that answers "what would the
    account show". Realised alone rewards a setting that banks winners and
    quietly accumulates lots it never closes.<br><br>
    <b>P/L per $DD</b> -- profit divided by the worst the equity curve got --
    is how every table here is ranked. Ranking by profit just selects for
    whichever run took the most risk.<br><br>
    <b>Profit factor</b> is gross win over gross loss, and reads
    <b>&mdash;</b> when there were no losing trades at all. That is a fact
    about the window, not an edge.<br><br>
    <b>Max lots</b> -- if this equals the cap, the cap bound the result and you
    are comparing caps, not the parameter you swept.<br><br>
    <b>Sharpe</b> is computed on daily equity. Per-bar Sharpe on a 1-minute
    ladder returns nonsense in the twenties.</div>`;
}

/* ------------------------------------------------------------- mode panes */
function paintModeBox() {
  const box = el("btModeBox");
  if (!box) return;
  if (mode === "ladder") {
    box.innerHTML = `<div class="hint">Replays the engine's <b>own</b> decision
      functions over real bars, so a result here is about the strategy and not
      about a second implementation of it. It starts from the ticker's current
      settings; anything in the sweep box overrides them.</div>`;
  } else if (mode === "strategy") {
    box.innerHTML = `
      <label class="f"><span>Strategy document</span>
        <select id="btStrat"></select></label>
      <div class="hint" id="btStratNote"></div>`;
    fillStrategies();
  } else {
    box.innerHTML = `
      <div class="bt-codebar">
        <label class="f" style="flex:1 1 150px;margin:0"><span>Saved code</span>
          <select id="btCodeSel"></select></label>
        <button class="btn sm" id="btCodeNew">New</button>
        <button class="btn sm" id="btCodeSave">Save</button>
        <button class="btn sm" id="btCodePine"
          title="The same strategy as Pine Script">Pine</button>
        <button class="btn sm" id="btCodeDel">&times;</button>
      </div>
      <div id="btPineBox"></div>
      <textarea id="btCode" rows="16" spellcheck="false" class="code"></textarea>
      <div class="hint" id="btCodeErr"></div>
      <div class="hint">Define <code>on_bar(ctx, i)</code>;
        <code>init(ctx)</code> and <code>PARAMS</code> are optional.
        <b>Look-ahead is blocked</b> -- <code>ctx.c[i+1]</code> raises rather
        than quietly inventing money. An entry decided on bar <i>i</i> fills at
        bar <i>i+1</i>'s open, and a bar touching both stop and target resolves
        as the <b>stop</b>. Runs in a separate process with no broker
        credentials.</div>`;
    fillCode();
    el("btCode").value = TEMPLATE;
    el("btCode").addEventListener("input", debounceCheck);
    el("btCodeNew").onclick = () => {
      el("btCode").value = TEMPLATE;
      el("btCodeSel").value = "";
      checkCode();
    };
    el("btCodeSave").onclick = () => act(saveCode);
    el("btCodeDel").onclick = () => act(deleteCode);
    el("btCodePine").onclick = () => act(showPine);
    el("btCodeSel").onchange = () => act(openCode);
    checkCode();
  }
  updateCount();
}

function updateCount() {
  const box = el("btCount");
  if (!box) return;
  const sw = parseSweep((el("btSweep") || {}).value || "");
  const n = combos(sw);
  const kinds = sweepKinds(sw);
  box.innerHTML = kinds.length
    ? `<b>${n.toLocaleString()}</b> combination${n === 1 ? "" : "s"}
       ${n > 400 ? `<span class="warn">-- this will take a while</span>` : ""}
       <div class="faint">${kinds.map((k) =>
         `<code>${esc(k.key)}</code> &rarr; ${k.kind} (${k.n})`).join(" &middot; ")}</div>`
    : `Leave empty to run once with the current settings.`;
}

/* -------------------------------------------------------------- strategies */
async function loadStrategies() {
  try {
    const r = await GET("/api/strategies");
    STRATS = r.strategies || [];
    fillStrategies();
  } catch (e) { /* the ladder still works without them */ }
}

function fillStrategies() {
  const sel = el("btStrat");
  if (!sel) return;
  sel.innerHTML = STRATS.map((s) =>
    `<option value="${esc(s.slug)}">${esc(s.name)}</option>`).join("")
    || `<option value="">-- none saved --</option>`;
  const note = () => {
    const s = STRATS.find((x) => x.slug === sel.value);
    el("btStratNote").innerHTML = s
      ? `${esc(s.note || s.name)}${s.indicators && s.indicators.length
          ? ` <span class="faint">(${esc(s.indicators.join(", "))})</span>` : ""}`
      : `Build one on the <b>Builder</b> tab.`;
  };
  sel.onchange = note;
  note();
}

/* -------------------------------------------------------------------- code */
async function loadCode() {
  try {
    const r = await GET("/api/code");
    CODEFILES = r.files || [];
    TEMPLATE = r.template || "";
    fillCode();
  } catch (e) { /* editor still usable */ }
}

function fillCode() {
  const sel = el("btCodeSel");
  if (!sel) return;
  sel.innerHTML = `<option value="">(unsaved)</option>`
    + CODEFILES.map((f) => `<option value="${esc(f.slug)}">${esc(f.slug)}</option>`).join("");
}

async function openCode() {
  const slug = el("btCodeSel").value;
  if (!slug) { el("btCode").value = TEMPLATE; checkCode(); return; }
  const r = await GET("/api/code/" + encodeURIComponent(slug));
  el("btCode").value = r.code;
  checkCode();
}

async function saveCode() {
  const cur = el("btCodeSel").value;
  const name = cur || prompt("Name this coded strategy:", "my-strategy");
  if (!name) return;
  await POST("/api/code", { slug: name, code: el("btCode").value });
  await loadCode();
  el("btCodeSel").value = name.toLowerCase().replace(/\s+/g, "-");
  toast(`Saved <b>${esc(name)}</b>.`, "ok");
}

async function deleteCode() {
  const slug = el("btCodeSel").value;
  if (!slug) return;
  if (!(await ask({ title: `Delete ${slug}?`, body: "The file is removed from disk.",
                    ok: "Delete", danger: true }))) return;
  await DEL("/api/code/" + encodeURIComponent(slug));
  await loadCode();
  el("btCode").value = TEMPLATE;
}

/* The same strategy as Pine Script, for looking at it in TradingView with the
   entries, exits and connecting lines drawn. Nothing here executes Pine, so
   it is untested on this side and labelled as such. */
async function showPine() {
  const slug = (el("btCodeSel") || {}).value || "";
  const m = String(slug).match(/^study-(\d+)-/);
  const box = el("btPineBox");
  if (!box) return;
  if (!m) {
    box.innerHTML = `<div class="note warn">Pine is generated for the study's
      finalists -- pick one of the <b>study-...</b> strategies.</div>`;
    return;
  }
  const b = el("btCodePine");
  b.disabled = true;
  try {
    const r = await GET("/api/pine/" + m[1]);
    box.innerHTML = `
      <div class="note">
        <b>${esc(r.name)}</b> as Pine Script
        <div class="tip">Paste into TradingView &rarr; Pine Editor &rarr; Add to
          chart. Set the chart to <b>${esc(r.timeframe)}</b>; the study only
          tested that. Nothing here executes Pine, so it is untested on this
          side.</div>
        <div class="row-btns" style="margin:8px 0">
          <button class="btn sm primary" id="btPineCopy">Copy</button>
          <button class="btn sm" id="btPineHide">Hide</button>
        </div>
        <pre class="mono bt-pre">${esc(r.pine)}</pre>
      </div>`;
    el("btPineCopy").onclick = () => navigator.clipboard.writeText(r.pine).then(
      () => toast("Pine Script copied.", "ok"), () => toast("Could not copy.", "err"));
    el("btPineHide").onclick = () => { box.innerHTML = ""; };
  } finally { b.disabled = false; }
}

let checkTimer = null;
function debounceCheck() {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(checkCode, 500);
}

async function checkCode() {
  const box = el("btCodeErr");
  if (!box) return;
  try {
    const r = await POST("/api/code/check", { code: el("btCode").value });
    box.innerHTML = r.ok
      ? `<span class="up">Compiles.</span>`
      : `<span class="down">${esc(r.error)}</span>`;
  } catch (e) { box.innerHTML = ""; }
}

/* --------------------------------------------------------------- run + poll */
async function runIt() {
  const spec = {
    symbol: el("btSym").value.trim().toUpperCase(),
    timeframe: el("btTf").value,
    days: Number(el("btDays").value) || 30,
    label: el("btLabel").value.trim(),
    mode,
    sweep: parseSweep(el("btSweep").value),
  };
  if (!spec.symbol) { toast("A symbol is required.", "err"); return; }
  if (mode === "strategy") {
    spec.strategy = (el("btStrat") || {}).value || "";
    if (!spec.strategy) { toast("Pick a strategy first.", "err"); return; }
  }
  if (mode === "code") {
    spec.code = (el("btCode") || {}).value || "";
    if (!spec.code.trim()) { toast("There is no code to run.", "err"); return; }
  }
  detail = null; selRow = 0; compare = [];
  el("btResult").innerHTML = `<div class="card"><div class="card-b">
    <div class="skel" style="width:40%"></div>
    <div class="skel" style="width:70%;margin-top:10px"></div></div></div>`;
  const j = await POST("/api/backtest", spec);
  toast(`Queued -- ${j.total} combination(s).`, "ok");
  watch(j.id);
  loadJobs();
}

function watch(id) {
  clearInterval(poll);
  const tick = async () => {
    let j;
    try { j = await GET("/api/backtest/" + id); }
    catch (e) { clearInterval(poll); return; }
    lastJob = j;
    renderJob(j);
    if (j.state === "done" || j.state === "error") {
      clearInterval(poll);
      loadJobs();
      if (j.state === "done" && (j.results || []).length) loadDetail(id, 0);
    }
  };
  tick();
  poll = setInterval(tick, 900);
}

async function loadDetail(id, row) {
  selRow = row;
  try {
    const r = await GET(`/api/backtest/${id}/detail?row=${row}`);
    detail = r.report;
  } catch (e) {
    detail = null;
    toast(esc(e.message), "err");
  }
  renderJob(lastJob);
}

async function loadJobs() {
  try {
    const r = await GET("/api/backtest/jobs?limit=12");
    const host = el("btJobs");
    if (!host) return;
    host.innerHTML = (r.jobs || []).length
      ? (r.jobs || []).map((j) => `
        <div class="bt-job" data-job="${esc(j.id)}">
          <div class="bt-job-t">
            <b>${esc(j.symbol)}</b>
            <span class="faint">${esc(j.label || j.timeframe)}</span>
            <span class="bt-job-s ${j.state === "done" ? "up"
              : j.state === "error" ? "down" : "warn"}">${esc(j.state)}</span>
          </div>
          <div class="bt-job-b">
            <span class="faint">${new Date(j.created * 1000)
              .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
            <span class="faint">${j.total > 1 ? j.total + " combos" : ""}</span>
            <span class="bt-job-p">${pl(j.best)}</span>
          </div>
        </div>`).join("")
      : `<div class="empty" style="padding:18px">Nothing run yet.</div>`;
    host.querySelectorAll("[data-job]").forEach((b) => {
      b.onclick = () => { detail = null; compare = []; watch(b.dataset.job); };
    });
  } catch (e) { /* panel is optional */ }
}

/* ===================================================================== results */
function renderJob(j) {
  const host = el("btResult");
  if (!host || !j) return;

  if (j.state === "error") {
    host.innerHTML = `<div class="note bad"><b>The backtest failed:</b>
      ${esc(j.error)}</div>`;
    return;
  }
  if (j.state !== "done") {
    const p = j.total ? Math.round((j.done / j.total) * 100) : 0;
    host.innerHTML = card("Running", `
      <div class="bar"><div class="bar-f" style="width:${p}%"></div></div>
      <div class="faint" style="margin-top:8px">${j.done} of ${j.total}
        combination${j.total === 1 ? "" : "s"}${j.bars
        ? ` &middot; ${j.bars.toLocaleString()} bars` : ""}</div>`);
    return;
  }

  const rows = j.results || [];
  const okRows = rows.filter((r) => !r.error);
  const failed = rows.filter((r) => r.error);
  const multi = rows.length > 1;
  const s = detail ? detail.summary : (rows[selRow] || okRows[0] || rows[0]);
  const ctx = { look_ahead: (rows[selRow] || {}).look_ahead };

  host.innerHTML =
    verdictHTML(j, s, rows, failed, ctx)
    + card("Equity", `<div id="btChart"></div>`,
        `<span class="faint" id="btChartX"></span>`)
    + card("Against doing nothing", `<div id="btBench"></div>`,
        `<span class="faint">the number it has to beat</span>`, { flush: true })
    + card("", `
      <div class="tabs2" id="btResTabs">
        <button class="t2 ${resTab === "summary" ? "on" : ""}"
          data-t="summary">Performance</button>
        <button class="t2 ${resTab === "trades" ? "on" : ""}"
          data-t="trades">Trades</button>
        ${multi ? `<button class="t2 ${resTab === "combos" ? "on" : ""}"
          data-t="combos">All ${rows.length} combinations</button>
          <button class="t2 ${resTab === "compare" ? "on" : ""}"
          data-t="compare">Compare${compare.length ? ` (${compare.length})` : ""}</button>` : ""}
        <button class="t2 ${resTab === "logs" ? "on" : ""}" data-t="logs">Log</button>
      </div>
      <div id="btResBody"></div>`)
    + card("Use this result", `<div id="btActions"></div>`);

  el("btResTabs").querySelectorAll(".t2").forEach((b) => {
    b.onclick = () => { resTab = b.dataset.t; renderJob(j); };
  });

  renderChart();
  renderBench(s);
  renderResBody(j, rows);
  renderActions(j, rows);
}

/* ------------------------------------------------------------ the verdict
   The one card that decides whether this page is honest. Realised and open
   are the SAME SIZE as each other and the total is drawn as their sum, so the
   +$2,190 / -$2,577 / -$387 case reads as what it is rather than as a good
   day with a footnote. */
function verdictHTML(j, s, rows, failed, ctx) {
  if (!s) return card("Result", `<div class="empty">No summary.</div>`);
  const d = decompose(s);
  const cs = caveats(s, ctx);
  const lvl = worstLevel(cs);
  const sc = score(s);
  const spark = (detail && detail.curve && detail.curve.equity)
    || (rows[selRow] || {}).spark || [];
  const dd = (detail && detail.curve && detail.curve.drawdown) || [];

  const head = `<div class="faint bt-head">
      <b>${esc(j.symbol)}</b> &middot; ${esc(j.timeframe)} &middot;
      ${cnt(j.bars)} bars &middot;
      ${esc(String(j.from).slice(0, 10))} &rarr; ${esc(String(j.to).slice(0, 10))}
      &middot; the tape itself moved ${pc(j.drift, 2)}
      &middot; ${esc(String(j.seconds))}s
      ${rows.length > 1 ? `&middot; ${rows.length} combinations, showing
        #${selRow + 1}` : ""}
    </div>`;

  return card("Result", head + `
    <div class="bt-verdict">
      <div class="bt-vmain">
        <div class="bt-vk">Total P/L</div>
        <div class="bt-vv ${measured(d.total) && d.total < 0 ? "down"
          : measured(d.total) && d.total > 0 ? "up" : "faint"}">${
          measured(d.total) ? (d.total > 0 ? "+" : "") + money(d.total) : "&mdash;"}</div>
        <div class="bt-vs">what the account would actually show</div>
      </div>
      <div class="bt-veq">
        <div class="bt-part">
          <div class="bt-pk">Realised</div>
          <div class="bt-pv">${pl(d.net)}</div>
          <div class="bt-ps">${cnt(s.total_trades)} closed</div>
        </div>
        <div class="bt-plus">+</div>
        <div class="bt-part">
          <div class="bt-pk">Still open at the end</div>
          <div class="bt-pv">${pl(d.open)}</div>
          <div class="bt-ps">${cnt(d.open_count)} never closed</div>
        </div>
        <div class="bt-plus">=</div>
        <div class="bt-part">
          <div class="bt-pk">Total</div>
          <div class="bt-pv">${pl(d.total)}</div>
          <div class="bt-ps">${d.adds_up === false
            ? `<span class="down">these do not add up</span>` : "the whole picture"}</div>
        </div>
      </div>
    </div>

    ${cs.length ? `<div class="bt-cav ${lvl}">${cs.map((c) =>
      `<div class="bt-cav-r ${c.level}"><i></i><span>${esc(c.text)}</span></div>`
      ).join("")}</div>` : ""}

    ${failed.length ? `<div class="note bad" style="margin-top:12px">
      <b>${failed.length} combination(s) failed.</b>
      <pre class="err">${esc(failed[0].error)}</pre></div>` : ""}

    <div class="bt-kpis">
      ${kpi("P/L per $ of drawdown", sc.value === null ? DASH
            : `<span class="${sc.value >= 0 ? "up" : "down"}">${
               sc.value.toFixed(2)}</span>`,
            sc.reason || "how every table here is ranked", "")}
      ${kpi("Max drawdown", pl(s.max_drawdown),
            measured(s.max_drawdown_pct)
              ? `${Number(s.max_drawdown_pct).toFixed(2)}% of capital at risk` : "",
            sparkSVG(dd, { fill: true, force: "down" }))}
      ${kpi("Profit factor", n2(s.profit_factor, 2),
            s.profit_factor != null ? "gross win / gross loss"
              : measured(s.total_trades) && Number(s.total_trades)
                ? "no losing trade in this window"
                : "nothing was traded", "")}
      ${kpi("Win rate", measured(s.win_rate)
              ? `${Number(s.win_rate).toFixed(1)}%` : DASH,
            `${cnt(s.winning_trades)}W / ${cnt(s.losing_trades)}L`, "")}
      ${kpi("Equity", pl(d.total), `${cnt(s.bars)} bars`, sparkSVG(spark, { fill: true }))}
      ${kpi("Time in market", measured(s.exposure_pct)
              ? `${Number(s.exposure_pct).toFixed(1)}%` : DASH,
            measured(s.trades_per_day)
              ? `${Number(s.trades_per_day).toFixed(2)} trades/day` : "", "")}
    </div>`);
}

const kpi = (k, v, sub, extra) => `
  <div class="bt-kpi">
    <div class="bt-kk">${k}</div>
    <div class="bt-kv num">${v}</div>
    ${extra || ""}
    ${sub ? `<div class="bt-ks">${esc(sub)}</div>` : ""}
  </div>`;

/* A sparkline as inline SVG rather than a canvas: six of these in a row is six
   canvases, six contexts and six resize observers for a shape that never
   needs a pointer. Zero is on the scale for the same reason the big chart puts
   it there. */
function sparkSVG(values, opt = {}) {
  const v = (values || []).filter((x) => Number.isFinite(Number(x)));
  if (v.length < 2) return "";
  const W = 100, H = 26;
  const step = Math.max(1, Math.ceil(v.length / 120));
  const pts = [];
  for (let i = 0; i < v.length; i += step) pts.push(Number(v[i]));
  if (pts[pts.length - 1] !== Number(v[v.length - 1])) pts.push(Number(v[v.length - 1]));
  let lo = Math.min(0, ...pts), hi = Math.max(0, ...pts);
  if (hi === lo) { hi += 1; lo -= 1; }
  const x = (i) => (i / (pts.length - 1)) * W;
  const y = (n) => H - ((n - lo) / (hi - lo)) * H;
  const dir = opt.force || (pts[pts.length - 1] >= 0 ? "up" : "down");
  const line = pts.map((n, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(n).toFixed(1)}`).join("");
  const zero = y(0).toFixed(1);
  return `<svg class="bt-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"
    aria-hidden="true">
    ${opt.fill ? `<path d="${line}L${W},${zero}L0,${zero}Z"
      fill="var(--${dir})" opacity=".14"/>` : ""}
    <path d="${line}" fill="none" stroke="var(--${dir})" stroke-width="1.5"
      vector-effect="non-scaling-stroke"/>
  </svg>`;
}

/* ------------------------------------------------------------------ chart */
const METRICS = [
  { key: "equity", label: "Equity" },
  { key: "drawdown", label: "Drawdown" },
  { key: "trades", label: "Per trade" },
];

function renderChart() {
  const host = el("btChart");
  if (!host) return;
  if (!detail || !detail.curve) {
    host.innerHTML = `<div class="faint" style="padding:22px 0;text-align:center">
      Loading the curve...</div>`;
    return;
  }
  if (chart) chart.destroy();
  chart = new ResultChart(host, {
    height: 300, storeKey: "bt", metrics: METRICS, metric: chartMetric,
    onMetric: (k) => { chartMetric = k; feedChart(); },
  });
  feedChart();
  const s = detail.summary || {};
  el("btChartX").innerHTML = measured(s.exposure_pct)
    ? `in the market ${Number(s.exposure_pct).toFixed(1)}% of bars &middot;
       ${cnt(s.total_trades)} trades`
    : `${cnt(s.total_trades)} trades`;
}

function feedChart() {
  if (!chart || !detail) return;
  const c = detail.curve || {};
  if (chartMetric === "drawdown") {
    chart.setSeries({
      values: c.drawdown || [], stamps: c.t || [],
      note: "How far below its own peak the run was, marked every bar. This is "
          + "not the worst closed trade; it is the worst it ever felt.",
      empty: "No drawdown series was returned for this run.",
    });
    return;
  }
  if (chartMetric === "trades") {
    const t = tradeSeries(detail.trades || []);
    chart.setSeries({
      values: t.values, stamps: t.stamps,
      note: t.values.length
        ? "One point per CLOSED trade, in the order they closed. Anything the "
        + "run never closed is not here -- it is in Open at the end."
        : "",
      empty: "This run closed no trades, so there is nothing per-trade to draw.",
    });
    return;
  }
  chart.setSeries({
    values: c.equity || [], stamps: c.t || [], trades: detail.trades || [],
    note: "Realised P/L lands on the bar a trade exits; anything open is "
        + "marked to market on every bar in between, which is why the dips are "
        + "here at all.",
    empty: "No equity curve was returned for this run.",
  });
}

/* ------------------------------------------------------------- benchmarks
   btstats computes a passive twin and a buy-and-hold and the old page drew
   only the second. The twin is the fair one: signed, so a short strategy is
   not measured against a long position, and time-weighted, so a strategy in
   the market a tenth of the time is measured against a tenth of a position. */
function renderBench(s) {
  const host = el("btBench");
  if (!host) return;
  const b = benchmarks(s);
  if (!b.rows.length) {
    host.innerHTML = `<div class="empty">No benchmark was computed for this
      run.</div>`;
    return;
  }
  host.innerHTML = tableHTML(
    ["", "It made", "This strategy made", "Edge", "Which won"],
    b.rows.map((r) => `<tr>
      <td style="text-align:left"><b>${esc(r.label)}</b>
        <div class="faint" style="font-size:11px">${esc(r.note)}${
          r.key === "twin" && measured(r.shares)
            ? ` &middot; ${Number(r.shares).toFixed(2)} shares` : ""}</div></td>
      <td class="num">${pl(r.value)}${measured(r.pct)
        ? `<div class="faint" style="font-size:11px">${Number(r.pct).toFixed(2)}%</div>` : ""}</td>
      <td class="num">${pl(b.mine)}</td>
      <td class="num"><b>${pl(r.edge)}</b></td>
      <td>${measured(r.edge)
        ? (r.edge >= 0 ? `<b class="up">the strategy</b>`
                       : `<b class="down">doing nothing</b>`) : DASH}</td>
    </tr>`));
}

/* -------------------------------------------------------------- result body */
function renderResBody(j, rows) {
  const host = el("btResBody");
  if (!host) return;
  const s = detail ? detail.summary : rows[selRow] || rows[0];

  if (resTab === "summary") return renderPerf(host, s);
  if (resTab === "trades") return renderTrades(host);
  if (resTab === "combos") return renderCombos(host, j, rows);
  if (resTab === "compare") return renderCompare(host, rows);
  return renderLogs(host);
}

function renderPerf(host, s) {
  if (!s) { host.innerHTML = `<div class="empty">No summary.</div>`; return; }
  const row = (k, v, note = "") =>
    `<tr><td style="text-align:left">${k}</td><td class="num">${v}</td>
     <td class="faint bt-note">${note}</td></tr>`;
  const grp = (t) => `<tr class="bt-grp"><td colspan="3">${t}</td></tr>`;
  host.innerHTML = tableHTML(["", "Value", ""], [
    grp("What it made"),
    row("Realised", pl(s.net_profit)),
    row("Open at the end", pl(s.open_pl),
        `${cnt(s.open_at_end)} position(s) never closed`),
    row("Total P/L", `<b>${pl(s.total_pl)}</b>`,
        "what the account would actually show"),
    row("Gross profit", pl(s.gross_profit)),
    row("Gross loss", measured(s.gross_loss)
        ? sgn(-Math.abs(Number(s.gross_loss))) : DASH),
    row("Profit factor", n2(s.profit_factor, 3),
        s.profit_factor == null ? "no losing trade in this window" : ""),
    row("Expectancy", pl(s.expectancy), "per closed trade"),

    grp("What it cost"),
    row("Max drawdown", pl(s.max_drawdown),
        "worst the equity curve got, marked every bar"),
    row("Max drawdown %", measured(s.max_drawdown_pct)
        ? `${Number(s.max_drawdown_pct).toFixed(2)}%` : DASH,
        "of the capital at risk, not of the curve's own peak"),
    row("P/L per $ of drawdown", (() => {
      const sc = score(s);
      return sc.value === null ? DASH : `<b>${sc.value.toFixed(3)}</b>`;
    })(), score(s).reason || "the ranking number"),
    row("Peak capital used", measured(s.peak_capital)
        ? money0(s.peak_capital) : DASH),
    row("Return on peak capital", measured(s.return_on_peak_capital_pct)
        ? `${Number(s.return_on_peak_capital_pct).toFixed(3)}%` : DASH),
    row("Max lots held", cnt(s.max_lots_held),
        s.hit_max_lots ? `<span class="down">this equals the cap -- the cap
          bound the result</span>` : ""),

    grp("How it traded"),
    row("Total trades", cnt(s.total_trades)),
    row("Winners / losers", `${cnt(s.winning_trades)} / ${cnt(s.losing_trades)}`),
    row("Win rate", measured(s.win_rate)
        ? `${Number(s.win_rate).toFixed(1)}%` : DASH),
    row("Average trade", pl(s.avg_trade)),
    row("Average win", pl(s.avg_win)),
    row("Average loss", pl(s.avg_loss)),
    row("Win/loss size ratio", n2(s.win_loss_ratio, 2),
        "above 1 means winners are bigger than losers"),
    row("Largest win", pl(s.largest_win)),
    row("Largest loss", pl(s.largest_loss)),
    row("Max consecutive wins", cnt(s.max_consecutive_wins)),
    row("Max consecutive losses", cnt(s.max_consecutive_losses),
        "what the worst stretch actually felt like"),
    row("Average bars in trade", n2(s.avg_bars_in_trade, 1)),
    row("Max bars in trade", cnt(s.max_bars_in_trade)),
    row("Fill rate", measured(s.fill_rate_pct)
        ? `${Number(s.fill_rate_pct).toFixed(1)}%` : DASH,
        measured(s.entries_missed) && Number(s.entries_missed)
          ? `${s.entries_missed} entries this strategy asked for could not fill`
          : ""),

    grp("Risk-adjusted, and the window"),
    row("Sharpe (daily)", n2(s.sharpe, 2),
        "on daily equity. Per-bar Sharpe on 1-minute bars is nonsense"),
    row("Sortino (daily)", n2(s.sortino, 2),
        s.sortino == null ? "fewer than three down days -- not meaningful" : ""),
    row("Time in market", measured(s.exposure_pct)
        ? `${Number(s.exposure_pct).toFixed(1)}%` : DASH),
    row("Trades per day", n2(s.trades_per_day, 2)),
    row("Window", measured(s.span_days)
        ? `${Number(s.span_days).toFixed(1)} days` : DASH,
        measured(s.span_days) && Number(s.span_days) < 30
          ? "a hypothesis, not a finding" : ""),
    row("Bars", cnt(s.bars), esc(s.bar_size || "")),
    row("Exits", Object.entries(s.exits || {})
        .map(([k, v]) => `${esc(k)} ${v}`).join(", ") || DASH),
  ], "No summary.");
}

function renderTrades(host) {
  const ts = (detail && detail.trades) || [];
  host.innerHTML = tableHTML(
    ["#", "Side", "In", "Entry", "Out", "Exit", "Why", "Shares", "P/L", "Running"],
    ts.slice(0, 400).map((t, i) => `<tr>
      <td class="faint">${i + 1}</td>
      <td>${t.side === "short" ? `<span class="down">short</span>` : "long"}</td>
      <td class="faint">${esc(String(t.entry_t).slice(5, 16).replace("T", " "))}</td>
      <td class="num">${measured(t.entry) ? Number(t.entry).toFixed(4) : DASH}</td>
      <td class="faint">${esc(String(t.exit_t).slice(5, 16).replace("T", " "))}</td>
      <td class="num">${measured(t.exit) ? Number(t.exit).toFixed(4) : DASH}</td>
      <td><span class="pill ${t.why === "stop" ? "down" : t.why === "target" ? "up" : ""}"
        >${esc(t.why)}</span></td>
      <td class="num">${measured(t.shares) ? Number(t.shares) : DASH}</td>
      <td class="num">${pl(t.pnl)}</td>
      <td class="num faint">${pl(t.cum_pnl)}</td>
    </tr>`),
    detail ? "This run closed no trades." : "Loading...");
  if (ts.length > 400) {
    host.insertAdjacentHTML("beforeend",
      `<div class="faint" style="padding:8px">Showing the first 400 of
       ${ts.length.toLocaleString()}.</div>`);
  }
}

/* Ranked by P/L per dollar of drawdown, never by profit, and the rows that
   cannot be ranked are shown UNDER the ranked ones with the reason -- dropping
   them is how a sweep of two hundred comes to look like a sweep of six. */
function renderCombos(host, j, rows) {
  const R = rank(rows);
  const line = (e, i, ranked) => {
    const r = e.row;
    /* A combination that RAISED has no summary to read caveats out of, and
       running them anyway reported "it never opened a position" -- true, and
       not the reason. The exception is the reason. */
    const cs = r.error ? []
      : caveats(r, { look_ahead: r.look_ahead, hit_max_lots: r.hit_max_lots });
    const flags = cs.filter((c) => c.level !== "info");
    return `<tr class="${e.i === selRow ? "on" : ""}" data-row="${e.i}">
      <td class="faint">${ranked ? i + 1 : "&mdash;"}</td>
      <td><input type="checkbox" class="bt-cmp" data-cmp="${e.i}"
        ${compare.includes(e.i) ? "checked" : ""}></td>
      <td class="mono bt-params">${esc(Object.entries(r.params || {})
        .map(([k, v]) => `${k}=${v}`).join("  ")) || "&mdash;"}</td>
      <td class="num" title="${esc(e.reason)}">${e.score === null ? DASH
        : `<b class="${e.score >= 0 ? "up" : "down"}">${e.score.toFixed(2)}</b>`}</td>
      <td class="num">${r.error ? `<span class="down">failed</span>` : pl(r.total_pl)}</td>
      <td class="num faint">${pl(r.net_profit)}</td>
      <td class="num faint">${pl(r.open_pl)}</td>
      <td class="num">${pl(r.max_drawdown)}</td>
      <td class="num">${cnt(r.total_trades)}</td>
      <td class="num">${n2(r.profit_factor, 2)}</td>
      <td class="num faint">${n2(r.sharpe, 1)}</td>
      <td style="text-align:left">${r.error
        ? `<span class="down" title="${esc(r.error)}">${esc(
            String(r.error).slice(0, 44))}</span>`
        : flags.length
          ? flags.map((c) => `<span class="pill ${c.level === "bad" ? "down" : "warn"}"
              title="${esc(c.text)}">${esc(c.code.replace(/_/g, " "))}</span>`).join(" ")
          : (e.reason ? `<span class="faint">${esc(e.reason)}</span>` : "")}</td>
    </tr>`;
  };

  const heads = ["#", "", "Parameters", "P/L per $DD", "Total P/L", "Realised",
                 "Open", "Max DD", "Trades", "PF", "Sharpe", "Flags"];
  host.innerHTML = tableHTML(heads,
    R.ranked.map((e, i) => line(e, i, true))
      .concat(R.excluded.length
        ? [`<tr class="bt-grp"><td colspan="${heads.length}">Not ranked
            &mdash; ${R.excluded.length} combination(s) under
            ${R.min_trades} closed trades or failed outright</td></tr>`]
          .concat(R.excluded.map((e, i) => line(e, i, false)))
        : []),
    "No combinations.")
    + `<div class="faint bt-foot">Ranked by <b>total P/L per dollar of
       drawdown</b>. Ranking by profit selects for whichever combination took
       the most risk, which is the opposite of the question being asked. Tick
       any two to four rows and open <b>Compare</b>.</div>`;

  host.querySelectorAll("[data-row]").forEach((tr) => {
    tr.onclick = (e) => {
      if (e.target.closest(".bt-cmp")) return;
      loadDetail(j.id, Number(tr.dataset.row));
    };
  });
  host.querySelectorAll(".bt-cmp").forEach((cb) => {
    cb.onclick = (e) => {
      e.stopPropagation();
      const i = Number(cb.dataset.cmp);
      if (cb.checked) { if (!compare.includes(i)) compare.push(i); }
      else compare = compare.filter((x) => x !== i);
      compare = compare.slice(0, 4);
      renderJob(lastJob);
    };
  });
}

/* Side by side, because "which of these is better" is the question a sweep is
   run to answer and reading it off a twelve-column table is not an answer. */
function renderCompare(host, rows) {
  if (compare.length < 2) {
    host.innerHTML = `<div class="empty">Tick two to four rows on the
      <b>All combinations</b> tab. They are compared here column by column,
      with their equity curves drawn on the same scale.</div>`;
    return;
  }
  const picked = compare.map((i) => ({ i, r: rows[i] })).filter((x) => x.r);
  /* The same ranking the table above used, so a row that is "not ranked"
     there cannot quietly print a ratio here. A thin row's P/L per $DD is
     arithmetic over two trades and putting it in the same column as a ratio
     over ninety is the comparison this tab exists to prevent. */
  const R = rank(rows);
  const rankOf = new Map(R.ranked.map((e, i) => [e.i, i + 1]));
  const F = [
    ["P/L per $DD", (r) => {
      const s = score(r);
      return s.value === null
        ? `<span class="faint" title="${esc(s.reason)}">&mdash;</span>`
        : `<b class="${s.value >= 0 ? "up" : "down"}">${s.value.toFixed(2)}</b>`;
    }],
    ["Total P/L", (r) => `<b>${pl(r.total_pl)}</b>`],
    ["Realised", (r) => pl(r.net_profit)],
    ["Open at end", (r) => pl(r.open_pl)],
    ["Max drawdown", (r) => pl(r.max_drawdown)],
    ["Trades", (r) => cnt(r.total_trades)],
    ["Win rate", (r) => measured(r.win_rate)
      ? `${Number(r.win_rate).toFixed(1)}%` : DASH],
    ["Profit factor", (r) => n2(r.profit_factor, 2)],
    ["Sharpe", (r) => n2(r.sharpe, 2)],
    ["Time in market", (r) => measured(r.exposure_pct)
      ? `${Number(r.exposure_pct).toFixed(1)}%` : DASH],
  ];
  /* One shared scale across every curve, or a comparison of pictures is a
     comparison of autoscales. */
  const all = picked.flatMap((p) => p.r.spark || []);
  const lo = Math.min(0, ...all), hi = Math.max(0, ...all);
  host.innerHTML = `<div class="tw"><table class="bt-cmp-t">
    <thead><tr><th></th>${picked.map((p) =>
      `<th><div class="bt-cmp-h">${rankOf.has(p.i)
        ? `rank ${rankOf.get(p.i)}` : `<span class="faint">not ranked</span>`}</div>
        <div class="mono bt-cmp-p">${esc(Object.entries(p.r.params || {})
          .map(([k, v]) => `${k}=${v}`).join(" ")) || "current settings"}</div>
        ${sparkFixed(p.r.spark || [], lo, hi)}</th>`).join("")}</tr></thead>
    <tbody>${F.map(([k, f]) => `<tr>
      <td style="text-align:left">${k}</td>
      ${picked.map((p) => `<td class="num">${f(p.r)}</td>`).join("")}
    </tr>`).join("")}
    <tr><td style="text-align:left">Flags</td>
      ${picked.map((p) => `<td style="text-align:left">${
        caveats(p.r, { hit_max_lots: p.r.hit_max_lots })
          .filter((c) => c.level !== "info")
          .map((c) => `<span class="pill ${c.level === "bad" ? "down" : "warn"}"
            title="${esc(c.text)}">${esc(c.code.replace(/_/g, " "))}</span>`)
          .join(" ") || DASH}</td>`).join("")}</tr>
    </tbody></table></div>
    <div class="faint bt-foot">Every curve above is drawn on the SAME scale.
    Four sparklines each autoscaled to their own range compare nothing.</div>`;
}

function sparkFixed(values, lo, hi) {
  const v = (values || []).map(Number).filter(Number.isFinite);
  if (v.length < 2 || hi === lo) return "";
  const W = 140, H = 34;
  const x = (i) => (i / (v.length - 1)) * W;
  const y = (n) => H - ((n - lo) / (hi - lo)) * H;
  const dir = v[v.length - 1] >= 0 ? "up" : "down";
  const d = v.map((n, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(n).toFixed(1)}`).join("");
  return `<svg class="bt-spark wide" viewBox="0 0 ${W} ${H}"
    preserveAspectRatio="none" aria-hidden="true">
    <path d="${d}L${W},${y(0).toFixed(1)}L0,${y(0).toFixed(1)}Z"
      fill="var(--${dir})" opacity=".13"/>
    <path d="${d}" fill="none" stroke="var(--${dir})" stroke-width="1.5"
      vector-effect="non-scaling-stroke"/></svg>`;
}

function renderLogs(host) {
  const lines = (detail && detail.logs) || [];
  const out = (detail && detail.stdout) || "";
  host.innerHTML = (lines.length || out)
    ? `<pre class="err" style="max-height:320px">${esc(
        lines.join("\n") + (out ? "\n--- print() output ---\n" + out : ""))}</pre>`
    : `<div class="faint" style="padding:10px 0">Nothing logged. Call
       <code>ctx.log("...")</code> in your code to see it here.</div>`;
}

/* ------------------------------------------------------ turn it into a thing */
function renderActions(j, rows) {
  const host = el("btActions");
  if (!host) return;
  const row = rows[selRow] || rows[0];
  const params = (row && row.params) || {};
  const hasParams = Object.keys(params).length > 0;

  host.innerHTML = `
    <div class="row-btns">
      <button class="btn primary sm" id="btAdd">Save as a strategy</button>
      <button class="btn sm" id="btBank">Bank it as evidence</button>
      <button class="btn sm" id="btCopy">Copy parameters</button>
    </div>
    <div class="tip">${hasParams
      ? `Row #${selRow + 1}'s parameters -- <code>${
          esc(Object.entries(params).map(([k, v]) => `${k}=${v}`).join(", "))}</code>
         -- travel with whichever you pick.`
      : `This run used the current settings with nothing swept.`}
      <br><b>Saving a strategy arms nothing.</b> It stores the settings under a
      name; you still choose which ticker runs it, and every ticker starts
      disarmed.<br>
      <b>Banking</b> writes the result to <code>state/risk_bank.jsonl</code>,
      which is append-only. That is the point: a finding that can be edited
      afterwards is not evidence.</div>`;

  el("btAdd").onclick = () => act(() => addStrategy(j, row));
  el("btBank").onclick = () => act(() => bankIt(j, row));
  el("btCopy").onclick = () => {
    navigator.clipboard.writeText(
      Object.entries(params).map(([k, v]) => `${k}=${v}`).join("\n"));
    toast("Parameters copied.", "ok");
  };
}

async function addStrategy(j, row) {
  const s = detail ? detail.summary : row;
  const dflt = `${j.symbol} ${j.mode} ${new Date().toISOString().slice(0, 10)}`;
  const name = prompt("Name this strategy:", (j.label || dflt).slice(0, 60));
  if (!name) return;

  const params = (row && row.params) || {};
  const note = `Backtested on ${j.symbol} ${j.timeframe}, ${j.bars} bars `
    + `(${String(j.from).slice(0, 10)} to ${String(j.to).slice(0, 10)}): `
    + `total P/L ${fmt(s && s.total_pl)}, realised ${fmt(s && s.net_profit)}, `
    + `open ${fmt(s && s.open_pl)}, ${s ? s.total_trades : 0} trades, `
    + `profit factor ${s && s.profit_factor != null
        ? Number(s.profit_factor).toFixed(2) : "n/a"}, `
    + `max drawdown ${fmt(s && s.max_drawdown)}.`;

  if (j.mode === "code") {
    // a coded strategy IS its source -- saving it as a document would lose it
    const slug = await POST("/api/code", {
      slug: name, code: (el("btCode") || {}).value || "",
    });
    toast(`Saved the code as <b>${esc(slug.slug)}</b>. ${esc(note)}`, "ok", 12000);
    await loadCode();
    return;
  }

  let spec;
  if (j.mode === "strategy") {
    const cur = await GET("/api/strategies/" + encodeURIComponent(j.strategy));
    spec = cur.spec;
    // fold the winning sweep values back into the document so the saved copy
    // IS the thing that was tested, not the thing that was started from
    for (const [k, v] of Object.entries(params)) {
      if (k.startsWith("target.")) spec.target = { ...(spec.target || {}), [k.slice(7)]: v };
      else if (k.startsWith("stop.")) spec.stop = { ...(spec.stop || {}), [k.slice(5)]: v };
      else if (k.startsWith("ind.")) {
        const [, iname, pname] = k.split(".");
        spec.indicators = { ...(spec.indicators || {}) };
        spec.indicators[iname] = { ...(spec.indicators[iname] || {}), [pname]: v };
      }
    }
  } else {
    // the ladder: record it as a document that says so, with the tested numbers
    spec = {
      name, indicators: {},
      entry: { lt: ["close", "open"] },
      exit: { target_reached: true },
      target: { points: Number(params.take_profit != null ? params.take_profit : 0.10) },
      stop: {},
    };
  }
  spec.name = name;
  spec.note = note;
  const r = await POST("/api/strategies", spec);
  toast(`Saved as <b>${esc(r.slug)}</b> with the backtest attached. `
        + `Set that slug on a ticker to run it.`, "ok", 12000);
}

async function bankIt(j, row) {
  const s = detail ? detail.summary : row;
  let profs = [];
  try { profs = (await GET("/api/risk/profiles")).profiles || []; }
  catch (e) { toast("Could not load risk profiles.", "err"); return; }
  const slug = prompt(
    "Bank this result against which risk profile?\n\n"
    + profs.map((p) => "  " + p.slug).join("\n"), profs[0] ? profs[0].slug : "");
  if (!slug) return;
  const prof = profs.find((p) => p.slug === slug.trim());
  if (!prof) { toast(`No risk profile "${esc(slug)}".`, "err"); return; }

  await POST("/api/risk/bank", {
    profile: prof,
    result: { summary: s, _params: (row && row.params) || {} },
    strategy: j.mode === "ladder" ? "ladder" : (j.strategy || j.label || j.mode),
    symbol: j.symbol, timeframe: j.timeframe, days: j.days,
    mode: j.mode, job: j.id, actor: "human",
  });
  toast(`Banked against <b>${esc(prof.name)}</b>. See the <b>Bank</b> tab.`,
        "ok", 9000);
}

const fmt = (v) => (measured(v)
  ? (v < 0 ? "-$" : "$") + Math.abs(Number(v)).toFixed(2) : "n/a");

/* ===================================================================== css
   app.css belongs to another agent, so this room carries its own style
   element, written once, in theme tokens only -- both themes follow for free.
   Same pattern as views/options.js. */
const CSS = `
.bt-modes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;
            margin-bottom: 16px; }
.bt-mode { appearance: none; text-align: left; cursor: pointer;
           background: transparent; color: var(--muted); font: inherit;
           border: 1px solid var(--hairline); border-radius: var(--radius-sm);
           padding: 10px 12px; transition: border-color .15s, background .15s; }
.bt-mode b { display: block; color: var(--text); font-size: 13px;
             margin-bottom: 2px; }
.bt-mode span { font-size: 11px; color: var(--faint); line-height: 1.35;
                display: block; }
.bt-mode:hover { border-color: var(--hairline2); }
.bt-mode.on { background: var(--accent-dim); border-color: var(--accent);
              box-shadow: inset 0 0 0 1px var(--accent-dim); }
.bt-mode.on b { color: var(--accent-2); }

.bt-row { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
.bt-row .f { margin: 0 0 10px; }
.bt-row .f > span { margin-bottom: 4px; }
.bt-sym { width: 116px; } .bt-tf { width: 104px; } .bt-days { width: 116px; }
.bt-label { flex: 1 1 140px; }
.bt-row .rc-chips { margin-bottom: 10px; }

.bt-sweep { border: 1px solid var(--hairline); border-radius: var(--radius-sm);
            padding: 10px 12px; margin: 4px 0 14px; }
.bt-sweep > summary { cursor: pointer; font-size: 12.5px; font-weight: 600;
                      list-style: none; }
.bt-sweep > summary::-webkit-details-marker { display: none; }
.bt-sweep > summary::before { content: "+"; display: inline-block; width: 14px;
                              color: var(--faint); }
.bt-sweep[open] > summary::before { content: "\\2212"; }
.bt-sweep textarea { margin-top: 10px; }
.bt-count { font-size: 11.5px; margin: 6px 0 10px; line-height: 1.5; }
.bt-count code { font-size: 11px; }
.bt-go { display: flex; gap: 8px; align-items: center; }
.bt-go .btn.primary { flex: 1 1 auto; }

.bt-head { margin-bottom: 14px; font-size: 11.5px; line-height: 1.6; }
.bt-verdict { display: flex; gap: 22px; align-items: center; flex-wrap: wrap;
              padding-bottom: 4px; }
.bt-vmain { min-width: 190px; }
.bt-vk, .bt-pk, .bt-kk { font-size: 10.5px; letter-spacing: .07em;
                         text-transform: uppercase; color: var(--faint); }
.bt-vv { font-size: 40px; font-weight: 700; line-height: 1.1; margin: 2px 0;
         font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
.bt-vs { font-size: 11.5px; color: var(--faint); }
.bt-veq { display: flex; gap: 14px; align-items: center; flex: 1 1 340px;
          flex-wrap: wrap; }
.bt-part { flex: 1 1 100px; }
.bt-pv { font-size: 19px; font-weight: 650; margin: 3px 0 1px;
         font-variant-numeric: tabular-nums; }
.bt-ps { font-size: 11px; color: var(--faint); }
.bt-plus { color: var(--faint); font-size: 17px; }

.bt-cav { margin-top: 16px; display: grid; gap: 7px; }
.bt-cav-r { display: flex; gap: 9px; align-items: flex-start; font-size: 12px;
            line-height: 1.5; padding: 9px 11px; border-radius: 10px;
            border: 1px solid var(--hairline); }
.bt-cav-r i { width: 4px; align-self: stretch; border-radius: 3px;
              flex: 0 0 4px; }
.bt-cav-r.bad { background: color-mix(in srgb, var(--down) 9%, transparent); }
.bt-cav-r.bad i { background: var(--down); }
.bt-cav-r.warn { background: color-mix(in srgb, var(--warn) 9%, transparent); }
.bt-cav-r.warn i { background: var(--warn); }
.bt-cav-r.info i { background: var(--faint); }
@supports not (color: color-mix(in srgb, red 1%, blue)) {
  .bt-cav-r.bad, .bt-cav-r.warn { background: var(--surface-2); }
}

.bt-kpis { display: grid; gap: 1px; margin-top: 18px;
           grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           background: var(--hairline); border: 1px solid var(--hairline);
           border-radius: var(--radius-sm); overflow: hidden; }
.bt-kpi { padding: 12px 13px; background: var(--solid); min-width: 0; }
.bt-kv { font-size: 21px; font-weight: 650; margin: 4px 0 2px;
         font-variant-numeric: tabular-nums; }
.bt-ks { font-size: 10.5px; color: var(--faint); line-height: 1.4; }
.bt-spark { display: block; width: 100%; height: 26px; margin: 2px 0 4px; }
.bt-spark.wide { height: 34px; }

.bt-job { padding: 9px 18px; border-bottom: 1px solid var(--hairline);
          cursor: pointer; }
.bt-job:hover { background: var(--surface-2); }
.bt-job-t { display: flex; gap: 7px; align-items: baseline; font-size: 12.5px; }
.bt-job-t .faint { flex: 1; overflow: hidden; text-overflow: ellipsis;
                   white-space: nowrap; font-size: 11px; }
.bt-job-s { font-size: 10.5px; text-transform: uppercase;
            letter-spacing: .05em; }
.bt-job-b { display: flex; gap: 8px; font-size: 11px; margin-top: 2px; }
.bt-job-p { margin-left: auto; font-variant-numeric: tabular-nums; }

.bt-grp td { background: var(--surface-2); font-size: 10.5px;
             letter-spacing: .07em; text-transform: uppercase;
             color: var(--faint); text-align: left !important; }
.bt-note { text-align: left !important; font-size: 11px; }
.bt-params { text-align: left !important; font-size: 11px; max-width: 220px;
             overflow: hidden; text-overflow: ellipsis; }
.bt-foot { font-size: 11.5px; padding: 10px 2px 2px; line-height: 1.55; }
.bt-pre { white-space: pre-wrap; font-size: 10.5px; max-height: 320px;
          overflow: auto; }
.bt-codebar { display: flex; gap: 8px; align-items: flex-end;
              margin-bottom: 6px; flex-wrap: wrap; }
.bt-cmp-t th { vertical-align: top; }
.bt-cmp-h { font-size: 12px; font-weight: 650; }
.bt-cmp-p { font-size: 10px; color: var(--faint); max-width: 170px;
            white-space: normal; line-height: 1.35; margin: 2px 0 4px;
            text-transform: none; letter-spacing: 0; }
.bt-cmp-h { text-transform: none; letter-spacing: 0; }

@media (max-width: 720px) {
  .bt-modes { grid-template-columns: 1fr; }
  .bt-vv { font-size: 32px; }
  .bt-veq { gap: 10px; }
  .bt-part { flex: 1 1 78px; }
  .bt-pv { font-size: 16px; }
  .bt-sym, .bt-tf, .bt-days { flex: 1 1 88px; width: auto; }
  .bt-label { flex: 1 1 100%; }
}
`;

function ensureStyle() {
  if (document.getElementById("btCss")) return;
  const s = document.createElement("style");
  s.id = "btCss";
  s.textContent = CSS;
  document.head.appendChild(s);
}
