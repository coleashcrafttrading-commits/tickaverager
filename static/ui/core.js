/* ============================================================================
   core.js -- state, API, router, formatting, modals.

   Everything shared lives here so a view module can be read on its own. Views
   register themselves into VIEWS; the router never needs to know they exist.
   ========================================================================= */
"use strict";

/* ------------------------------------------------------------------ dom */
export const $  = (s, r = document) => r.querySelector(s);
export const $$ = (s, r = document) => [...r.querySelectorAll(s)];
export const el = (id) => document.getElementById(id);

export const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");

/* --------------------------------------------------------- formatting */
export const money = (n, dp = 2) => {
  n = Number(n) || 0;
  return (n < 0 ? "-" : "") + "$" + Math.abs(n).toLocaleString(undefined,
    { minimumFractionDigits: dp, maximumFractionDigits: dp });
};
export const money0 = (n) => money(n, 0);
/* signed: the only place colour is allowed to mean something */
export const sgn = (n, dp = 2) => {
  n = Number(n) || 0;
  const c = n > 0 ? "up" : n < 0 ? "down" : "faint";
  return `<span class="${c}">${n > 0 ? "+" : ""}${money(n, dp)}</span>`;
};
export const pct = (n, dp = 2) => {
  n = Number(n) || 0;
  const c = n > 0 ? "up" : n < 0 ? "down" : "faint";
  return `<span class="${c}">${n > 0 ? "+" : ""}${n.toFixed(dp)}%</span>`;
};
export const px = (v, dp = 2) => (Number(v) ? "$" + Number(v).toFixed(dp) : "—");
/* a share count: "100" for a whole number, "0.01" / "0.3" for a fraction (0.30000000000000004
   trims to 0.3), never "100.0" and never a raw binary float */
export const qty = (n, dp = 6) => {
  const v = Number(n) || 0;
  return Number.isInteger(v) ? String(v) : v.toFixed(dp).replace(/0+$/, "").replace(/\.$/, "");
};
export const dur = (s) => {
  s = Number(s) || 0;
  if (!s) return "—";
  if (s < 90) return `${Math.round(s)}s`;
  if (s < 5400) return `${Math.round(s / 60)}m`;
  if (s < 172800) return `${(s / 3600).toFixed(1)}h`;
  return `${(s / 86400).toFixed(1)}d`;
};
export const clock = (ts) => String(ts || "").slice(11, 19);
export const day = (ts) => String(ts || "").slice(0, 10);

/* stat block — no boxes, space does the separating */
export const stat = (k, v, sub) =>
  `<div><div class="stat-k">${k}</div><div class="stat-v num">${v}</div>` +
  (sub ? `<div class="stat-s">${sub}</div>` : "") + `</div>`;

/* opts.cls adds a modifier -- "hero" is the one place the theme's gradient is
   the surface rather than an accent, so at most one card per page may have it. */
export const card = (title, body, extra = "", opts = {}) =>
  `<div class="card${opts.cls ? " " + opts.cls : ""}"${opts.id ? ` id="${opts.id}"` : ""}>
     ${title ? `<div class="card-h"><div class="card-t">${title}</div>
       ${extra ? `<div class="card-x">${extra}</div>` : ""}</div>` : ""}
     <div class="card-b${opts.flush ? " flush" : ""}">${body}</div>
   </div>`;

export const tableHTML = (heads, rows, emptyMsg = "Nothing here.") => `
  <div class="tw"><table>
    <thead><tr>${heads.map((h) => `<th>${h}</th>`).join("")}</tr></thead>
    <tbody>${rows.length ? rows.join("")
      : `<tr><td colspan="${heads.length}" class="empty">${emptyMsg}</td></tr>`}
    </tbody></table></div>`;

/* ---------------------------------------------------------------- api */
/* A request that never answers is worse than one that fails: the poll loop
   awaits it for ever and the page sits on "connecting..." with no error and
   no retry. That is exactly what a server restart looks like from here -- the
   socket is accepted and then nothing comes back -- so every request gets a
   deadline and a hang is turned into an ordinary failure the caller retries. */
const TIMEOUT_MS = { GET: 12000, POST: 240000, DELETE: 30000 };

/* Every account-scoped route lives under /api/a/{acct}/...; the old unprefixed
   path is only a compatibility alias for the default account. Views keep
   passing the plain '/api/...' path and this is the ONE place the prefix is
   put on. Anything under a shared prefix is a library shared by every account
   and is left alone. THE MATCH IS ON WHOLE PATH SEGMENTS and not a bare
   startsWith, and it stays that way now that the one pair which proved it
   necessary has been split up: /api/risk was per-account exposure and went
   with the Risk room in round 8, while /api/risk/profiles and /api/risk/bank
   are shared and are still here. A startsWith list holding "/api/risk" would
   have swallowed both of those the moment either was added back. */
export const SHARED_API = [
  "/api/accounts", "/api/strategies", "/api/code", "/api/indicators",
  /* "/api/scanner" left with the Scanner room and "/api/research" never
     existed at all -- it was a prefix for a route nobody ever registered.
     "/api/pine", "/api/risk/profiles" and "/api/risk/bank" STAY: the Research
     room was not their only caller. agentctl.py's risk-profiles, risk-save,
     risk-record and risk-bank commands are the documented way findings are
     banked, and views/backtest.js still reads all three. */
  "/api/pine", "/api/risk/profiles",
  "/api/risk/bank", "/api/health", "/api/restart", "/api/presets",
  // the strategy bank is the same shelf as /api/strategies and /api/code --
  // one library every account draws from, not per-account state
  "/api/bank",
];
export function api(path) {
  if (!path.startsWith("/api/") || path.startsWith("/api/a/")) return path;
  const bare = path.split("?")[0];
  if (SHARED_API.some((p) => bare === p || bare.startsWith(p + "/"))) return path;
  if (!S.account) return path;         // no account known: the default alias
  return "/api/a/" + encodeURIComponent(S.account) + path.slice(4);
}

async function req(method, path, body) {
  const url = api(path);
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(),
                       TIMEOUT_MS[method] || 30000);
  let r;
  try {
    r = await fetch(url, {
      method,
      headers: body !== undefined ? { "content-type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store",
      signal: ctl.signal,
    });
  } catch (e) {
    throw new Error(e.name === "AbortError"
      ? `${method} ${url} did not answer in time`
      : (e.message || String(e)));
  } finally {
    clearTimeout(t);
  }
  const txt = await r.text();
  if (!r.ok) {
    let m = txt;
    try { m = JSON.parse(txt).detail || txt; } catch (e) { /* plain text */ }
    // the status travels with the message so a caller can tell "this account
    // is gone" (404) from "the server is down"
    const err = new Error(m || r.statusText);
    err.status = r.status;
    throw err;
  }
  return txt ? JSON.parse(txt) : {};
}
export const GET  = (p) => req("GET", p);
export const POST = (p, b = {}) => req("POST", p, b);
export const DEL  = (p) => req("DELETE", p);

/* --------------------------------------------------------------- state */
export const S = {
  view: { kind: "overview" },
  ov: null,          // /api/a/<acct>/overview
  ticker: null,      // /api/a/<acct>/ticker/<sym>
  cache: {},         // per-view scratch
  timer: null,
  mounted: "",
  touched: false,    // a form is being typed in; do not repaint over it

  account: "",       // id of the account every scoped request goes to
  accounts: [],      // summaries from GET /api/accounts (refreshed by the poll)
  defaultAccount: "",
  accountsKnown: false,  // false until GET /api/accounts has answered once
};

/* ------------------------------------------------------------ accounts */
const LS_ACCT = "ta-account";
export const savedAccount = () => {
  try { return localStorage.getItem(LS_ACCT) || ""; } catch (e) { return ""; }
};

/* Switching account throws away everything that belonged to the old one.
   Views keep their own module-scope caches and reset them in mount(), which
   render() calls because sig() carries the account. */
export function rememberAccount() {
  if (!S.account) return;
  try { localStorage.setItem(LS_ACCT, S.account); } catch (e) { /* private mode */ }
}
export function setAccount(id) {
  id = id || "";
  if (id === S.account) return;
  S.account = id;
  S.ov = null;
  S.ticker = null;
  S.touched = false;
  // remembered only once it is known to exist: an id typed into the hash that
  // turns out to be gone must not overwrite the last account actually used
  if (id && (!S.accountsKnown || S.accounts.some((a) => a.id === id))) rememberAccount();
}

export async function loadAccounts() {
  const r = await GET("/api/accounts");
  S.accounts = r.accounts || [];
  S.defaultAccount = r.default
    || (S.accounts.find((a) => a.is_default) || S.accounts[0] || {}).id || "";
  S.accountsKnown = true;
  return S.accounts;
}

/* The account a hash without one should land on: the last one used, else the
   default. An id that is not in the list is never returned. */
export function pickAccount(wanted = "") {
  const ids = S.accounts.map((a) => a.id);
  if (wanted && ids.includes(wanted)) return wanted;
  const saved = savedAccount();
  if (saved && ids.includes(saved)) return saved;
  if (S.defaultAccount && ids.includes(S.defaultAccount)) return S.defaultAccount;
  return ids[0] || "";
}

export const curAccount = () =>
  S.accounts.find((a) => a.id === S.account)
  || ((S.ov && S.ov.account && (S.ov.account.id || "") === S.account) ? S.ov.account : null);
export const acctLabel = () => {
  const a = curAccount();
  return a ? (a.label || a.id || S.account) : (S.account || "this account");
};
export const acctNumber = () => {
  const a = curAccount() || (S.ov && S.ov.account) || {};
  return a.account_number || a.number || "";
};

/* --------------------------------------------------------------- toast */
export function toast(msg, kind = "", ms = 5200) {
  const d = document.createElement("div");
  d.className = "toast " + kind;
  d.innerHTML = msg;
  el("toasts").appendChild(d);
  setTimeout(() => d.remove(), ms);
}

/* --------------------------------------------------------------- modal */
/* requireWord forces the operator to TYPE it. Used for anything that
   transmits orders or sells stock. */
export function ask({ title, body, ok = "Confirm", danger = false,
                      requireWord = "", checkbox = null }) {
  return new Promise((resolve) => {
    const v = document.createElement("div");
    v.className = "veil";
    v.innerHTML = `<div class="modal">
      <h3>${title}</h3>
      <div class="body">${body}</div>
      ${checkbox ? `<label style="display:flex;gap:10px;align-items:flex-start;
        margin-top:16px;padding:11px 13px;border:1px solid var(--hairline2);
        border-radius:8px;cursor:pointer">
        <input type="checkbox" id="mchk" style="width:auto;margin-top:2px"
          ${checkbox.checked ? "checked" : ""}>
        <span style="font-size:12.5px;line-height:1.55">${checkbox.label}</span></label>` : ""}
      ${requireWord ? `<div style="margin-top:16px">
        <span style="color:var(--muted);font-size:12px">Type
          <b style="color:var(--text)">${requireWord}</b> to confirm</span>
        <input id="mword" style="margin-top:6px" autocomplete="off" spellcheck="false"></div>` : ""}
      <div class="acts">
        <button class="btn" id="mno">Cancel</button>
        <button class="btn ${danger ? "danger" : "primary"}" id="myes"
          ${requireWord ? "disabled" : ""}>${ok}</button>
      </div></div>`;
    document.body.appendChild(v);
    const yes = v.querySelector("#myes"), w = v.querySelector("#mword");
    // the checkbox must be read BEFORE the veil leaves the DOM
    const done = (r) => {
      const chk = v.querySelector("#mchk");
      const val = (r && checkbox) ? { checked: !!(chk && chk.checked) } : r;
      v.remove();
      document.removeEventListener("keydown", onKey);
      resolve(val);
    };
    function onKey(e) { if (e.key === "Escape") done(false); }
    if (w) {
      w.focus();
      w.addEventListener("input", () => {
        yes.disabled = w.value.trim().toUpperCase() !== requireWord.toUpperCase();
      });
      w.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !yes.disabled) done(true);
      });
    } else yes.focus();
    yes.onclick = () => done(true);
    v.querySelector("#mno").onclick = () => done(false);
    v.addEventListener("click", (e) => { if (e.target === v) done(false); });
    document.addEventListener("keydown", onKey);
  });
}

/* an action that can fail must never leave a dead button */
export async function act(fn) {
  try { await fn(); await window.__tick(); }
  catch (e) { toast(esc(e.message), "err", 9000); }
}

/* =================================================== shared components ====
   Two pieces of chrome used to be pasted into more than one view. They are
   defined ONCE here, so a change to the wording of a confirmation or to the
   sign-in instructions cannot land on one page and not the other. */

/* ---- the four DCA-LADDER actions -----------------------------------------
   Rendered on Portfolio and nowhere else. Settings used to carry a
   byte-for-byte copy; it links to Portfolio now. Every confirmation names
   the account, so nobody arms the wrong one.

   THEY ARE NOT ACCOUNT-WIDE AND THE BUTTONS NOW SAY SO. `/api/fleet/*` walks
   `fleet.engines`, which is the share ladders and nothing else: an options
   play is armed on the Options tab and none of these four touches it. The
   labels used to read "Start all" / "Stop all" / "Disarm all" / "Panic",
   which on the account's own page reads as the whole account -- the ladder
   standing in for the product again, in the one place a mistaken click costs
   money. overview.js carried that scope as a `title` on the SPAN around all
   four, where it was not reachable from the keyboard and could not be read
   per button; it hands the sentence over here, which is what its comment
   asked for. */
export const LADDER_SCOPE =
  "Share ladders only. An options play is armed on the Options tab.";
export const fleetControlsHTML = () => `
  <span class="row-btns fleet-btns">
    <button class="btn sm" data-fleet="start" title="${esc(
      "Start every DCA-ladder engine on this account. " + LADDER_SCOPE)
    }">Start ladders</button>
    <button class="btn sm" data-fleet="stop" title="${esc(
      "Stop every DCA-ladder engine on this account. " + LADDER_SCOPE)
    }">Stop ladders</button>
    <button class="btn sm" data-fleet="disarm" title="${esc(
      "Put every DCA ladder back into dry run. " + LADDER_SCOPE)
    }">Disarm ladders</button>
    <button class="btn sm danger" data-fleet="panic" title="${esc(
      "Stop and disarm every DCA ladder, selling nothing. " + LADDER_SCOPE)
    }">Panic</button>
  </span>`;

const FLEET = {
  start: async () => {
    const who = acctLabel();
    if (!await ask({
      title: `Start every DCA ladder in ${esc(who)}?`,
      body: `Each DCA ladder in <b>${esc(who)}</b> (${esc(acctNumber() || "—")}) begins `
          + `deciding on its own settings. Any ladder that is <b>armed</b> will transmit `
          + `real orders immediately. Other accounts are untouched.`,
      ok: "Start all",
    })) return;
    const r = await POST("/api/fleet/start_all");
    toast(`${esc(who)}: started ${r.started} engine(s).`, "ok");
  },
  stop: async () => {
    const r = await POST("/api/fleet/stop_all");
    toast(`${esc(acctLabel())}: stopped ${r.stopped}. Resting take-profits stay live `
        + `at Alpaca.`, "ok");
  },
  disarm: async () => {
    const r = await POST("/api/fleet/disarm_all");
    const no = (r.refused || []);
    if (no.length) toast(`${esc(acctLabel())}: ${r.disarmed} back to dry run, but `
      + `<b>${esc(no.join(", "))} is STILL ARMED</b> — a rung is still working at Alpaca. `
      + `Stop the ladder, then disarm.`, "err", 9000);
    else toast(`${esc(acctLabel())}: ${r.disarmed} ladder(s) back to dry run.`, "ok");
  },
  panic: async () => {
    const who = acctLabel();
    if (!await ask({
      title: `Stop and disarm every DCA ladder in ${esc(who)}?`, danger: true, ok: "Panic",
      requireWord: "PANIC",
      body: `Every DCA-ladder engine in <b>${esc(who)}</b> (${esc(acctNumber() || "—")}) `
          + `stops and returns to dry run. ${esc(LADDER_SCOPE)} Other accounts are `
          + `untouched.<br><br>`
          + `<b>Nothing is sold.</b> Open positions and the take-profits resting `
          + `against them are left exactly as they are — flattening stays a `
          + `per-ticker decision.`,
    })) return;
    const r = await POST("/api/fleet/panic", { confirm: "PANIC" });
    const no = (r.refused || []);
    if (no.length) toast(`${esc(who)}: everything stopped, but <b>${esc(no.join(", "))} is `
      + `STILL ARMED</b> — a rung cancel is still pending at Alpaca. Disarm again in a moment.`,
      "err", 9000);
    else toast(`${esc(who)}: everything stopped and disarmed.`, "ok");
  },
};

export function wireFleetControls(root) {
  if (!root) return;
  root.querySelectorAll("[data-fleet]").forEach((b) => {
    b.onclick = () => act(FLEET[b.dataset.fleet]);
  });
}

/* ---- "can this dashboard reach a model" ---------------------------------
   The agent runner and the indicator builder use the SAME credential. They
   used to explain it two different ways on two pages; one explanation now.
   `r` is {ready, auth|how, problem, fix} from /api/agents or
   /api/indicators/custom. */
const AUTH_NAME = {
  api_key: "an API key from .env — billed per token",
  cli_login: "the Claude Code CLI login — your subscription",
};
export function modelCredsHTML(r, { test = false, id = "mcTest" } = {}) {
  r = r || {};
  const how = r.auth || r.how || "";
  if (r.ready) {
    /* A working credential is not news. This used to be a full green banner
       congratulating the reader on a healthy state; it is now one faint line
       carrying the Test button, and nothing at all on the page that does not
       offer that button. The FAILING branch below is unchanged -- that one is
       a real problem and still shouts. */
    if (!test) return "";
    return `<div class="tip" style="margin:0 0 10px">Model:
      ${esc(AUTH_NAME[how] || how || "the Claude Code CLI")}.
      <button class="btn sm" id="${id}" style="margin-left:8px">Test it</button>
      <span id="${id}Out" class="faint"></span></div>`;
  }
  return `<div class="note warn" style="margin-top:0">
    <b>This dashboard cannot reach a model yet.</b> ${esc(r.problem || "")}
    ${r.fix ? `<br>${esc(r.fix)}` : ""}
    <div class="tip">Two separate one-time steps, and you need <b>both</b>:<br>
      <b>1. Trust</b> — open a terminal in the bot folder, run <code>claude</code>,
      accept the trust prompt. Without it the CLI silently ignores every
      permission rule in <code>.claude/settings.json</code>.<br>
      <b>2. Credentials</b> — in that same session run <code>/login</code> (uses
      your subscription), <i>or</i> put <code>ANTHROPIC_API_KEY=sk-ant-…</code> in
      <code>.env</code> and restart the dashboard (billed per token, a few cents
      a run).</div>
    ${test ? `<button class="btn sm" id="${id}" style="margin-top:8px">Test it anyway</button>
      <span id="${id}Out" class="faint"></span><br>
      <span class="faint">Schedules still save; every run until then is recorded as
      blocked rather than failing silently.</span>` : ""}</div>`;
}

/* Wire the Test button. `onDone` receives the rendered HTML so a caller can
   keep it across its own repaints. */
export function wireModelCreds(id = "mcTest", onDone = null) {
  const b = el(id);
  if (!b) return;
  const out = el(id + "Out");
  b.onclick = async () => {
    b.disabled = true; b.textContent = "Asking…";
    let html = `<span class="faint">asking the model…</span>`;
    if (out) out.innerHTML = html;
    try {
      const r = await POST("/api/agents/selftest", {});
      html = r.ok
        ? `<span class="up">replied “${esc(r.reply)}” in ${r.seconds}s${
            r.cost_usd ? `, $${Number(r.cost_usd).toFixed(4)}` : ""}</span>`
        : `<span class="down">${esc(r.error || r.problem || "no answer")}</span>`;
    } catch (e) {
      html = `<span class="down">${esc(e.message)}</span>`;
    } finally {
      b.disabled = false;
      b.textContent = "Test it";
      const o = el(id + "Out");
      if (o) o.innerHTML = html;
      if (onDone) onDone(html);
    }
  };
}

/* -------------------------------------------------------------- router */
export const VIEWS = {};        // kind -> { title, sub, mount, paint, tabs? }

/* Hashes carry the account:  #/a/<id>/               portfolio, its live tab
                              #/a/<id>/portfolio/tab  portfolio's other tabs
                              #/a/<id>/t/SYM/tab      ticker
                              #/a/<id>/kind/tab       any registered view
   A view without an account (none configured yet) drops the 'a/<id>' pair. */
export function hashFor(view) {
  const a = view.account !== undefined ? view.account : S.account;
  const base = a ? `#/a/${encodeURIComponent(a)}/` : "#/";
  if (view.kind === "ticker") return base + `t/${view.sym}/${view.tab || "live"}`;
  if (!view.kind || view.kind === "overview") {
    // Portfolio's Live tab IS the account's home page; its other tabs hang
    // off /portfolio/ so they can be linked to and bookmarked.
    const t = view.tab || "";
    return base + (t && t !== "live" ? "portfolio/" + t : "");
  }
  return base + view.kind + (view.tab ? "/" + view.tab : "");
}

export function go(view) {
  const account = view.account !== undefined ? view.account : S.account;
  view = { ...view, account };
  const switched = account !== S.account;
  if (switched) setAccount(account);
  S.view = view;
  S.ticker = null;
  S.touched = false;
  const h = hashFor(view);
  if (location.hash !== h) location.hash = h;
  // render now rather than at the next poll; the hashchange that follows sees
  // the same signature and does nothing
  window.__render(true);
  if (switched && window.__tick) window.__tick();
}

/* ---------------------------------------------------------------- moved
   Eleven nav destinations became six and, in round 8, five. Every URL whose
   CONTENT still exists resolves to where that content lives now -- never to
   a 404 and never to a page with no highlighted nav item. app.js rewrites the
   address bar on arrival, so an old bookmark quietly upgrades itself the
   first time it is used. Keyed "kind" or "kind/tab"; the longer key wins.

   A URL whose content was DELETED is not in this map. Moved and deleted are
   different facts and this table only records the first; see the note at the
   end of it. */
export const MOVED = {
  // Performance is Portfolio's History tab
  "performance":       { kind: "overview", tab: "history" },
  "portfolio":         { kind: "overview", tab: "" },        // the home page
  // Agents is a Settings tab
  "agents":            { kind: "settings", tab: "agents" },
  /* NOT "strategies" any more. That row used to send #/strategies to the
     research BUILDER, and readHash() tests MOVED before VIEWS -- so once
     VIEWS.strategies shipped (the catalogue where the ladder finally appears
     as ONE CARD beside the options plays) the page could never open. It was
     the single reason the ladder still looked dominant: the one room that
     demotes it was unreachable. The builder is the Strategies room's own
     second tab, which is where this now points. */
  "strategy-builder":  { kind: "strategies", tab: "builder" },
  /* ROUND 8: Risk, Research and Scanner were deleted from the code, so the
     eight rows that pointed INTO them are gone rather than redirected. There
     is nowhere honest to send them -- the backtester, the indicator writer,
     the risk bank, the exposure page and the momentum screen are not
     somewhere else, they are not there -- and a redirect to a room that
     answers a different question is worse than the fall-through. readHash()
     ends at `{kind:"overview"}` for any hash it does not recognise, so
     #/risk, #/research, #/scanner, #/backtest, #/tester and their old sub-
     tabs all land on Portfolio with Portfolio highlighted.

     The rows deleted here, for the record, were: backtest, tester,
     research/tester, risk/live, risk/profiles, risk/bank, scanner/list and
     scanner/run. */
};

export function readHash() {
  const h = (location.hash || "#/").slice(2).split("/").filter(Boolean);
  let account;
  if (h[0] === "a" && h[1]) {
    try { account = decodeURIComponent(h[1]); } catch (e) { account = h[1]; }
    h.splice(0, 2);
  }
  let v;
  const kind = h[0] || "", tab = h[1] || "";
  if (kind === "t" && h[1]) {
    v = { kind: "ticker", sym: h[1].toUpperCase(), tab: h[2] || "live" };
  } else if (MOVED[kind + "/" + tab]) v = { ...MOVED[kind + "/" + tab] };
  else if (MOVED[kind]) v = { ...MOVED[kind], tab: MOVED[kind].tab || tab };
  else if (kind && VIEWS[kind]) v = { kind, tab };
  else v = { kind: "overview" };
  if (account !== undefined) v.account = account;
  return v;
}

/* the account is part of the signature, so the same view on another account
   is a different mount. Portfolio's Live tab and its bare hash are the same
   place, so they must sign the same or every click on Live mounts twice. */
export const sig = (v) => {
  const tab = (v.kind === "overview" && v.tab === "live") ? "" : (v.tab || "");
  return `${v.account !== undefined ? v.account : S.account}|`
    + (v.kind === "ticker" ? `ticker:${v.sym}:${v.tab || "live"}` : `${v.kind}:${tab}`);
};

/* ------------------------------------------------------------- theme */
export function initTheme() {
  const saved = (() => {
    try { return localStorage.getItem("ta-theme"); } catch (e) { return null; }
  })();
  if (saved) document.documentElement.setAttribute("data-theme", saved);
}
export function toggleTheme() {
  const cur = document.documentElement.getAttribute("data-theme") === "light"
    ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", cur);
  try { localStorage.setItem("ta-theme", cur); } catch (e) { /* private mode */ }
}

/* ============================================================================
   ==========================  THE SHARED PRIMITIVES  ========================

   Every view in this dashboard is built from the pieces below. They exist so
   that a tile on Portfolio and a tile on a ticker page are the SAME object --
   one change of mind lands everywhere instead of on whichever page was edited
   last. Nothing here knows what a ladder is; they all take the hub's metric
   envelope, which is strategy-neutral by construction.

   THE ONE RULE THEY ENFORCE: a number nobody measured renders as a dash that
   carries its reason, never as 0. `money()` and `sgn()` above coerce null to
   0 -- they are the OLD formatters and they stay that way because views still
   pass raw floats to them. Anything coming out of /api/hub/* is an envelope
   and must go through `mnum` / `mfmt`, which cannot print a confident zero.

   THE ENVELOPE, as hub.py ships it:
       {value, n, unit, reason, thin, as_of}
       unit in usd | pct | ratio | count | qty | days | seconds
       pct is a FRACTION: 0.0123 means +1.23%.
       value === null  -> nobody measured it; `reason` says why.
       thin === true   -> there IS a number but n === 0, so its reason still
                          has to be readable. It renders with a dotted rule
                          under it and the reason on hover.

   THE PIECES (all return HTML strings; none of them touch the DOM):

     mv(m) measured(m) mreason(m) isThin(m) isMetric(x) munit(m) mAsOf(m)
     mfmt(m, opts)      plain text, an em dash when unmeasured
     mnum(m, opts)      HTML, toned, with the reason on hover
     unmeasured(why)    the dash on its own
     pctf(fraction)     hub's pct unit; core's older pct() takes PERCENT POINTS
     toneOf(n)          "up" | "down" | "flat"

     sparkline(data, opts)      inline SVG, no library
     tile(o)                    a metric tile, with an optional sparkline
     tileGrid(tiles, opts)      the responsive grid tiles live in
     panel(title, body, opts)   a section card
     dataTable(o)               a table with per-column alignment and an
                                honest empty row
     segmented(o) / wireSegmented(root, fn)   the line|bar|candle control
     emptyState(o)              "nothing yet", on purpose, not broken
     stateChip(state, opts)     ONE strategy-state vocabulary, defined once
     chip(text, tone, title)

   Signatures are documented at each definition. None of the pre-existing
   helpers (el, esc, card, stat, tableHTML, money, sgn, pct, qty, GET, POST,
   api, VIEWS, go, sig) changed.
   ========================================================================= */

/* ------------------------------------------------------------ the envelope */
export const isMetric = (x) =>
  !!x && typeof x === "object" && !Array.isArray(x) && "value" in x && "unit" in x;

/* the number inside, or null. A bare number passed in is returned as-is, so a
   view can hand these helpers either an envelope or a plain float. */
export const mv = (m) => {
  if (isMetric(m)) return m.value === undefined ? null : m.value;
  return m === undefined ? null : m;
};
export const measured = (m) => {
  const v = mv(m);
  return v !== null && v !== undefined && v !== "" && !Number.isNaN(Number(v));
};
export const mreason = (m) => (isMetric(m) ? (m.reason || "") : "");
export const isThin = (m) => !!(isMetric(m) && m.thin);
export const munit = (m) => (isMetric(m) ? (m.unit || "") : "");
export const mAsOf = (m) => (isMetric(m) ? (m.as_of || null) : null);

export const toneOf = (n) => (Number(n) > 0 ? "up" : Number(n) < 0 ? "down" : "flat");

/* One formatter per unit. `dp` overrides the decimal places everywhere. */
const UNIT_FMT = {
  usd:     (v, o) => money(v, o.dp === undefined ? 2 : o.dp),
  pct:     (v, o) => (v * 100).toFixed(o.dp === undefined ? 2 : o.dp) + "%",
  ratio:   (v, o) => Number(v).toFixed(o.dp === undefined ? 2 : o.dp) + "×",
  count:   (v)    => Number(v).toLocaleString(),
  qty:     (v)    => qty(v),
  days:    (v, o) => Number(v).toFixed(o.dp === undefined ? 1 : o.dp) + "d",
  seconds: (v)    => dur(v),
};

/* mfmt(m, {unit, dp, signed, dash}) -> plain text, an em dash when unmeasured */
export function mfmt(m, opts = {}) {
  if (!measured(m)) return opts.dash || "—";
  const v = Number(mv(m));
  const f = UNIT_FMT[opts.unit || munit(m) || "usd"] || ((x) => String(x));
  const s = f(v, opts);
  return (opts.signed && v > 0) ? "+" + s : s;
}

/* mnum(m, {unit, dp, signed, tone, title, dash, reason}) -> HTML.
   `signed` (or tone:"auto") colours by sign -- the ONLY place colour means
   anything. A thin number keeps its reason on hover instead of losing it. */
export function mnum(m, opts = {}) {
  if (!measured(m)) return unmeasured(opts.reason || mreason(m), opts);
  const v = Number(mv(m));
  const cls = ["num"];
  if (opts.signed || opts.tone === "auto") cls.push(toneOf(v));
  else if (opts.tone) cls.push(opts.tone);
  if (isThin(m)) cls.push("thin-num");
  const why = isThin(m)
    ? (mreason(m) || "there is a number here but no sample behind it")
    : (opts.title || "");
  return `<span class="${cls.join(" ")}"${why ? ` title="${esc(why)}"` : ""}>${
    esc(mfmt(m, opts))}</span>`;
}

/* The dash, and why it is a dash. Never a zero. */
export function unmeasured(reason = "", opts = {}) {
  const why = reason || "nobody measured this";
  return `<span class="unmeasured" title="${esc(why)}">${esc(opts.dash || "—")}</span>`;
}

/* hub's `pct` unit is a FRACTION. core's older pct() takes percent points and
   is left alone, because a dozen views already call it that way. */
export function pctf(f, dp = 2) {
  if (f === null || f === undefined || Number.isNaN(Number(f))) {
    return unmeasured("no previous close to compare against");
  }
  const v = Number(f) * 100;
  return `<span class="num ${toneOf(v)}">${v > 0 ? "+" : ""}${v.toFixed(dp)}%</span>`;
}

/* --------------------------------------------------------------- sparkline */
let SPARK_N = 0;
/* sparkline(data, {w, h, tone, fill, why}) -> inline SVG.
   `data` is an array of numbers, or of {c} / {value} objects, so an OHLC
   series from /api/hub/series can be passed straight in and reads its close.
   Fewer than two usable points is NOT a flat line at zero: it is a dotted
   placeholder carrying `why`, because a drawn line would be a claim. */
export function sparkline(data, opts = {}) {
  const w = opts.w || 92, h = opts.h || 26, pad = 2;
  const ys = (data || [])
    .map((p) => (p && typeof p === "object")
      ? Number(p.c !== undefined ? p.c : p.value)
      : Number(p))
    .filter((n) => n !== null && n !== undefined && !Number.isNaN(n));
  if (ys.length < 2) {
    return `<span class="spark spark-none" style="width:${w}px;height:${h}px"
      title="${esc(opts.why || "not enough history to draw a line")}"></span>`;
  }
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const span = (hi - lo) || 1;
  const n = ys.length;
  const X = (i) => pad + (i * (w - pad * 2)) / (n - 1);
  const Y = (v) => h - pad - ((v - lo) / span) * (h - pad * 2);
  const d = ys.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)} ${Y(v).toFixed(1)}`).join(" ");
  const tone = (opts.tone && opts.tone !== "auto") ? opts.tone : toneOf(ys[n - 1] - ys[0]);
  const gid = "spk" + (++SPARK_N);
  const area = `${d} L${X(n - 1).toFixed(1)} ${h} L${X(0).toFixed(1)} ${h} Z`;
  return `<svg class="spark spark-${tone}" viewBox="0 0 ${w} ${h}" width="${w}"
    height="${h}" preserveAspectRatio="none" aria-hidden="true" focusable="false">
    <defs><linearGradient id="${gid}" x1="0" x2="0" y1="0" y2="1">
      <stop offset="0" stop-color="currentColor" stop-opacity=".30"/>
      <stop offset="1" stop-color="currentColor" stop-opacity="0"/></linearGradient></defs>
    ${opts.fill === false ? "" : `<path d="${area}" fill="url(#${gid})" stroke="none"/>`}
    <path d="${d}" fill="none" stroke="currentColor" stroke-width="1.5"
      stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

/* -------------------------------------------------------------- the tile */
/* tile({label, metric|value, html, sub, spark, sparkTone, sparkWhy, signed,
         dp, unit, tone, big, cls, id, go, sym, tab, hint})
   -> one metric tile. `metric` is an envelope; `value` a plain number with
   `unit`; `html` bypasses both when a view has already composed the figure.
   `go`/`sym`/`tab` make the whole tile a router link (app.js delegates every
   [data-go] click), which is how a KPI becomes a way in rather than a label. */
export function tile(o = {}) {
  const m = o.metric !== undefined ? o.metric : o.value;
  const body = o.html !== undefined ? o.html : mnum(m, {
    signed: o.signed, dp: o.dp, unit: o.unit, tone: o.tone, dash: o.dash,
  });
  const spark = o.spark
    ? sparkline(o.spark, { tone: o.sparkTone, w: o.sparkW, h: o.sparkH, why: o.sparkWhy })
    : "";
  const nav = o.go ? ` data-go="${esc(o.go)}"${o.sym ? ` data-sym="${esc(o.sym)}"` : ""}${
    o.tab ? ` data-tab="${esc(o.tab)}"` : ""}` : "";
  const hint = o.hint || (o.html === undefined && !measured(m) ? mreason(m) : "");
  return `<div class="tile${o.big ? " big" : ""}${o.go ? " tile-go" : ""}${
    o.cls ? " " + o.cls : ""}"${o.id ? ` id="${esc(o.id)}"` : ""}${nav}${
    hint ? ` title="${esc(hint)}"` : ""}>
    <div class="tile-k">${o.label || ""}</div>
    <div class="tile-v">${body}</div>
    ${o.sub ? `<div class="tile-s">${o.sub}</div>` : ""}
    ${spark ? `<div class="tile-spark">${spark}</div>` : ""}
  </div>`;
}

/* tileGrid(tiles, {cols, cls}) -- `cols` is the MAXIMUM across; the grid
   falls to fewer on its own as the page narrows, down to two on a phone. */
export const tileGrid = (tiles, opts = {}) =>
  `<div class="tile-grid${opts.cls ? " " + opts.cls : ""}"${
    opts.cols ? ` style="--tile-cols:${opts.cols}"` : ""}>${
    Array.isArray(tiles) ? tiles.join("") : tiles}</div>`;

/* ------------------------------------------------------------- the panel */
/* panel(title, body, {sub, titleHint, actions, flush, cls, id}) -- the
   section card. `card()` above is the OLD one and is unchanged; views migrate
   at their own pace. A panel differs in having a sub-line under its title and
   a proper header slot, which is where a segmented control belongs.

   `titleHint` IS THE SUB-LINE THAT DOES NOT PRINT. Added in round 6, when the
   owner's complaint was that every heading on the dashboard carried a
   sentence explaining the block under it. A sub-line that names a REAL fact
   ("2 of 3 strategies active") is worth its line; a sub-line that defines the
   panel it sits on is not, and it belongs on hover. Views pass one or the
   other, never both.

   ROUND 7: AND "ON HOVER" IS NOT GOOD ENOUGH ON ITS OWN. A title attribute
   does not exist on a phone and a keyboard user never reaches it, so a panel
   that carries a hint is marked `data-why` here and reason.js turns it into a
   real disclosure -- focusable, openable by tap and by Enter -- wherever a
   room has called `wireReasons`. The heading stays a heading; reason.js is
   careful not to put role="button" on it. Nothing changes for a room that has
   not wired it: the attribute is inert and the title still hovers. */
export function panel(title, body, opts = {}) {
  const head = (title || opts.sub || opts.actions)
    ? `<header class="panel-h">
         <div class="panel-tt">
           ${title ? `<h2 class="panel-t"${opts.titleHint
             ? ` data-why title="${esc(opts.titleHint)}"` : ""}>${title}</h2>` : ""}
           ${opts.sub ? `<div class="panel-sub">${opts.sub}</div>` : ""}
         </div>
         ${opts.actions ? `<div class="panel-x">${opts.actions}</div>` : ""}
       </header>`
    : "";
  return `<section class="panel${opts.cls ? " " + opts.cls : ""}"${
    opts.id ? ` id="${esc(opts.id)}"` : ""}>${head}
    <div class="panel-b${opts.flush ? " flush" : ""}">${body}</div></section>`;
}

/* -------------------------------------------------------------- the table */
/* dataTable({cols, rows, empty, dense, id, cls})
     cols  ["Symbol", {label:"Value", num:true, title:"...", w:"90px"}]
     rows  either arrays of cell HTML (aligned by the column spec) or whole
           "<tr>...</tr>" strings for a row that needs its own attributes.
   An empty table says what would be there, not "Nothing here." */
export function dataTable(o = {}) {
  const cols = (o.cols || []).map((c) => (typeof c === "string" ? { label: c } : c));
  const heads = cols.map((c) =>
    `<th${c.num ? ` class="dt-n"` : (c.cls ? ` class="${c.cls}"` : "")}${
      c.title ? ` title="${esc(c.title)}"` : ""}${
      c.w ? ` style="width:${c.w}"` : ""}>${c.label === undefined ? "" : c.label}</th>`).join("");
  const rows = o.rows || [];
  const body = rows.length
    ? rows.map((r) => Array.isArray(r)
        ? `<tr>${r.map((cell, i) =>
            `<td${cols[i] && cols[i].num ? ` class="dt-n num"` : ""}>${cell}</td>`).join("")}</tr>`
        : r).join("")
    : `<tr><td colspan="${cols.length || 1}" class="empty">${
        o.empty || "Nothing to show yet."}</td></tr>`;
  return `<div class="tw dt${o.dense ? " dense" : ""}${o.cls ? " " + o.cls : ""}"${
    o.id ? ` id="${esc(o.id)}"` : ""}><table>
    <thead><tr>${heads}</tr></thead><tbody>${body}</tbody></table></div>`;
}

/* --------------------------------------------------- the segmented control */
/* segmented({options, value, id, name, size, label})
     options  ["line", ["candle", "Candle", "tooltip"]]
   Markup is the SAME .seg/.seg-b the backtester already styles, so this is a
   constructor for an existing control rather than a second one. `size` may be
   "sm". Pair with wireSegmented(rootOrId, (value, button) => ...). */
export function segmented(o = {}) {
  const opts = (o.options || []).map((x) => (Array.isArray(x) ? x : [x, x]));
  return `<div class="seg${o.size ? " " + o.size : ""}"${o.id ? ` id="${esc(o.id)}"` : ""}${
    o.name ? ` data-seg="${esc(o.name)}"` : ""} role="tablist"${
    o.label ? ` aria-label="${esc(o.label)}"` : ""}>${
    opts.map(([v, l, t]) => `<button type="button" class="seg-b${
      String(v) === String(o.value) ? " on" : ""}" data-v="${esc(v)}" role="tab"
      aria-selected="${String(v) === String(o.value) ? "true" : "false"}"${
      t ? ` title="${esc(t)}"` : ""}>${l === undefined ? esc(v) : l}</button>`).join("")}</div>`;
}
export function wireSegmented(root, onPick) {
  const r = typeof root === "string" ? el(root) : root;
  if (!r) return;
  r.querySelectorAll(".seg-b").forEach((b) => {
    b.onclick = () => {
      if (b.classList.contains("on")) return;
      r.querySelectorAll(".seg-b").forEach((x) => {
        x.classList.toggle("on", x === b);
        x.setAttribute("aria-selected", x === b ? "true" : "false");
      });
      if (onPick) onPick(b.dataset.v, b);
    };
  });
}

/* ------------------------------------------------------- the empty state */
/* emptyState({title, body, action, icon, cls}) -- an account with nothing in
   it must look DELIBERATE. A grid of zeroes looks like a bug, and this repo
   has shipped that bug before. */
export function emptyState(o = {}) {
  return `<div class="blank${o.cls ? " " + o.cls : ""}">
    ${o.icon ? `<div class="blank-i" aria-hidden="true">${o.icon}</div>` : ""}
    <div class="blank-t">${o.title || "Nothing here yet"}</div>
    ${o.body ? `<div class="blank-b">${o.body}</div>` : ""}
    ${o.action ? `<div class="blank-a">${o.action}</div>` : ""}</div>`;
}

/* ------------------------------------------------- the state vocabulary ---
   ONE set of words for what a strategy is doing, defined here and nowhere
   else. This is the fix for "armed": the shell used to mean "a ladder engine
   is armed" and the options tab "the playbook may open", and both words were
   on screen at once meaning different things. These are hub.py's own state
   words and the shell never invents a seventh. */
export const STATE_WORDS = {
  armed:   { tone: "armed",  label: "armed",
             why: "Transmits REAL orders at the broker." },
  live:    { tone: "live",   label: "live",
             /* NO SUBSYSTEM IN THE DEFINITION. This used to read "the
                ladder says this while it is still in dry run; an options play
                says it once it is assigned and enabled" -- accurate, and it
                put the word "ladder" into the hover of a chip on a ticker
                whose only strategy was an options play. One word, one
                meaning, named after neither. */
             why: "Switched on and deciding. It is not armed, so nothing it "
                + "decides reaches the broker." },
  idle:    { tone: "idle",   label: "idle",
             why: "Attached to this ticker but not deciding right now." },
  halted:  { tone: "halt",   label: "halted",
             why: "It stopped itself on a problem and wants a human." },
  adopted: { tone: "adopt",  label: "adopted",
             why: "Positions are open here that this strategy did not place." },
  off:     { tone: "off",    label: "off",
             why: "Attached to nothing." },
  error:   { tone: "halt",   label: "error",
             why: "This strategy could not be read." },
};
/* stateChip(state, {sub, title, sm}) -> the pill. An unknown word renders as
   itself rather than being swallowed, so a new strategy kind is visible on
   day one instead of silently reading "off". */
export function stateChip(state, o = {}) {
  const k = String(state || "off").toLowerCase();
  const d = STATE_WORDS[k] || { tone: "idle", label: k, why: "" };
  return `<span class="chip st-${d.tone}${o.sm ? " sm" : ""}" title="${
    esc(o.title || d.why)}">${esc(d.label)}${
    o.sub ? `<b class="chip-s">${esc(o.sub)}</b>` : ""}</span>`;
}
/* chip(text, tone, title) -- the generic one. tone: "" | up | down | warn |
   accent | mute. */
export const chip = (text, tone = "", title = "") =>
  `<span class="chip${tone ? " ch-" + tone : ""}"${
    title ? ` title="${esc(title)}"` : ""}>${text}</span>`;
