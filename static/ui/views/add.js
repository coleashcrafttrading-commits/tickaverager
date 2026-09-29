/* ============================================================================
   Add a ticker -- and ONLY a ticker.

   This page used to be "configure a ladder". It opened a 72-field form, did
   per-lot and max-exposure arithmetic, and POSTed to /api/tickers, which
   builds an Engine and writes ladder settings into config.json. Adding a
   symbol created a strategy, which is the complaint the owner led with.

   It does one thing now: POST /api/hub/ticker, which writes one row to the
   account's watchlist and builds nothing. A ticker with no strategy is a
   perfectly good ticker -- it carries market data and a record either way --
   and attaching a strategy is a separate, deliberate second step on the
   ticker's own Strategies tab.

   The one convenience kept is the optional "and attach" picker: it is OFF by
   default, it is a SECOND request after the add has succeeded, and it never
   arms anything. If it fails, the ticker is still added and the page says so
   rather than rolling back a thing the owner asked for.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, act, ask, toast, el, esc, panel, chip, px, go, hashFor,
  acctLabel,
} from "../core.js";
import { ensureCSS } from "../tkstyle.js";

const st = {
  q: "", picked: null, info: null, attach: "",
  known: null,        // symbols already on this account, from /api/hub/tickers
  strategies: null,   // the catalogue, from /api/hub/strategies
  adding: false,
};
let forAcct = "";     // whose account the candidate above was checked against

VIEWS.add = {
  title: () => "Add a ticker",
  sub: () => "a symbol is just a symbol — strategies come afterwards",

  mount() {
    if (forAcct !== S.account) {
      // "already here" and the strategy catalogue are facts about ONE account
      Object.assign(st, { q: "", picked: null, info: null, attach: "",
                          known: null, strategies: null });
      forAcct = S.account;
    }
    ensureCSS();
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${panel("1 · Find the symbol", `
            <input id="aq" class="big" placeholder="Ticker or company name — NVDA, Tesla…"
              style="padding:11px 13px" autocomplete="off" spellcheck="false">
            <div class="hint" id="aqHint" style="margin-top:8px"></div>
            <div id="aRes" style="margin-top:12px"></div>`)}
          ${panel("What adding actually does", `
            <div class="tip" style="margin-top:0">
              It writes <b>one row</b> to ${esc(acctLabel())}'s ticker list.
              No engine is built, no settings file is touched, and
              <b>no order is placed</b>.<br><br>
              The ticker then carries its own market data, its own chart and its
              own record whether or not anything trades it. Strategies — the DCA
              ladder, an options play, whatever comes next — are attached to it
              one at a time, each with its own settings, on the ticker's
              <b>Strategies</b> tab.
            </div>`)}
        </div>
        <div>
          ${panel("2 · The candidate", `<div id="aPick" class="faint">Pick a symbol.</div>`)}
          <div id="aGo" style="display:none">
            ${panel("3 · Add it", `
              <label class="f"><span>Attach a strategy now (optional)</span>
                <select id="aAttach"><option value="">No — watchlist only</option></select></label>
              <div class="hint" id="aAttachWhy">A ticker with no strategy is a
                perfectly good ticker.</div>
              <button class="btn primary" id="aAdd" style="width:100%;margin-top:16px">
                Add to <span id="aAcct"></span></button>
              <div class="tip">Attaching never arms. A ladder arrives
                <b>stopped and in dry run</b>; an options play is assigned and the
                arm file is not touched.</div>`)}
          </div>
        </div>
      </div>`;

    el("aAcct").textContent = acctLabel();

    const q = el("aq");
    q.focus();
    let t;
    q.addEventListener("input", () => {
      clearTimeout(t);
      st.q = q.value;
      el("aqHint").textContent = "searching…";
      t = setTimeout(search, 260);
    });
    el("aAttach").addEventListener("change", (e) => {
      st.attach = e.target.value;
      const s = (st.strategies || []).find((x) => x.id === st.attach);
      el("aAttachWhy").textContent = s
        ? `${s.label} (${s.kind}) is attached right after the ticker is added, `
          + `with its own defaults. It is not armed and it places no order.`
        : "A ticker with no strategy is a perfectly good ticker.";
    });
    el("aAdd").onclick = () => act(addIt);

    loadContext();
    search();
  },

  paint() { /* driven by its own interactions */ },
};

/* What this account already has, and what can be attached. Both are read once
   per mount: neither changes while somebody types a ticker into a box. */
async function loadContext() {
  try {
    const r = await GET("/api/hub/tickers");
    st.known = new Set((r.tickers || []).map((x) => String(x.symbol).toUpperCase()));
  } catch (e) {
    st.known = null;          // unknown is not the same as empty; say so below
  }
  try {
    const r = await GET("/api/hub/strategies");
    st.strategies = r.strategies || [];
  } catch (e) {
    st.strategies = [];
  }
  const sel = el("aAttach");
  if (!sel) return;
  sel.innerHTML = ['<option value="">No — watchlist only</option>']
    .concat((st.strategies || []).map((s) =>
      `<option value="${esc(s.id)}">${esc(s.label)} — ${esc(s.kind)}</option>`))
    .join("");
  sel.value = st.attach || "";
}

const alreadyHere = (sym) =>
  st.known ? st.known.has(String(sym).toUpperCase()) : false;

async function search() {
  const q = (st.q || "").trim();
  if (!q) {
    el("aRes").innerHTML = "";
    el("aqHint").textContent = "The whole tradable universe is searchable.";
    return;
  }
  try {
    const r = await GET("/api/search?q=" + encodeURIComponent(q));
    el("aqHint").textContent = r.ready ? `${r.results.length} match(es)`
      : r.loading ? "loading the symbol list… exact tickers work already"
      : "exact tickers work already";
    el("aRes").innerHTML = r.results.length ? r.results.map((x) => `
      <div class="nav-item" data-pick="${esc(x.symbol)}"
           style="border:1px solid var(--hairline);margin-bottom:6px">
        <b style="width:76px;flex:none">${esc(x.symbol)}</b>
        <span class="faint" style="overflow:hidden;text-overflow:ellipsis;
          white-space:nowrap">${esc(x.name || "")}</span>
        <span class="tail">${alreadyHere(x.symbol)
          ? chip("already here", "accent", "this symbol is already on this account")
          : esc(x.exchange || "")}</span></div>`).join("")
      : `<div class="empty">Nothing matched “${esc(q)}”.</div>`;
    el("aRes").querySelectorAll("[data-pick]").forEach((n) => {
      n.onclick = () => pick(n.dataset.pick);
    });
  } catch (e) {
    el("aqHint").innerHTML = `<span class="down">${esc(e.message)}</span>`;
  }
}

/* The candidate card is MARKET data and nothing else. It used to carry per-lot
   and max-exposure arithmetic, which are the ladder's numbers and meant the
   first thing anybody saw when adding a symbol was a ladder being sized. */
async function pick(sym) {
  st.picked = sym;
  el("aPick").innerHTML = `<span class="faint">Checking ${esc(sym)}…</span>`;
  try {
    const r = await GET("/api/inspect/" + encodeURIComponent(sym));
    st.info = r;
    if (!r.ok) {
      el("aPick").innerHTML = `<div class="note bad" style="margin:0">${esc(r.msg)}</div>`;
      el("aGo").style.display = "none";
      return;
    }
    const spread = (r.bid && r.ask) ? r.ask - r.bid : 0;
    const here = alreadyHere(r.symbol);
    el("aPick").innerHTML = `
      <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:6px">
        <span style="font-size:22px;font-weight:650">${esc(r.symbol)}</span>
        <span style="font-size:19px;font-weight:600">${px(r.price)}</span></div>
      <div class="faint" style="margin-bottom:16px">${esc(r.name || "")}</div>
      <dl class="tkx-kv" style="font-size:12.5px">
        <dt>Exchange</dt><dd>${esc(r.exchange || "—")}</dd>
        <dt>Bid / ask</dt><dd>${r.bid ? r.bid.toFixed(2) : "—"} / ${r.ask ? r.ask.toFixed(2) : "—"}</dd>
        <dt>Spread</dt><dd>${spread ? "$" + spread.toFixed(3) : "—"}</dd>
        <dt>Fractionable</dt><dd>${r.fractionable ? "yes" : "no"}</dd>
        ${r.fractionable && (r.min_trade_increment || r.min_order_size)
          ? `<dt>Minimum</dt><dd>${esc(String(r.min_order_size || "—"))} sh
             · step ${esc(String(r.min_trade_increment || "—"))}</dd>` : ""}
      </dl>
      ${st.known === null ? `<div class="note warn" style="margin-top:14px">
        This account's ticker list could not be read, so “already here” is
        unknown. Adding an existing symbol is harmless — it rewrites the same
        row.</div>` : ""}
      ${here ? `<div class="note info" style="margin-top:14px">${esc(r.symbol)} is
        already on ${esc(acctLabel())} —
        <a href="${hashFor({ kind: "ticker", sym: r.symbol, tab: "strategies" })}">open
        its strategies</a>.</div>` : ""}`;
    el("aGo").style.display = here ? "none" : "";
  } catch (e) {
    el("aPick").innerHTML = `<div class="note bad" style="margin:0">${esc(e.message)}</div>`;
    el("aGo").style.display = "none";
  }
}

async function addIt() {
  const r = st.info;
  if (!r || !r.ok || st.adding) return;
  const sid = st.attach;
  const s = (st.strategies || []).find((x) => x.id === sid);
  if (!await ask({
    title: `Add ${r.symbol} to ${esc(acctLabel())}?`, ok: "Add ticker",
    body: `One row on the ticker list. <b>No engine is built and no order is
      placed.</b><br><br>${s
        ? `<b>${esc(s.label)}</b> is then attached to it — stopped, unarmed, on
           its own defaults. You size it on ${esc(r.symbol)}'s own page.`
        : `<b>No strategy is attached.</b> ${esc(r.symbol)} arrives as a
           watchlist row with live market data, a chart and a record.`}`,
  })) return;

  st.adding = true;
  el("aAdd").disabled = true;
  try {
    await POST("/api/hub/ticker", { symbol: r.symbol, by: "dashboard" });
  } finally {
    st.adding = false;
    const b = el("aAdd");
    if (b) b.disabled = false;
  }

  let attached = false;
  if (sid) {
    // a second request, after the first succeeded. A failure here leaves the
    // ticker added -- which is what was asked for -- and says so, rather than
    // pretending the whole thing failed or silently rolling it back.
    try {
      await POST(`/api/hub/ticker/${encodeURIComponent(r.symbol)}/strategy`,
                 { strategy: sid, action: "attach", by: "dashboard" });
      attached = true;
    } catch (e) {
      toast(`${r.symbol} was added, but <b>${esc(s ? s.label : sid)} could not be
        attached</b> — ${esc(e.message)}. Attach it from its Strategies tab.`,
        "err", 9000);
    }
  }
  if (st.known) st.known.add(String(r.symbol).toUpperCase());
  toast(attached
    ? `${r.symbol} added and ${esc(s.label)} attached — not armed.`
    : `${r.symbol} added. No strategy is attached.`, "ok");
  /* The shell's ticker list is the HUB's, and it is cached for 20 s. Landing
     on a ticker the shell has not heard of yet gets bounced straight back to
     Portfolio -- measured in a browser, not guessed. Forcing that one slow
     poll before navigating is the whole fix, and it is guarded because the
     shell owns that function and may rename it. */
  if (window.__hubTick) { try { await window.__hubTick(true); } catch (e) { /* the page still works */ } }
  await window.__tick();
  go({ kind: "ticker", sym: r.symbol, tab: sid ? "strategies" : "live" });
}
