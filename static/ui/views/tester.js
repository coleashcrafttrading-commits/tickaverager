/* ============================================================================
   Strategy tester -- put a strategy on a chart and see what it did.

   Pick a symbol, pick one of the study's winners (or any saved code), run it,
   and the trades are drawn on the candles with the equity curve underneath.
   The point is to SEE the thing rather than read a summary of it: a strategy
   with a good profit factor and three trades doing all the work looks
   completely different on a chart than it does in a table.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, act, toast, el, esc, card, stat, tableHTML,
  money, money0, sgn, pct, px, go,
} from "../core.js";
import { ChartPanel } from "../chartpanel.js";
import { EqChart } from "../eqchart.js";

let panel = null;
let eq = null;
let report = null;
let codes = [];
let running = false;

const TFS = ["1Min", "5Min", "15Min", "30Min", "1Hour", "1Day"];

VIEWS.tester = {
  title: () => "Strategy tester",
  sub: () => "run a strategy on any symbol and see it on the chart",

  async mount() {
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${card("Chart", `<div id="tsChart"></div>`, `<span class="faint" id="tsMeta"></span>`)}
          ${card("Equity curve", `<div id="tsEq"></div>`,
            `<span class="faint" id="tsEqNote"></span>`)}
          ${card("Trades", `<div id="tsTrades"></div>`, "", { flush: true })}
        </div>
        <div>
          ${card("Run", `
            <label class="f"><span>Symbol</span>
              <input id="tsSym" value="SPY" placeholder="any US equity or ETF"></label>
            <label class="f"><span>Strategy</span>
              <select id="tsCode"></select></label>
            <label class="f"><span>Bars</span>
              <select id="tsTf">${TFS.map((t) =>
                `<option value="${t}"${t === "15Min" ? " selected" : ""}>${t}</option>`
              ).join("")}</select></label>
            <label class="f"><span>Days of history</span>
              <input id="tsDays" type="number" value="250" min="5" max="900" step="5"></label>
            <div class="hint">The study ran these on <b>15-minute bars over 250
              days</b>. Other settings are yours to explore, but the reported
              numbers only describe that one.</div>
            <div class="row-btns">
              <button class="btn primary" id="tsRun">Run it</button>
            </div>
            <div id="tsStatus" class="tip"></div>`)}
          ${card("Result", `<div id="tsStats"></div>`)}
          ${card("Against buy and hold", `<div id="tsBh"></div>`)}
        </div>
      </div>`;

    panel = new ChartPanel(el("tsChart"), { key: "tester", symbol: "SPY" });
    eq = new EqChart(el("tsEq"), { height: 200 });

    try {
      const r = await GET("/api/code");
      codes = r.files || [];
    } catch (e) { codes = []; }
    // the study's winners first, then everything else
    codes.sort((a, b) => {
      const A = String(a.slug || a).startsWith("study-") ? 0 : 1;
      const B = String(b.slug || b).startsWith("study-") ? 0 : 1;
      return A - B || String(a.slug || a).localeCompare(String(b.slug || b));
    });
    el("tsCode").innerHTML = codes.length
      ? codes.map((c) => {
          const s = c.slug || c;
          const star = String(s).startsWith("study-") ? "★ " : "";
          return `<option value="${esc(s)}">${star}${esc(s)}</option>`;
        }).join("")
      : `<option value="">no saved strategies</option>`;

    el("tsRun").onclick = run;
    el("tsSym").onkeydown = (e) => { if (e.key === "Enter") run(); };
    run();
  },

  paint() { /* nothing polls here -- a test is a deliberate act */ },
};

async function run() {
  if (running) return;
  const sym = (el("tsSym").value || "").trim().toUpperCase();
  const slug = el("tsCode").value;
  const tf = el("tsTf").value;
  const days = Number(el("tsDays").value) || 250;
  if (!sym || !slug) { toast("Pick a symbol and a strategy.", "err"); return; }

  running = true;
  el("tsRun").disabled = true;
  el("tsStatus").innerHTML = `Running <b>${esc(slug)}</b> on ${esc(sym)}…`;
  try {
    const r = await POST("/api/backtest", {
      symbol: sym, timeframe: tf, days, code_slug: slug, mode: "code",
      label: `${slug} on ${sym}`,
    });
    const job = r.job_id || r.id || r.job;
    let rep = r.report;
    if (!rep && job) {
      for (let i = 0; i < 90 && !rep; i++) {
        await new Promise((s) => setTimeout(s, 1000));
        const st = await GET("/api/backtest/" + job);
        const done = st.state === "done" || st.done || st.finished
                     || (st.job && st.job.state === "done");
        if (done) {
          const d = await GET(`/api/backtest/${job}/detail?row=0`);
          rep = d.report;
        } else if (st.state === "error" || (st.job && st.job.state === "error")) {
          throw new Error(st.error || (st.job && st.job.error) || "the run failed");
        }
        el("tsStatus").innerHTML = `Running… ${i + 1}s`;
      }
    }
    if (!rep) throw new Error("no report came back");
    report = rep;
    el("tsStatus").innerHTML = "";
    await draw(sym, tf, days);
  } catch (e) {
    el("tsStatus").innerHTML = `<span class="down">${esc(e.message)}</span>`;
    toast(esc(e.message), "err");
  } finally {
    running = false;
    el("tsRun").disabled = false;
  }
}

async function draw(sym, tf, days) {
  const s = report.summary || {};
  const trades = report.trades || [];

  // TWO markers per trade, in the shape the chart consumes: {t, price, side}
  // keyed on the bar TIMESTAMP. Passing the backtest's own {entry_i, side:
  // "short"} shape drew nothing at all -- every marker was silently dropped
  // because no bar timestamp matched.
  //
  // Direction is expressed as the order actually sent, which is the honest
  // label: opening a short SELLS and closing it BUYS. Exits also carry whether
  // the trade won, so a chart of a short-only strategy does not paint every
  // exit the same colour regardless of outcome.
  const marks = [];
  for (const t of trades) {
    const short = t.side === "short";
    const pnl = (t.exit - t.entry) * t.shares * (short ? -1 : 1);
    marks.push({ t: t.entry_t, price: t.entry, side: short ? "sell" : "buy",
                 kind: "entry" });
    marks.push({ t: t.exit_t, price: t.exit, side: short ? "buy" : "sell",
                 kind: "exit", win: pnl >= 0 });
  }
  // THE CHART MUST BE ON THE SAME BARS THE BACKTEST RAN ON.
  // A trade's entry_i is an index into the bar array the run used. Drawing
  // those indices on a chart of a DIFFERENT timeframe puts every marker in
  // the wrong place, which is worse than showing none -- the panel remembers
  // its own timeframe between visits, so this has to be forced, persisted and
  // reflected in its toolbar rather than merely assigned.
  panel.symbol = sym;
  panel.tf = tf;
  panel.days = days;
  // enough candles to carry every trade the run produced
  panel.limit = Math.max(2000, ((report.summary || {}).bars || 0) + 50);
  if (panel._persist) panel._persist();
  if (panel._syncTfs) panel._syncTfs();
  await panel.load();

  // and if the bars still do not line up, refuse to draw rather than mislead
  const n = (panel.bars || []).length;
  const need = (report.summary || {}).bars || 0;
  if (need && n && Math.abs(n - need) > Math.max(5, need * 0.02)) {
    panel.setTrades([]);
    el("tsMeta").innerHTML =
      `<span class="warn">markers hidden — the chart has ${n.toLocaleString()} `
      + `bars but the run used ${need.toLocaleString()}</span>`;
  } else {
    panel.setTrades(marks);
    el("tsMeta").textContent =
      `${trades.length.toLocaleString()} trades on ${n.toLocaleString()} bars`;
  }

  eq.setData(report);
  el("tsEqNote").textContent = `peak-to-trough ${money(s.max_drawdown)}`;

  el("tsStats").innerHTML =
    stat("Total P/L", sgn(s.total_pl), `${s.total_trades || 0} trades`)
    + stat("Win rate", pct(s.win_rate, 1), `${s.winning_trades || 0} winners`)
    + stat("Profit factor", (s.profit_factor ?? "—"), "gross profit ÷ loss")
    + stat("Expectancy", sgn(s.expectancy, 2), "per trade")
    + stat("Max drawdown", money(s.max_drawdown), "worst peak-to-trough")
    + stat("Exposure", pct(s.exposure_pct, 1), "of all bars")
    + stat("Avg lots open", (s.avg_open_when_in ?? "—"), `max ${s.max_open ?? "—"}`)
    + stat("Peak capital", money0(s.peak_capital), "most committed at once");

  const bh = s.buy_hold_pct;
  const twin = s.twin_dollars, edge = s.edge_vs_twin;
  el("tsBh").innerHTML = tableHTML(["", "Value"], [
    [`This strategy`, `<b class="${(s.total_pl || 0) >= 0 ? "up" : "down"}">${sgn(s.total_pl)}</b>`],
    [`Buy and hold, same window`, `<span class="${(bh || 0) >= 0 ? "up" : "down"}">${pct(bh, 2)}</span>`],
    [`Passive twin (matched, signed)`, `<span class="${(twin || 0) >= 0 ? "up" : "down"}">${sgn(twin)}</span>`],
    [`Edge over the twin`, `<b class="${(edge || 0) >= 0 ? "up" : "down"}">${sgn(edge)}</b>`],
    [`Tilt`, `${s.tilt ?? "—"} <span class="faint">+1 all long, −1 all short</span>`],
  ]);

  const rows = trades.slice(-80).reverse().map((t) => {
    const pnl = (t.exit - t.entry) * t.shares * (t.side === "short" ? -1 : 1);
    return `<tr>
      <td class="faint">${esc(String(t.entry_t || "").slice(5, 16).replace("T", " "))}</td>
      <td><span class="pill ${t.side === "short" ? "down" : "up"}">${esc(t.side)}</span></td>
      <td class="num">${t.shares}</td>
      <td class="num">${px(t.entry, 2)}</td>
      <td class="num">${px(t.exit, 2)}</td>
      <td class="num ${pnl >= 0 ? "up" : "down"}">${sgn(pnl, 2)}</td>
      <td class="faint">${esc(t.why || "")}</td></tr>`;
  });
  el("tsTrades").innerHTML = tableHTML(
    ["Entered", "Side", "Shares", "In", "Out", "P/L", "Why"], rows,
    "No trades on this symbol and window.");
}
