/* ============================================================================
   Backtest -- run the ladder over any ticker, timeframe and window, sweeping
   any parameters, and rank the results.

   Ranking defaults to TOTAL P/L rather than realized: realized alone rewards a
   setting that banks winners while quietly accumulating lots it never closes.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, act, toast, el, esc, card, stat, tableHTML,
  money, money0, sgn, pct, go,
} from "../core.js";
import { Chart } from "../chart.js";

let poll = null;
let sortKey = "total_pl";

const PRESETS = {
  "Take profit": { take_profit: "0.05,0.10,0.20,0.30,0.40,0.50" },
  "Add distance": { add_distance: "0.05,0.10,0.15,0.20,0.30,0.40" },
  "TP × add": { take_profit: "0.10,0.20,0.30,0.40", add_distance: "0.10,0.20,0.30,0.40" },
  "Exit style": { exit_mode: "limit,trail", trail_amount: "0.03,0.05,0.10" },
  "Lot size": { shares_per_lot: "25,50,100,200" },
};

function parseSweep(txt) {
  /* "take_profit=0.1,0.2\nadd_distance=0.1" -> {take_profit:[.1,.2], ...} */
  const out = {};
  for (const line of String(txt || "").split(/[\n;]/)) {
    const t = line.trim();
    if (!t || !t.includes("=")) continue;
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

function combos(sw) {
  return Object.values(sw).reduce((a, v) => a * (v.length || 1), 1);
}

VIEWS.backtest = {
  title: () => "Backtest",
  sub: () => "replay any strategy over real Alpaca history",

  mount() {
    const syms = (S.ov?.tickers || []).map((t) => t.symbol);
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${card("Run", `
            <div class="f2">
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
                <input id="btDays" type="number" value="30" min="1" max="1000"></label>
              <label class="f"><span>Label</span>
                <input id="btLabel" placeholder="optional"></label>
            </div>
            <label class="f"><span>Sweep — one <code>param=a,b,c</code> per line</span>
              <textarea id="btSweep" rows="4"
                placeholder="take_profit=0.05,0.10,0.20,0.40&#10;add_distance=0.10,0.20"></textarea></label>
            <div class="hint" id="btCount">Leave empty to test the ticker's current
              settings once.</div>
            <div class="row-btns" style="margin-bottom:12px">
              ${Object.keys(PRESETS).map((p) =>
                `<button class="btn sm preset" data-p="${esc(p)}">${p}</button>`).join("")}
            </div>
            <button class="btn primary" id="btRun" style="width:100%">Run backtest</button>
            <div class="tip">Bars are cached per symbol and window, so a sweep pulls
              the tape once and replays it. Ranking is on <b>total P/L</b> — realized
              alone rewards a setting that banks winners while accumulating lots it
              never closes.</div>`)}
          <div id="btResult"></div>
        </div>
        <div>
          ${card("Recent runs", `<div id="btJobs"></div>`, "", { flush: true })}
          ${card("Reading the numbers", `<div class="tip" style="margin-top:0">
            <b>Total P/L</b> includes open inventory marked to market at the end.<br>
            <b>Ret/cap</b> is return on the peak capital the strategy actually
            deployed — the fair way to compare settings that risk different amounts.<br>
            <b>$/$1 DD</b> is profit per dollar of worst drawdown.<br>
            <b>Fill%</b> — a passive entry peg backtests as fewer, better trades and
            behaves in reality as an idle ladder.<br>
            <b>Max lots</b> — if this equals your cap, the cap bound the result and
            you are comparing caps, not the parameter you swept.</div>`)}
        </div>
      </div>`;

    el("view").querySelectorAll(".preset").forEach((b) => {
      b.onclick = () => {
        const p = PRESETS[b.dataset.p];
        el("btSweep").value = Object.entries(p)
          .map(([k, v]) => `${k}=${v}`).join("\n");
        updateCount();
      };
    });
    el("btSweep").addEventListener("input", updateCount);
    el("btRun").onclick = () => act(runIt);
    loadJobs();
  },

  paint() { /* results are pushed by the poller */ },
};

function updateCount() {
  const sw = parseSweep(el("btSweep").value);
  const n = combos(sw);
  const keys = Object.keys(sw);
  el("btCount").innerHTML = keys.length
    ? `<b>${n.toLocaleString()}</b> combination${n === 1 ? "" : "s"} across
       ${keys.length} parameter${keys.length === 1 ? "" : "s"}${n > 400
       ? ` — <span class="warn">this will take a while</span>` : ""}`
    : "Leave empty to test the ticker's current settings once.";
}

async function runIt() {
  const spec = {
    symbol: el("btSym").value.trim().toUpperCase(),
    timeframe: el("btTf").value,
    days: Number(el("btDays").value) || 30,
    label: el("btLabel").value.trim(),
    sweep: parseSweep(el("btSweep").value),
  };
  if (!spec.symbol) { toast("A symbol is required.", "err"); return; }
  el("btResult").innerHTML = `<div class="card"><div class="card-b">
    <div class="skel" style="width:40%"></div>
    <div class="skel" style="width:70%;margin-top:10px"></div></div></div>`;
  const j = await POST("/api/backtest", spec);
  toast(`Backtest queued — ${j.total} combination(s).`, "ok");
  watch(j.id);
  loadJobs();
}

function watch(id) {
  clearInterval(poll);
  const tick = async () => {
    let j;
    try { j = await GET(`/api/backtest/${id}`); }
    catch (e) { clearInterval(poll); return; }
    renderJob(j);
    if (j.state === "done" || j.state === "error") {
      clearInterval(poll);
      loadJobs();
      if (j.state === "done") toast(`Backtest finished in ${j.seconds}s.`, "ok");
      if (j.state === "error") toast(esc(j.error), "err", 9000);
    }
  };
  tick();
  poll = setInterval(tick, 1200);
}

function renderJob(j) {
  const host = el("btResult");
  if (!host) return;
  if (j.state === "error") {
    host.innerHTML = `<div class="note bad"><b>Backtest failed</b> — ${esc(j.error)}</div>`;
    return;
  }
  const running = j.state !== "done";
  const rows = (j.results || []).slice().sort((a, b) =>
    (b[sortKey] ?? 0) - (a[sortKey] ?? 0));
  const pKeys = [...new Set(rows.flatMap((r) => Object.keys(r.params || {})))];

  const head = [...pKeys, "Total P/L", "Realized", "Open", "Trades", "$/trade",
                "Peak cap", "Max DD", "Ret/cap", "$/$1 DD", "Fill%", "Max lots"];
  const body = rows.slice(0, 200).map((r, i) => `<tr${i === 0 && !running
      ? ' style="background:rgba(53,201,139,.07)"' : ""}>
    ${pKeys.map((k) => `<td style="text-align:left"><b>${esc(r.params[k])}</b></td>`).join("")}
    <td class="num">${sgn(r.total_pl, 0)}</td>
    <td class="num">${sgn(r.realized, 0)}</td>
    <td class="num">${sgn(r.open_pl, 0)}</td>
    <td class="num">${r.closed}</td>
    <td class="num">$${Number(r.avg_per_trade).toFixed(2)}</td>
    <td class="num">${money0(r.peak_capital)}</td>
    <td class="num down">${money0(r.max_dd)}</td>
    <td class="num">${Number(r.roc).toFixed(1)}%</td>
    <td class="num">${Number(r.per_dd).toFixed(2)}</td>
    <td class="num faint">${Number(r.fill_rate).toFixed(0)}</td>
    <td class="num faint">${r.max_lots}</td></tr>`);

  const pctDone = j.total ? Math.round(100 * j.done / j.total) : 0;
  host.innerHTML = card(
    `${j.symbol} · ${j.timeframe} · ${j.days}d${j.label ? " · " + esc(j.label) : ""}`,
    `<div class="stats" style="margin-bottom:16px">
       ${stat("Window", j.bars ? `${j.bars.toLocaleString()}` : "—",
              j.first ? `$${j.first.toFixed(2)} → $${j.last.toFixed(2)}` : "")}
       ${stat("Drift", j.drift != null ? pct(j.drift, 1) : "—",
              "a rising window flatters everything")}
       ${stat("Combinations", `${j.done}/${j.total}`, running ? `${pctDone}% done` : `${j.seconds}s`)}
       ${rows.length ? stat("Best total P/L", sgn(rows[0].total_pl, 0),
              Object.entries(rows[0].params).map(([k, v]) => `${k}=${v}`).join(" · ") || "current settings") : ""}
     </div>
     ${running ? `<div class="note info">Running… ${j.done} of ${j.total}</div>` : ""}
     ${j.drift != null && Math.abs(j.drift) > 15 ? `<div class="note warn">
        This window drifted <b>${j.drift > 0 ? "+" : ""}${j.drift}%</b>. A long-only
        ladder makes money almost regardless of settings in a strong trend — read
        these as a comparison <i>between</i> settings, not an expected return.</div>` : ""}
     ${tableHTML(head, body, "No results yet.")}`,
    `<span class="faint">sort:</span>
     <select id="btSort" style="width:auto;display:inline-block;padding:3px 7px">
       <option value="total_pl">total P/L</option>
       <option value="roc">return on capital</option>
       <option value="per_dd">profit per drawdown</option>
       <option value="realized">realized</option>
       <option value="closed">trades</option>
     </select>`, { flush: false });

  const sel = el("btSort");
  if (sel) { sel.value = sortKey; sel.onchange = () => { sortKey = sel.value; renderJob(j); }; }
}

async function loadJobs() {
  const host = el("btJobs");
  if (!host) return;
  try {
    const r = await GET("/api/backtest/jobs");
    host.innerHTML = tableHTML(
      ["Run", "State", "Combos", "Best"],
      (r.jobs || []).map((j) => `<tr class="click" data-job="${j.id}">
        <td style="text-align:left"><b>${j.symbol}</b>
          <span class="faint">${j.timeframe} ${j.days}d</span>
          ${j.label ? `<br><span class="faint">${esc(j.label)}</span>` : ""}</td>
        <td><span class="pill ${j.state === "done" ? "up"
          : j.state === "error" ? "down" : "acc"}">${j.state}</span></td>
        <td class="num">${j.done}/${j.total}</td>
        <td class="num">${j.best == null ? "—" : sgn(j.best, 0)}</td></tr>`),
      "No runs yet.");
    host.querySelectorAll("[data-job]").forEach((tr) => {
      tr.onclick = () => watch(tr.dataset.job);
    });
  } catch (e) { host.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}
