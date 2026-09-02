/* ============================================================================
   fields.js -- one definition of the strategy form.

   Defined once and rendered by BOTH the per-ticker settings tab and the
   add-a-ticker screen, so a new setting can never appear in one and be missing
   from the other.
   ========================================================================= */
"use strict";
import { esc } from "./core.js";

export const STRATEGY_FIELDS = [
  { legend: "Size", fields: [
    { k: "shares_per_lot", t: "num", label: "Shares per lot", step: 1, min: 1,
      hint: "Every rung of the ladder buys this many shares." },
    { k: "max_lots", t: "num", label: "Max lots", step: 1, min: 1,
      hint: "Caps <i>adds</i>, not losses. shares × price × max lots is your worst case." },
  ]},

  { legend: "Position sizing", fields: [
    { k: "size_mode", t: "sel", label: "Size each lot by",
      opts: [["fixed", "a fixed share count"], ["dollars", "a dollar amount"],
             ["atr_risk", "risk per lot (ATR)"]],
      hint: "A fixed share count means a $12 stock and a $500 one carry wildly "
          + "different risk for the same 'lot'. <b>Dollars</b> makes every lot the "
          + "same size; <b>ATR risk</b> makes every lot the same <i>risk</i>, which "
          + "is the only sizing that means the same thing across symbols." },
    { k: "lot_dollars", t: "num", label: "Dollars per lot", step: 100, min: 10 },
    { k: "risk_dollars", t: "num", label: "Risk per lot ($)", step: 10, min: 1,
      hint: "ATR mode: shares = risk ÷ (ATR × stop multiple)." },
    { k: "atr_stop_mult", t: "num", label: "ATR stop multiple", step: 0.1, min: 0.1 },
    { k: "atr_period", t: "num", label: "ATR period", step: 1, min: 2 },
    { k: "min_shares", t: "num", label: "Min shares", step: 1, min: 1 },
    { k: "max_shares", t: "num", label: "Max shares", step: 1, min: 1 },
  ]},

  { legend: "Strategy control", fields: [
    { k: "strategy", t: "txt", label: "Strategy slug",
      hint: "Leave blank to run the ladder. A slug from the Backtest page hands "
          + "decisions to that strategy document." },
    { k: "strategy_entries", t: "bool", label: "Strategy decides entries",
      hint: "Replaces <b>both</b> the first-entry rule and the add rule. "
          + "<code>max_lots</code> and every portfolio guard still apply." },
    { k: "strategy_exits", t: "bool", label: "Strategy decides exits",
      hint: "An indicator exit <b>overrides the take-profit</b>: the resting order "
          + "is cancelled and the lot is sold now. A target is a guess about where "
          + "to leave; an indicator is a reason to." },
  ]},

  { legend: "Entry", fields: [
    { k: "first_entry", t: "sel", label: "First entry",
      opts: [["red_bar", "on a red bar close"], ["immediate", "immediately"]] },
    { k: "bar_size", t: "sel", label: "Bar size",
      opts: ["1Min", "2Min", "3Min", "5Min", "10Min", "15Min", "30Min", "1Hour"]
        .map((x) => [x, x]) },
    { k: "add_mode", t: "sel", label: "Add trigger",
      opts: [["points", "$ below the last fill"], ["percent", "% below the last fill"],
             ["beyond_average", "any close below the average"]] },
    { k: "add_distance", t: "num", label: "Add distance ($)", step: 0.01, min: 0 },
    { k: "add_percent", t: "num", label: "Add distance (%)", step: 0.01, min: 0 },
  ]},

  { legend: "Exit", fields: [
    { k: "take_profit", t: "num", label: "Take profit ($/share)", step: 0.01, min: 0.01,
      hint: "Each lot exits at <b>its own fill + this</b>. Changing it re-prices "
          + "every resting take-profit on this ticker immediately." },
    { k: "exit_mode", t: "sel", label: "Exit style",
      opts: [["limit", "resting limit at the target"],
             ["trail", "trail after the target is reached"]],
      hint: "<b>Limit</b> rests an order at Alpaca from the moment the lot opens, so "
          + "it fills even if this process dies. <b>Trail</b> rests nothing: the lot "
          + "is watched, arms at the target, then sells once price falls back by the "
          + "trail amount. Uncapped upside, but it gives back the trail on every "
          + "trade that arms and reverses." },
    { k: "trail_amount", t: "num", label: "Trail ($)", step: 0.01, min: 0.01 },
    { k: "trail_use_broker_stop", t: "bool", label: "Rest a broker trailing stop",
      hint: "When a lot arms, place a real Alpaca trailing stop so the exit survives "
          + "this process dying. Strongly recommended if you use trail mode." },
  ]},

  { legend: "Trend filter", fields: [
    { k: "trend_filter", t: "bool", label: "Require trend agreement",
      hint: "A SuperTrend + EMA stack across 4h / 1h / 1m must agree before an entry "
          + "is allowed. <b>Not yet validated by any backtest</b> — the backtester "
          + "does not model it." },
    { k: "trend_flat_blocks_entries", t: "bool", label: "Block entries when flat" },
    { k: "side_mode", t: "sel", label: "Direction",
      opts: [["auto", "long only (follows a long bias)"],
             ["long", "long only, long bias required"],
             ["short", "short only, short bias required"],
             ["both", "either side — follow the bias"]],
      hint: "<b>auto</b> is what every existing ticker runs: longs only, and it "
          + "waits out a short bias rather than fading it. <b>both</b> lets the "
          + "ladder open shorts when the stack turns down — rungs go <i>up</i>, "
          + "targets go <i>down</i>, exits BUY back. A ladder never mixes sides "
          + "and never flips while a position is open. Shorting needs margin and "
          + "an available borrow, and a short ladder with no stop has "
          + "<b>unbounded</b> risk, unlike a long one." },
    { k: "ema_4h_period", t: "num", label: "4h EMA period", step: 1, min: 5 },
    { k: "st_1h_atr", t: "num", label: "1h SuperTrend ATR", step: 1, min: 2 },
    { k: "st_1h_mult", t: "num", label: "1h SuperTrend mult", step: 0.1, min: 0.5 },
    { k: "st_1m_atr", t: "num", label: "1m SuperTrend ATR", step: 1, min: 2 },
    { k: "st_1m_mult", t: "num", label: "1m SuperTrend mult", step: 0.1, min: 0.5 },
  ]},

  { legend: "Order pricing", fields: [
    { k: "entry_order_type", t: "sel", label: "Order type",
      opts: [["limit", "limit"], ["market", "market"]] },
    { k: "entry_limit_ref", t: "sel", label: "Peg the limit to",
      opts: [["ask", "the ask — crosses, fills"], ["mid", "the mid — saves ½ spread"],
             ["bid", "the bid — passive, often misses"], ["rung", "the rung price"]],
      hint: "Pegging to the bid is passive: it saves the spread but frequently never "
          + "fills at all." },
    { k: "entry_limit_offset", t: "num", label: "Offset ($)", step: 0.01 },
    { k: "cap_at_rung", t: "bool", label: "Never pay above the rung" },
    { k: "entry_on_timeout", t: "sel", label: "If unfilled at timeout",
      opts: [["keep_partial", "keep the partial lot"], ["market", "buy the rest at market"]] },
    { k: "entry_fill_timeout", t: "num", label: "Entry timeout (s)", step: 5, min: 5 },
  ]},

  { legend: "Sessions — America/New_York", fields: [
    { k: "session_mode", t: "sel", label: "Mode",
      opts: [["always", "24/5 — any open session"], ["sessions", "pick sessions"],
             ["times", "a clock window"]] },
    { k: "trade_overnight", t: "bool", label: "Overnight 20:00–04:00" },
    { k: "trade_premarket", t: "bool", label: "Pre-market 04:00–09:30" },
    { k: "trade_regular", t: "bool", label: "Regular 09:30–16:00" },
    { k: "trade_afterhours", t: "bool", label: "After-hours 16:00–20:00" },
    { k: "session_start", t: "txt", label: "Window start" },
    { k: "session_end", t: "txt", label: "Window end" },
    { k: "use_wind_down", t: "bool", label: "Use wind-down" },
    { k: "wind_down_start", t: "txt", label: "Wind-down start" },
    { k: "allow_extended_hours", t: "bool", label: "Extended hours (window mode)" },
  ]},

  { legend: "Safety", fields: [
    { k: "daily_loss_limit", t: "num", label: "Daily loss limit ($)", step: 10, min: 0,
      hint: "This ladder's own realized loss. 0 turns it off. The account-wide limit "
          + "is in Settings." },
    { k: "auto_reconcile", t: "bool", label: "Auto-fix position drift",
      hint: "When Alpaca and the ladder disagree for 25 seconds, the ladder is "
          + "corrected to match <b>Alpaca</b> and trading continues. Turning this off "
          + "makes a disagreement halt the ticker instead." },
    { k: "autostart", t: "bool", label: "Start with the dashboard",
      hint: "Starts this engine on boot, <b>in whatever armed state it was left in</b>." },
    { k: "notes", t: "area", label: "Notes" },
  ]},
];

const ALL = STRATEGY_FIELDS.flatMap((g) => g.fields);
export const fieldByKey = (k) => ALL.find((f) => f.k === k);

function inputHTML(f, v) {
  if (f.t === "sel") {
    return `<select name="${f.k}">` + f.opts.map(([o, l]) =>
      `<option value="${o}"${String(v) === String(o) ? " selected" : ""}>${l}</option>`
    ).join("") + `</select>`;
  }
  if (f.t === "bool") {
    return `<select name="${f.k}">
      <option value=""${!v ? " selected" : ""}>no</option>
      <option value="true"${v ? " selected" : ""}>yes</option></select>`;
  }
  if (f.t === "num") {
    return `<input name="${f.k}" type="number" value="${esc(v)}"`
      + (f.step !== undefined ? ` step="${f.step}"` : "")
      + (f.min !== undefined ? ` min="${f.min}"` : "") + `>`;
  }
  if (f.t === "area") return `<textarea name="${f.k}" rows="3">${esc(v)}</textarea>`;
  return `<input name="${f.k}" value="${esc(v)}">`;
}

export function formHTML(cfg, { omit = [] } = {}) {
  return STRATEGY_FIELDS.map((g) => {
    const fs = g.fields.filter((f) => !omit.includes(f.k));
    if (!fs.length) return "";
    const body = fs.map((f) => {
      const v = cfg[f.k] === undefined ? "" : cfg[f.k];
      return `<label class="f"><span>${f.label}</span>${inputHTML(f, v)}</label>`
        + (f.hint ? `<div class="hint">${f.hint}</div>` : "");
    }).join("");
    return `<fieldset><legend>${g.legend}</legend>${body}</fieldset>`;
  }).join("");
}

/* A form hands back strings. Booleans are ""/"true" and the engine coerces the
   numbers, but sending "" for a number would be read as 0 -- so blanks are
   dropped rather than saved over a real value. */
export function formPatch(form) {
  const out = {};
  for (const [k, v] of new FormData(form).entries()) {
    const f = fieldByKey(k);
    if (f && f.t === "num" && String(v).trim() === "") continue;
    out[k] = (f && f.t === "bool") ? (v === "true") : v;
  }
  return out;
}
