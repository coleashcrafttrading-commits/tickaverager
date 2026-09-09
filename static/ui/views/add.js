/* ============================================================================
   Add a ticker -- search, inspect, size it, connect it.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, act, ask, toast, el, esc, card, stat,
  money, money0, px, go, hashFor,
} from "../core.js";
import { formHTML, formPatch } from "../fields.js";

const st = { q: "", picked: null, info: null, copyFrom: "" };
let forAcct = "";     // whose fleet the candidate above was checked against

VIEWS.add = {
  title: () => "Add a ticker",
  sub: () => "look one up, size it, connect it",

  mount() {
    if (forAcct !== S.account) {
      // "in fleet" and the copy-from template are facts about ONE account
      Object.assign(st, { q: "", picked: null, info: null, copyFrom: "" });
      forAcct = S.account;
    }
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${card("1 · Find the symbol", `
            <input id="aq" placeholder="Ticker or company name — NVDA, Tesla…"
              style="font-size:15px;padding:11px 13px" autocomplete="off" spellcheck="false">
            <div class="hint" id="aqHint" style="margin-top:8px"></div>
            <div id="aRes" style="margin-top:12px"></div>`)}
          <div id="aCfgCard" style="display:none">
            ${card(`3 · Configure <span id="aCfgSym"></span>`, `<form id="aform"></form>`)}
          </div>
        </div>
        <div>
          ${card("2 · The candidate", `<div id="aPick" class="faint">Pick a symbol.</div>`)}
          <div id="aTpl" style="display:none">
            ${card("Start from", `
              <label class="f"><span>Template</span><select id="aCopy"></select></label>
              <div class="hint">Copying brings across every strategy setting, then you
                tune it below.</div>
              <div class="stats" id="aMath"></div>
              <button class="btn primary" id="aAdd" style="width:100%;margin-top:16px">
                Add to the fleet</button>
              <div class="tip">It arrives <b>stopped</b> and in <b>dry run</b>.
                Nothing transmits until you arm it.</div>`)}
          </div>
        </div>
      </div>`;

    const q = el("aq");
    q.focus();
    let t;
    q.addEventListener("input", () => {
      clearTimeout(t);
      st.q = q.value;
      el("aqHint").textContent = "searching…";
      t = setTimeout(search, 260);
    });
    el("aCopy").addEventListener("change", (e) => {
      st.copyFrom = e.target.value;
      template();
    });
    el("aAdd").onclick = () => act(addIt);
    search();
  },

  paint() { /* driven by its own interactions */ },
};

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
      <div class="nav-item" data-pick="${x.symbol}"
           style="border:1px solid var(--hairline);margin-bottom:6px">
        <b style="width:76px;flex:none">${x.symbol}</b>
        <span class="faint" style="overflow:hidden;text-overflow:ellipsis;
          white-space:nowrap">${esc(x.name || "")}</span>
        <span class="tail">${x.in_fleet ? `<span class="pill acc">in fleet</span>`
          : esc(x.exchange || "")}</span></div>`).join("")
      : `<div class="empty">Nothing matched “${esc(q)}”.</div>`;
    el("aRes").querySelectorAll("[data-pick]").forEach((n) => {
      n.onclick = () => pick(n.dataset.pick);
    });
  } catch (e) {
    el("aqHint").innerHTML = `<span class="down">${esc(e.message)}</span>`;
  }
}

async function pick(sym) {
  st.picked = sym;
  el("aPick").innerHTML = `<span class="faint">Checking ${esc(sym)}…</span>`;
  try {
    const r = await GET("/api/inspect/" + encodeURIComponent(sym));
    st.info = r;
    if (!r.ok) {
      el("aPick").innerHTML = `<div class="note bad" style="margin:0">${esc(r.msg)}</div>`;
      el("aTpl").style.display = el("aCfgCard").style.display = "none";
      return;
    }
    const spread = (r.bid && r.ask) ? r.ask - r.bid : 0;
    el("aPick").innerHTML = `
      <div style="display:flex;align-items:baseline;gap:12px;margin-bottom:6px">
        <span style="font-size:22px;font-weight:650">${r.symbol}</span>
        <span style="font-size:19px;font-weight:600">${px(r.price)}</span></div>
      <div class="faint" style="margin-bottom:16px">${esc(r.name || "")}</div>
      <div class="stats">
        ${stat("Exchange", esc(r.exchange || "—"))}
        ${stat("Bid / ask", `${r.bid ? r.bid.toFixed(2) : "—"} / ${r.ask ? r.ask.toFixed(2) : "—"}`,
               spread ? `spread $${spread.toFixed(3)}` : "")}
        ${stat("Fractionable", r.fractionable ? "yes" : "no")}
      </div>
      ${r.in_fleet ? `<div class="note warn" style="margin-top:14px">${r.symbol} is
        already in the fleet — <a href="${hashFor({ kind: "ticker", sym: r.symbol, tab: "settings" })}">open its settings</a>.
        </div>` : ""}`;
    if (r.in_fleet) {
      el("aTpl").style.display = el("aCfgCard").style.display = "none";
      return;
    }
    el("aTpl").style.display = el("aCfgCard").style.display = "";
    el("aCfgSym").textContent = r.symbol;
    el("aCopy").innerHTML = ['<option value="">Basic $0.10 ladder (the default strategy)</option>']
      .concat((S.ov && S.ov.tickers || []).map((t) =>
        `<option value="${t.symbol}">Copy ${t.symbol}</option>`)).join("");
    el("aCopy").value = st.copyFrom || "";
    await template();
  } catch (e) {
    el("aPick").innerHTML = `<div class="note bad" style="margin:0">${esc(e.message)}</div>`;
  }
}

async function template() {
  let cfg;
  try {
    if (st.copyFrom) {
      cfg = (await GET("/api/ticker/" + st.copyFrom)).config;
    } else {
      // what a new ticker actually gets: the defaults with the default preset on top
      const [st_, pr] = await Promise.all([GET("/api/settings"), GET("/api/presets")]);
      const d = (pr.presets || []).find((p) => p.id === pr.default);
      cfg = Object.assign({}, st_.ticker_defaults, d ? d.settings : {}, { preset: pr.default });
    }
  } catch (e) { toast(esc(e.message), "err"); return; }
  cfg = Object.assign({}, cfg,
    { symbol: st.picked, dry_run: true, autostart: false, notes: "" });
  const f = el("aform");
  if (!f) return;
  f.innerHTML = formHTML(cfg, { omit: ["symbol"] });
  f.addEventListener("input", math);
  math();
}

function math() {
  const f = el("aform"), r = st.info;
  if (!f || !r) return;
  const p = formPatch(f);
  const price = r.price || 0;
  const per = (Number(p.shares_per_lot) || 0) * price;
  const max = per * (Number(p.max_lots) || 0);
  const win = (Number(p.take_profit) || 0) * (Number(p.shares_per_lot) || 0);
  const spread = (r.bid && r.ask) ? r.ask - r.bid : 0;
  const tp = Number(p.take_profit) || 0;
  const bp = S.ov ? S.ov.portfolio.buying_power : 0;
  const share = bp ? Math.round(100 * max / bp) : 0;
  const spreadPct = tp ? Math.round(100 * spread / tp) : 0;

  el("aMath").innerHTML =
    stat("Per lot", money(per), `${p.shares_per_lot || 0} sh @ ${px(price)}`)
    + stat("Max exposure", money(max), `${p.max_lots || 0} lots`)
    + stat("Win per lot", money(win), "before fees")
    + stat("Of buying power", bp
        ? `<span class="${max > bp ? "down" : max > bp * 0.5 ? "warn" : "up"}">${share}%</span>`
        : "—", bp ? (max > bp ? "more than the account has" : "have " + money0(bp)) : "")
    + stat("Spread vs target", spread
        ? `<span class="${spreadPct > 40 ? "down" : spreadPct > 20 ? "warn" : "up"}">${spreadPct}%</span>`
        : "—", spread ? `$${spread.toFixed(3)} spread` : "no quote");
}

async function addIt() {
  const r = st.info;
  if (!r || !r.ok) return;
  const cfg = formPatch(el("aform"));
  const per = (Number(cfg.shares_per_lot) || 0) * (r.price || 0);
  const max = per * (Number(cfg.max_lots) || 0);
  if (!await ask({
    title: `Add ${r.symbol}?`, ok: "Add ticker",
    body: `A new ladder with its own ledger and its own settings.<br><br>
      Each lot ≈ <b>${money(per)}</b>, up to <b>${cfg.max_lots}</b> lots ≈
      <b>${money(max)}</b>.<br><br>
      It arrives <b>stopped and in dry run</b> — nothing transmits until you arm it.`,
  })) return;
  await POST("/api/tickers",
    { symbol: r.symbol, copy_from: st.copyFrom || "", config: cfg });
  toast(`${r.symbol} added — stopped and in dry run.`, "ok");
  await window.__tick();
  go({ kind: "ticker", sym: r.symbol, tab: "live" });
}
