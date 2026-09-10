/* ============================================================================
   Performance -- what the ladders have actually done, and printable reports.

   Realized P/L is always shown NEXT TO open inventory and its age. This
   strategy has no stop loss, so realized alone looks excellent right up until
   the day it doesn't; the age and size of what is still open is where the risk
   lives.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, toast, el, esc, card, stat, tableHTML,
  money, money0, sgn, pct, px, qty, dur, go,
} from "../core.js";

let perf = null;
let reports = [];
let scope = { symbol: "", days: 7 };
let forAcct = "";       // the journal, reports and symbol filter are per account

VIEWS.performance = {
  title: () => "Performance",
  sub: () => "from the append-only trade journal",

  mount() {
    if (forAcct !== S.account) {
      perf = null; reports = []; scope = { symbol: "", days: 7 };
      forAcct = S.account;
    }
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${card("Results", `
            <div style="display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap">
              <select id="pfSym" style="width:auto"></select>
              <select id="pfDays" style="width:auto">
                <option value="1">today</option>
                <option value="7" selected>7 days</option>
                <option value="30">30 days</option>
                <option value="0">all time</option>
              </select>
              <span class="faint" id="pfRows" style="align-self:center"></span>
            </div>
            <div class="stats" id="pfStats"></div>
            <div class="tip" id="pfNote"></div>`)}
          ${card("By ladder rung", `<div id="pfRungs"></div>`,
            "where the capital goes", { flush: true })}
          ${card("Recent trades", `<div id="pfTrades"></div>`, "", { flush: true })}
        </div>
        <div>
          ${card("Reports", `
            <div class="tip" style="margin-top:0">A printable PDF of everything on
              this page. An agent can generate these on a schedule too — the same
              endpoint.</div>
            <div class="row-btns" style="margin:12px 0">
              <button class="btn primary sm" data-rep="daily">Daily</button>
              <button class="btn sm" data-rep="weekly">Weekly</button>
              <button class="btn sm" data-rep="inventory">Inventory</button>
              <button class="btn sm" data-rep="full">Full history</button>
            </div>
            <div id="pfReports"></div>`)}
          ${card("Open inventory", `<div id="pfInv"></div>`,
            `<span id="pfInvN"></span>`, { flush: true })}
        </div>
      </div>`;

    el("pfSym").onchange = () => { scope.symbol = el("pfSym").value; load(); };
    el("pfDays").onchange = () => { scope.days = Number(el("pfDays").value); load(); };
    el("view").querySelectorAll("[data-rep]").forEach((b) => {
      b.onclick = () => act(async () => {
        b.disabled = true;
        b.textContent = "Building…";
        try {
          const r = await POST("/api/reports", { kind: b.dataset.rep });
          toast(`Report ready — <a href="${r.url}" target="_blank">${esc(r.name)}</a>`,
                "ok", 12000);
          // opens in a tab with a real title and draws its own charts; the
          // PDF used to arrive as an attachment and left a blank tab behind
          window.open(r.url, "_blank");
          await loadReports();
        } finally {
          b.disabled = false;
          b.textContent = b.dataset.rep === "full" ? "Full history"
            : b.dataset.rep[0].toUpperCase() + b.dataset.rep.slice(1);
        }
      });
    });
    load();
    loadReports();
  },

  paint() {
    const sel = el("pfSym");
    if (sel && !sel.options.length && S.ov) {
      sel.innerHTML = `<option value="">All tickers</option>`
        + S.ov.tickers.map((t) => `<option value="${t.symbol}">${t.symbol}</option>`).join("");
      sel.value = scope.symbol;
    }
    if (perf) render();
  },
};

async function load() {
  try {
    perf = await GET(`/api/performance?symbol=${encodeURIComponent(scope.symbol)}`
                   + `&days=${scope.days}`);
  } catch (e) { toast(esc(e.message), "err"); return; }
  render();
}

async function loadReports() {
  try {
    const r = await GET("/api/reports");
    reports = r.reports || [];
  } catch (e) { reports = []; }
  renderReports();
}

function renderReports() {
  const host = el("pfReports");
  if (!host) return;
  host.innerHTML = reports.length ? reports.slice(0, 12).map((r) => `
    <div style="display:flex;gap:9px;align-items:center;padding:7px 0;
                border-bottom:1px solid var(--hairline)">
      <a href="/reports/${encodeURIComponent(r.name)}" target="_blank"
         style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        ${esc(r.name)}</a>
      <span class="faint" style="font-size:11px">${(r.size / 1024).toFixed(0)}kB</span>
      <a class="btn sm" href="/reports/${encodeURIComponent(r.name)}?download=1"
         title="Save the file instead of opening it">↓</a>
      <button class="btn sm" data-del="${esc(r.name)}">×</button>
    </div>`).join("")
    : `<div class="faint" style="padding:8px 0">No reports yet.</div>`;
  host.querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = () => act(async () => {
      await DEL(`/api/reports/${encodeURIComponent(b.dataset.del)}`);
      await loadReports();
    });
  });
}

function render() {
  if (!perf || !el("pfStats")) return;
  const st = perf.stats;
  const inv = perf.inventory || [];
  const aged = inv.filter((x) => x.age_days > 3);

  el("pfRows").textContent = `${perf.rows} journal rows`;

  el("pfStats").innerHTML =
    stat("Realized", sgn(st.realized), `${st.closes} lots closed`)
    + stat("Still open", inv.length, `${money0(perf.inventory_cost)} tied up`)
    + stat("Oldest lot", perf.oldest_days ? perf.oldest_days.toFixed(1) + "d" : "—",
           aged.length ? `${aged.length} over 3 days` : "nothing stale")
    + stat("Deployed", money0(st.capital_deployed), `${st.opens} lots opened`)
    + stat("Return on deployed", st.return_on_deployed_pct.toFixed(2) + "%")
    + stat("Median hold", dur(st.median_hold_seconds), `max ${dur(st.max_hold_seconds)}`)
    + stat("Per day", sgn(st.realized_per_day), `${st.closes_per_day} closes/day`)
    + stat("Deepest ladder", st.max_ladder_depth);

  el("pfNote").innerHTML = st.closes
    ? `Win rate is deliberately absent: every lot exits on its own take-profit and
       there is no stop loss, so closed trades are ~100% winners by construction.
       The number that matters is how much capital is <b>still open</b> and how old.`
    : `No closed trades in this window.`;

  const rungs = Object.entries(st.by_rung || {});
  el("pfRungs").innerHTML = tableHTML(
    ["Rung", "Opened", "Closed", "Still open", "Realized", "Avg hold"],
    rungs.map(([r, x]) => {
      const open = x.opened - x.closed;
      return `<tr><td><b>${r}</b></td>
        <td class="num">${x.opened}</td><td class="num">${x.closed}</td>
        <td class="num ${open > 0 ? "warn" : "faint"}">${open}</td>
        <td class="num">${sgn(x.realized)}</td>
        <td class="num faint">${x.avg_hold_seconds ? dur(x.avg_hold_seconds) : "—"}</td>
      </tr>`;
    }), "No lots recorded yet.");

  el("pfTrades").innerHTML = tableHTML(
    ["When", "Sym", "Event", "Lot", "Shares", "Entry", "Exit", "Realized", "Held"],
    (perf.recent || []).slice(0, 60).map((r) => `<tr>
      <td class="faint">${esc(String(r.ts).slice(5, 16).replace("T", " "))}</td>
      <td><b>${esc(r.symbol || "")}</b></td>
      <td class="${r.event === "open" ? "" : "up"}">${esc(r.event)}
        ${r.inferred ? `<span class="pill warn">inferred</span>` : ""}</td>
      <td class="faint mono" style="text-align:left">${esc(r.lot_id || "")}</td>
      <td class="num">${qty(r.shares)}</td>
      <td class="num">${px(r.entry_price, 4)}</td>
      <td class="num">${px(r.exit_price, 4)}</td>
      <td class="num">${r.event === "open" ? `<span class="faint">—</span>`
        : sgn(r.realized)}</td>
      <td class="num faint">${r.hold_seconds ? dur(r.hold_seconds) : "—"}</td>
    </tr>`), "Nothing recorded yet.");

  el("pfInvN").textContent = `${inv.length} lots`;
  el("pfInv").innerHTML = tableHTML(
    ["Lot", "Age", "Sh", "Entry", "Target", "Cost"],
    inv.map((x) => `<tr>
      <td class="faint mono" style="text-align:left">${esc(x.lot_id)}</td>
      <td class="num ${x.age_days > 3 ? "down" : x.age_days > 1 ? "warn" : "faint"}">
        ${x.age_days.toFixed(1)}d</td>
      <td class="num">${qty(x.shares)}</td>
      <td class="num">${px(x.entry_price)}</td>
      <td class="num">${px(x.tp_price)}</td>
      <td class="num">${money0(x.cost)}</td></tr>`),
    "Nothing open.");
}
