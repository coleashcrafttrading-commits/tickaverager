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
      opts: [["with_trend", "first candle in the trend's colour, on the trend's side of the MA"],
             ["red_bar", "on a red bar close"], ["immediate", "immediately"]],
      hint: "<b>with_trend</b>: a long trend's candles are mostly green, so the ladder "
          + "catches the move on the first green close above the MA; a short trend on the "
          + "first red close below it. Adds are unchanged: one rung against the last fill." },
    { k: "entry_ma", t: "sel", label: "The MA that candle must clear",
      opts: [["vwap", "session VWAP (from 04:00 ET)"], ["ema", "1-minute EMA"]] },
    { k: "entry_ma_period", t: "num", label: "EMA length (1-minute bars)", step: 1, min: 2 },
    { k: "bias_source", t: "sel", label: "What decides the side",
      opts: [["1h", "the 1-hour SuperTrend: its sign is the side, its flip is the reversal"],
             ["rd", "R AND D: 4h regime and day bias must agree"]],
      hint: "<b>1h</b> is the owner's rule and what the v2 profile applies. R and D stay "
          + "on the Trend filter card either way." },
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

  { legend: "Ladder v2 — calculated adds and the cap", fields: [
    { k: "add_k", t: "num", label: "Rung distance (x 15m ATR)", step: 0.05, min: 0.1,
      hint: "With add_mode = atr, the next rung sits this many 15-minute ATRs beyond "
          + "the last fill, never tighter than 2x the spread. The fixed $0.10 rung was "
          + "3x the one-minute range — inside the noise — and built depth 29." },
    { k: "add_floor", t: "num", label: "Rung floor ($)", step: 0.01, min: 0.01 },
    { k: "f_ladder", t: "num", label: "Max share of equity in one ladder", step: 0.01, min: 0,
      hint: "0 = off. 0.20 means this ladder may hold at most 20% of live equity in "
          + "cost basis; the last lot is truncated to fit, never skipped." },
    { k: "n_target", t: "num", label: "Rungs the cap is spread over", step: 1, min: 1 },
    { k: "regime_band_atr", t: "num", label: "Regime band (x 4h ATR)", step: 0.05, min: 0,
      hint: "The 4h regime only flips when the close clears the EMA by this many ATRs. "
          + "0 reproduces the raw sign, which flipped 16 times in two weeks on RAM." },
    { k: "vwap_hysteresis", t: "num", label: "VWAP side hysteresis (bars)", step: 1, min: 1 },
    { k: "depth_by_strength", t: "bool", label: "Halve depth when the 15m slope opposes",
      hint: "Helped MSTX, hurt RAM in replay. Per ticker; validate before enabling." },
  ]},

  { legend: "Ladder v2 — the staged unwind", fields: [
    { k: "reversal_mode", t: "sel", label: "When the trend turns against the ladder",
      opts: [["off", "hold — per-lot take-profits only"],
             ["flatten", "stage 1: close the deepest half on the 1h flip; stage 2: close the rest if the 4h regime agrees 4h later"],
             ["reverse", "flip: the moment the 1h trend goes the other way, close the whole ladder at the market and re-open it on the new side at the same size, lot for lot"]],
      hint: "<b>reverse</b> is the owner's rule: no depth minimum, no second timeframe — "
          + "the 1h flip is the signal, the loss is taken, and the ladder is banking on "
          + "the other direction. The re-entry is one market order per closed lot, each "
          + "with its own take-profit; adds continue afterwards. <b>flatten</b> is the "
          + "researched staged unwind (half on the 1h flip at depth ≥4, the rest only if "
          + "the 4h regime agrees 4h later)." },
    { k: "unwind_min_lots", t: "num", label: "Stage 1 only at this depth (flatten)", step: 1, min: 1 },
    { k: "unwind_stage_hours", t: "num", label: "Hours between stage 1 and 2", step: 0.5, min: 0.5 },
    { k: "unwind_cooldown_h", t: "num", label: "Cooldown after stage 1 (h)", step: 1, min: 1 },
  ]},

  { legend: "Ladder v2 — basket exits from the average", fields: [
    { k: "basket_tp_enabled", t: "bool", label: "Basket take-profit from the average",
      hint: "Fires at avg + max(take_profit, 0.5 x 1h ATR). Measured inert: by the time "
          + "price is there the lower lots have left through their own TPs." },
    { k: "basket_stop_enabled", t: "bool", label: "Basket stop from the average",
      hint: "Sits one buffer below the rung that would fill lot n_target, so it fires "
          + "only after the cap has bound and price keeps going. Net negative in every "
          + "recovering window measured; it exists to bound the tail with a hard number." },
    { k: "basket_stop_atr", t: "num", label: "Stop buffer (x 1h ATR)", step: 0.1, min: 0 },
    { k: "ladder_max_bars", t: "num", label: "Time stop (session bars, 0 = off)", step: 10, min: 0,
      hint: "Removes the multi-day tail (the 8.8-day holds) at the cost of most of the "
          + "recovering-window profit. Buys capital velocity, not P/L." },
  ]},

  { legend: "Trend filter", fields: [
    { k: "trend_filter", t: "bool", label: "Require trend agreement",
      hint: "Three layers: <b>R</b> the 4h regime (may a ladder exist on this side), "
          + "<b>D</b> the day bias — session VWAP side and 15m DMI (may it add now), "
          + "<b>M</b> the 1h trend-change (drives the unwind, not the gate). "
          + "Replayed over the June and July crashes: R AND D held the worst open "
          + "drawdown to about $2k where the ungated ladder lost $74k." },
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
