/* ============================================================================
   app.js -- the shell: rail, header, account switcher, command bar, polls.

   WHAT CHANGED AND WHY (Sep 2026)

   This shell used to be a LADDER shell. Every ticker in the rail rendered as
   "0/100000 lots - 0 sh", which is ladder vocabulary printed on an object
   that is no longer a ladder, and the header's money came from
   fleet.overview().totals, which is ladder-only. A ticker is now a symbol the
   account cares about; strategies hang off it, zero or many, and the ladder
   is one of them. So the rail asks /api/hub/tickers for price, change, what
   is held and which strategies are on it, and the header asks
   /api/hub/portfolio for the account's money across EVERY strategy.

   THE WORD "ARMED". It meant "a ladder engine is armed" here and "the options
   playbook may open" in the options tab, and both were on screen at once. The
   shell now renders hub's ONE vocabulary and invents nothing:
       armed   transmits REAL orders at the broker
       live    switched on and deciding
       idle    attached, not deciding
       halted  stopped itself on a problem
       adopted positions here it did not place
       off     attached to nothing
   The definitions live in core.js STATE_WORDS and reach the screen as the
   tooltip on every chip and pill, so the word can never be read two ways.

   POLL BUDGET. /api/overview stays on the fast loop (~2 s) because views
   depend on S.ov. The hub endpoints call app._perf_positions(), which is on
   Alpaca's 200/min TRADING budget and cached 20 s, so they get their own slow
   loop at exactly that cadence. Polling them faster would take request budget
   away from the ladders and return the same cached answer.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, el, esc, money, money0, sgn, qty, go, readHash, sig, hashFor,
  setAccount, rememberAccount, loadAccounts, pickAccount, savedAccount,
  curAccount, acctLabel, initTheme, toggleTheme, toast,
  mv, mfmt, mnum, measured, mreason, pctf, sparkline, stateChip, STATE_WORDS,
} from "./core.js";

/* Six destinations, six modules. The pages that stopped being destinations
   did not stop existing: Performance is a tab of Portfolio (overview.js
   imports it), Agents a tab of Settings, and the strategy builder, the
   backtester and the risk bank are tabs of Research, each imported by the
   page that now hosts it. core.js's MOVED map redirects their old URLs. */
import "./views/overview.js";
import "./views/ticker.js";
import "./views/research.js";
import "./views/scanner.js";
import "./views/risk.js";
import "./views/add.js";
import "./views/settings.js";
import "./views/strategies.js";
import "./views/addaccount.js";
import "./views/options.js";

const labelOf = (id) => {
  const a = S.accounts.find((x) => x.id === id);
  return a ? (a.label || a.id) : id;
};

/* ============================================================== the hub ===
   One slow loop for the strategy-neutral model. It never blocks the fast
   loop: a hub request that is slow or down leaves the rail on its last good
   answer and says so, rather than freezing the page. */
const HUB_MS = 20000;         // app._perf_positions() is cached exactly this
const hubBlank = () => ({ portfolio: null, tickers: null, series: null,
                          account: null, at: 0, err: "", tried: false,
                          busy: false });
S.hub = hubBlank();

/* Switching account throws the hub's answers away IMMEDIATELY. Showing the
   previous account's positions under the new account's name for one poll is
   the kind of wrong this dashboard must never be, so the reset is eager and
   `S.hub.account` is checked again at paint time as a belt on the braces. */
function hubReset() { S.hub = hubBlank(); }
/* the hub's answers, but only if they belong to the account on screen */
const hubOf = () => (S.hub.account === S.account ? S.hub : hubBlank());

async function hubTick(force) {
  const h = S.hub;
  if (h.busy) return;
  if (!force && h.account === S.account && h.at
      && Date.now() - h.at < HUB_MS) return;
  if (!S.account && !S.accountsKnown) return;
  h.busy = true;
  const acct = S.account;
  try {
    /* Three reads inside one 20 s cache window cost ONE upstream position
       fetch, so they go together rather than on three staggered timers. */
    const [p, t, s] = await Promise.all([
      GET("/api/hub/portfolio"),
      GET("/api/hub/tickers"),
      // the header sparkline. /series does NOT touch the trading budget.
      GET("/api/hub/series?metric=value&tf=1M&form=line").catch(() => null),
    ]);
    if (acct !== S.account) return;          // switched while waiting
    h.portfolio = p;
    h.tickers = (t && t.tickers) || [];
    h.series = s;
    h.account = acct;
    h.err = "";
  } catch (e) {
    if (acct !== S.account) return;
    // Keep the last good answer on screen. A stale number that says how old
    // it is beats a blank rail, and the rail foot carries the failure.
    h.err = e.message || String(e);
  } finally {
    /* The account is stamped on FAILURE too, not only on success. Without it
       hubOf() treats a failed attempt as belonging to some other account and
       throws the error away, and the rail then says nothing at all about why
       it is showing the ladder's own numbers. */
    if (acct === S.account) h.account = acct;
    h.tried = true;
    h.at = Date.now();
    h.busy = false;
    if (acct === S.account) render(false);
  }
}
window.__hubTick = hubTick;

/* the strongest state among a ticker's strategies, in the order a trader
   cares: is anything transmitting, is anything broken, is anything running */
const STATE_RANK = { armed: 6, halted: 5, live: 4, adopted: 3, idle: 2,
                     error: 5, off: 1 };
const DOT_FOR = { armed: "armed", halted: "halt", error: "halt", live: "run",
                  adopted: "run", idle: "", off: "" };
function topState(cards) {
  let best = "";
  for (const c of cards || []) {
    const st = String((c && c.state) || "").toLowerCase();
    if (!st) continue;
    if (!best || (STATE_RANK[st] || 0) > (STATE_RANK[best] || 0)) best = st;
  }
  return best;
}
const stateWhy = (st) => (STATE_WORDS[st] || {}).why || "";

/* ------------------------------------------------------------------ rail */
/* The account switcher's own rows. Rendered into the popover, not the nav,
   so nine accounts cannot push the tickers off the bottom of the rail. */
function accountRows() {
  const rows = S.accounts.map((a) => {
    const dot = a.connected === false ? "hollow" : a.frozen ? "frozen"
      : a.armed ? "armed" : a.running ? "run" : "";
    const why = a.connected === false ? "these keys did not answer"
      : a.frozen ? "frozen: nothing arms and no lot opens"
      : a.armed ? `${a.armed} strategy/strategies transmit real orders`
      : "nothing is transmitting";
    return `<div class="acct-row ${a.id === S.account ? "on" : ""}"
                 data-go="overview" data-acct="${esc(a.id)}"
                 title="${esc(a.account_number || a.id)} — ${esc(why)}">
      <span class="dot ${dot}"></span>
      <span class="acct-tx"><span class="acct-n">${esc(a.label || a.id)}</span>
        <span class="acct-m">${esc(a.account_number || a.id)}</span></span>
      <span class="acct-eq num">${a.connected === false
        ? `<span class="unmeasured" title="these keys did not answer">—</span>`
        : money0(a.equity)}</span></div>`;
  }).join("");
  return rows || `<div class="acct-row" style="cursor:default;color:var(--faint)">
    No accounts yet</div>`;
}

function paintAccountSwitch() {
  const btn = el("acctSw");
  if (!btn) return;
  const a = curAccount();
  const ov = S.ov;
  const label = a ? (a.label || a.id) : (S.account || "No account");
  const number = a ? (a.account_number || a.number || "")
    : (ov && ov.account ? (ov.account.account_number || ov.account.number || "") : "");
  const eq = a && a.equity !== undefined ? a.equity
    : (hubOf().portfolio ? mv(hubOf().portfolio.value) : null);
  const initials = String(label).replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase()
    || "··";
  btn.innerHTML = `<span class="acct-av">${esc(initials)}</span>
    <span class="acct-tx"><span class="acct-n">${esc(label)}</span>
      <span class="acct-m">${esc(number || (S.accountsKnown && !S.accounts.length
        ? "no accounts yet" : "connecting…"))}</span></span>
    <span class="acct-eq num">${eq === null || eq === undefined
      ? `<span class="unmeasured" title="Alpaca's account snapshot has not been read">—</span>`
      : money0(eq)}</span>
    <span class="acct-cv" aria-hidden="true">▾</span>`;

  const pop = el("acctPop");
  if (pop && !pop.hidden) {
    pop.innerHTML = accountRows()
      + `<div class="acct-row acct-add" data-go="addaccount">
           <span class="dot hollow"></span><span class="acct-tx">
           <span class="acct-n" style="color:var(--accent-2)">Add an account</span>
           </span></div>`
      + `<div class="acct-pop-f">Each account keeps its own keys, ledgers and
           journal. Switching here changes every page at once.</div>`;
  }
}

/* one compact chip per strategy actually attached to this ticker */
function stratChips(cards) {
  const list = (cards || []).filter(Boolean);
  if (!list.length) {
    return `<span class="chip ch-mute sm"
      title="No strategy is attached. This ticker is on the watchlist: it still
carries market data and history, and nothing trades it.">watching</span>`;
  }
  /* ONE chip, plus a count. Two full strategy names on a 262 px row squeeze
     the price and the change into an ellipsis, and the price is the thing a
     trader is actually looking at. The one shown is the strongest state, so
     "armed" can never hide behind "idle"; the rest are in the +n tooltip. */
  const order = list.slice().sort(
    (a, b) => (STATE_RANK[String(b.state || "off").toLowerCase()] || 0)
            - (STATE_RANK[String(a.state || "off").toLowerCase()] || 0));
  const c = order[0];
  const st = String(c.state || "off").toLowerCase();
  const d = STATE_WORDS[st] || { tone: "idle" };
  const first = `<span class="chip st-${d.tone} sm" title="${
    esc((c.label || c.id || "strategy") + " — " + st + ": " + stateWhy(st))
  }">${esc(c.label || c.id)}</span>`;
  const more = order.length > 1
    ? `<span class="chip sm" title="${esc(order.slice(1)
        .map((x) => (x.label || x.id) + " (" + (x.state || "off") + ")")
        .join(", "))}">+${order.length - 1}</span>`
    : "";
  return first + more;
}

/* A TICKER ROW. Price, change, what is held, which strategies -- what a
   trader wants from a symbol, with nothing in it that assumes a ladder. */
function tickerRowHub(t, on) {
  const cards = t.strategies || [];
  const st = topState(cards);
  const dot = DOT_FOR[st] || "";
  const pos = t.position, opt = t.options;
  const heldValue = (pos && pos.value !== null && pos.value !== undefined)
    ? pos.value
    : (opt && opt.value !== null && opt.value !== undefined ? opt.value : null);
  const heldWhat = pos ? `${qty(pos.qty)} sh`
    : (opt ? `${qty(opt.contracts)} ct` : "");
  return `<div class="nav-item tick-item ${on ? "on" : ""}"
               data-go="ticker" data-sym="${esc(t.symbol)}"
               title="${esc(t.name || t.symbol)}">
    <div class="tick-top">
      ${dot ? `<span class="dot ${dot}" title="${esc(st + ": " + stateWhy(st))}"></span>` : ""}
      <span class="tick-sym">${esc(t.symbol)}</span>
      <span class="tick-px num">${mnum(t.price, { dp: 2 })}</span>
    </div>
    <div class="tick-bot">
      <span class="tick-chg">${measured(t.change_pct)
        ? pctf(mv(t.change_pct), 2)
        : `<span class="unmeasured" title="${esc(mreason(t.change_pct)
            || "no previous close to compare against")}">—</span>`}</span>
      ${heldValue !== null
        ? `<span class="tick-sep">·</span>
           <span class="tick-val num" title="${esc(heldWhat + " held at the broker")
             }">${money0(heldValue)}</span>`
        : `<span class="tick-sep">·</span>
           <span class="tick-val" title="Nothing of this symbol is held at the
broker right now.">flat</span>`}
      <span class="tick-chips">${stratChips(cards)}</span>
    </div>
  </div>`;
}

/* THE FALLBACK, used only while /api/hub/tickers has never answered. It
   renders the ladder's own numbers and SAYS they are the ladder's, instead of
   printing lot counts under a bare symbol as if that were what a ticker is. */
function tickerRowLegacy(t, on) {
  const dot = t.halted ? "halt" : (t.running && !t.dry_run) ? "armed"
    : t.running ? "run" : "";
  const open = Number(t.unrealized) || 0;
  return `<div class="nav-item tick-item ${on ? "on" : ""}"
               data-go="ticker" data-sym="${esc(t.symbol)}">
    <div class="tick-top">
      ${dot ? `<span class="dot ${dot}"></span>` : ""}
      <span class="tick-sym">${esc(t.symbol)}</span>
      <span class="tick-px num" title="Open P/L on what the ladder holds"
        >${open ? sgn(open, 0) : `<span class="unmeasured"
          title="the ladder holds nothing here">—</span>`}</span>
    </div>
    <div class="tick-bot"><span class="tick-val">ladder ${t.lot_count}/${
      t.max_lots} lots · ${qty(t.shares)} sh</span>
      <span class="tick-chips">${t.dry_run ? ""
        : `<span class="chip st-armed sm" title="${esc(stateWhy("armed"))
           }">armed</span>`}</span></div>
  </div>`;
}

function paintRail() {
  const ov = S.ov;
  const cur = S.view;
  const acct = curAccount();
  const hub = hubOf();
  const P = hub.portfolio;

  /* the brand dot: red the moment ANYTHING transmits real orders, whichever
     strategy it belongs to. That is the only fact this dot has ever meant. */
  const armedRows = P ? (P.by_strategy || []).filter((r) => r.state === "armed") : null;
  const liveRows = P ? (P.by_strategy || []).filter(
    (r) => r.state === "live" || r.state === "armed") : null;
  let dotCls = "off";
  if (ov && ov.frozen) dotCls = "off";
  else if (armedRows && armedRows.length) dotCls = "armed";
  else if (liveRows && liveRows.length) dotCls = "run";
  else if (!P && ov) dotCls = ov.totals.armed ? "armed" : ov.totals.running ? "run" : "off";
  el("brandDot").className = "brand-dot " + dotCls;

  /* THE STANDING RISK LINE. Paper, live money, or frozen -- never more than
     one glance away, and never mixed in with anything that moves. */
  const paper = acct && acct.paper !== undefined ? acct.paper : (ov ? ov.paper : true);
  const sub = el("brandSub");
  if (ov && ov.frozen) {
    sub.className = "brand-s frozen";
    sub.textContent = "frozen · nothing arms";
    sub.title = "state/FROZEN exists. Nothing arms and no lot opens until a "
              + "human removes it.";
  } else if (paper) {
    sub.className = "brand-s paper";
    sub.textContent = "paper account";
    sub.title = "Orders go to Alpaca's paper endpoint. No real money moves.";
  } else {
    sub.className = "brand-s live";
    sub.textContent = "live money";
    sub.title = "This account trades real money.";
  }

  paintAccountSwitch();

  const item = (kind, label, ico, tail = "") => `
    <div class="nav-item ${cur.kind === kind ? "on" : ""}" data-go="${kind}">
      <span class="ico">${ico}</span><span>${label}</span>
      ${tail ? `<span class="tail faint">${tail}</span>` : ""}</div>`;

  let html = "";

  if (S.account || ov) {
    html += item("overview", "Portfolio", "▦")
      + item("strategies", "Strategies", "◇")
      + item("options", "Options", "◈")
      + item("risk", "Risk", "◎")
      + item("settings", "Settings", "⚙");
  }

  /* ---- the tickers ------------------------------------------------------ */
  const hubRows = hub.tickers;
  if (hubRows) {
    const rows = hubRows.map((t) => tickerRowHub(
      t, cur.kind === "ticker" && cur.sym === t.symbol)).join("");
    const withStrat = hubRows.filter((t) => (t.strategies || []).length).length;
    html += `<div class="nav-label">Tickers
        <span class="tail" title="${withStrat} of ${hubRows.length} carry a strategy"
          >${hubRows.length}</span>
        <button class="nav-add" data-go="add" type="button"
          title="Add a ticker. It arrives with NO strategy attached.">＋</button></div>`
      + (rows || `<div class="nav-item" style="cursor:default;color:var(--faint)">
           Nothing on the watchlist yet</div>`);
  } else if (ov) {
    const rows = (ov.tickers || []).map((t) => tickerRowLegacy(
      t, cur.kind === "ticker" && cur.sym === t.symbol)).join("");
    html += `<div class="nav-label">Tickers <span class="tail">${
        ov.totals.count}</span>
        <button class="nav-add" data-go="add" type="button">＋</button></div>`
      + (rows || `<div class="nav-item" style="cursor:default;color:var(--faint)">
           No tickers</div>`)
      + `<div class="nav-item" style="cursor:default;color:var(--faint);
           font-size:11px;line-height:1.5;white-space:normal">${
           hub.tried ? "Showing the ladder's own numbers: the hub could not be read."
                     : "Loading market data…"}</div>`;
  } else if (S.account) {
    html += `<div class="nav-item" style="cursor:default;color:var(--faint)"
      >Loading ${esc(acctLabel())}…</div>`;
  }

  html += `<div class="nav-label">Shared library</div>`
    + item("research", "Research", "◭")
    + item("scanner", "Scanner", "◉");
  el("nav").innerHTML = html;

  /* ---- the foot: how old the data is, and whether the hub answered ------ */
  const foot = el("railFoot");
  if (hub.err) {
    foot.innerHTML = `<span class="down">●</span>
      <span title="${esc(hub.err)}">hub unreachable</span>
      <span class="kbd">Ctrl K</span>`;
  } else if (ov) {
    const age = ov.snap_age;
    foot.innerHTML = ov.snap_error
      ? `<span class="down">●</span> <span title="${esc(String(ov.snap_error))
          }">data error</span><span class="kbd">Ctrl K</span>`
      : `<span class="${age > 20 ? "warn" : "up"}">●</span>
         <span>${age == null ? "—" : age + "s"} · ${esc(ov.session)}</span>
         <span class="kbd">Ctrl K</span>`;
  } else if (!S.pollFails) {
    foot.innerHTML = `<span class="faint">—</span><span class="kbd">Ctrl K</span>`;
  }
}

/* ----------------------------------------------------------------- shell */
/* The left column collapses on every screen size. On a desktop it is a
   real column that comes and goes and the choice is remembered per browser
   (ta-rail, default open). Under 900 px it is an off-canvas drawer: shut by
   default, opened from the menu button or a swipe in from the left edge,
   closed by a tap outside, a swipe left, Escape, or choosing anything in it.
   The shell's extra pieces -- the menu button, the account switcher, the
   command bar, the scrim -- are created here rather than in index.html, so
   the markup the server hands out is unchanged. */
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

/* ---- the account popover ------------------------------------------------ */
function closeAcct() {
  const p = el("acctPop"), b = el("acctSw");
  if (p) p.hidden = true;
  if (b) b.setAttribute("aria-expanded", "false");
}
function toggleAcct() {
  const p = el("acctPop"), b = el("acctSw");
  if (!p) return;
  const open = p.hidden;
  p.hidden = !open;
  if (b) b.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) paintAccountSwitch();
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

  // running / armed beside the title. It carries no money: the KPI strip is
  // the one home for the account's figures, on a phone too.
  const status = document.createElement("div");
  status.className = "top-status"; status.id = "topStatus";
  status.innerHTML = `<span class="pill" id="topPill" hidden></span>`;
  const spacer = top.querySelector(".spacer");
  top.insertBefore(status, spacer || el("kpis"));

  rail.id = "rail";

  // the theme switch, beside the wordmark
  const brandN = rail.querySelector(".brand-n");
  if (brandN) {
    const acts = document.createElement("span");
    acts.className = "brand-act";
    acts.innerHTML = `<button class="icon-btn" id="themeBtn" type="button"
      title="Light or dark" aria-label="Switch between light and dark">
      <svg viewBox="0 0 20 20" fill="none" stroke="currentColor"
        stroke-width="1.6" aria-hidden="true">
        <path d="M16 11.5A6.5 6.5 0 0 1 8.5 4a6.5 6.5 0 1 0 7.5 7.5Z"/></svg>
    </button>`;
    brandN.appendChild(acts);
    acts.querySelector("#themeBtn").onclick = (e) => {
      e.stopPropagation(); toggleTheme();
    };
  }

  // the account switcher and the search field, above the navigation
  const railTop = document.createElement("div");
  railTop.className = "rail-top";
  railTop.innerHTML = `
    <button class="acct-sw" id="acctSw" type="button" aria-haspopup="true"
      aria-expanded="false" aria-controls="acctPop"></button>
    <div class="acct-pop" id="acctPop" hidden role="menu"></div>
    <button class="rail-find" id="railFind" type="button" style="text-align:left"
      title="Jump to a ticker, a page or an account">Search…
      <span class="kbd" style="float:right">Ctrl K</span></button>`;
  const nav = rail.querySelector(".nav");
  rail.insertBefore(railTop, nav);
  el("acctSw").onclick = (e) => { e.stopPropagation(); toggleAcct(); };
  el("railFind").onclick = () => openCmd();
  document.addEventListener("click", (e) => {
    const p = el("acctPop");
    if (p && !p.hidden && !p.contains(e.target) && e.target !== el("acctSw")) {
      closeAcct();
    }
  });

  const close = document.createElement("button");
  close.className = "rail-close"; close.type = "button";
  close.setAttribute("aria-label", "Close the menu");
  close.textContent = "×";
  (rail.querySelector(".brand") || rail).appendChild(close);

  const scrim = document.createElement("div");
  scrim.className = "scrim"; scrim.id = "scrim";
  a.appendChild(scrim);

  buildCmd();

  btn.onclick = toggleRail;
  close.onclick = closeDrawer;
  scrim.onclick = closeDrawer;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closeAcct(); closeDrawer(); }
  });

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

/* ======================================================= the command bar ==
   Ctrl/Cmd-K. Nine tickers plus six pages plus every account is already more
   than a sidebar can hold at a glance; this is how you get anywhere in two
   keystrokes. It navigates and nothing else -- no action in here can place,
   cancel or arm anything. */
let cmdSel = 0, cmdRows = [];

function cmdSources() {
  const out = [];
  const seen = new Set();
  const push = (r) => { if (!seen.has(r.key)) { seen.add(r.key); out.push(r); } };
  const syms = hubOf().tickers
    ? hubOf().tickers.map((t) => ({ symbol: t.symbol, name: t.name,
        n: (t.strategies || []).length }))
    : ((S.ov && S.ov.tickers) || []).map((t) => ({ symbol: t.symbol }));
  for (const t of syms) {
    push({ key: "t:" + t.symbol, label: t.symbol,
           meta: t.name || (t.n ? `${t.n} strateg${t.n > 1 ? "ies" : "y"}`
                                 : "watchlist only"),
           tail: "Ticker", view: { kind: "ticker", sym: t.symbol, tab: "live" } });
  }
  for (const [kind, label] of [["overview", "Portfolio"],
       ["strategies", "Strategies"], ["options", "Options"],
       ["risk", "Risk"], ["settings", "Settings"], ["research", "Research"],
       ["scanner", "Scanner"], ["add", "Add a ticker"],
       ["addaccount", "Add an account"]]) {
    if (!VIEWS[kind]) continue;
    push({ key: "v:" + kind, label, meta: "", tail: "Page", view: { kind, tab: "" } });
  }
  for (const a of S.accounts) {
    if (a.id === S.account) continue;
    push({ key: "a:" + a.id, label: a.label || a.id,
           meta: a.account_number || a.id, tail: "Account",
           view: { kind: "overview", account: a.id } });
  }
  return out;
}

function cmdPaint(q) {
  const needle = String(q || "").trim().toLowerCase();
  cmdRows = cmdSources().filter((r) => !needle
    || (r.label + " " + (r.meta || "") + " " + r.tail).toLowerCase().includes(needle));
  if (cmdSel >= cmdRows.length) cmdSel = Math.max(0, cmdRows.length - 1);
  const list = el("cmdkList");
  list.innerHTML = cmdRows.length
    ? cmdRows.map((r, i) => `<div class="cmdk-row ${i === cmdSel ? "on" : ""}"
        data-i="${i}"><span class="cmdk-k">${esc(r.label)}</span>
        ${r.meta ? `<span>${esc(r.meta)}</span>` : ""}
        <span class="cmdk-t">${esc(r.tail)}</span></div>`).join("")
    : `<div class="cmdk-empty">Nothing matches “${esc(q)}”.</div>`;
  list.querySelectorAll(".cmdk-row").forEach((d) => {
    d.onclick = () => cmdRun(Number(d.dataset.i));
  });
}

function cmdRun(i) {
  const r = cmdRows[i];
  closeCmd();
  if (r) go(r.view);
}

export function openCmd() {
  const v = el("cmdk");
  if (!v) return;
  v.hidden = false;
  cmdSel = 0;
  const inp = el("cmdkIn");
  inp.value = "";
  cmdPaint("");
  inp.focus();
}
function closeCmd() {
  const v = el("cmdk");
  if (v) v.hidden = true;
}

function buildCmd() {
  const v = document.createElement("div");
  v.className = "cmdk"; v.id = "cmdk"; v.hidden = true;
  v.innerHTML = `<div class="cmdk-box" role="dialog" aria-label="Go to">
    <input class="cmdk-in" id="cmdkIn" placeholder="Go to a ticker, a page or an account…"
      autocomplete="off" spellcheck="false" aria-controls="cmdkList">
    <div class="cmdk-list" id="cmdkList" role="listbox"></div></div>`;
  document.body.appendChild(v);
  v.addEventListener("click", (e) => { if (e.target === v) closeCmd(); });
  el("cmdkIn").addEventListener("input", (e) => { cmdSel = 0; cmdPaint(e.target.value); });
  el("cmdkIn").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault(); cmdSel = Math.min(cmdRows.length - 1, cmdSel + 1);
      cmdPaint(e.target.value);
      const on = el("cmdkList").querySelector(".cmdk-row.on");
      if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest" });
    } else if (e.key === "ArrowUp") {
      e.preventDefault(); cmdSel = Math.max(0, cmdSel - 1); cmdPaint(e.target.value);
      const on = el("cmdkList").querySelector(".cmdk-row.on");
      if (on && on.scrollIntoView) on.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault(); cmdRun(cmdSel);
    } else if (e.key === "Escape") {
      e.preventDefault(); closeCmd();
    }
  });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "k" || e.key === "K")) {
      e.preventDefault();
      if (el("cmdk").hidden) openCmd(); else closeCmd();
    }
  });
}

/* the pill beside the title: strategy-neutral, in hub's one vocabulary. On a
   ticker page it is that ticker's strongest state; everywhere else it is the
   account's. Its tooltip always carries the definition of the word it shows,
   which is what "armed" lacked when it meant two different things at once. */
function paintStatus(v) {
  const pill = el("topPill");
  if (!pill) return;
  const P = hubOf().portfolio;
  const ov = S.ov;
  let cls = "", txt = "", why = "";

  if (v.kind === "ticker") {
    const t = hubOf().tickers
      ? hubOf().tickers.find((x) => x.symbol === v.sym) : null;
    if (t) {
      const st = topState(t.strategies || []);
      if (!st) {
        txt = "watching"; cls = "";
        why = "No strategy is attached to this ticker. It keeps its market "
            + "data and history; nothing trades it.";
      } else {
        txt = st; why = stateWhy(st);
        cls = st === "armed" ? "down" : st === "halted" ? "warn"
          : st === "live" ? "up" : "";
      }
    } else if (ov) {
      const o = (ov.tickers || []).find((x) => x.symbol === v.sym);
      if (o) {
        if (o.halted) { cls = "warn"; txt = "halted"; }
        else if (o.running && !o.dry_run) { cls = "down"; txt = "armed"; }
        else if (o.running) { cls = "up"; txt = "live"; }
        else txt = "idle";
        why = stateWhy(txt) + " (the ladder's own state; the hub has not answered)";
      }
    }
  } else if (ov && ov.frozen) {
    cls = "down"; txt = "frozen";
    why = "state/FROZEN exists. Nothing arms and no lot opens.";
  } else if (P) {
    const rows = P.by_strategy || [];
    const armed = rows.filter((r) => r.state === "armed").length;
    const halted = rows.filter((r) => r.state === "halted").length;
    const live = rows.filter((r) => r.state === "live").length;
    if (armed) { cls = "down"; txt = `${armed} armed`; why = stateWhy("armed"); }
    else if (halted) { cls = "warn"; txt = `${halted} halted`; why = stateWhy("halted"); }
    else if (live) { cls = "up"; txt = `${live} live`; why = stateWhy("live"); }
    else { txt = "idle"; why = "No strategy on this account is deciding."; }
    /* THE ONE THING THIS PILL CANNOT SEE. hub's option-play rows report
       live / idle / adopted / off and NEVER "armed" -- whether the playbook
       may actually open a structure is state/options/PLAYS_ARMED, which the
       hub contract does not carry. So a count of armed strategies is a count
       of ladders, and saying otherwise would be the old "armed means two
       things" bug in a new place. The pill says so rather than implying a
       completeness it does not have. */
    if (rows.some((r) => r.kind === "options" && r.state === "live")) {
      why += " An options play reads 'live' once it is assigned and enabled;"
          + " whether it may OPEN is the arm on the Options tab, which the hub"
          + " does not report, so this count covers ladders only.";
    }
  } else if (ov) {
    const T = ov.totals || {};
    if (T.halted) { cls = "warn"; txt = `${T.halted} halted`; }
    else if (T.running) { cls = T.armed ? "down" : "up"; txt = `${T.running} running`; }
    else txt = "idle";
    why = "The ladder's own state; the hub has not answered.";
  }
  pill.hidden = !txt;
  pill.className = "pill " + cls;
  pill.textContent = txt;
  pill.title = why;
}

/* ---------------------------------------------------------------- topbar */
function paintTop() {
  const ov = S.ov, v = S.view;
  const view = VIEWS[v.kind];
  const label = (S.account && v.kind !== "addaccount") ? acctLabel() : "";
  el("title").innerHTML = esc(view ? view.title(ov, v) : "…")
    + (label ? ` <span class="title-acct">${esc(label)}</span>` : "");
  el("subtitle").textContent = view && view.sub ? view.sub(ov, v) : "";
  paintStatus(v);

  /* The tab bar. A view may name the tab to highlight (`activeTab`) when a
     route is hosted by a tab but does not share its key -- Research's
     Profiles pane lives under the Bank tab. On a phone the bar scrolls
     inside its own strip rather than widening the page (app.css). */
  const tabs = view && view.tabs;
  const bar = el("tabs");
  if (tabs && tabs.length) {
    const on = (view.activeTab ? view.activeTab(v) : (v.tab || "")) || tabs[0][0];
    bar.style.display = "flex";
    bar.innerHTML = tabs.map(([k, l]) => `
      <div class="tab ${on === k ? "on" : ""}"
           data-go="${v.kind}" ${v.sym ? `data-sym="${v.sym}"` : ""}
           data-tab="${k}">${l}</div>`).join("");
  } else bar.style.display = "none";

  paintKpis(view, v, ov);
}

/* THE ACCOUNT'S MONEY HAS ONE HOME, and it is this strip: it follows you from
   page to page so no two pages can disagree about the account total.

   NEW CONTRACT FOR VIEWS: a view that carries the SAME figures in its own
   hero declares `ownsKpis = true` (or a function of the view) on its VIEWS
   entry, and the strip steps aside for it. Declaring nothing means the strip
   shows, which is the right default for a trading hub. */
function paintKpis(view, v, ov) {
  const box = el("kpis");
  /* A view whose own hero carries these same three figures declares
     `ownsKpis` (true, or a function of the view) and the strip steps aside.
     Declaring NOTHING falls back to the rule this shell has always had:
     Portfolio's landing tab owns them, every other page gets the strip. That
     default is here so a view that has not been updated yet cannot end up
     printing the account total twice in one screen. */
  const landing = v.kind === "overview"
    && (!v.tab || v.tab === "live"
        || (view && view.tabs && view.tabs.length && v.tab === view.tabs[0][0]));
  const owns = view && view.ownsKpis !== undefined
    ? (typeof view.ownsKpis === "function" ? view.ownsKpis(v) : view.ownsKpis)
    : landing;
  if (owns) { box.innerHTML = ""; return; }

  const P = hubOf().portfolio;
  if (P) {
    const pts = (hubOf().series && hubOf().series.points) || [];
    const spark = sparkline(pts, {
      w: 92, h: 20,
      why: (hubOf().series && hubOf().series.reason)
        || "the value series has fewer than two points",
    });
    const dd = P.drawdown || {};
    box.innerHTML = `
      <div data-go="overview">
        <div class="kpi-k">Account value</div>
        <div class="kpi-v num">${mnum(P.value, { dp: 2 })}</div>
        <div class="kpi-spark">${spark}</div></div>
      <div>
        <div class="kpi-k">P/L today</div>
        <div class="kpi-v num">${mnum(P.pl.today, { signed: true })}</div>
        <div class="kpi-sub" title="${esc((P.pl.basis && P.pl.basis.today) || "")
          }">vs yesterday's close</div></div>
      <div>
        <div class="kpi-k">P/L all time</div>
        <div class="kpi-v num">${mnum(P.pl.total, { signed: true })}</div>
        <div class="kpi-sub" title="${esc((P.pl.basis && P.pl.basis.open) || "")
          }">${mnum(P.pl.open, { signed: true })} open</div></div>
      <div class="kpi-dd">
        <div class="kpi-k">Drawdown</div>
        <div class="kpi-v num">${mnum(dd.current_pct, { dp: 2, unit: "pct" })}</div>
        <div class="kpi-sub" title="${esc((dd.basis || "")
          + " Peak: " + (dd.peak_at || "unknown"))}">max ${
          mfmt(dd.max_pct, { unit: "pct", dp: 2 })}</div></div>`;
    return;
  }

  /* The hub has not answered. /api/overview's own portfolio block is the
     fallback -- it is the ACCOUNT's money either way, and it is labelled the
     same, so nothing on screen changes meaning when the hub comes back. */
  if (!ov) { box.innerHTML = ""; return; }
  const p = ov.portfolio;
  const todayPl = p.today_pl != null ? p.today_pl : p.made_today;
  const uToday = p.unrealized_today != null ? p.unrealized_today : p.open_today;
  const uTotal = p.unrealized_total != null ? p.unrealized_total : p.open_pl;
  box.innerHTML = `
    <div><div class="kpi-k">Account value</div>
         <div class="kpi-v num">${money(p.account_value)}</div></div>
    <div><div class="kpi-k">P/L today</div><div class="kpi-v num">${sgn(todayPl)}</div>
         <div class="kpi-sub num">since yesterday's close</div></div>
    <div><div class="kpi-k">P/L all time</div><div class="kpi-v num">${
           sgn(p.total_pl)}</div>
         <div class="kpi-sub num">equity less what you funded</div></div>`;
  /* NO "booked" SUB-LINE. It used to read "<realised> booked - <open> open"
     under both figures, and booked is WINS-ONLY on this account: the ladder has
     no stop loss, so a losing lot is never closed and never books. Measured on
     the journal -- 322 realised rows, zero losses. Putting that number under
     the headline made the account look like it was making money it had not
     made, which is the owner's complaint verbatim. The decomposition still
     exists, on the Portfolio page, where realised and open are shown as parts
     of a total that has to add up. */
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
    /* ONE banner, not one per paint. A view that throws on every paint used
       to stack a fresh banner every two seconds until the page was nothing
       but banners and the error itself was off the top of the screen. */
    const host = el("view");
    host.querySelectorAll(".render-err").forEach((n) => n.remove());
    host.insertAdjacentHTML("afterbegin",
      `<div class="note bad render-err"><b>Render error:</b> ${esc(e.message)}<br>
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
      /* A symbol with no ladder is a legitimate ticker now, so its absence
         from the ladder fleet is NOT a reason to throw the page away. Only
         bounce when the hub has answered and does not know it either. */
      const known = hubOf().tickers
        && hubOf().tickers.some((t) => t.symbol === S.view.sym);
      if (hubOf().tickers && !known) {
        toast(`${esc(S.view.sym)} is not a ticker on this account.`, "err");
        go({ kind: "overview" });
        S.timer = setTimeout(tick, 1000);   // go() does not re-poll on the same account
        return;
      }
    }
    try {
      const tk = await GET("/api/ticker/" + S.view.sym);
      if (acct === S.account && S.view.kind === "ticker") S.ticker = tk;
    } catch (e) { /* a strategy-less ticker has no ladder view; keep last */ }
  }
  render(false);

  // the slow loop, fired and not awaited: the hub must never delay the rail
  hubTick(false);

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
  closeAcct();
  // a different account: drop the hub's answers before the first paint, so no
  // frame ever shows one account's positions under another account's name
  if (v.account !== undefined && v.account !== S.account) hubReset();
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
    if (v.account !== S.account) { setAccount(v.account); hubReset(); }
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
  hubTick(true);
})();
