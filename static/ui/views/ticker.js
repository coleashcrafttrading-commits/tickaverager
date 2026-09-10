/* ============================================================================
   Ticker detail -- Live (chart + ladder), Orders & positions, Settings.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, ask, toast, el, esc, card, stat, tableHTML, money, money0, sgn, pct, px, dur, go,
  acctLabel, acctNumber,
} from "../core.js";
import { ChartPanel, matchToBars } from "../chartpanel.js";
import { STRATEGY_FIELDS, formHTML, formPatch } from "../fields.js";

let panel = null;

/* ---------------------------------------------------- historical trades */
/* Past fills drawn on the chart so a ticker can be QC'd by eye: an entry and
   an exit arrow on the candle that contains each fill, and a dashed line
   joining the two ends of every closed trade. The rows come from
   /api/ticker/<sym>/trades and are re-matched to the bars every time the
   chart reloads, so they land on the right candle at any timeframe. The
   fetch itself is throttled -- the live chart refreshes every 20 s on 1Min
   and the fills do not change that often -- unless the bars themselves
   changed, in which case a new fill may have landed. */
const LS_TRADES = "ta-chart-trades";
let TR = { data: null, at: 0, key: "" };

const tradesOn = () => {
  try { return localStorage.getItem(LS_TRADES) !== "0"; } catch (e) { return true; }
};
const rememberTradesOn = (on) => {
  try { localStorage.setItem(LS_TRADES, on ? "1" : "0"); } catch (e) { /* private mode */ }
};
const tradesCount = (html) => { const n = el("tkTradesCount"); if (n) n.innerHTML = html; };

/* The panel calls this after every bar load: the first paint, a timeframe or
   days change, and each quiet live refresh. */
async function onBars(bars) {
  const show = el("tkShowTrades");
  if (!panel || !show) return;
  if (!show.checked) {
    panel.setTrades([]); panel.setLinks([]);
    tradesCount(`<span class="faint">trades hidden</span>`);
    return;
  }
  const key = `${panel.symbol}|${panel.tf}|${panel.days}|${bars.length}`;
  const fresh = TR.data && TR.key === key && Date.now() - TR.at < 30000;
  if (!fresh && !TR.busy) {
    const me = TR;                       // a remount replaces TR; a late answer must not land on it
    me.busy = true;
    try {
      const r = await GET(`/api/ticker/${panel.symbol}/trades?days=${panel.days}`);
      if (me !== TR) return;
      TR.data = r; TR.at = Date.now(); TR.key = key;
    } catch (e) {
      if (me !== TR) return;
      tradesCount(`<span class="down">trades unavailable — ${esc(e.message)}</span>`);
      if (!TR.data) return;              // nothing older to fall back on
    } finally { me.busy = false; }
  }
  applyTrades();
}

function applyTrades() {
  const show = el("tkShowTrades");
  if (!panel || !TR.data || !show || !show.checked) return;
  const inc = !!(el("tkShowInferred") && el("tkShowInferred").checked);
  const r = convertTrades(TR.data, panel.bars || [], inc);
  panel.setTrades(r.marks);
  panel.setLinks(r.links);
  tradesCount(`<b>${r.closed}</b> closed · <b>${r.open}</b> open`
    + (r.off ? ` · <span class="faint">${r.off} fill${r.off > 1 ? "s" : ""} outside the loaded bars</span>` : "")
    + (r.dropped && !inc ? ` · <span class="faint">${r.dropped} bookkeeping row${r.dropped > 1 ? "s" : ""} hidden</span>` : ""));
}

/* API rows -> chart markers and links. A marker's side is the ORDER that was
   sent (a short opens with a sell and closes with a buy), which is what the
   chart places by; it also carries what the hover tooltip reads. A row that
   lands on no loaded bar -- older than the window, or a fill after the newest
   candle -- is counted, not drawn. Rows flagged `inferred` are ledger
   bookkeeping rather than real fills and stay hidden unless asked for; a pair
   whose exit was hidden goes with it. */
function convertTrades(d, bars, inc) {
  const marks = [], links = [];
  let off = 0, dropped = 0;
  const opens = (s) => (s === "short" ? "sell" : "buy");
  const keep = (r) => { if (inc || !r.inferred) return true; dropped++; return false; };
  const place = (iso) => { const t = matchToBars(bars, iso); if (!t) off++; return t; };
  const drawn = new Set();                    // lot ids that have an entry marker
  for (const e of d.entries || []) {
    if (!keep(e)) continue;
    drawn.add(e.lot_id);
    const t = place(e.t); if (!t) continue;
    marks.push({ t, price: e.price, kind: "entry", side: opens(e.side),
                 lot: e.lot_id, shares: e.shares, note: e.why, inferred: !!e.inferred });
  }
  const hidden = new Set();                   // exits filtered out
  for (const x of d.exits || []) {
    if (!keep(x)) { hidden.add(x.lot_id + "|" + Date.parse(x.t)); continue; }
    const t = place(x.t); if (!t) continue;
    const pl = x.realized == null ? NaN : Number(x.realized);
    marks.push({ t, price: x.price, kind: "exit",
                 side: x.side === "short" ? "buy" : "sell",
                 win: isFinite(pl) ? pl >= 0 : undefined, pl: isFinite(pl) ? pl : null,
                 lot: x.lot_id, shares: x.shares, note: x.why,
                 partial: !!x.partial, inferred: !!x.inferred });
  }
  let closed = 0;
  for (const p of d.pairs || []) {
    if ((p.inferred && !inc) || hidden.has(p.lot_id + "|" + Date.parse(p.exit_t))) continue;
    closed++;
    const t0 = matchToBars(bars, p.entry_t), t1 = matchToBars(bars, p.exit_t);
    if (!t0 || !t1) continue;
    links.push({ t0, p0: p.entry_price, t1, p1: p.exit_price, win: !!p.win, label: p.lot_id });
  }
  const open = d.open_lots || [];
  for (const o of open) {
    if (drawn.has(o.lot_id)) continue;        // its entry fill is already on the chart
    const t = place(o.t); if (!t) continue;
    marks.push({ t, price: o.price, kind: "entry", side: opens(o.side),
                 lot: o.lot_id, shares: o.shares,
                 note: o.tp_price ? `still open · target $${Number(o.tp_price).toFixed(2)}` : "still open" });
  }
  return { marks, links, closed, open: open.length, off, dropped };
}

/* ------------------------------------------------------------------ live */
function liveNotes(s) {
  const b = [];
  if (s.halted) b.push(`<div class="note bad"><b>Halted</b> — ${esc(s.halt_reason)}.
    ${s.symbol} will not trade until this is cleared.</div>`);
  if (!s.in_sync && !s.dry_run) b.push(`<div class="note warn"><b>Out of sync</b> —
    Alpaca holds <b>${s.broker_qty}</b> shares, the ladder tracks <b>${s.shares}</b>.
    Auto-correction handles this within 25 seconds.</div>`);
  if (s.reconcile && s.reconcile.uncovered > 0) {
    b.push(`<div class="note bad"><b>${s.reconcile.uncovered} shares have no resting
      sell.</b> They will not exit on their own.</div>`);
  }
  for (const a of (s.attention || [])) {
    b.push(`<div class="note warn">${esc(a)}</div>`);
  }
  if (s.running && !s.halted && s.block_reason) {
    b.push(`<div class="note info">Not looking for entries:
      <b>${esc(s.block_reason)}</b>.</div>`);
  }
  if (s.reconciles_this_hour) {
    b.push(`<div class="note warn">Position auto-corrected
      <b>${s.reconciles_this_hour}×</b> in the last hour. It keeps trading and keeps
      correcting, but repeated drift means something upstream is wrong.</div>`);
  }
  if (!s.dry_run && !s.halted) {
    b.push(`<div class="note bad"><b>Armed</b> — orders transmit to
      ${s.paper ? "the paper" : "the LIVE"} account. Max exposure
      <b>${money(s.max_exposure)}</b> (${s.config.max_lots} × ${s.config.shares_per_lot}
      sh). There is <b>no stop loss</b>.</div>`);
  }
  return b.join("");
}

let PRESETS = [];          // from /api/presets, shared across accounts

function presetOptions(current) {
  const opts = PRESETS.map((p) =>
    `<option value="${esc(p.id)}"${p.id === current ? " selected" : ""}>${esc(p.label)}</option>`);
  opts.push(`<option value="custom"${current === "custom" || !PRESETS.some((p) => p.id === current) ? " selected" : ""}>Custom (edited by hand)</option>`);
  return opts.join("");
}

function presetDesc(id) {
  const p = PRESETS.find((x) => x.id === id);
  return p ? p.description : (id === "custom" ? "Settings were edited by hand on the Settings tab." : "");
}

function mountLive(sym) {
  el("view").innerHTML = `
    <div id="tkNotes"></div>
    ${card("", `
      <div class="strat" id="tkStrat">
        <span class="strat-k">Active strategy</span>
        <select id="tkPreset" class="strat-sel"><option>Loading…</option></select>
        <button class="btn sm primary" id="tkApply">Apply</button>
        <span class="strat-d faint" id="tkPresetDesc"></span>
      </div>
      <div style="display:flex;align-items:baseline;gap:18px;flex-wrap:wrap">
        <div><div class="stat-k">Last</div>
          <div class="stat-v num" id="tkPx">—</div></div>
        <div><div class="stat-k">Spread</div>
          <div class="stat-s num" id="tkSpread" style="font-size:13px;margin-top:8px">—</div></div>
        <div class="spacer" style="flex:1"></div>
        <div class="row-btns" id="tkCtl">
          <button class="btn sm good" id="bStart">Start</button>
          <button class="btn sm" id="bStop">Stop</button>
          <button class="btn sm danger" id="bArm">Arm</button>
          <button class="btn sm" id="bDisarm">Disarm</button>
          <button class="btn sm" id="bRecover">Re-cover lots</button>
          <button class="btn sm" id="bClear">Clear halt</button>
          <button class="btn sm danger" id="bFlatten">Flatten</button>
        </div>
      </div>`)}
    ${card("Chart", `<div id="chartHost"></div>
      <div class="tip"><span style="color:var(--accent)">━━</span> lot entries ·
        <span style="color:var(--up)">━━</span> resting sells ·
        <span style="color:var(--warn)">━━</span> targets with no order ·
        <span style="color:var(--faint)">━━</span> ladder average ·
        <span style="color:var(--down)">━━</span> next add</div>
      <div class="tip chart-trades">
        <label><input type="checkbox" id="tkShowTrades" checked> Show trades</label>
        <label title="Rows the ledger wrote to stay in step with Alpaca — a rebuilt ladder, a lot closed outside the bot. Bookkeeping, not real fills.">
          <input type="checkbox" id="tkShowInferred"> include bookkeeping rows</label>
        <span>▲ entry (<span style="color:var(--up)">green</span> long ·
          <span style="color:var(--warn)">orange</span> short) ·
          ▼ exit (<span style="color:var(--up)">green</span> profit ·
          <span style="color:var(--down)">red</span> loss) ·
          dashed line = closed trade</span>
        <span id="tkTradesCount" style="margin-left:auto">—</span>
      </div>`)}
    <div class="grid main">
      <div>
        ${card("Ladder", `<div class="stats" id="tkStats"></div>
          <div style="margin-top:18px" id="tkLots"></div>`, "", {})}
      </div>
      <div>
        ${card("Money", `<div class="stats" id="tkMoney"></div>`)}
        ${card("Trend filter", `<div class="stats" id="tkTrendStats"></div>
          <div style="margin-top:12px" id="tkTrend"></div>`,
          `<span class="faint">R may exist · D may add · M drives the unwind</span>`)}
        ${card("Activity", `<div class="log" id="tkLog"></div>`, "", { flush: true })}
      </div>
    </div>`;

  const A = (fn) => () => act(fn);
  el("bStart").onclick = A(async () => {
    await POST(`/api/ticker/${sym}/start`); toast(`${sym} started.`, "ok"); });
  el("bStop").onclick = A(async () => {
    await POST(`/api/ticker/${sym}/stop`); toast(`${sym} stopped.`, "ok"); });
  el("bDisarm").onclick = A(async () => {
    await POST(`/api/ticker/${sym}/arm`, { live: false });
    toast(`${sym} back to dry run.`, "ok"); });
  el("bClear").onclick = A(async () => {
    await POST(`/api/ticker/${sym}/clear_halt`); toast(`${sym} halt cleared.`, "ok"); });
  el("bRecover").onclick = A(async () => {
    const r = await POST(`/api/ticker/${sym}/ensure_tps`);
    if (r.failed) toast(`Placed ${r.placed}, but <b>${r.failed} could not be covered</b>. `
      + `Usually an old sell is still cancelling and holding the shares.`, "err", 9000);
    else toast(`${sym}: re-covered ${r.placed || 0} lot(s).`, "ok");
  });
  el("bArm").onclick = A(async () => {
    const s = S.ticker; if (!s) return;
    const c = s.config;
    const per = (c.shares_per_lot || 0) * (s.last_price || 0);
    // the account is named by label AND number: two accounts may run the
    // same symbol, and the wrong one armed is real money in the wrong place
    const who = acctLabel();
    const num = (s.account && (s.account.account_number || s.account.number)) || acctNumber();
    if (!await ask({
      title: `Arm ${sym} on ${esc(who)}?`, danger: true, ok: "Arm", requireWord: "ARM",
      body: `<table style="width:100%"><tbody>
        <tr><td style="border:0;padding:3px 0">Account</td>
            <td style="border:0;padding:3px 0;text-align:right"><b>${esc(who)}</b> · ${esc(num || "—")}
            ${s.paper ? "(paper)" : "<b class='down'>LIVE MONEY</b>"}</td></tr>
        <tr><td style="border:0;padding:3px 0">Each lot</td>
            <td style="border:0;padding:3px 0;text-align:right">${c.shares_per_lot} sh ≈ ${money(per)}</td></tr>
        <tr><td style="border:0;padding:3px 0">Cap</td>
            <td style="border:0;padding:3px 0;text-align:right">${c.max_lots} lots ≈ ${money(s.max_exposure)}</td></tr>
        <tr><td style="border:0;padding:3px 0">Take profit</td>
            <td style="border:0;padding:3px 0;text-align:right">$${c.take_profit}/share</td></tr>
        </tbody></table><br><b class="down">There is no stop loss.</b> Only ${sym} in
        <b>${esc(who)}</b> is affected — every other ticker and every other account is untouched.`,
    })) return;
    await POST(`/api/ticker/${sym}/arm`, { live: true, confirm: "ARM" });
    toast(`${sym} on ${esc(who)} is ARMED — orders now transmit.`, "err", 8000);
  });
  el("bFlatten").onclick = A(async () => {
    const s = S.ticker;
    if (!await ask({
      title: `Flatten ${sym} on ${esc(acctLabel())}?`, danger: true, ok: "Flatten", requireWord: "FLATTEN",
      body: `In <b>${esc(acctLabel())}</b>: cancels every resting take-profit on ${sym} and
        <b>market-sells all ${s ? s.alpaca.qty : "?"} shares</b> at whatever the book gives.<br><br>
        Other tickers and other accounts are untouched.`,
    })) return;
    const r = await POST(`/api/ticker/${sym}/flatten`, { confirm: "FLATTEN" });
    toast(`${sym} flattened: cancelled ${r.cancelled}, sold ${r.sold} sh.`, "ok");
  });

  // ---- the Active strategy dropdown ----
  const sel = el("tkPreset");
  const fill = () => {
    const cur = (S.ticker && S.ticker.config && S.ticker.config.preset) || "custom";
    sel.innerHTML = presetOptions(cur);
    el("tkPresetDesc").textContent = presetDesc(sel.value);
  };
  GET("/api/presets").then((r) => { PRESETS = r.presets || []; fill(); })
    .catch((e) => { sel.innerHTML = `<option>presets unavailable</option>`; toast(esc(e.message), "err"); });
  sel.addEventListener("change", () => { el("tkPresetDesc").textContent = presetDesc(sel.value); });
  el("tkApply").onclick = A(async () => {
    const id = sel.value;
    const p = PRESETS.find((x) => x.id === id);
    if (!p) { toast("Pick a named strategy to apply.", "err"); return; }
    const s = S.ticker, cur = (s && s.config && s.config.preset) || "custom";
    if (!await ask({
      title: `Put ${sym} on "${esc(p.label)}"?`, ok: "Apply strategy",
      body: `<b>${esc(p.description)}</b><br><br>This overwrites ${sym}'s strategy settings on
        <b>${esc(acctLabel())}</b> (currently: ${esc(cur)}). Open lots keep their exits; a changed
        take-profit re-prices resting sells. Arming is unchanged.`,
    })) return;
    await POST(`/api/ticker/${sym}/preset`, { id });
    toast(`${sym} is now on ${esc(p.label)}.`, "ok");
  });

  panel = new ChartPanel(el("chartHost"), { key: "ticker", symbol: sym, onBars });
  TR = { data: null, at: 0, key: "" };
  const show = el("tkShowTrades");
  show.checked = tradesOn();
  show.onchange = () => { rememberTradesOn(show.checked); onBars(panel.bars || []); };
  el("tkShowInferred").onchange = applyTrades;
  panel.load();
}

function paintLive() {
  // keep the strategy dropdown honest without fighting the user's cursor
  const sel = el("tkPreset");
  if (sel && PRESETS.length && document.activeElement !== sel && S.ticker && S.ticker.config) {
    const cur = S.ticker.config.preset || "custom";
    const want = PRESETS.some((p) => p.id === cur) ? cur : "custom";
    if (sel.value !== want) { sel.value = want; el("tkPresetDesc").textContent = presetDesc(want); }
  }
  const s = S.ticker;
  if (!s || !el("tkStats")) return;
  const A = s.alpaca, c = s.config, P = s.pnl;

  el("tkNotes").innerHTML = liveNotes(s);
  el("tkPx").textContent = s.last_price ? "$" + s.last_price.toFixed(2) : "—";
  el("tkSpread").textContent = (s.bid && s.ask)
    ? `${s.bid.toFixed(2)} / ${s.ask.toFixed(2)} (${((s.ask - s.bid)).toFixed(3)})`
    : "no quote";

  el("bStart").disabled = s.running;
  el("bStop").disabled = !s.running;
  el("bArm").disabled = !s.dry_run;
  el("bDisarm").disabled = s.dry_run;
  el("bClear").disabled = !s.halted;

  const bar = s.last_bar;
  el("tkStats").innerHTML =
    stat("Lots", `${s.lot_count}<span class="faint" style="font-size:15px">/${c.max_lots}</span>`,
         `${s.shares} shares`)
    + stat("Ladder avg", px(s.avg_price, 4), s.in_sync ? "in sync" : `Alpaca: ${s.broker_qty}`)
    + stat("Next add", px(s.next_add_at))
    + stat("Open P/L", sgn(s.unrealized))
    + stat("Closed", s.closed_count, `today ${money0(s.realized_today)}`)
    + stat("Last bar", bar
        ? `<span class="${bar.color === "red" ? "down" : bar.color === "green" ? "up" : "faint"}">${bar.color}</span>`
        : "—", s.last_tick_at ? `tick ${s.last_tick_at}` : "");

  const byCoid = Object.fromEntries((A.orders || []).map((o) => [o.coid, o]));
  el("tkLots").innerHTML = tableHTML(
    ["Lot", "Shares", "Entry", "Target", "To go", "P/L", "Placed in", "Resting"],
    (s.lots || []).map((l) => {
      const pl = (s.last_price - l.entry_price) * l.shares;
      const to = l.tp_price - s.last_price;
      const o = byCoid[l.tp_client_id];
      const sell = o
        ? `<span class="up">${o.remaining} @ ${px(o.limit)}</span>`
        : l.armed ? `<span class="warn">trailing from ${px(l.peak)}</span>`
        : s.dry_run ? `<span class="faint">dry run</span>`
        : `<span class="down">none</span>`;
      return `<tr>
        <td class="mono faint" style="text-align:left">${esc(l.id)}</td>
        <td class="num">${l.shares}</td>
        <td class="num">${px(l.entry_price, 4)}</td>
        <td class="num">${px(l.tp_price)}</td>
        <td class="num ${to <= 0 ? "up" : "faint"}">${to <= 0 ? "at target" : "$" + to.toFixed(2)}</td>
        <td class="num">${sgn(pl)}</td>
        <td class="num faint" title="strategy trigger → entry accepted by Alpaca; take-profit accepted in ${l.tp_latency_ms ? l.tp_latency_ms.toFixed(0) + " ms" : "—"}">${l.entry_latency_ms ? l.entry_latency_ms.toFixed(0) + " ms" : "—"}</td>
        <td style="text-align:right">${sell}</td></tr>`;
    }), `Flat — no open lots on ${s.symbol}.`);

  // the three-layer filter. For a week it read "flat" on five bars and

  // nothing on this page showed it; now the stack, the bar counts and the

  // block reason are all here.

  const tr = s.trend || {};

  const tone = (b) => (b === "long" ? "up" : b === "short" ? "down" : "faint");

  if (el("tkTrendStats")) el("tkTrendStats").innerHTML =

    stat("Bias", `<span class="${tone(tr.bias)}">${esc(tr.bias || "—")}</span>`,

         s.block_reason ? esc(s.block_reason) : "clear to trade")

    + stat("R · D · M", `${tr.R ?? "—"} · ${tr.D ?? "—"} · ${tr.M ?? "—"}`, "regime · day bias · trend-change")

    + stat("15m slope t", tr.t15 == null ? "—" : Number(tr.t15).toFixed(2), tr.S == null ? "" : `S=${Number(tr.S).toFixed(2)}`)

    + stat("15m ATR", tr.atr15 == null ? "—" : "$" + Number(tr.atr15).toFixed(3), `${tr.bars_1m ?? 0} bars of history`);

  if (el("tkTrend")) el("tkTrend").innerHTML = tableHTML(

    ["Layer", "Timeframe", "Params", "Last", "Bias", ""],

    (tr.stack || []).map((x) => `<tr><td><b>${esc(x.name)}</b></td><td class="faint">${esc(x.timeframe)}</td>

      <td class="faint">${esc(x.params)}</td><td class="num">${x.last == null ? "—" : esc(String(x.last))}</td>

      <td class="${tone(x.bias)}">${esc(x.bias)}</td><td class="faint">${esc(x.note || "")}</td></tr>`),

    "No trend data yet — the engine has not refreshed.");

  el("tkMoney").innerHTML =
    stat("Position", money(A.market_value), `${A.qty} sh`)
    + stat("Cost", money(A.cost_basis))
    + stat("Open P/L", sgn(A.unrealized_pl),
           A.unrealized_plpc ? `${A.unrealized_plpc.toFixed(2)}% since entry` : "")
    + stat("Today", sgn(P.realized_ladder), "this ladder's own lots")
    + stat("All time", sgn(s.realized_all), `${s.closed_count} lots closed`);

  el("tkLog").innerHTML = (s.events || []).slice(0, 60).map((e) => `
    <div class="log-row"><span class="log-t">${esc(e.t)}</span>
      <span class="log-l lv-${esc(e.level)}">${esc(e.level)}</span>
      <span class="log-m">${esc(e.msg)}</span></div>`).join("")
    || `<div class="empty">Nothing yet.</div>`;

  if (panel && panel.bars.length) panel.setStatus(s);
}

/* --------------------------------------------------------------- orders */
function mountOrders(sym) {
  el("view").innerHTML = `
    ${card("Position at Alpaca", `<div class="stats" id="poStats"></div>
      <div id="poRec" style="margin-top:16px"></div>`)}
    ${card("Working orders", `<div id="poOrders"></div>`, "", { flush: true })}
    ${card("Order history", `<div id="poHist"></div>`,
      `<button class="btn sm" id="poReload">Reload</button>`, { flush: true })}`;
  el("poReload").onclick = () => loadHistory(sym);
  loadHistory(sym);
}

async function loadHistory(sym) {
  const b = el("poHist");
  if (!b) return;
  b.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const rows = await GET(`/api/ticker/${sym}/orders?status=all&limit=100`);
    b.innerHTML = tableHTML(
      ["Time", "Order", "Side", "Type", "Qty", "Filled", "Avg fill", "Limit", "Status"],
      rows.map((o) => `<tr>
        <td class="faint">${esc((o.submitted_at || "").slice(11, 19))}</td>
        <td class="mono faint" style="text-align:left">${esc(o.client_order_id || "")}</td>
        <td class="${o.side === "sell" ? "up" : ""}">${esc((o.side || "").toUpperCase())}</td>
        <td class="faint">${esc(o.type || "")}</td>
        <td class="num">${Number(o.qty || 0)}</td>
        <td class="num">${Number(o.filled_qty || 0)}</td>
        <td class="num">${o.filled_avg_price ? "$" + Number(o.filled_avg_price).toFixed(4) : "—"}</td>
        <td class="num">${o.limit_price ? "$" + Number(o.limit_price).toFixed(2) : "—"}</td>
        <td class="${o.status === "filled" ? "up" : "faint"}">${esc(o.status || "")}</td>
      </tr>`), "No orders on record.");
  } catch (e) {
    b.innerHTML = `<div class="empty down">${esc(e.message)}</div>`;
  }
}

function paintOrders() {
  const s = S.ticker;
  if (!s || !el("poStats")) return;
  const A = s.alpaca, R = s.reconcile;
  el("poStats").innerHTML =
    stat("Shares held", A.qty)
    + stat("Avg entry", px(A.avg_entry_price, 4))
    + stat("Cost basis", money(A.cost_basis))
    + stat("Market value", money(A.market_value))
    + stat("Open P/L", sgn(A.unrealized_pl))
    + stat("Covered", `${R.covered_shares}`,
           R.uncovered ? `<span class="down">${R.uncovered} uncovered</span>` : "all covered");

  el("poRec").innerHTML = `<div class="note ${R.in_sync ? "info" : "bad"}">
    <b>${R.in_sync ? "In sync" : "Out of sync"}</b> — Alpaca holds
    <b>${R.alpaca_shares}</b> sh, the ladder tracks <b>${R.ledger_shares}</b> sh.
    Covered by resting sells: <b>${R.covered_shares}</b>${R.uncovered
      ? ` · <span class="down">uncovered ${R.uncovered}</span>` : ""}.</div>`;

  el("poOrders").innerHTML = tableHTML(
    ["Order", "Side", "Qty", "Filled", "Working", "Limit", "Ext", "Status"],
    (A.orders || []).map((o) => `<tr>
      <td class="mono faint" style="text-align:left">${esc(o.coid)}</td>
      <td class="${o.side === "sell" ? "up" : ""}">${o.side.toUpperCase()}</td>
      <td class="num">${o.qty}</td><td class="num">${o.filled || 0}</td>
      <td class="num"><b>${o.remaining}</b></td>
      <td class="num">${px(o.limit)}</td>
      <td class="${o.extended_hours ? "up" : "down"}">${o.extended_hours ? "yes" : "no"}</td>
      <td class="faint">${esc(o.status)}</td></tr>`),
    "No working orders at Alpaca.");
}

/* -------------------------------------------------------------- settings */
function mountSettings() {
  const s = S.ticker;
  if (!s) { el("view").innerHTML = `<div class="empty">Loading…</div>`; return; }
  const sym = s.symbol;
  el("view").innerHTML = `
    <div class="grid main">
      <div>${card(`${sym} strategy`, `<form id="tform">${formHTML(s.config)}
        <button type="submit" class="btn primary" style="width:100%">Save ${sym}</button>
        <div class="tip" id="tsaveMsg"></div></form>`,
        "independent of every other ticker")}</div>
      <div>
        ${card("What this ladder does", `<div class="stats" id="tsSum"></div>
          <div class="tip" id="tsNote"></div>`)}
        ${card("Remove", `<div class="tip" style="margin-top:0">Removing a ticker
          deletes its settings. Its ledger is kept and <b>nothing at Alpaca is
          cancelled or sold</b>.</div>
          <button class="btn danger" id="bRemove" style="margin-top:12px">
            Remove ${sym}</button>`)}
      </div>
    </div>`;

  const f = el("tform");
  f.addEventListener("input", () => { S.touched = true; });
  f.addEventListener("submit", async (e) => {
    e.preventDefault();
    await act(async () => {
      await POST(`/api/ticker/${sym}/config`, formPatch(f));
      S.touched = false;
      toast(`${sym} settings saved.`, "ok");
      el("tsaveMsg").innerHTML = `<span class="up">Saved — live on the next tick.</span>`;
      setTimeout(() => { const m = el("tsaveMsg"); if (m) m.textContent = ""; }, 4000);
    });
  });
  el("bRemove").onclick = () => act(async () => {
    if (!await ask({
      title: `Remove ${sym}?`, danger: true, ok: "Remove", requireWord: sym,
      body: `${sym} stops being managed. Its ledger stays on disk and any position or
             resting order at Alpaca is <b>left exactly as it is</b>.`,
    })) return;
    try { await DEL(`/api/ticker/${sym}`); }
    catch (err) {
      if (!await ask({ title: "Still holding stock", danger: true,
        ok: "Remove anyway", requireWord: "FORCE", body: esc(err.message) })) return;
      await DEL(`/api/ticker/${sym}?force=true`);
    }
    toast(`${sym} removed.`, "ok");
    go({ kind: "overview" });
  });
}

function paintSettings() {
  const s = S.ticker;
  if (!s || !el("tsSum")) return;
  const c = s.config;
  const per = (c.shares_per_lot || 0) * (s.last_price || 0);
  el("tsSum").innerHTML =
    stat("Per lot", money(per), `${c.shares_per_lot} sh @ ${px(s.last_price)}`)
    + stat("Max exposure", money(s.max_exposure), `${c.max_lots} lots`)
    + stat("Take profit", "$" + Number(c.take_profit).toFixed(2), "per share, per lot")
    + stat("Win per lot", money(c.take_profit * c.shares_per_lot), "before fees");
  el("tsNote").innerHTML =
    `Adds ${c.add_mode === "points" ? `every <b>$${Number(c.add_distance).toFixed(2)}</b> below the last fill`
      : c.add_mode === "percent" ? `every <b>${c.add_percent}%</b> below the last fill`
      : "on <b>any close below the ladder average</b>"}, on ${c.bar_size} closes, up to
     <b>${c.max_lots}</b> lots. Exit mode: <b>${esc(c.exit_mode || "limit")}</b>${
       c.exit_mode === "trail" ? ` (arms at target, trails $${c.trail_amount})` : ""}.
     The cap stops <i>adds</i>, not losses — there is no stop loss.`;
}

/* ------------------------------------------------------------------ view */
VIEWS.ticker = {
  title: (ov, v) => v.sym,
  sub: (ov, v) => {
    const t = (ov?.tickers || []).find((x) => x.symbol === v.sym);
    return t ? `${t.state} · ${t.lot_count}/${t.max_lots} lots · ${t.shares} shares` : "";
  },
  tabs: [["live", "Live"], ["orders", "Orders & positions"], ["settings", "Settings"]],

  mount(v) {
    if (panel) { panel.destroy(); panel = null; }
    const tab = v.tab || "live";
    if (tab === "live") mountLive(v.sym);
    else if (tab === "orders") mountOrders(v.sym);
    else mountSettings();
  },
  paint(v) {
    const tab = v.tab || "live";
    if (tab === "live") paintLive();
    else if (tab === "orders") paintOrders();
    else {
      if (!el("tform") && S.ticker) mountSettings();
      if (!S.touched) paintSettings();
    }
  },
};
