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

export const card = (title, body, extra = "", opts = {}) =>
  `<div class="card"${opts.id ? ` id="${opts.id}"` : ""}>
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
async function req(method, path, body) {
  const r = await fetch(path, {
    method,
    headers: body !== undefined ? { "content-type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    cache: "no-store",
  });
  const txt = await r.text();
  if (!r.ok) {
    let m = txt;
    try { m = JSON.parse(txt).detail || txt; } catch (e) { /* plain text */ }
    throw new Error(m || r.statusText);
  }
  return txt ? JSON.parse(txt) : {};
}
export const GET  = (p) => req("GET", p);
export const POST = (p, b = {}) => req("POST", p, b);
export const DEL  = (p) => req("DELETE", p);

/* --------------------------------------------------------------- state */
export const S = {
  view: { kind: "overview" },
  ov: null,          // /api/overview
  ticker: null,      // /api/ticker/<sym>
  cache: {},         // per-view scratch
  timer: null,
  mounted: "",
  touched: false,    // a form is being typed in; do not repaint over it
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

/* -------------------------------------------------------------- router */
export const VIEWS = {};        // kind -> { title, sub, mount, paint, tabs? }

export function go(view) {
  S.view = view;
  S.ticker = null;
  S.touched = false;
  const h = view.kind === "ticker"
    ? `#/t/${view.sym}/${view.tab || "live"}`
    : view.kind === "overview" ? "#/" : `#/${view.kind}${view.tab ? "/" + view.tab : ""}`;
  if (location.hash !== h) location.hash = h;
  else window.__render(true);
}

export function readHash() {
  const h = (location.hash || "#/").slice(2).split("/").filter(Boolean);
  if (h[0] === "t" && h[1]) {
    return { kind: "ticker", sym: h[1].toUpperCase(), tab: h[2] || "live" };
  }
  if (h[0] && VIEWS[h[0]]) return { kind: h[0], tab: h[1] || "" };
  return { kind: "overview" };
}

export const sig = (v) => v.kind === "ticker"
  ? `ticker:${v.sym}:${v.tab || "live"}` : `${v.kind}:${v.tab || ""}`;

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
