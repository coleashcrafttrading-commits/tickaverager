/* ============================================================================
   app.js -- bootstrap, rail, topbar and the poll loop.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, el, esc, money, money0, sgn, go, readHash, sig,
  initTheme, toast,
} from "./core.js";

import "./views/overview.js";
import "./views/ticker.js";
import "./views/backtest.js";
import "./views/performance.js";
import "./views/tester.js";
import "./views/strategies.js";
import "./views/risk.js";
import "./views/agents.js";
import "./views/add.js";
import "./views/settings.js";

/* ------------------------------------------------------------------ rail */
function paintRail() {
  const ov = S.ov;
  if (!ov) return;
  const cur = S.view;

  const armed = ov.totals.armed;
  const dotCls = ov.frozen ? "off" : armed ? "live" : ov.totals.running ? "" : "off";
  el("brandDot").className = "brand-dot " + dotCls;
  el("brandSub").innerHTML =
    `${ov.paper ? "Paper" : `<span class="down">LIVE</span>`} · ${esc(ov.account.number || "—")}`;

  const item = (kind, label, ico, tail = "") => `
    <div class="nav-item ${cur.kind === kind ? "on" : ""}" data-go="${kind}">
      <span class="ico">${ico}</span><span>${label}</span>
      ${tail ? `<span class="tail faint">${tail}</span>` : ""}</div>`;

  const tickers = (ov.tickers || []).map((t) => {
    const on = cur.kind === "ticker" && cur.sym === t.symbol;
    const dot = t.halted ? "halt" : (t.running && !t.dry_run) ? "live"
      : t.running ? "run" : "";
    const pl = (t.unrealized || 0) + (t.realized_today || 0);
    return `<div class="nav-item tick-item ${on ? "on" : ""}"
                 data-go="ticker" data-sym="${t.symbol}">
      <div class="tick-top"><span class="dot ${dot}"></span>
        <span class="tick-sym">${t.symbol}</span>
        <span class="tick-pl">${pl ? sgn(pl, 0) : `<span class="faint">—</span>`}</span></div>
      <div class="tick-sub">${t.lot_count}/${t.max_lots} lots · ${t.shares} sh${
        t.dry_run ? "" : ` · <span class="down">armed</span>`}</div>
    </div>`;
  }).join("") || `<div class="nav-item faint" style="cursor:default">No tickers</div>`;

  el("nav").innerHTML =
    item("overview", "Portfolio", "▦")
    + `<div class="nav-label">Tickers <span class="tail">${ov.totals.count}</span></div>`
    + tickers
    + `<div class="nav-item" data-go="add"><span class="ico">＋</span>
        <span style="color:var(--accent)">Add a ticker</span></div>`
    + `<div class="nav-label">Research</div>`
    + item("performance", "Performance", "◧")
    + item("strategies", "Strategies", "◇")
    + item("tester", "Strategy tester", "◭")
    + item("backtest", "Backtest", "◫")
    + item("risk", "Risk", "◎")
    + `<div class="nav-label">System</div>`
    + item("agents", "Agents", "◈")
    + item("settings", "Settings", "⚙");

  const age = ov.snap_age;
  el("railFoot").innerHTML = ov.snap_error
    ? `<span class="down">data error</span>`
    : `<span class="${age > 20 ? "warn" : "up"}">●</span>
       <span>${age == null ? "—" : age + "s"} · ${esc(ov.session)}</span>`;
}

/* ---------------------------------------------------------------- topbar */
function paintTop() {
  const ov = S.ov, v = S.view;
  const view = VIEWS[v.kind];
  el("title").textContent = view ? view.title(ov, v) : "…";
  el("subtitle").textContent = view && view.sub ? view.sub(ov, v) : "";

  if (!ov) { el("kpis").innerHTML = ""; return; }
  const p = ov.portfolio;
  el("kpis").innerHTML = `
    <div><div class="kpi-k">Account</div><div class="kpi-v num">${money(p.account_value)}</div></div>
    <div><div class="kpi-k">Today</div><div class="kpi-v num">${sgn(p.made_today)}</div></div>
    <div><div class="kpi-k">Open P/L</div><div class="kpi-v num">${sgn(p.open_pl)}</div></div>
    <div><div class="kpi-k">Deployed</div><div class="kpi-v num">${money0(p.deployed)}</div></div>`;

  const tabs = view && view.tabs;
  const bar = el("tabs");
  if (tabs) {
    bar.style.display = "flex";
    bar.innerHTML = tabs.map(([k, l]) => `
      <div class="tab ${(v.tab || tabs[0][0]) === k ? "on" : ""}"
           data-go="${v.kind}" ${v.sym ? `data-sym="${v.sym}"` : ""}
           data-tab="${k}">${l}</div>`).join("");
  } else bar.style.display = "none";
}

/* ---------------------------------------------------------------- render */
function render(force) {
  const s = sig(S.view);
  const view = VIEWS[S.view.kind];
  if (!view) { go({ kind: "overview" }); return; }
  if (force || S.mounted !== s) {
    S.mounted = s;
    S.touched = false;
    el("view").innerHTML = "";
    try { view.mount(S.view); }
    catch (e) {
      console.error(e);
      el("view").innerHTML = `<div class="note bad"><b>View failed to load:</b>
        ${esc(e.message)}<br>The bots are unaffected — this is display code.</div>`;
    }
  }
  paintTop();
  paintRail();
  try { if (view.paint) view.paint(S.view); }
  catch (e) {
    console.error(e);
    el("view").insertAdjacentHTML("afterbegin",
      `<div class="note bad"><b>Render error:</b> ${esc(e.message)}<br>
       <span class="faint">The bots are unaffected — this is display code only.
       Check the console.</span></div>`);
  }
}
window.__render = render;

/* ------------------------------------------------------------------ poll */
async function tick() {
  try { S.ov = await GET("/api/overview"); }
  catch (e) {
    // The server is restarting. Returning here without re-arming the timer is
    // what left the page stuck on "connecting..." for ever after every
    // restart -- one failed poll killed the loop and nothing ever retried.
    S.pollFails = (S.pollFails || 0) + 1;
    el("railFoot").innerHTML =
      `<span class="warn">●</span> <span>reconnecting… (${S.pollFails})</span>`;
    clearTimeout(S.timer);
    S.timer = setTimeout(tick, Math.min(5000, 700 * S.pollFails));
    return;
  }
  S.pollFails = 0;

  if (S.view.kind === "ticker") {
    if (!S.ov.tickers.some((t) => t.symbol === S.view.sym)) {
      toast(`${S.view.sym} is no longer in the fleet.`, "err");
      go({ kind: "overview" });
      return;
    }
    try { S.ticker = await GET("/api/ticker/" + S.view.sym); } catch (e) { /* keep last */ }
  }
  render(false);

  clearTimeout(S.timer);
  const ms = Math.max(600, (S.ov.global && S.ov.global.ui_refresh_ms) || 2000);
  S.timer = setTimeout(tick, ms);
}
window.__tick = tick;

/* --------------------------------------------------------------- events */
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-go]");
  if (!g) return;
  e.preventDefault();
  const kind = g.dataset.go;
  go(kind === "ticker"
    ? { kind: "ticker", sym: g.dataset.sym, tab: g.dataset.tab || "live" }
    : { kind, tab: g.dataset.tab || "" });
});

window.addEventListener("hashchange", () => {
  const v = readHash();
  if (sig(v) !== sig(S.view)) { S.view = v; S.ticker = null; render(true); tick(); }
});

initTheme();
S.view = readHash();
render(true);
tick();
