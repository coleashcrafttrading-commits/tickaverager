/* ============================================================================
   Risk -- what is actually at stake, in dollars and in ATR.

   Dollar settings hide the thing that matters: a $0.10 target on a symbol that
   ranges $0.02 a minute is a completely different trade from the same target on
   one that ranges $0.15. Everything here is shown in both units.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, el, esc, card, stat, tableHTML,
  money, money0, sgn, pct, px, go,
} from "../core.js";

let data = null;

VIEWS.risk = {
  title: () => "Risk",
  sub: () => "exposure, volatility and what a move against you costs",

  mount() {
    el("view").innerHTML = `
      <div id="rkNotes"></div>
      ${card("Account", `<div class="stats" id="rkAcct"></div>`)}
      ${card("Limits", `<div id="rkLimits"></div>`,
        `<button class="btn sm" data-go="settings">Change</button>`)}
      ${card("Per ladder", `<div id="rkTickers"></div>`,
        "everything in dollars and in ATR", { flush: true })}
      ${card("If it goes against you", `<div id="rkStress"></div>`,
        "no stop loss — these are the numbers that matter", { flush: true })}`;
    load();
  },

  paint() { if (data) render(); else load(); },
};

async function load() {
  try { data = await GET("/api/risk"); } catch (e) { return; }
  render();
}

function render() {
  if (!data || !el("rkAcct")) return;
  const a = data.account, L = data.limits, T = data.tickers || [];

  /* ---- notes that actually need action ---- */
  const n = [];
  if (a.deployed_pct > 90) {
    n.push(`<div class="note bad"><b>${a.deployed_pct}% of equity is deployed.</b>
      With no stop loss and little cash left, a further fall cannot be averaged
      into and cannot be met with new lots.</div>`);
  } else if (a.deployed_pct > 65) {
    n.push(`<div class="note warn"><b>${a.deployed_pct}% of equity is deployed.</b>
      Room is getting thin.</div>`);
  }
  const noLimits = !L.max_total_exposure && !L.reserve_cash
                && !L.account_daily_loss_limit;
  if (noLimits) {
    n.push(`<div class="note warn"><b>No portfolio guardrails are set.</b>
      Nothing caps total exposure, protects a cash reserve, or halts the fleet on an
      account-level loss. Each ladder is limited only by its own max lots —
      <a href="#/settings">set them</a>.</div>`);
  }
  if (data.worst_case > a.equity) {
    n.push(`<div class="note bad"><b>Worst case exceeds the account.</b> Every ladder
      filling every rung would need <b>${money0(data.worst_case)}</b> against
      <b>${money0(a.equity)}</b> of equity. That state is unreachable — the ladders
      would stop filling — but it means your caps are not the thing limiting you,
      your buying power is.</div>`);
  }
  el("rkNotes").innerHTML = n.join("");

  /* ---- account ---- */
  el("rkAcct").innerHTML =
    stat("Equity", money(a.equity))
    + stat("Deployed", money0(a.deployed),
        `<span class="${a.deployed_pct > 90 ? "down" : a.deployed_pct > 65 ? "warn" : "up"}">
         ${a.deployed_pct}% of equity</span>`)
    + stat("Cash", money0(a.cash))
    + stat("Buying power", money0(a.buying_power))
    + stat("Open P/L", sgn(a.open_pl))
    + stat("Today", sgn(a.made_today));

  /* ---- limits, each with how close you are ---- */
  const lim = (label, val, used, unit = "$") => {
    if (!val) {
      return `<tr><td style="text-align:left">${label}</td>
        <td class="faint">not set</td><td></td><td class="faint">—</td></tr>`;
    }
    const p = Math.round(100 * used / val);
    return `<tr><td style="text-align:left">${label}</td>
      <td class="num">${unit === "$" ? money0(val) : val}</td>
      <td class="num">${unit === "$" ? money0(used) : used}</td>
      <td class="num ${p > 90 ? "down" : p > 70 ? "warn" : "up"}">${p}%</td></tr>`;
  };
  const running = T.filter((t) => t.running).length;
  el("rkLimits").innerHTML = tableHTML(["Limit", "Set to", "Now", "Used"], [
    lim("Max total exposure", L.max_total_exposure, a.deployed),
    lim("Cash reserve", L.reserve_cash, Math.max(0, L.reserve_cash - a.buying_power)),
    lim("Account daily loss", L.account_daily_loss_limit, Math.max(0, -a.made_today)),
    lim("Max running tickers", L.max_running_tickers, running, "n"),
  ]);

  /* ---- per ladder ---- */
  el("rkTickers").innerHTML = tableHTML(
    ["Ticker", "Price", "ATR(14)", "ATR %", "Target", "in ATR", "Add", "in ATR",
     "Lots", "Held", "Cost", "Max exposure", "Open P/L"],
    T.map((t) => {
      const tpAtr = t.tp_in_atr;
      const warnTp = tpAtr != null && tpAtr < 0.5;
      return `<tr class="click" data-go="ticker" data-sym="${t.symbol}">
        <td><b>${t.symbol}</b>${t.armed ? ` <span class="pill down">armed</span>` : ""}</td>
        <td class="num">${px(t.price)}</td>
        <td class="num">${t.atr ? "$" + t.atr.toFixed(3) : "—"}</td>
        <td class="num faint">${t.atr_pct ? t.atr_pct.toFixed(2) + "%" : "—"}</td>
        <td class="num">$${t.take_profit.toFixed(2)}</td>
        <td class="num ${warnTp ? "warn" : ""}">${tpAtr == null ? "—" : tpAtr.toFixed(2) + "×"}</td>
        <td class="num">$${t.add_distance.toFixed(2)}</td>
        <td class="num">${t.add_in_atr == null ? "—" : t.add_in_atr.toFixed(2) + "×"}</td>
        <td class="num">${t.lots_open}<span class="faint">/${t.max_lots}</span></td>
        <td class="num">${t.shares_held}</td>
        <td class="num">${money0(t.cost_basis)}</td>
        <td class="num faint">${money0(t.max_exposure)}</td>
        <td class="num">${sgn(t.unrealized)}</td></tr>`;
    }), "No tickers configured.");

  /* ---- stress ---- */
  el("rkStress").innerHTML = tableHTML(
    ["Ticker", "Held", "A 1× ATR fall", "Full ladder depth", "Cost to fill it",
     "Est. loss at the bottom"],
    T.map((t) => `<tr>
      <td><b>${t.symbol}</b></td>
      <td class="num">${t.shares_held}</td>
      <td class="num">${sgn(t.loss_1atr)}</td>
      <td class="num">${t.ladder_depth
        ? `$${t.ladder_depth.toFixed(2)} <span class="faint">(${t.ladder_depth_pct}%)</span>`
        : "—"}</td>
      <td class="num faint">${money0(t.max_exposure)}</td>
      <td class="num">${t.loss_full_ladder ? sgn(t.loss_full_ladder) : "—"}</td>
    </tr>`), "No tickers configured.");

  el("rkStress").insertAdjacentHTML("beforeend", `
    <div class="tip" style="padding:0 18px 16px">
      <b>A 1× ATR fall</b> is what one average bar's range costs you on what you hold
      right now. <b>Full ladder depth</b> is how far price must fall for every rung to
      fill (add distance × max lots) — past that the ladder stops buying and simply
      holds. <b>Est. loss at the bottom</b> assumes the average lot is half the ladder
      depth underwater, which is the right order of magnitude, not a precise figure.
      <br><br>There is <b>no stop loss</b>. These numbers do not include a scenario
      where price keeps falling after the ladder is full, because in that scenario the
      loss is unbounded until price recovers.</div>`);
}
