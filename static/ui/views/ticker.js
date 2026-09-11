/* ============================================================================
   Ticker detail -- Live (the chart and the decision beside it) and Settings
   (the strategy as eight widgets, not a 72-field wall).

   Live is ordered the way a decision is made: the chart first, the strategy
   and the controls beside it, then what the ladder is holding, then the
   money, then the record. "Orders & positions" is gone -- every stat and the
   working-orders table on it were already on this tab and on the Portfolio
   page; only its Order history was unique, and that is now the last card
   here.

   Settings renders the SAME fields.js definitions grouped by what they
   control, with everything inert for the modes this ticker is in hidden
   rather than greyed, and one plain-English line per card.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, DEL, act, ask, toast, el, esc, card, stat, tableHTML, money, sgn, px, qty, go,
  acctLabel, acctNumber,
} from "../core.js";
import { ChartPanel, matchToBars } from "../chartpanel.js";
import {
  formPatch, FIELD_GROUPS, groupHTML, applyVisibility, readValues, summaries,
} from "../fields.js";

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
    // `open` puts the arrow on its own style layer (mark_open), which inherits
    // the side's entry colour until the operator gives it one of its own
    marks.push({ t, price: o.price, kind: "entry", side: opens(o.side), open: true,
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
    Alpaca holds <b>${qty(s.broker_qty)}</b> shares, the ladder tracks <b>${qty(s.shares)}</b>.
    Auto-correction handles this within 25 seconds.</div>`);
  if (s.reconcile && s.reconcile.uncovered > 0) {
    if (s.reconcile.uncovered <= (s.offbook_shares || 0) + 1e-6) {
      // a fractional lot's DAY exit inside its retry timer (or dust): the engine
      // re-places it itself, so this is information, not the naked-share alarm
      b.push(`<div class="note info"><b>Fractional exit off the book:</b> ${qty(s.offbook_shares)} sh —
        re-placed by the engine at the next eligible session or retry (fractional lots rest DAY orders).</div>`);
    } else {
      b.push(`<div class="note bad"><b>${qty(s.reconcile.uncovered)} shares have no resting
        sell.</b> They will not exit on their own.</div>`);
    }
  }
  for (const a of (s.attention || [])) {
    b.push(`<div class="note warn">${esc(a)}</div>`);
  }
  if (s.running && !s.halted && s.block_reason) {
    b.push(`<div class="note info">Not looking for entries:
      <b>${esc(s.block_reason)}</b>.</div>`);
  }
  if (s.running && !s.halted && s.add_trigger === "touch" && s.lot_count
      && !(s.resting_adds || []).length && s.adds_hold) {
    b.push(`<div class="note info">Not resting adds: <b>${esc(s.adds_hold)}</b>.</div>`);
  }
  if (s.reconciles_this_hour) {
    b.push(`<div class="note warn">Position auto-corrected
      <b>${s.reconciles_this_hour}×</b> in the last hour. It keeps trading and keeps
      correcting, but repeated drift means something upstream is wrong.</div>`);
  }
  if (!s.dry_run && !s.halted) {
    b.push(`<div class="note bad"><b>Armed</b> — orders transmit to
      ${s.paper ? "the paper" : "the LIVE"} account. Max exposure
      <b>${money(s.max_exposure)}</b> (${s.config.max_lots} × ${qty(s.config.shares_per_lot)}
      sh). There is <b>no stop loss</b>.</div>`);
  }
  return b.join("");
}

/* ------------------------------------------------- the strategy control */
/* One preset picker, rendered on Live (beside the chart) and at the top of
   Settings (where it is the primary way to set a strategy). Only one tab is
   ever mounted, so the ids are shared. */
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

function presetHTML() {
  return `<div class="tk-strat">
      <select id="tkPreset" class="strat-sel" aria-label="Active strategy">
        <option>Loading…</option></select>
      <button class="btn sm" type="button" id="tkApply">Apply</button>
    </div>
    <div class="tk-strat-d faint" id="tkPresetDesc"></div>`;
}

function fillPreset() {
  const sel = el("tkPreset");
  if (!sel) return;
  const cur = (S.ticker && S.ticker.config && S.ticker.config.preset) || "custom";
  sel.innerHTML = presetOptions(cur);
  const d = el("tkPresetDesc");
  if (d) d.textContent = presetDesc(sel.value);
}

function wirePreset(sym) {
  const sel = el("tkPreset");
  if (!sel) return;
  if (PRESETS.length) fillPreset();
  else {
    GET("/api/presets").then((r) => { PRESETS = r.presets || []; fillPreset(); })
      .catch((e) => { sel.innerHTML = `<option>presets unavailable</option>`; toast(esc(e.message), "err"); });
  }
  sel.addEventListener("change", () => {
    const d = el("tkPresetDesc");
    if (d) d.textContent = presetDesc(sel.value);
  });
  el("tkApply").onclick = () => act(async () => {
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
    S.touched = false;                 // the server's config is now the truth
    toast(`${sym} is now on ${esc(p.label)}.`, "ok");
    if (el("tform")) mountSettings();  // re-render the widgets against the new config
  });
}

/* keep the dropdown honest on every poll without fighting the user's cursor */
function syncPreset() {
  const sel = el("tkPreset");
  if (!sel || !PRESETS.length || document.activeElement === sel) return;
  if (!S.ticker || !S.ticker.config) return;
  const cur = S.ticker.config.preset || "custom";
  const want = PRESETS.some((p) => p.id === cur) ? cur : "custom";
  if (sel.value !== want) {
    sel.value = want;
    const d = el("tkPresetDesc");
    if (d) d.textContent = presetDesc(want);
  }
}

/* ------------------------------------------------------------ live mount */
function mountLive(sym) {
  el("view").innerHTML = `
    <div id="tkNotes"></div>

    <div class="tk-top">
      ${card("Chart", `<div id="chartHost"></div>
        <div class="tip" id="tkLegend"></div>
        <div class="tip chart-trades">
          <label><input type="checkbox" id="tkShowTrades" checked> Show trades</label>
          <label title="Rows the ledger wrote to stay in step with Alpaca — a rebuilt ladder, a lot closed outside the bot. Bookkeeping, not real fills.">
            <input type="checkbox" id="tkShowInferred"> include bookkeeping rows</label>
          <span id="tkMarkLegend"></span>
          <span id="tkTradesCount" style="margin-left:auto">—</span>
        </div>`)}

      <aside class="tk-side">
        ${card("Active strategy", presetHTML(), `<span id="tkSide"></span>`,
               { cls: "hero" })}
        ${card("Price", `<div class="stats tk-px">
          <div><div class="stat-k">Last</div>
               <div class="stat-v num" id="tkPx">—</div>
               <div class="stat-s num" id="tkPxSub"></div></div>
          <div><div class="stat-k">Spread</div>
               <div class="stat-v num" id="tkSpread">—</div>
               <div class="stat-s num" id="tkSpreadSub"></div></div>
        </div>`)}
        ${card("Controls", `<div class="row-btns tk-ctl" id="tkCtl">
          <button class="btn sm good" id="bStart">Start</button>
          <button class="btn sm" id="bStop">Stop</button>
          <button class="btn sm danger" id="bArm">Arm</button>
          <button class="btn sm" id="bDisarm">Disarm</button>
          <button class="btn sm" id="bRecover">Re-cover lots</button>
          <button class="btn sm" id="bClear">Clear halt</button>
          <button class="btn sm danger" id="bFlatten">Flatten</button>
        </div>`)}
      </aside>
    </div>

    <div class="grid main">
      <div>
        ${card("Ladder", `<div class="stats" id="tkStats"></div>
          <div style="margin-top:18px" id="tkLots"></div>
          <div style="margin-top:12px" id="tkAdds"></div>`, "", {})}
      </div>
      <div>
        ${card("Money", `<div class="stats" id="tkMoney"></div>`)}
        ${card("Trend filter", `<div class="stats" id="tkTrendStats"></div>
          <div style="margin-top:12px" id="tkTrend"></div>`,
          `<span class="faint">R may exist · D may add · M drives the unwind</span>`)}
        ${card("Activity", `<div class="log" id="tkLog"></div>`, "", { flush: true })}
      </div>
    </div>

    ${card("Order history", `<div id="poHist"></div>`,
      `<button class="btn sm" id="poReload">Reload</button>`, { flush: true })}`;

  const A = (fn) => () => act(fn);
  el("bStart").onclick = A(async () => {
    await POST(`/api/ticker/${sym}/start`); toast(`${sym} started.`, "ok"); });
  el("bStop").onclick = A(async () => {
    await POST(`/api/ticker/${sym}/stop`); toast(`${sym} stopped.`, "ok"); });
  el("bDisarm").onclick = A(async () => {
    const r = await POST(`/api/ticker/${sym}/arm`, { live: false });
    if (r.ok === false) toast(esc(r.error || `${sym} was NOT disarmed.`), "err", 9000);
    else toast(`${sym} back to dry run.`, "ok"); });
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
            <td style="border:0;padding:3px 0;text-align:right">${qty(c.shares_per_lot)} sh ≈ ${money(per)}</td></tr>
        <tr><td style="border:0;padding:3px 0">Cap</td>
            <td style="border:0;padding:3px 0;text-align:right">${c.max_lots} lots ≈ ${money(s.max_exposure)}</td></tr>
        <tr><td style="border:0;padding:3px 0">Take profit</td>
            <td style="border:0;padding:3px 0;text-align:right">$${c.take_profit}/share</td></tr>
        </tbody></table><br><b class="down">There is no stop loss.</b> Only ${sym} in
        <b>${esc(who)}</b> is affected — every other ticker and every other account is untouched.`,
    })) return;
    const r = await POST(`/api/ticker/${sym}/arm`, { live: true, confirm: "ARM" });
    if (r.ok === false) toast(esc(r.error || `${sym} was NOT armed.`), "err", 9000);
    else toast(`${sym} on ${esc(who)} is ARMED — orders now transmit.`, "err", 8000);
  });
  el("bFlatten").onclick = A(async () => {
    const s = S.ticker;
    if (!await ask({
      title: `Flatten ${sym} on ${esc(acctLabel())}?`, danger: true, ok: "Flatten", requireWord: "FLATTEN",
      body: `In <b>${esc(acctLabel())}</b>: cancels every resting take-profit on ${sym} and
        <b>market-sells all ${s ? qty(s.alpaca.qty) : "?"} shares</b> at whatever the book gives.<br><br>
        Other tickers and other accounts are untouched.`,
    })) return;
    const r = await POST(`/api/ticker/${sym}/flatten`, { confirm: "FLATTEN" });
    toast(`${sym} flattened: cancelled ${r.cancelled}, sold ${r.sold} sh.`, "ok");
  });

  wirePreset(sym);

  el("poReload").onclick = () => loadHistory(sym);
  loadHistory(sym);

  // live: the forming candle follows /api/ticks; onStyle: the legend under
  // the chart is drawn in whatever colours the operator chose
  panel = new ChartPanel(el("chartHost"), { key: "ticker", symbol: sym, onBars,
                                            live: true, onStyle: paintLegend });
  TR = { data: null, at: 0, key: "" };
  const show = el("tkShowTrades");
  show.checked = tradesOn();
  show.onchange = () => { rememberTradesOn(show.checked); onBars(panel.bars || []); };
  el("tkShowInferred").onchange = applyTrades;
  paintLegend();
  panel.load();
}

/* The legends under the chart, in the chart's own resolved colours -- a
   static legend went wrong the moment a colour was changed in Style. A layer
   that is switched off drops out of the legend, so the two never disagree. */
function paintLegend() {
  if (!panel) return;
  const sw = (kind, text, glyph = "━━") => {
    const s = panel.layer(kind);
    if (!s.on) return "";
    return `<span style="color:${esc(s.color)};opacity:${s.alpha}">${glyph}</span> ${text}`;
  };
  const lines = panel.opts.showMarkers ? [
    sw("entry", "lot entries"), sw("tp", "resting sells"),
    sw("tp_pending", "targets with no order"), sw("avg", "ladder average"),
    sw("next_add", "next add"), sw("resting_add", "resting adds"),
  ].filter(Boolean) : [];
  lines.push(sw("last", "last price", "╌╌"));
  const lg = el("tkLegend");
  if (lg) lg.innerHTML = lines.filter(Boolean).join(" · ")
    || `<span class="faint">order lines are hidden — see Style in the toolbar</span>`;
  const tone = (kind, text) => {
    const s = panel.layer(kind);
    return s.on ? `<span style="color:${esc(s.color)};opacity:${s.alpha}">${text}</span>` : `<s class="faint">${text}</s>`;
  };
  const ml = el("tkMarkLegend");
  if (ml) ml.innerHTML =
    `▲ entry (${tone("mark_entry_long", "long")} · ${tone("mark_entry_short", "short")}) · `
    + `▼ exit (${tone("mark_exit_win", "profit")} · ${tone("mark_exit_loss", "loss")}) · `
    + `${tone("mark_open", "◆ still open")} · `
    + `${tone("link_win", "╌╌")} closed trade`;
}

function paintLive() {
  syncPreset();
  const s = S.ticker;
  if (!s || !el("tkStats")) return;
  const A = s.alpaca, c = s.config, P = s.pnl;

  el("tkNotes").innerHTML = liveNotes(s);
  // one figure per tile: the price, and the width of the book. The bid and the
  // ask are the sub-line -- they explain the spread, they are not two more
  // numbers to read.
  el("tkPx").textContent = s.last_price ? "$" + s.last_price.toFixed(2) : "—";
  el("tkPxSub").textContent = s.last_tick_at ? `tick ${s.last_tick_at}` : "";
  const quote = s.bid && s.ask;
  el("tkSpread").textContent = quote ? "$" + (s.ask - s.bid).toFixed(3) : "—";
  el("tkSpreadSub").textContent = quote
    ? `${s.bid.toFixed(2)} bid · ${s.ask.toFixed(2)} ask` : "no quote";
  const sideEl = el("tkSide");
  if (sideEl) sideEl.textContent = s.lot_count ? `${s.side} · ${s.lot_count}/${c.max_lots} lots` : "flat";

  el("bStart").disabled = s.running;
  el("bStop").disabled = !s.running;
  el("bArm").disabled = !s.dry_run;
  el("bDisarm").disabled = s.dry_run;
  el("bClear").disabled = !s.halted;

  /* Open P/L and this ladder's realized-today are rendered ONCE, on the Money
     card. They used to appear here as well -- the same server values through
     two formatters under two labels ("Closed/today" and "Today"), which is
     how one number came to disagree with itself on one screen. */
  const bar = s.last_bar;
  el("tkStats").innerHTML =
    stat("Lots", `${s.lot_count}<span class="faint" style="font-size:15px">/${c.max_lots}</span>`,
         `${qty(s.shares)} shares`)
    + stat("Ladder avg", px(s.avg_price, 4), s.in_sync ? "in sync" : `Alpaca: ${qty(s.broker_qty)}`)
    + stat("Next add", px(s.next_add_at),
           s.anchor && s.anchor.price
             ? `from ${s.anchor.kind === "last_open" ? "last open" : esc(s.anchor.kind)} ${px(s.anchor.price)}`
             : "")
    + stat("Closed", s.closed_count, "lots, all time")
    + stat("Last bar", bar
        ? `<span class="${bar.color === "red" ? "down" : bar.color === "green" ? "up" : "faint"}">${bar.color}</span>`
        : "—", `${esc(c.bar_size || "1Min")} candles`);

  const byCoid = Object.fromEntries((A.orders || []).map((o) => [o.coid, o]));
  el("tkLots").innerHTML = tableHTML(
    ["Lot", "Shares", "Entry", "Target", "To go", "P/L", "Placed in", "Resting"],
    (s.lots || []).map((l) => {
      // a short lot makes money as price FALLS: the unsigned difference was
      // rendering every short lot's P/L with the wrong sign, and its target
      // (which sits below the entry) as permanently "at target"
      const d = (l.side || s.side) === "short" ? -1 : 1;
      const pl = (s.last_price - l.entry_price) * l.shares * d;
      const to = (l.tp_price - s.last_price) * d;
      const o = byCoid[l.tp_client_id];
      const sell = o
        ? `<span class="up">${qty(o.remaining)} @ ${px(o.limit)}</span>`
        : l.armed ? `<span class="warn">trailing from ${px(l.peak)}</span>`
        : s.dry_run ? `<span class="faint">dry run</span>`
        : `<span class="down">none</span>`;
      // a fractional lot's exit is a DAY order (re-placed each session), never GTC
      const frac = !Number.isInteger(Number(l.shares));
      return `<tr>
        <td class="mono faint" style="text-align:left">${esc(l.id)}</td>
        <td class="num">${qty(l.shares)}${frac ? ' <span class="pill" title="a fractional lot exits with a DAY limit the engine re-places each session">DAY exit</span>' : ""}</td>
        <td class="num">${px(l.entry_price, 4)}</td>
        <td class="num">${px(l.tp_price)}</td>
        <td class="num ${to <= 0 ? "up" : "faint"}">${to <= 0 ? "at target" : "$" + to.toFixed(2)}</td>
        <td class="num">${sgn(pl)}</td>
        <td class="num faint" title="strategy trigger → entry accepted by Alpaca; take-profit accepted in ${l.tp_latency_ms ? l.tp_latency_ms.toFixed(0) + " ms" : "—"}">${l.entry_latency_ms ? l.entry_latency_ms.toFixed(0) + " ms" : "—"}</td>
        <td style="text-align:right">${sell}</td></tr>`;
    }), `Flat — no open lots on ${s.symbol}.`);

  // touch mode: the rungs resting at Alpaca as entry limits, each a lot in
  // waiting. "resting" means the order is in the current snapshot.
  const adds = s.resting_adds || [];
  if (el("tkAdds")) el("tkAdds").innerHTML =
    `<div class="faint" style="margin-bottom:4px">Resting adds${
      s.add_trigger === "touch" ? ` (touch mode, ${s.add_depth} deep)` : ""}</div>`
    + tableHTML(["Rung", s.side === "short" ? "Sell" : "Buy", "At", "To go", "Placed in", "Age", "State"],
      adds.map((a) => {
        const to = (s.last_price - a.price) * (a.side === "short" ? -1 : 1);
        const state = a.state === "cancelling" ? `<span class="warn">cancelling</span>`
          : a.resting ? `<span class="up">resting</span>`
          : `<span class="warn">not in snapshot</span>`;
        return `<tr><td class="num">${a.k}</td>
          <td class="num">${a.shares}${a.booked ? ` (${a.booked} filled)` : ""}</td>
          <td class="num">${px(a.price)}</td>
          <td class="num ${to <= 0 ? "up" : "faint"}">${to <= 0 ? "touched" : "$" + to.toFixed(2)}</td>
          <td class="num faint">${a.placed_ms ? a.placed_ms.toFixed(0) + " ms" : "—"}</td>
          <td class="num faint">${Math.round(a.age_s)}s</td>
          <td style="text-align:right" title="${esc(a.coid)}">${state}</td></tr>`;
      }),
      s.add_trigger === "touch"
        ? (s.adds_hold ? `No rungs resting — ${esc(s.adds_hold)}.`
           : (s.lot_count ? "No rungs resting." : "Flat — the first lot waits for its candle rule."))
        : "Close mode — adds are judged on bar closes.");

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

  /* The one home of this ticker's money. `alpaca.unrealized_pl` and
     `pnl.realized_ladder` are each rendered exactly once on this page. */
  el("tkMoney").innerHTML =
    stat("Position", money(A.market_value), `${qty(A.qty)} sh`)
    + stat("Cost", money(A.cost_basis))
    + stat("Open P/L", sgn(A.unrealized_pl),
           A.unrealized_plpc ? `${A.unrealized_plpc.toFixed(2)}% since entry` : "")
    + stat("Realized today", sgn(P.realized_ladder), "this ladder's own closed lots")
    + stat("All time", sgn(s.realized_all), `${s.closed_count} lots closed`);

  el("tkLog").innerHTML = (s.events || []).slice(0, 60).map((e) => `
    <div class="log-row"><span class="log-t">${esc(e.t)}</span>
      <span class="log-l lv-${esc(e.level)}">${esc(e.level)}</span>
      <span class="log-m">${esc(e.msg)}</span></div>`).join("")
    || `<div class="empty">Nothing yet.</div>`;

  if (panel && panel.bars.length) panel.setStatus(s);
}

/* ---------------------------------------------------------- order history */
/* The one card that was unique to the old "Orders & positions" tab. Its
   stats and its working-orders table were the Live tab's and Portfolio's. */
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
        <td class="num">${qty(o.qty)}</td>
        <td class="num">${qty(o.filled_qty)}</td>
        <td class="num">${o.filled_avg_price ? "$" + Number(o.filled_avg_price).toFixed(4) : "—"}</td>
        <td class="num">${o.limit_price ? "$" + Number(o.limit_price).toFixed(2) : "—"}</td>
        <td class="${o.status === "filled" ? "up" : "faint"}">${esc(o.status || "")}</td>
      </tr>`), "No orders on record.");
  } catch (e) {
    b.innerHTML = `<div class="empty down">${esc(e.message)}</div>`;
  }
}

/* -------------------------------------------------------------- settings */
/* Eight widgets instead of one 72-field column, and about half of them
   showing fewer fields than they hold, because a field whose mode is off is
   hidden rather than greyed. The preset picker on top is the fast way in;
   the cards below are the way to adjust what it set. */
function mountSettings() {
  const s = S.ticker;
  if (!s) { el("view").innerHTML = `<div class="empty">Loading…</div>`; return; }
  const sym = s.symbol;

  const cards = FIELD_GROUPS.map((g) => card(
    `<span class="setg-i" aria-hidden="true">${g.icon}</span>${g.title}`,
    `<div class="setg-sum" id="sum-${g.id}"></div>
     <div class="setg-f" data-group="${g.id}">${groupHTML(g, s.config)}</div>`,
    `<span class="setg-x" id="cx-${g.id}"></span>`)).join("");

  el("view").innerHTML = `
    ${card("Strategy", `${presetHTML()}
      <div class="tip" style="margin-top:14px">A preset sets every card below in one step.
        Change anything by hand afterwards and ${sym} is stamped
        <b>custom</b> — the preset is a starting point, not a lock.</div>`,
      "", { cls: "hero" })}

    <form id="tform" autocomplete="off">
      <div class="setg">${cards}</div>
      <div class="setg-save">
        <button type="submit" class="btn primary">Save ${sym}</button>
        <span class="tip" id="tsaveMsg" style="margin:0"></span>
        <span class="spacer" style="flex:1"></span>
        <span class="faint setg-hid" id="setgHidden"></span>
      </div>
    </form>

    <div class="grid main">
      <div>${card("What this ladder does", `<div class="stats" id="tsSum"></div>`,
        "independent of every other ticker")}</div>
      <div>${card("Remove", `<div class="tip" style="margin-top:0">Removing a ticker
        deletes its settings. Its ledger is kept and <b>nothing at Alpaca is
        cancelled or sold</b>.</div>
        <button class="btn danger" type="button" id="bRemove" style="margin-top:12px">
          Remove ${sym}</button>`)}</div>
    </div>`;

  wirePreset(sym);

  const f = el("tform");
  /* The governing selects decide which OTHER fields mean anything, so the
     visible set and every summary are recomputed on each keystroke. Nothing
     is re-rendered: the fields are toggled in place, so whatever the user has
     typed in a field that is not changing survives. */
  const refresh = () => {
    const v = readValues(f);
    const counts = applyVisibility(f, v);
    const sums = summaries(v, { side: (S.ticker && S.ticker.side) || "long" });
    let hidden = 0;
    for (const g of FIELD_GROUPS) {
      const sum = el(`sum-${g.id}`);
      if (sum) sum.textContent = sums[g.id] || g.lead;
      const c = counts[g.id] || { shown: 0, hidden: 0 };
      hidden += c.hidden;
      const x = el(`cx-${g.id}`);
      if (x) {
        x.textContent = c.hidden ? `${c.shown} of ${c.shown + c.hidden}` : `${c.shown}`;
        x.title = c.hidden
          ? `${c.hidden} setting${c.hidden === 1 ? " does" : "s do"} nothing in this mode and ${
              c.hidden === 1 ? "is" : "are"} hidden`
          : "every setting in this card is live";
      }
    }
    const h = el("setgHidden");
    if (h) h.textContent = hidden
      ? `${hidden} setting${hidden === 1 ? "" : "s"} hidden — they do nothing in the modes ${sym} is in`
      : "every setting is live in these modes";
  };
  refresh();

  f.addEventListener("input", () => { S.touched = true; refresh(); });
  f.addEventListener("change", () => { S.touched = true; refresh(); });
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
  syncPreset();
  const s = S.ticker;
  if (!s || !el("tsSum")) return;
  const c = s.config;
  const per = (c.shares_per_lot || 0) * (s.last_price || 0);
  el("tsSum").innerHTML =
    stat("Per lot", money(per), `${qty(c.shares_per_lot)} sh @ ${px(s.last_price)}`)
    + stat("Max exposure", money(s.max_exposure), `${c.max_lots} lots`)
    + stat("Take profit", "$" + Number(c.take_profit).toFixed(2), "per share, per lot")
    + stat("Win per lot", money(c.take_profit * c.shares_per_lot), "before fees");
}

/* ------------------------------------------------------------------ view */
VIEWS.ticker = {
  title: (ov, v) => v.sym,
  sub: (ov, v) => {
    const t = (ov?.tickers || []).find((x) => x.symbol === v.sym);
    return t ? `${t.state} · ${t.lot_count}/${t.max_lots} lots · ${qty(t.shares)} shares` : "";
  },
  // "Orders & positions" folded into Live: everything on it but Order history
  // was already rendered on Live and on Portfolio.
  tabs: [["live", "Live"], ["settings", "Settings"]],

  mount(v) {
    if (panel) { panel.destroy(); panel = null; }
    // an old #/t/SYM/orders bookmark lands on Live, which now holds its content
    if (v.tab === "settings") mountSettings();
    else mountLive(v.sym);
  },
  paint(v) {
    if (v.tab === "settings") {
      if (!el("tform") && S.ticker) mountSettings();
      if (!S.touched) paintSettings();
    } else paintLive();
  },
};
