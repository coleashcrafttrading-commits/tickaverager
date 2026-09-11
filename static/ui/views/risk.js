/* ============================================================================
   Risk -- one question: am I about to lose more than I can afford?

   Dollar settings hide the thing that matters: a $0.10 target on a symbol that
   ranges $0.02 a minute is a completely different trade from the same target on
   one that ranges $0.15. Everything here is shown in both units.

   This page used to carry three other things, and none of them was this
   question:
     - an Account stat row (equity, cash, buying power, open P/L, "Today") --
       the same /api/overview .portfolio the topbar strip renders on every
       page, with one tile labelled "Today" that was a DIFFERENT number from
       the "Today" on Portfolio. The strip is above this page too; it is not
       repeated here.
     - a read-only Limits table mirroring a form on Settings, whose only
       action was a button back to Settings. The "used" figures it existed to
       show are on the Settings guardrail fields themselves now.
     - Profiles and Bank tabs. A risk profile is a research artefact -- it is
       written, tested and ranked, never watched -- so both are rooms in
       Research now.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, el, esc, card, stat, tableHTML,
  money0, sgn, px, qty, hashFor,
} from "../core.js";

let data = null;        // /api/risk -- this account's exposure
let dataFor = "";       // which account it belongs to

VIEWS.risk = {
  title: () => "Risk",
  sub: () => "what a move against you costs, in dollars and in ATR",

  mount() {
    if (dataFor !== S.account) { data = null; dataFor = S.account; }
    /* The summary sits in a rail beside the per-ladder detail: the three
       numbers that answer the page's question, then the table that shows
       where they come from. */
    el("view").innerHTML = `
      <div id="rkNotes"></div>
      <div class="grid main lead-rail">
        <div>
          ${card("Per ladder", `<div id="rkTickers"></div>`,
            "everything in dollars and in ATR", { flush: true })}
          ${card("If it goes against you", `<div id="rkStress"></div>`,
            "no stop loss — these are the numbers that matter", { flush: true })}
        </div>
        <div>
          ${card("", `<div id="rkHero"></div>`, "", { cls: "hero" })}
          ${card("Exposure", `<div class="stats col" id="rkHead"></div>`)}
          ${card("Where the caps are set", `<div class="tip" style="margin-top:0">
            Max total exposure, the cash reserve, the account-wide daily loss
            limit and how many tickers may run are on
            <a href="${hashFor({ kind: "settings", tab: "engine" })}">Settings →
            Engine</a>, each showing how much of itself is used. This page does
            not mirror them — it says what a move against you would
            <i>cost</i>.</div>`)}
        </div>
      </div>`;
    load();
  },

  paint() {
    if (data) render(); else load();
  },
};

async function load() {
  try { data = await GET("/api/risk"); } catch (e) { return; }
  render();
}

function render() {
  if (!data || !el("rkHead")) return;
  const a = data.account, L = data.limits, T = data.tickers || [];
  /* add_mode is not on /api/risk, and it decides whether the server could
     compute a ladder depth at all -- join it from the fleet snapshot rather
     than printing a bare dash and letting it look like zero risk. */
  const modeOf = {};
  for (const t of (S.ov ? S.ov.tickers || [] : [])) modeOf[t.symbol] = t.add_mode;

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
      <a href="${hashFor({ kind: "settings", tab: "engine" })}">set them</a>.</div>`);
  }
  if (data.worst_case > a.equity) {
    n.push(`<div class="note bad"><b>Worst case exceeds the account.</b> Every ladder
      filling every rung would need <b>${money0(data.worst_case)}</b> against
      <b>${money0(a.equity)}</b> of equity. That state is unreachable — the ladders
      would stop filling — but it means your caps are not the thing limiting you,
      your buying power is.</div>`);
  }
  el("rkNotes").innerHTML = n.join("");

  /* ---- the numbers this page exists for ----
     Not balances, and not P/L: the topbar strip carries the account's money
     on every page, including this one. These are exposure. */
  const atr1 = T.reduce((s, t) => s + (Number(t.loss_1atr) || 0), 0);
  const held = T.reduce((s, t) => s + (Number(t.cost_basis) || 0), 0);
  el("rkHero").innerHTML = `
    <div class="hero-k">Deployed</div>
    <div class="hero-v num">${a.deployed_pct}%</div>
    <div class="hero-row">
      <div><span class="hero-lk">Of equity</span>
        <span class="hero-lv num">${money0(a.deployed)} of ${money0(a.equity)}</span></div>
    </div>`;
  el("rkHead").innerHTML =
    stat("A 1× ATR move against you", sgn(atr1),
         `on the ${money0(held)} these ladders hold right now`)
    + stat("If every rung fills", money0(data.worst_case),
           data.worst_case > a.equity
             ? `<span class="down">more than the account has</span>`
             : `${Math.round(100 * data.worst_case / (a.equity || 1))}% of equity`)
    + stat("Portfolio guardrails", noLimits
        ? `<span class="warn">none set</span>`
        : `${[L.max_total_exposure, L.reserve_cash, L.account_daily_loss_limit,
              L.max_running_tickers].filter(Boolean).length} of 4`,
        `set on Settings → Engine`);

  /* ---- per ladder ---- */
  el("rkTickers").innerHTML = tableHTML(
    ["Ticker", "Price", "ATR(14)", "ATR %", "Target", "in ATR", "Add", "in ATR",
     "Lots", "Held", "Cost", "Max exposure", "Open P/L"],
    T.map((t) => {
      const tpAtr = t.tp_in_atr;
      const warnTp = tpAtr != null && tpAtr < 0.5;
      const atrRungs = (modeOf[t.symbol] || "points") !== "points";
      return `<tr class="click" data-go="ticker" data-sym="${t.symbol}">
        <td><b>${t.symbol}</b>${t.armed ? ` <span class="pill down">armed</span>` : ""}</td>
        <td class="num">${px(t.price)}</td>
        <td class="num">${t.atr ? "$" + t.atr.toFixed(3) : "—"}</td>
        <td class="num faint">${t.atr_pct ? t.atr_pct.toFixed(2) + "%" : "—"}</td>
        <td class="num">$${t.take_profit.toFixed(2)}</td>
        <td class="num ${warnTp ? "warn" : ""}">${tpAtr == null ? "—" : tpAtr.toFixed(2) + "×"}</td>
        <td class="num">${atrRungs ? `<span class="faint">1× ATR</span>`
          : "$" + t.add_distance.toFixed(2)}</td>
        <td class="num">${atrRungs ? `<span class="faint">1.00×</span>`
          : (t.add_in_atr == null ? "—" : t.add_in_atr.toFixed(2) + "×")}</td>
        <td class="num">${t.lots_open}<span class="faint">/${t.max_lots}</span></td>
        <td class="num">${qty(t.shares_held)}</td>
        <td class="num">${money0(t.cost_basis)}</td>
        <td class="num faint">${money0(t.max_exposure)}</td>
        <td class="num">${sgn(t.unrealized)}</td></tr>`;
    }), "No tickers configured.");

  /* ---- stress ---- */
  const anyAtr = T.some((t) => (modeOf[t.symbol] || "points") !== "points");
  el("rkStress").innerHTML = tableHTML(
    ["Ticker", "Held", "A 1× ATR fall", "Full ladder depth", "Cost to fill it",
     "Est. loss at the bottom"],
    T.map((t) => {
      const atrRungs = (modeOf[t.symbol] || "points") !== "points";
      const noDepth = atrRungs
        ? `<span class="warn">not computed</span>`
        : `<span class="faint">—</span>`;
      return `<tr>
      <td><b>${t.symbol}</b></td>
      <td class="num">${qty(t.shares_held)}</td>
      <td class="num">${sgn(t.loss_1atr)}</td>
      <td class="num">${t.ladder_depth
        ? `$${t.ladder_depth.toFixed(2)} <span class="faint">(${t.ladder_depth_pct}%)</span>`
        : noDepth}</td>
      <td class="num faint">${money0(t.max_exposure)}</td>
      <td class="num">${t.loss_full_ladder ? sgn(t.loss_full_ladder) : noDepth}</td>
    </tr>`;
    }), "No tickers configured.");

  el("rkStress").insertAdjacentHTML("beforeend", `
    <div class="tip" style="padding:0 18px 16px">
      <b>A 1× ATR fall</b> is what one average bar's range costs you on what you hold
      right now. <b>Full ladder depth</b> is how far price must fall for every rung to
      fill (add distance × max lots) — past that the ladder stops buying and simply
      holds. <b>Est. loss at the bottom</b> assumes the average lot is half the ladder
      depth underwater, which is the right order of magnitude, not a precise figure.
      ${anyAtr ? `<br><br><b class="warn">Not computed</b> means exactly that: the
      server only measures ladder depth for fixed-dollar rungs
      (<code>add_mode=points</code>), and a ladder on ATR rungs has no fixed
      depth to measure. Read it as unknown, never as zero.` : ""}
      <br><br>There is <b>no stop loss</b>. These numbers do not include a scenario
      where price keeps falling after the ladder is full, because in that scenario the
      loss is unbounded until price recovers.</div>`);
}
