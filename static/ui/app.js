/* ============================================================================
   app.js -- bootstrap, rail, topbar and the poll loop.

   Multi-account: every scoped request goes to /api/a/<S.account>/... (the
   prefix is applied in core.js), the hash carries the account, and the rail
   lists every account above the current one's fleet.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, el, esc, money, money0, sgn, qty, go, readHash, sig, hashFor,
  setAccount, rememberAccount, loadAccounts, pickAccount, savedAccount,
  curAccount, acctLabel, initTheme, toast,
} from "./core.js";

import "./views/overview.js";
import "./views/ticker.js";
import "./views/backtest.js";
import "./views/performance.js";
import "./views/tester.js";
import "./views/research.js";
import "./views/scanner.js";
import "./views/strategies.js";
import "./views/risk.js";
import "./views/agents.js";
import "./views/add.js";
import "./views/settings.js";
import "./views/addaccount.js";

const labelOf = (id) => {
  const a = S.accounts.find((x) => x.id === id);
  return a ? (a.label || a.id) : id;
};

/* ------------------------------------------------------------------ rail */
function accountRows() {
  const rows = S.accounts.map((a) => {
    // green armed · grey idle · red frozen · hollow when the keys do not answer
    const dot = a.connected === false ? "hollow" : a.frozen ? "frozen"
      : a.armed ? "run" : "";
    return `<div class="nav-item tick-item acct-item ${a.id === S.account ? "on" : ""}"
                 data-go="overview" data-acct="${esc(a.id)}"
                 title="${esc(a.account_number || a.id)}">
      <div class="tick-top"><span class="dot ${dot}"></span>
        <span class="tick-sym">${esc(a.label || a.id)}</span>
        <span class="tick-pl num">${a.connected === false
          ? `<span class="faint">no answer</span>` : money(a.equity)}</span></div>
      <div class="tick-sub">${a.running || 0} running · ${a.armed || 0} armed${
        a.frozen ? ` · <span class="down">frozen</span>` : ""}</div>
    </div>`;
  }).join("");
  return rows || `<div class="nav-item faint" style="cursor:default">No accounts yet</div>`;
}

function paintRail() {
  const ov = S.ov;
  const cur = S.view;
  const acct = curAccount();

  if (ov) {
    const armed = ov.totals.armed;
    const dotCls = ov.frozen ? "off" : armed ? "live" : ov.totals.running ? "" : "off";
    el("brandDot").className = "brand-dot " + dotCls;
  } else el("brandDot").className = "brand-dot off";

  // the brand line names the CURRENT account, so the rail and every modal
  // agree about whose fleet is on screen
  const paper = acct && acct.paper !== undefined ? acct.paper : (ov ? ov.paper : true);
  const number = acct ? (acct.account_number || acct.number || "")
    : (ov && ov.account ? (ov.account.account_number || ov.account.number || "") : "");
  el("brandSub").innerHTML = acct || ov
    ? `${acct ? esc(acct.label || acct.id) + " · " : ""}${esc(number || "—")} · ${
        paper ? "Paper" : `<span class="down">LIVE</span>`}`
    : (S.accountsKnown && !S.accounts.length ? "no accounts yet" : "connecting…");

  const item = (kind, label, ico, tail = "") => `
    <div class="nav-item ${cur.kind === kind ? "on" : ""}" data-go="${kind}">
      <span class="ico">${ico}</span><span>${label}</span>
      ${tail ? `<span class="tail faint">${tail}</span>` : ""}</div>`;

  let html = "";
  if (S.accountsKnown || S.accounts.length) {
    html += `<div class="nav-label">Accounts <span class="tail">${S.accounts.length}</span></div>`
      + accountRows()
      + `<div class="nav-item ${cur.kind === "addaccount" ? "on" : ""}" data-go="addaccount">
           <span class="ico">＋</span><span style="color:var(--accent)">Add account</span></div>`;
  }

  if (ov) {
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
        <div class="tick-sub">${t.lot_count}/${t.max_lots} lots · ${qty(t.shares)} sh${
          t.dry_run ? "" : ` · <span class="down">armed</span>`}</div>
      </div>`;
    }).join("") || `<div class="nav-item faint" style="cursor:default">No tickers</div>`;

    html += item("overview", "Portfolio", "▦")
      + `<div class="nav-label">Tickers <span class="tail">${ov.totals.count}</span></div>`
      + tickers
      + `<div class="nav-item" data-go="add"><span class="ico">＋</span>
          <span style="color:var(--accent)">Add a ticker</span></div>`
      + `<div class="nav-label">This account</div>`
      + item("performance", "Performance", "◧")
      + item("risk", "Risk", "◎")
      + item("agents", "Agents", "◈")
      + item("settings", "Settings", "⚙");
  } else if (S.account) {
    html += `<div class="nav-item faint" style="cursor:default">Loading ${esc(acctLabel())}…</div>`;
  }

  html += `<div class="nav-label">Shared library</div>`
    + item("strategies", "Strategies", "◇")
    + item("research", "Research", "◭")
    + item("scanner", "Scanner", "◉");
  el("nav").innerHTML = html;

  if (ov) {
    const age = ov.snap_age;
    el("railFoot").innerHTML = ov.snap_error
      ? `<span class="down">data error</span>`
      : `<span class="${age > 20 ? "warn" : "up"}">●</span>
         <span>${age == null ? "—" : age + "s"} · ${esc(ov.session)}</span>`;
  } else if (!S.pollFails) el("railFoot").innerHTML = `<span class="faint">—</span>`;
}

/* ----------------------------------------------------------------- shell */
/* The left column collapses on every screen size. On a desktop it is a
   real column that comes and goes and the choice is remembered per browser
   (ta-rail, default open). Under 900 px it is an off-canvas drawer: shut by
   default, opened from the menu button or a swipe in from the left edge,
   closed by a tap outside, a swipe left, Escape, or choosing anything in it.
   The shell's extra pieces -- the menu button, the status beside the title,
   the scrim and the drawer's close button -- are created here rather than in
   index.html, so the markup the server hands out is unchanged. */
const LS_RAIL = "ta-rail";
const NARROW = window.matchMedia ? window.matchMedia("(max-width: 900px)") : null;
const isNarrow = () => !!(NARROW && NARROW.matches);
const railSaved = () => {
  try { return localStorage.getItem(LS_RAIL) !== "0"; } catch (e) { return true; }
};
const railRemember = (open) => {
  try { localStorage.setItem(LS_RAIL, open ? "1" : "0"); } catch (e) { /* private mode */ }
};
const appEl = () => document.querySelector(".app");

export function railOpen() {
  const a = appEl();
  if (!a) return true;
  return isNarrow() ? a.classList.contains("drawer-open")
                    : !a.classList.contains("rail-collapsed");
}

export function setRail(open, { remember = true } = {}) {
  const a = appEl();
  if (!a) return;
  if (isNarrow()) {
    a.classList.remove("rail-collapsed");     // a stale desktop choice must not hide the drawer
    a.classList.toggle("drawer-open", !!open);
  } else {
    a.classList.remove("drawer-open");
    a.classList.toggle("rail-collapsed", !open);
    if (remember) railRemember(!!open);
  }
  const b = el("menuBtn");
  if (b) {
    b.setAttribute("aria-expanded", open ? "true" : "false");
    b.title = open ? "Hide the sidebar" : "Show the sidebar";
  }
}
const toggleRail = () => setRail(!railOpen());
/* only the drawer closes on its own; a desktop column stays where it was put */
const closeDrawer = () => { if (isNarrow() && railOpen()) setRail(false); };

/* a desktop remembers its choice; a phone always starts with the drawer shut */
function applyRailMode() {
  if (isNarrow()) setRail(false);
  else setRail(railSaved(), { remember: false });
}

function buildShell() {
  const top = document.querySelector(".topbar");
  const rail = document.querySelector(".rail");
  const a = appEl();
  if (!top || !rail || !a || el("menuBtn")) return;
  const titleWrap = top.firstElementChild;
  if (titleWrap) titleWrap.classList.add("title-wrap");

  const btn = document.createElement("button");
  btn.id = "menuBtn"; btn.className = "menu-btn"; btn.type = "button";
  btn.setAttribute("aria-label", "Toggle the sidebar");
  btn.setAttribute("aria-controls", "rail");
  btn.innerHTML = `<svg viewBox="0 0 20 20" aria-hidden="true" fill="none"
    stroke="currentColor" stroke-width="2" stroke-linecap="round">
    <path d="M3 5h14M3 10h14M3 15h14"/></svg>`;
  top.insertBefore(btn, top.firstChild);

  // running / halted beside the title; today's realized P/L joins it on a
  // phone, where the KPI strip has moved to its own row
  const status = document.createElement("div");
  status.className = "top-status"; status.id = "topStatus";
  status.innerHTML = `<span class="pill" id="topPill" hidden></span>
    <span class="top-pl num" id="topPl"></span>`;
  const spacer = top.querySelector(".spacer");
  top.insertBefore(status, spacer || el("kpis"));

  rail.id = "rail";
  const close = document.createElement("button");
  close.className = "rail-close"; close.type = "button";
  close.setAttribute("aria-label", "Close the menu");
  close.textContent = "×";
  (rail.querySelector(".brand") || rail).appendChild(close);

  const scrim = document.createElement("div");
  scrim.className = "scrim"; scrim.id = "scrim";
  a.appendChild(scrim);

  btn.onclick = toggleRail;
  close.onclick = closeDrawer;
  scrim.onclick = closeDrawer;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  // swipe left on the open drawer shuts it; swipe in from the left edge opens it
  let t0 = null;
  document.addEventListener("touchstart", (e) => {
    t0 = null;
    if (!isNarrow() || e.touches.length !== 1) return;
    const t = e.touches[0];
    const open = railOpen();
    if (open && !rail.contains(e.target) && e.target !== scrim) return;
    if (!open && t.clientX > 28) return;          // only from the edge
    t0 = { x: t.clientX, y: t.clientY, open };
  }, { passive: true });
  document.addEventListener("touchend", (e) => {
    if (!t0) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - t0.x, dy = t.clientY - t0.y;
    if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.4) {
      if (dx < 0 && t0.open) setRail(false);
      else if (dx > 0 && !t0.open) setRail(true);
    }
    t0 = null;
  }, { passive: true });
  document.addEventListener("touchcancel", () => { t0 = null; }, { passive: true });

  if (NARROW) {
    if (NARROW.addEventListener) NARROW.addEventListener("change", applyRailMode);
    else if (NARROW.addListener) NARROW.addListener(applyRailMode);
  }
  applyRailMode();
}
window.__setRail = setRail;

/* the pill beside the title: the ticker's own state on a ticker page, the
   fleet's on every other page */
function paintStatus(ov, v) {
  const pill = el("topPill"), pl = el("topPl");
  if (!pill || !pl) return;
  if (!ov) { pill.hidden = true; pl.innerHTML = ""; return; }
  let cls = "", txt = "";
  if (v.kind === "ticker") {
    const t = (ov.tickers || []).find((x) => x.symbol === v.sym);
    if (t) {
      if (t.halted) { cls = "warn"; txt = "halted"; }
      else if (t.running && !t.dry_run) { cls = "down"; txt = "armed"; }
      else if (t.running) { cls = "up"; txt = "running"; }
      else txt = "stopped";
    }
  } else {
    const T = ov.totals || {};
    if (ov.frozen) { cls = "down"; txt = "frozen"; }
    else if (T.halted) { cls = "warn"; txt = `${T.halted} halted`; }
    else if (T.running) { cls = T.armed ? "down" : "up"; txt = `${T.running} running`; }
    else txt = "idle";
  }
  pill.hidden = !txt;
  pill.className = "pill " + cls;
  pill.textContent = txt;
  const p = ov.portfolio || {};
  pl.innerHTML = `<span class="faint">today</span>${sgn(p.realized_today)}`;
}

/* ---------------------------------------------------------------- topbar */
function paintTop() {
  const ov = S.ov, v = S.view;
  const view = VIEWS[v.kind];
  const label = (S.account && v.kind !== "addaccount") ? acctLabel() : "";
  el("title").innerHTML = esc(view ? view.title(ov, v) : "…")
    + (label ? ` <span class="title-acct">${esc(label)}</span>` : "");
  el("subtitle").textContent = view && view.sub ? view.sub(ov, v) : "";
  paintStatus(ov, v);

  const tabs = view && view.tabs;
  const bar = el("tabs");
  if (tabs) {
    bar.style.display = "flex";
    bar.innerHTML = tabs.map(([k, l]) => `
      <div class="tab ${(v.tab || tabs[0][0]) === k ? "on" : ""}"
           data-go="${v.kind}" ${v.sym ? `data-sym="${v.sym}"` : ""}
           data-tab="${k}">${l}</div>`).join("");
  } else bar.style.display = "none";

  if (!ov) { el("kpis").innerHTML = ""; return; }
  const p = ov.portfolio;
  /* Two numbers, each the whole truth: realized AND unrealized together.
     Today is the account against yesterday's close, all time against the
     equity it opened with. The split sits underneath as the sub-line, so a
     number on this strip can never disagree with the one beside it. */
  const todayPl = p.today_pl != null ? p.today_pl : p.made_today;
  const totalPl = p.total_pl;
  const uToday = p.unrealized_today != null ? p.unrealized_today : p.open_today;
  const uTotal = p.unrealized_total != null ? p.unrealized_total : p.open_pl;
  el("kpis").innerHTML = `
    <div><div class="kpi-k">Account value</div><div class="kpi-v num">${money(p.account_value)}</div></div>
    <div><div class="kpi-k">P/L today</div><div class="kpi-v num">${sgn(todayPl)}</div>
         <div class="kpi-sub num">${sgn(p.realized_today)} booked · ${sgn(uToday)} open</div></div>
    <div><div class="kpi-k">P/L all time</div><div class="kpi-v num">${sgn(totalPl)}</div>
         <div class="kpi-sub num">${sgn(p.realized_total)} booked · ${sgn(uTotal)} open</div></div>`;
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
function reconnecting() {
  S.pollFails = (S.pollFails || 0) + 1;
  el("railFoot").innerHTML =
    `<span class="warn">●</span> <span>reconnecting… (${S.pollFails})</span>`;
  clearTimeout(S.timer);
  S.timer = setTimeout(tick, Math.min(5000, 700 * S.pollFails));
}

/* The account in the hash answered 404: it was removed. Fall back to the
   default (or whatever is left) and say so, rather than sitting on
   "reconnecting" for an account that will never come back. */
async function accountGone(gone) {
  S.gone = (S.gone || 0) + 1;
  try { await loadAccounts(); } catch (e) { /* use what we have */ }
  S.accounts = S.accounts.filter((a) => a.id !== gone);
  const id = pickAccount();
  if (S.gone > 3 || id === gone) { reconnecting(); return; }   // the fallback fails too
  toast(`Account <b>${esc(labelOf(gone) || gone)}</b> no longer exists — showing ${
    id ? `<b>${esc(labelOf(id))}</b>` : "the Add-account page"} instead.`, "", 10000);
  if (!id) {
    setAccount("");
    go({ kind: "addaccount", account: "" });
    clearTimeout(S.timer);
    S.timer = setTimeout(tick, 4000);
    return;
  }
  go({ kind: "overview", account: id });     // go() re-polls on a switch
}

async function tick() {
  clearTimeout(S.timer);

  // No account to poll: none is configured yet, so watch for one to appear.
  // (If GET /api/accounts never answered the plain path still means the
  // default account, and polling it below is right.)
  if (!S.account && S.accountsKnown) {
    try { await loadAccounts(); } catch (e) { /* keep waiting */ }
    if (S.accounts.length) { go({ kind: "overview", account: pickAccount() }); return; }
    render(false);
    S.timer = setTimeout(tick, 4000);
    return;
  }

  const acct = S.account;
  let ov;
  try { ov = await GET("/api/overview"); }
  catch (e) {
    if (acct !== S.account) return;            // switched while waiting
    if (e.status === 404 && acct) { await accountGone(acct); return; }
    // The server is restarting. Returning here without re-arming the timer is
    // what left the page stuck on "connecting..." for ever after every
    // restart -- one failed poll killed the loop and nothing ever retried.
    reconnecting();
    return;
  }
  if (acct !== S.account) return;              // a stale answer for the old account
  S.pollFails = 0;
  S.gone = 0;
  rememberAccount();                           // it answered, so it is real

  // the overview carries the account list, so the rail refreshes for free
  if (Array.isArray(ov.accounts)) {
    S.accounts = ov.accounts;
    S.accountsKnown = true;
    if (!S.defaultAccount) {
      S.defaultAccount = (S.accounts.find((a) => a.is_default) || {}).id || "";
    }
  }
  if (!S.account && ov.account && ov.account.id) {
    // booted without an account list (the server was down); the overview
    // knows which account it is, so adopt it and put it in the hash
    setAccount(ov.account.id);
    S.view = { ...S.view, account: ov.account.id };
    history.replaceState(null, "", hashFor(S.view));
  }
  S.ov = ov;

  if (S.view.kind === "ticker") {
    if (!S.ov.tickers.some((t) => t.symbol === S.view.sym)) {
      toast(`${S.view.sym} is no longer in the fleet.`, "err");
      go({ kind: "overview" });
      S.timer = setTimeout(tick, 1000);     // go() does not re-poll on the same account
      return;
    }
    try {
      const tk = await GET("/api/ticker/" + S.view.sym);
      if (acct === S.account && S.view.kind === "ticker") S.ticker = tk;
    } catch (e) { /* keep last */ }
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
  const v = kind === "ticker"
    ? { kind: "ticker", sym: g.dataset.sym, tab: g.dataset.tab || "live" }
    : { kind, tab: g.dataset.tab || "" };
  if (g.dataset.acct !== undefined) v.account = g.dataset.acct;   // an account row
  go(v);
  closeDrawer();                       // picking anything in the drawer shuts it
});

window.addEventListener("hashchange", () => {
  const v = readHash();
  // a hash with no account means the one in use (which is what localStorage
  // holds), else the default -- and the URL is rewritten to say so
  if (v.account === undefined) v.account = S.accountsKnown ? pickAccount(S.account) : S.account;
  const h = hashFor(v);
  if (location.hash !== h) history.replaceState(null, "", h);
  if (sig(v) !== sig(S.view)) {
    if (v.account !== S.account) setAccount(v.account);
    S.view = v; S.ticker = null; render(true); tick();
  }
});

// Load AI-written indicators before the first chart draws, so they are in the
// picker everywhere rather than only after visiting the builder.
(async () => {
  try {
    const r = await GET("/api/indicators/custom");
    const m = await import("./views/research.js");
    if (m.installAll) m.installAll(r.indicators || []);
  } catch (e) { /* the dashboard works without them */ }
})();

/* ------------------------------------------------------------------ boot */
(async function boot() {
  initTheme();
  buildShell();
  let ok = true;
  try { await loadAccounts(); } catch (e) { ok = false; }
  const v = readHash();
  if (ok && !S.accounts.length) {
    // nothing to trade with yet: the only useful page is the one that adds one
    v.account = "";
    if (v.kind !== "addaccount") { v.kind = "addaccount"; v.tab = ""; delete v.sym; }
  } else if (ok) {
    const id = pickAccount(v.account);
    if (v.account && v.account !== id) {
      toast(`Account <b>${esc(v.account)}</b> no longer exists — showing
             <b>${esc(labelOf(id))}</b> instead.`, "", 10000);
    }
    v.account = id;
  } else if (v.account === undefined) {
    // the server did not answer; keep the last account so the poll goes to
    // the right place once it is back
    v.account = savedAccount();
  }
  setAccount(v.account);
  S.view = v;
  history.replaceState(null, "", hashFor(v));
  render(true);
  tick();
})();
