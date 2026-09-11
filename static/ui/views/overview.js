/* ============================================================================
   Portfolio -- what the account is doing, and the one lever that stops it.

   Three tabs, three questions:
     Live     what is happening right now, and the four fleet-wide actions
     Orders   what Alpaca holds and what is working there -- the broker's view
     History  what the ladders have booked, and the printable reports

   The account's money -- value, P/L today, P/L all time -- is in the strip at
   the top of EVERY page and is not repeated here. This page's own stats are
   the ones the strip cannot carry: where the capital is and what is holding it.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, el, esc, card, stat, tableHTML,
  money, money0, sgn, pct, px, qty,
  fleetControlsHTML, wireFleetControls,
} from "../core.js";
import { PortfolioChart } from "../portfolio.js";
import { mountHistory, paintHistory } from "./performance.js";

/* the account-value chart; one per mount, thrown away with the page */
let pf = null;

function banners(ov) {
  const b = [];
  const t = ov.totals || {};
  const p = ov.portfolio || {};
  if (ov.frozen) {
    b.push(`<div class="note bad"><b>Trading is frozen</b> — ${esc(ov.frozen)}.
      No ladder will open a lot and nothing can be armed. Resting take-profits are
      unaffected. Delete <code>state/FROZEN</code> to lift it.</div>`);
  }
  if (ov.snap_error) {
    b.push(`<div class="note warn"><b>Market data is failing</b> — ${esc(ov.snap_error)}.
      Every ladder reads this snapshot, so decisions are running on stale prices.</div>`);
  }
  if (t.halted) {
    b.push(`<div class="note bad"><b>${t.halted} ladder(s) halted.</b>
      Open each one to see why.</div>`);
  }
  if (t.uncovered) {
    if (t.uncovered <= (t.offbook || 0) + 1e-6) {
      // fractional DAY exits inside their retry timer (or dust): re-placed by the engine
      b.push(`<div class="note info"><b>Fractional exit off the book:</b> ${qty(t.offbook)} sh —
        re-placed by the engine at the next eligible session or retry (fractional lots rest DAY orders).</div>`);
    } else {
      b.push(`<div class="note bad"><b>${qty(t.uncovered)} share(s) have no resting sell.</b>
        They will not exit on their own.</div>`);
    }
  }
  const un = (p.unmanaged || []);
  if (un.length) {
    b.push(`<div class="note info">Held but not managed by any ladder:
      <b>${un.map(esc).join(", ")}</b>. The fleet leaves these alone.</div>`);
  }
  return b.join("");
}

function tickerRows(ov) {
  return (ov.tickers || []).map((t) => {
    const state = t.halted ? "warn" : t.running ? (t.dry_run ? "up" : "down") : "";
    const add = t.add_mode === "points" ? `$${Number(t.add_distance).toFixed(2)}`
      : t.add_mode === "percent" ? `${t.add_percent}%` : "below avg";
    return `<tr class="click" data-go="ticker" data-sym="${t.symbol}">
      <td><b>${t.symbol}</b>${t.dry_run ? "" : ` <span class="pill down">live</span>`}</td>
      <td><span class="pill ${state}">${esc(t.state)}</span></td>
      <td class="num">${px(t.last_price)}</td>
      <td class="num">${t.lot_count}<span class="faint">/${t.max_lots}</span></td>
      <td class="num">${qty(t.shares)}${t.in_sync ? "" : ' <span class="down">!</span>'}</td>
      <td class="num">${px(t.avg_price, 4)}</td>
      <td class="num">${px(t.next_add_at)}</td>
      <td class="num">${sgn(t.unrealized)}</td>
      <td class="num">${sgn(t.realized_today)}</td>
      <td class="faint">$${Number(t.take_profit).toFixed(2)} / ${add}</td>
    </tr>`;
  });
}

function positionRows(p) {
  return (p.positions || []).map((x) => `<tr>
    <td><b>${x.symbol}</b>${x.managed ? "" : ' <span class="pill">unmanaged</span>'}</td>
    <td class="num">${qty(x.qty)}</td>
    <td class="num">${px(x.avg_entry_price, 4)}</td>
    <td class="num">${px(x.current_price)}</td>
    <td class="num">${money(x.cost_basis)}</td>
    <td class="num">${money(x.market_value)}</td>
    <td class="num">${sgn(x.unrealized_pl)}</td>
    <td class="num">${pct(x.unrealized_plpc)}</td>
  </tr>`);
}

const TABS = [["live", "Live"], ["orders", "Orders"], ["history", "History"]];
const SUB = {
  live: (ov) => ov ? `${ov.totals.count} ticker${ov.totals.count === 1 ? "" : "s"} · `
    + `${ov.totals.running} running · ${ov.totals.armed} armed` : "",
  orders: () => "what Alpaca holds and what is working there",
  history: () => "what the ladders booked, from the trade journal",
};

VIEWS.overview = {
  title: () => "Portfolio",
  sub: (ov, v) => (SUB[v.tab || "live"] || SUB.live)(ov),
  tabs: TABS,

  mount(v) {
    if (pf) { pf.destroy(); pf = null; }
    const tab = v.tab || "live";
    if (tab === "history") return mountHistory();
    if (tab === "orders") return mountOrders();
    mountLive();
  },

  paint(v) {
    const tab = v.tab || "live";
    if (tab === "history") return paintHistory();
    if (tab === "orders") return paintOrders();
    paintLive();
  },
};

/* ================================================================== live */
function mountLive() {
  el("view").innerHTML = `
    <div id="ovNotes"></div>
    ${card("", `<div class="stats" id="ovStats"></div>`)}
    ${card("", `<div id="ovChart"></div>`)}
    ${card("Ladders", `<div id="ovTickers"></div>`,
      fleetControlsHTML(), { flush: true })}
    ${card("Activity", `<div class="log" id="ovLog"></div>`,
      "every ladder in this account", { flush: true })}`;

  // one definition of the four fleet-wide actions, in core.js, and this is
  // the only page that renders them
  wireFleetControls(el("view"));

  // the account-value chart polls on its own: Alpaca's history on a cadence
  // matched to the period, the fleet's equity samples every 2 s
  pf = new PortfolioChart(el("ovChart"));
  pf.start();
}

function paintLive() {
  const ov = S.ov;
  if (!ov || !el("ovStats")) return;
  const p = ov.portfolio, t = ov.totals;

  el("ovNotes").innerHTML = banners(ov);

  /* Account value and the two P/L figures are in the topbar strip on every
     page. What is left is this page's own job: where the capital actually
     is. */
  const eq = Number(p.account_value) || 0;
  const depPct = eq ? Math.round(100 * (p.deployed || 0) / eq) : 0;
  const holding = (ov.tickers || []).filter((x) => x.lot_count > 0).length;
  el("ovStats").innerHTML =
    stat("Deployed", money0(p.deployed),
         `<span class="${depPct > 90 ? "down" : depPct > 65 ? "warn" : "up"}">${depPct}%
          of equity</span>`)
    + stat("Cash", money0(p.cash), `${money0(p.buying_power)} buying power`)
    + stat("Holding", `${holding}<span class="faint">/${t.count}</span>`,
           `${t.lots} lot${t.lots === 1 ? "" : "s"} · ${qty(t.shares)} shares`)
    + stat("At Alpaca", (p.positions || []).length,
           `position(s) · ${(p.orders || []).length} working
            <a href="#" data-go="overview" data-tab="orders">— see Orders</a>`);

  el("ovTickers").innerHTML = tableHTML(
    ["Ticker", "State", "Last", "Lots", "Shares", "Avg", "Next add",
     "Open P/L", "Booked today", "TP / add"],
    tickerRows(ov), "No tickers yet.");

  el("ovLog").innerHTML = (ov.events || []).slice(0, 60).map((e) => `
    <div class="log-row">
      <span class="log-t">${esc(e.t)}</span>
      <span class="log-s">${esc(e.symbol || "")}</span>
      <span class="log-l lv-${esc(e.level)}">${esc(e.level)}</span>
      <span class="log-m">${esc(e.msg)}</span>
    </div>`).join("") || `<div class="empty">Nothing yet.</div>`;
}

/* ================================================================ orders */
/* Alpaca's own view of this account: what it holds and what is working. Both
   tables used to sit under the ladders on one very long page; they are a
   different question from "what are my ladders doing", so they are a
   different tab. */
function mountOrders() {
  el("view").innerHTML = `
    ${card("Open positions", `<div id="ovPos"></div>`,
      `<span id="ovPosN"></span>`, { flush: true })}
    ${card("Working orders", `<div id="ovOrders"></div>`,
      `<span id="ovOrdN"></span>`, { flush: true })}
    ${card("What these are", `<div class="tip" style="margin-top:0">
      Straight from Alpaca, not from the ledgers — this is the broker's answer
      to "what do I own and what is resting". A resting <b>sell</b> is a
      take-profit covering a lot; a resting <b>buy</b> is a rung waiting to
      fill. A position with no matching working sell is the dangerous case and
      the Live tab raises it as an alarm.</div>`)}`;
  paintOrders();
}

function paintOrders() {
  const ov = S.ov;
  if (!ov || !el("ovPos")) return;
  const p = ov.portfolio;

  el("ovPosN").textContent = `${(p.positions || []).length} held`;
  el("ovPos").innerHTML = tableHTML(
    ["Symbol", "Qty", "Avg entry", "Last", "Cost", "Value", "Open P/L", "%"],
    positionRows(p), "Flat — the account holds nothing.");

  el("ovOrdN").textContent = `${(p.orders || []).length} working`;
  el("ovOrders").innerHTML = tableHTML(
    ["Symbol", "Order", "Side", "Qty", "Filled", "Working", "Limit", "Ext", "Status"],
    (p.orders || []).map((o) => `<tr>
      <td><b>${o.symbol}</b></td>
      <td class="faint mono" style="text-align:left">${esc(o.coid)}</td>
      <td class="${o.side === "sell" ? "up" : ""}">${o.side.toUpperCase()}</td>
      <td class="num">${qty(o.qty)}</td>
      <td class="num">${qty(o.filled || 0)}</td>
      <td class="num"><b>${qty(o.remaining)}</b></td>
      <td class="num">${px(o.limit)}</td>
      <td class="${o.extended_hours ? "up" : "faint"}">${o.extended_hours ? "yes" : "no"}</td>
      <td class="faint">${esc(o.status)}</td></tr>`),
    "Nothing working at Alpaca.");
}
