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
   and is left alone. /api/risk is exposure (per account) while /api/risk/
   profiles and /api/risk/bank are shared, which is why the match is on whole
   path segments and not a bare startsWith. */
export const SHARED_API = [
  "/api/accounts", "/api/strategies", "/api/code", "/api/indicators",
  "/api/scanner", "/api/pine", "/api/research", "/api/risk/profiles",
  "/api/risk/bank", "/api/health", "/api/restart", "/api/presets",
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

/* ---- the four fleet-wide actions ----------------------------------------
   Rendered on Portfolio and nowhere else. Settings used to carry a
   byte-for-byte copy; it links to Portfolio now. Every confirmation names
   the account, so nobody arms the wrong one. */
export const fleetControlsHTML = () => `
  <span class="row-btns fleet-btns">
    <button class="btn sm" data-fleet="start">Start all</button>
    <button class="btn sm" data-fleet="stop">Stop all</button>
    <button class="btn sm" data-fleet="disarm">Disarm all</button>
    <button class="btn sm danger" data-fleet="panic">Panic</button>
  </span>`;

const FLEET = {
  start: async () => {
    const who = acctLabel();
    if (!await ask({
      title: `Start every engine in ${esc(who)}?`,
      body: `Each ladder in <b>${esc(who)}</b> (${esc(acctNumber() || "—")}) begins `
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
      title: `Stop and disarm everything in ${esc(who)}?`, danger: true, ok: "Panic",
      requireWord: "PANIC",
      body: `Every engine in <b>${esc(who)}</b> (${esc(acctNumber() || "—")}) stops and `
          + `every ladder returns to dry run. Other accounts are untouched.<br><br>`
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
    return `<div class="note good" style="margin-top:0"><b>Connected to a model.</b>
      Authenticating with ${esc(AUTH_NAME[how] || how || "the Claude Code CLI")}.
      ${test ? `<button class="btn sm" id="${id}" style="margin-left:8px">Test it</button>
        <span id="${id}Out" class="faint"></span>` : ""}</div>`;
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
   Eleven nav destinations became six. Every URL the old ones answered still
   resolves -- to wherever its content lives now, never to a 404 and never to
   a page with no highlighted nav item. app.js rewrites the address bar on
   arrival, so an old bookmark quietly upgrades itself the first time it is
   used. Keyed "kind" or "kind/tab"; the longer key wins. */
export const MOVED = {
  // Performance is Portfolio's History tab
  "performance":       { kind: "overview", tab: "history" },
  "portfolio":         { kind: "overview", tab: "" },        // the home page
  // Agents is a Settings tab
  "agents":            { kind: "settings", tab: "agents" },
  // the strategy builder is a Research tab
  "strategies":        { kind: "research", tab: "builder" },
  // the two orphan routes: registered, routable, and in no nav at all
  "backtest":          { kind: "research", tab: "backtest" },
  "tester":            { kind: "research", tab: "backtest" },
  "research/tester":   { kind: "research", tab: "backtest" },
  // Risk keeps only live exposure; its two research artefacts moved
  "risk/live":         { kind: "risk",     tab: "" },
  "risk/profiles":     { kind: "research", tab: "profiles" },
  "risk/bank":         { kind: "research", tab: "bank" },
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
