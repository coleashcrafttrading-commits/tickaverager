/* ============================================================================
   Backtest -- three ways to describe a strategy, one way to judge it.

     Ladder    replays the live engine's own decision functions
     Strategy  replays a saved indicator document
     Code      runs real Python you or an agent wrote

   All three produce the SAME report object, so they can be compared. That is
   the whole reason for having three: an idea is only worth moving to the live
   fleet if it beats the ladder that is already running.

   Ranking is on TOTAL P/L -- realized alone rewards a setting that banks
   winners while quietly accumulating lots it never closes.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, ask, toast, el, esc, card, stat, tableHTML,
  money, money0, sgn, pct, qty, go,
} from "../core.js";
import { EqChart } from "../eqchart.js";

let poll = null;
let STRATS = [];
let CODEFILES = [];
let TEMPLATE = "";
let mode = "ladder";
let lastJob = null;      // the finished job status
let detail = null;       // the full report for the selected row
let selRow = 0;
let eq = null;           // EqChart instance
let resTab = "summary";
let forAcct = "";        // jobs and results are per account; strategies and code are shared

const PRESETS = {
  "Take profit": { take_profit: "0.05,0.10,0.20,0.30,0.40,0.50" },
  "Add distance": { add_distance: "0.05,0.10,0.15,0.20,0.30,0.40" },
  "TP × add": { take_profit: "0.10,0.20,0.30,0.40", add_distance: "0.10,0.20,0.30,0.40" },
  "Exit style": { exit_mode: "limit,trail", trail_amount: "0.03,0.05,0.10" },
  "Lot size": { shares_per_lot: "25,50,100,200" },
};

function parseSweep(txt) {
  /* "take_profit=0.1,0.2\np.rsi_period=7,14" -> {take_profit:[.1,.2], ...} */
  const out = {};
  for (const line of String(txt || "").split(/[\n;]/)) {
    const t = line.trim();
    if (!t || t.startsWith("#") || !t.includes("=")) continue;
    const [k, v] = t.split("=", 2);
    const vals = v.split(",").map((x) => x.trim()).filter(Boolean).map((x) => {
      if (/^-?\d+$/.test(x)) return parseInt(x, 10);
      if (/^-?\d*\.\d+$/.test(x)) return parseFloat(x);
      if (x === "true") return true;
      if (x === "false") return false;
      return x;
    });
    if (vals.length) out[k.trim()] = vals;
  }
  return out;
}

const combos = (sw) => Object.values(sw).reduce((a, v) => a * (v.length || 1), 1);

VIEWS.backtest = {
  title: () => "Backtest",
  sub: () => "replay any strategy over real Alpaca history",

  mount() {
    if (forAcct !== S.account) {
      clearInterval(poll);
      lastJob = null; detail = null; selRow = 0;
      forAcct = S.account;
    }
    const syms = (S.ov?.tickers || []).map((t) => t.symbol);
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${card("Run", `
            <div class="seg" id="btMode">
              <button class="seg-b on" data-m="ladder">Ladder</button>
              <button class="seg-b" data-m="strategy">Strategy</button>
              <button class="seg-b" data-m="code">Code</button>
            </div>

            <div class="f2" style="margin-top:14px">
              <label class="f"><span>Symbol</span>
                <input id="btSym" value="${syms[0] || "RAM"}" placeholder="RAM"></label>
              <label class="f"><span>Timeframe</span>
                <select id="btTf">
                  ${["1Min", "5Min", "15Min", "1Hour", "1Day"].map((t) =>
                    `<option${t === "1Min" ? " selected" : ""}>${t}</option>`).join("")}
                </select></label>
            </div>
            <div class="f2">
              <label class="f"><span>History (days)</span>
                <input id="btDays" type="number" value="30" min="1" max="2000"></label>
              <label class="f"><span>Label</span>
                <input id="btLabel" placeholder="optional"></label>
            </div>

            <div id="btModeBox"></div>

            <label class="f"><span>Sweep — one <code>param=a,b,c</code> per line</span>
              <textarea id="btSweep" rows="4"
                placeholder="take_profit=0.05,0.10,0.20&#10;p.rsi_period=7,14,21"></textarea></label>
            <div class="hint" id="btCount"></div>
            <div class="hint">A sweep may mix every kind of parameter at once:
              <code>take_profit</code> (a run setting), <code>target.points</code> and
              <code>stop.atr_mult</code> (a strategy document), <code>ind.rsi.period</code>
              (an indicator inside it), <code>p.oversold</code> (a PARAMS value in code).</div>
            <div class="row-btns" style="margin-bottom:12px" id="btPresets">
              ${Object.keys(PRESETS).map((p) =>
                `<button class="btn sm preset" data-p="${esc(p)}">${p}</button>`).join("")}
            </div>
            <button class="btn primary" id="btRun" style="width:100%">Run backtest</button>`)}
          <div id="btResult"></div>
        </div>
        <div>
          ${card("Recent runs", `<div id="btJobs"></div>`, "", { flush: true })}
          ${card("Reading the numbers", `<div class="tip" style="margin-top:0">
            <b>Total P/L</b> includes open inventory marked to market at the end.<br>
            <b>Profit factor</b> is gross win ÷ gross loss. It is the number that
            survives a strategy with no stop, because it cannot be flattered by
            never closing a loser. It reads <b>—</b> when there were no losing
            trades at all, which is a fact about the window, not an edge.<br>
            <b>Max DD</b> is the worst the equity curve got, marked to market every
            bar — not the worst closed trade.<br>
            <b>Sharpe</b> is computed on daily equity. Per-bar Sharpe on a 1-minute
            ladder returns nonsense in the twenties.<br>
            <b>Max lots</b> — if this equals your cap, the cap bound the result and
            you are comparing caps, not the parameter you swept.</div>`)}
        </div>
      </div>`;

    el("btMode").querySelectorAll(".seg-b").forEach((b) => {
      b.onclick = () => {
        mode = b.dataset.m;
        el("btMode").querySelectorAll(".seg-b").forEach((x) =>
          x.classList.toggle("on", x.dataset.m === mode));
        paintModeBox();
      };
    });
    el("view").querySelectorAll(".preset").forEach((b) => {
      b.onclick = () => {
        el("btSweep").value = Object.entries(PRESETS[b.dataset.p])
          .map(([k, v]) => `${k}=${v}`).join("\n");
        updateCount();
      };
    });
    el("btSweep").addEventListener("input", updateCount);
    el("btRun").onclick = () => act(runIt);

    paintModeBox();
    updateCount();
    loadStrategies();
    loadCode();
    loadJobs();
    if (lastJob) renderJob(lastJob);
  },

  paint() { /* results are pushed by the watcher */ },
};

/* ------------------------------------------------------------- mode panes */
function paintModeBox() {
  const box = el("btModeBox");
  if (!box) return;
  if (mode === "ladder") {
    box.innerHTML = `<div class="hint">Replays the engine's <b>own</b> decision
      functions over real bars — the same code the live ladder runs, so a
      result here is about the strategy and not about a second implementation
      of it. Uses the ticker's current settings unless the sweep overrides them.</div>`;
  } else if (mode === "strategy") {
    box.innerHTML = `
      <label class="f"><span>Strategy</span>
        <select id="btStrat"></select></label>
      <div class="hint" id="btStratNote"></div>`;
    fillStrategies();
  } else {
    box.innerHTML = `
      <div style="display:flex;gap:8px;align-items:end;margin-bottom:4px">
        <label class="f" style="flex:1;margin:0"><span>Saved code</span>
          <select id="btCodeSel"></select></label>
        <button class="btn sm" id="btCodeNew">New</button>
        <button class="btn sm" id="btCodeSave">Save</button>
        <button class="btn sm" id="btCodeDel">×</button>
      </div>
      <textarea id="btCode" rows="18" spellcheck="false" class="code"></textarea>
      <div class="hint" id="btCodeErr"></div>
      <div class="hint">Define <code>on_bar(ctx, i)</code>; <code>init(ctx)</code>
        and <code>PARAMS</code> are optional. <b>Look-ahead is blocked</b> —
        <code>ctx.c[i+1]</code> raises rather than quietly inventing money. An
        entry decided on bar <i>i</i> fills at bar <i>i+1</i>'s open, and a bar
        that touches both stop and target resolves as the <b>stop</b>.
        Runs in a separate process with no broker credentials.</div>`;
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
  const keys = Object.keys(sw);
  box.innerHTML = keys.length
    ? `<b>${n.toLocaleString()}</b> combination${n === 1 ? "" : "s"} across
       ${keys.length} parameter${keys.length === 1 ? "" : "s"}${n > 400
       ? ` — <span class="warn">this will take a while</span>` : ""}`
    : "Leave empty to run once with the current settings.";
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
    || `<option value="">— none saved —</option>`;
  const note = () => {
    const s = STRATS.find((x) => x.slug === sel.value);
    el("btStratNote").innerHTML = s
      ? `${esc(s.note || s.name)}${s.indicators && s.indicators.length
          ? ` <span class="faint">(${s.indicators.join(", ")})</span>` : ""}`
      : `Build one on the <b>Strategies</b> page.`;
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
  el("btResult").innerHTML = `<div class="card"><div class="card-b">
    <div class="skel" style="width:40%"></div>
    <div class="skel" style="width:70%;margin-top:10px"></div></div></div>`;
  const j = await POST("/api/backtest", spec);
  toast(`Queued — ${j.total} combination(s).`, "ok");
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
      if (j.state === "done" && j.results.length) loadDetail(id, 0);
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
    host.innerHTML = tableHTML(["When", "What", "State", ""],
      (r.jobs || []).map((j) => `<tr>
        <td class="faint" style="text-align:left">${new Date(j.created * 1000)
          .toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</td>
        <td style="text-align:left"><b>${esc(j.symbol)}</b>
          <span class="faint">${esc(j.label || j.timeframe)}</span></td>
        <td><span class="${j.state === "done" ? "up" : j.state === "error" ? "down" : "warn"}"
          >${esc(j.state)}</span></td>
        <td><button class="btn sm" data-job="${esc(j.id)}">Open</button></td>
      </tr>`), "Nothing run yet.");
    host.querySelectorAll("[data-job]").forEach((b) => {
      b.onclick = () => { detail = null; watch(b.dataset.job); };
    });
  } catch (e) { /* panel is optional */ }
}

/* ----------------------------------------------------------------- results */
function renderJob(j) {
  const host = el("btResult");
  if (!host || !j) return;

  if (j.state === "error") {
    host.innerHTML = `<div class="note bad"><b>Backtest failed:</b>
      ${esc(j.error)}</div>`;
    return;
  }
  if (j.state !== "done") {
    const p = j.total ? Math.round((j.done / j.total) * 100) : 0;
    host.innerHTML = card("Running", `
      <div class="bar"><div class="bar-f" style="width:${p}%"></div></div>
      <div class="faint" style="margin-top:8px">${j.done} of ${j.total}
        combination${j.total === 1 ? "" : "s"}${j.bars
        ? ` · ${j.bars.toLocaleString()} bars` : ""}</div>`);
    return;
  }

  const rows = j.results || [];
  const failed = rows.filter((r) => r.error);
  const okRows = rows.filter((r) => !r.error);
  const multi = rows.length > 1;

  host.innerHTML =
    card("Result", `
      <div class="faint" style="margin-bottom:12px">
        <b>${esc(j.symbol)}</b> · ${esc(j.timeframe)} ·
        ${(j.bars || 0).toLocaleString()} bars ·
        ${String(j.from).slice(0, 10)} → ${String(j.to).slice(0, 10)} ·
        the tape itself moved ${sgn(j.drift, 2)}% ·
        ${j.seconds}s${multi ? ` · ${rows.length} combinations` : ""}
      </div>
      ${failed.length ? `<div class="note bad" style="margin-bottom:12px">
        <b>${failed.length} combination(s) failed.</b>
        <pre class="err">${esc(failed[0].error)}</pre></div>` : ""}
      <div id="btSummary"></div>`)
    + card("Equity curve", `<div id="btChart"></div>`,
        `<span class="faint" id="btChartNote"></span>`)
    + card("", `
      <div class="tabs2" id="btResTabs">
        <button class="t2 ${resTab === "summary" ? "on" : ""}" data-t="summary">Performance</button>
        <button class="t2 ${resTab === "trades" ? "on" : ""}" data-t="trades">Trades</button>
        ${multi ? `<button class="t2 ${resTab === "combos" ? "on" : ""}"
                    data-t="combos">All combinations</button>` : ""}
        <button class="t2 ${resTab === "logs" ? "on" : ""}" data-t="logs">Log</button>
      </div>
      <div id="btResBody"></div>`, "", { flush: false })
    + card("Use this result", `<div id="btActions"></div>`);

  el("btResTabs").querySelectorAll(".t2").forEach((b) => {
    b.onclick = () => { resTab = b.dataset.t; renderJob(j); };
  });

  renderSummary(okRows[0] || rows[0]);
  renderChart();
  renderResBody(j, rows);
  renderActions(j, rows);
}

function renderSummary(row) {
  const host = el("btSummary");
  if (!host) return;
  const s = detail ? detail.summary : row;
  if (!s) { host.innerHTML = ""; return; }
  const pf = s.profit_factor == null ? "—" : s.profit_factor.toFixed(2);
  host.innerHTML = `<div class="stats">
    ${stat("Net profit", sgn(s.net_profit), `${s.total_trades} closed`)}
    ${stat("Open at end", sgn(s.open_pl), `${s.open_at_end || 0} still held`)}
    ${stat("Total P/L", sgn(s.total_pl))}
    ${stat("Profit factor", pf, "gross win ÷ gross loss")}
    ${stat("Max drawdown", sgn(s.max_drawdown),
           s.max_drawdown_pct ? `${s.max_drawdown_pct.toFixed(1)}%` : "")}
    ${stat("Win rate", (s.win_rate ?? 0).toFixed(1) + "%",
           `${s.winning_trades || 0}W / ${s.losing_trades || 0}L`)}
    ${stat("Sharpe", s.sharpe == null ? "—" : s.sharpe.toFixed(2), "daily")}
    ${stat("Buy & hold", (s.buy_hold_pct ?? 0).toFixed(2) + "%", "the tape itself")}
  </div>
  ${s.caveat ? `<div class="note warn" style="margin-top:12px">${esc(s.caveat)}</div>` : ""}`;
}

function renderChart() {
  const host = el("btChart");
  if (!host) return;
  if (!detail || !detail.curve) {
    host.innerHTML = `<div class="faint" style="padding:22px 0;text-align:center">
      Loading the curve…</div>`;
    return;
  }
  if (eq) eq.destroy();
  eq = new EqChart(host, { height: 250 });
  eq.setData(detail);
  const s = detail.summary;
  el("btChartNote").innerHTML =
    `in the market ${s.exposure_pct}% of bars · ${s.total_trades} trades`;
}

function renderResBody(j, rows) {
  const host = el("btResBody");
  if (!host) return;

  if (resTab === "summary") {
    const s = detail ? detail.summary : rows[0];
    if (!s) { host.innerHTML = ""; return; }
    const row = (k, v, note = "") =>
      `<tr><td style="text-align:left">${k}</td><td class="num">${v}</td>
       <td class="faint" style="text-align:left;font-size:11px">${note}</td></tr>`;
    const m = (v) => v == null ? "—" : sgn(v);
    host.innerHTML = tableHTML(["", "Value", ""], [
      row("Net profit", m(s.net_profit)),
      row("Gross profit", m(s.gross_profit)),
      row("Gross loss", m(-Math.abs(s.gross_loss || 0))),
      row("Profit factor", s.profit_factor == null ? "—" : s.profit_factor.toFixed(3),
          s.profit_factor == null ? "no losing trades in this window" : ""),
      row("Open P/L at end", m(s.open_pl), `${s.open_at_end || 0} position(s) never closed`),
      row("Total P/L", m(s.total_pl), "what the account would actually show"),
      row("Max drawdown", m(s.max_drawdown),
          "worst the equity curve got, marked every bar"),
      row("Max drawdown %", (s.max_drawdown_pct ?? 0).toFixed(2) + "%"),
      row("Total trades", s.total_trades),
      row("Winners / losers", `${s.winning_trades || 0} / ${s.losing_trades || 0}`),
      row("Win rate", (s.win_rate ?? 0).toFixed(1) + "%"),
      row("Average trade", m(s.avg_trade)),
      row("Average win", m(s.avg_win)),
      row("Average loss", m(s.avg_loss)),
      row("Win/loss size ratio", (s.win_loss_ratio ?? 0).toFixed(2),
          "above 1 means winners are bigger than losers"),
      row("Largest win", m(s.largest_win)),
      row("Largest loss", m(s.largest_loss)),
      row("Max consecutive wins", s.max_consecutive_wins ?? 0),
      row("Max consecutive losses", s.max_consecutive_losses ?? 0,
          "what the worst stretch actually felt like"),
      row("Avg bars in trade", s.avg_bars_in_trade ?? 0),
      row("Max bars in trade", s.max_bars_in_trade ?? 0),
      row("Peak capital used", money0(s.peak_capital || 0)),
      row("Return on peak capital", (s.return_on_peak_capital_pct ?? 0).toFixed(3) + "%"),
      row("Time in market", (s.exposure_pct ?? 0) + "%"),
      row("Sharpe (daily)", s.sharpe == null ? "—" : s.sharpe.toFixed(2)),
      row("Sortino (daily)", s.sortino == null ? "—" : s.sortino.toFixed(2),
          s.sortino == null ? "fewer than 3 down days — not meaningful" : ""),
      row("Trades per day", s.trades_per_day ?? 0),
      row("Buy &amp; hold over the window", (s.buy_hold_pct ?? 0).toFixed(2) + "%",
          "doing nothing but owning it"),
      row("Exits", Object.entries(s.exits || {})
          .map(([k, v]) => `${k} ${v}`).join(", ") || "—"),
    ], "No summary.");
    return;
  }

  if (resTab === "trades") {
    const ts = (detail && detail.trades) || [];
    host.innerHTML = tableHTML(
      ["#", "Side", "In", "Entry", "Out", "Exit", "Why", "Sh", "P/L", "Running"],
      ts.slice(0, 400).map((t, i) => `<tr>
        <td class="faint">${i + 1}</td>
        <td>${t.side === "short" ? `<span class="down">short</span>` : "long"}</td>
        <td class="faint">${String(t.entry_t).slice(5, 16).replace("T", " ")}</td>
        <td class="num">${(+t.entry).toFixed(4)}</td>
        <td class="faint">${String(t.exit_t).slice(5, 16).replace("T", " ")}</td>
        <td class="num">${(+t.exit).toFixed(4)}</td>
        <td><span class="pill ${t.why === "stop" ? "down" : t.why === "target" ? "up" : ""}"
          >${esc(t.why)}</span></td>
        <td class="num">${qty(t.shares)}</td>
        <td class="num">${sgn(t.pnl)}</td>
        <td class="num faint">${sgn(t.cum_pnl)}</td>
      </tr>`),
      detail ? "This run closed no trades." : "Loading…");
    if (ts.length > 400) {
      host.insertAdjacentHTML("beforeend",
        `<div class="faint" style="padding:8px">Showing the first 400 of
         ${ts.length.toLocaleString()}.</div>`);
    }
    return;
  }

  if (resTab === "combos") {
    host.innerHTML = tableHTML(
      ["", "Parameters", "Total P/L", "Trades", "PF", "Max DD", "Win%", "Sharpe"],
      rows.map((r, i) => `<tr class="${i === selRow ? "on" : ""}"
          style="cursor:pointer" data-row="${i}">
        <td class="faint">${i + 1}</td>
        <td class="mono" style="text-align:left;font-size:11px">${
          esc(Object.entries(r.params || {}).map(([k, v]) => `${k}=${v}`).join("  ")) || "—"}</td>
        <td class="num">${r.error ? `<span class="down">failed</span>` : sgn(r.total_pl)}</td>
        <td class="num">${r.total_trades ?? 0}</td>
        <td class="num">${r.profit_factor == null ? "—" : r.profit_factor.toFixed(2)}</td>
        <td class="num">${sgn(r.max_drawdown)}</td>
        <td class="num">${(r.win_rate ?? 0).toFixed(0)}</td>
        <td class="num faint">${r.sharpe == null ? "—" : r.sharpe.toFixed(1)}</td>
      </tr>`), "No combinations.");
    host.querySelectorAll("[data-row]").forEach((tr) => {
      tr.onclick = () => loadDetail(j.id, Number(tr.dataset.row));
    });
    return;
  }

  // logs
  const lines = (detail && detail.logs) || [];
  const out = (detail && detail.stdout) || "";
  host.innerHTML = (lines.length || out)
    ? `<pre class="err" style="max-height:300px">${esc(
        lines.join("\n") + (out ? "\n--- print() output ---\n" + out : ""))}</pre>`
    : `<div class="faint" style="padding:10px 0">Nothing logged. Call
       <code>ctx.log("…")</code> in your code to see it here.</div>`;
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
      <button class="btn primary sm" id="btAdd">Add as a strategy</button>
      <button class="btn sm" id="btBank">Bank this as a risk result</button>
      <button class="btn sm" id="btCopy">Copy parameters</button>
    </div>
    <div class="tip">${hasParams
      ? `The selected row's parameters — <code>${
          esc(Object.entries(params).map(([k, v]) => `${k}=${v}`).join(", "))}</code> —
         travel with whichever you pick.`
      : `This run used the current settings with nothing swept.`}
      <br><b>Adding a strategy does not arm anything.</b> It saves the settings
      under a name; you still choose which ticker runs it, and every ticker
      starts disarmed.</div>`;

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
    + `total P/L ${fmt(s && s.total_pl)}, ${s ? s.total_trades : 0} trades, `
    + `profit factor ${s && s.profit_factor != null ? s.profit_factor.toFixed(2) : "n/a"}, `
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
      target: { points: Number(params.take_profit ?? 0.10) },
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
  toast(`Banked against <b>${esc(prof.name)}</b>. See the Risk page.`, "ok", 9000);
}

const fmt = (v) => v == null ? "n/a"
  : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
