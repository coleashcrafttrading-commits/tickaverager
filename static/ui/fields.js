/* ============================================================================
   fields.js -- one definition of the strategy form.

   Defined once and rendered by BOTH the per-ticker settings tab and the
   add-a-ticker screen, so a new setting can never appear in one and be missing
   from the other.

   Two renderings live here:

   * formHTML(cfg, {omit})  -- the flat column of fieldsets. views/add.js still
     uses it and its output is unchanged.
   * FIELD_GROUPS + groupHTML() + applyVisibility() + summaries() -- the
     per-ticker Settings tab's widgets. Same fields, grouped by what they
     CONTROL, with everything that is inert for the mode the ticker is in
     hidden rather than greyed, and one plain-English line per group saying
     what the ticker currently does.
   ========================================================================= */
"use strict";
import { esc } from "./core.js";

export const STRATEGY_FIELDS = [
  { legend: "Size", fields: [
    { k: "shares_per_lot", t: "num", label: "Shares per lot", step: 0.01, min: 0.01,
      hint: "Every rung of the ladder buys this many shares. A fraction (0.01 SPY) only works with "
          + "<b>Fractional shares</b> switched on below." },
    { k: "max_lots", t: "num", label: "Max lots", step: 1, min: 1,
      hint: "Caps <i>adds</i>, not losses. shares × price × max lots is your worst case. "
          + "<b>Rungs the cap is spread over</b> below is meant to be the same number." },
  ]},

  { legend: "Position sizing", fields: [
    { k: "size_mode", t: "sel", label: "Size each lot by",
      opts: [["fixed", "a fixed share count"], ["dollars", "a dollar amount"],
             ["atr_risk", "risk per lot (ATR)"]],
      hint: "A fixed share count means a $12 stock and a $500 one carry wildly "
          + "different risk for the same 'lot'. <b>Dollars</b> makes every lot the "
          + "same size; <b>ATR risk</b> makes every lot the same <i>risk</i>, which "
          + "is the only sizing that means the same thing across symbols." },
    /* step "any": `min` is the STEP BASE, so min=10 step=100 made the engine's
       own default of 1500 a stepMismatch and the browser refused to submit the
       whole form. A dollar amount is any dollar amount. */
    { k: "lot_dollars", t: "num", label: "Dollars per lot", step: "any", min: 10 },
    { k: "risk_dollars", t: "num", label: "Risk per lot ($)", step: "any", min: 1,
      hint: "ATR mode: shares = risk ÷ (ATR × stop multiple)." },
    { k: "atr_stop_mult", t: "num", label: "ATR stop multiple", step: 0.1, min: 0.1,
      hint: "A sizing divisor only — <b>it is not a stop</b> and nothing is sold at this "
          + "distance. The stop of the same name lives on a Risk profile." },
    { k: "atr_period", t: "num", label: "ATR period", step: 1, min: 2 },
    { k: "min_shares", t: "num", label: "Min shares", step: 0.01, min: 0,
      hint: "On a fractional ladder the whole-share default (1) means no floor beyond Alpaca's minimum; "
          + "0.5 or 2 are honoured. Clamps every sizing mode, fixed included." },
    { k: "max_shares", t: "num", label: "Max shares", step: 0.01, min: 0.01 },
  ]},

  { legend: "Fractional shares", fields: [
    { k: "fractional", t: "sel", label: "Fractional shares",
      opts: [["off", "off — whole shares only (the default)"], ["on", "on — lots may be a fraction of a share"]],
      hint: "Only names Alpaca marks <b>fractionable</b> (the Add screen shows it). A fractional lot exits with a "
          + "<b>DAY</b> order that is re-placed each session, and it can <b>never be sold short</b>. Off: a "
          + "fraction in Shares per lot is refused, not rounded up." },
    { k: "fractional_sessions", t: "sel", label: "Fractional lots may trade in",
      opts: [["regular", "regular hours only, 09:30–16:00 ET"], ["extended", "regular plus pre-market and after-hours"],
             ["all", "any session, overnight included"]],
      hint: "Outside these hours a fractional ladder does not open or add and its exit is queued for the next "
          + "session — it never switches to whole shares on its own. Overnight is whole shares unless <b>all</b>." },
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
          + "to leave; an indicator is a reason to. A strategy exit's fill moves the "
          + "add anchor like a take-profit." },
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
    /* `atr` was missing from this select. The engine supports it and the
       ladder_v3 preset sets it, so an ATR ticker's form showed "points" and
       saving silently converted the ladder to fixed $ rungs. */
    { k: "add_mode", t: "sel", label: "Add distance measured as",
      opts: [["points", "$ below the last fill"], ["percent", "% below the last fill"],
             ["atr", "× the 15-minute ATR beyond the last fill"],
             ["beyond_average", "any close below the average"]] },
    { k: "add_distance", t: "num", label: "Add distance ($)", step: 0.01, min: 0 },
    { k: "add_percent", t: "num", label: "Add distance (%)", step: 0.01, min: 0 },
    { k: "add_trigger", t: "sel", label: "Adds fire when",
      opts: [["touch", "price touches the rung — a limit order rests at Alpaca and fills intracandle"],
             ["close", "a bar closes past the rung (the old rule)"]],
      hint: "<b>Touch</b> keeps an entry order resting at each rung, exactly like the take-profits, so a wick "
          + "through the level fills it. The FIRST lot still waits for its candle rule above." },
    { k: "add_anchor", t: "sel", label: "Rungs are measured from",
      opts: [["last_fill", "the last fill of any kind — an add or a take-profit"],
             ["last_open", "the newest open lot's entry (the old rule)"]],
      hint: "<b>Last fill</b> is the pullback rule: after a take-profit at $13.70 the next rung is $13.60." },
    { k: "add_depth", t: "num", label: "Rungs kept resting", step: 1, min: 1, max: 10,
      hint: "Touch only. 3 means the next three rungs rest at once so a fast dump fills them all. "
          + "Each resting rung reserves one lot of buying power at Alpaca." },
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
    { k: "n_target", t: "num", label: "Rungs the cap is spread over", step: 1, min: 1,
      hint: "Meant to equal <b>Max lots</b>. Nothing cross-checks the two, so set them together." },
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
    /* The six settings production actually depends on under reverse. They are
       in engine.TICKER_DEFAULTS and were reachable only from the CLI. */
    { k: "reverse_ttl_h", t: "num", label: "Re-open queue valid for (h)", step: 1, min: 0,
      hint: "The flip closes the ladder and queues one re-entry per closed lot. A queue "
          + "older than this is abandoned; the lots that never re-opened keep their flag." },
    { k: "reverse_cooldown_h", t: "num", label: "Hours between flips", step: 0.5, min: 0,
      hint: "0 is the owner's rule — no cooldown beyond the 1h SuperTrend's own hysteresis." },
    { k: "reverse_max_spread_pct", t: "num", label: "Hold the flip above this spread (% of mid)",
      step: 0.1, min: 0,
      hint: "A flip crosses the book twice. A 2x ETF's overnight book can be 5.8% wide, and "
          + "paying that twice for nothing is the whole cost of a bad flip. The hold clears "
          + "itself when the book tightens, usually 04:00 or 09:30. 0 = never wait." },
    { k: "basket_chase_s", t: "num", label: "Re-price a basket close after (s)", step: 1, min: 0,
      hint: "A basket close still resting after this many seconds is re-priced until it "
          + "prints. Applies to the flip and to the staged unwind alike." },
    { k: "unwind_on_gap", t: "bool", label: "Also unwind on an opening gap",
      hint: "A second trigger for the staged close: an opening gap against the ladder with "
          + "the 1h trend also against. Off by default." },
    { k: "gap_atr", t: "num", label: "Gap size (x 1h ATR)", step: 0.5, min: 0 },
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
      + (f.min !== undefined ? ` min="${f.min}"` : "")
      + (f.max !== undefined ? ` max="${f.max}"` : "") + `>`;
  }
  if (f.t === "area") return `<textarea name="${f.k}" rows="3">${esc(v)}</textarea>`;
  return `<input name="${f.k}" value="${esc(v)}">`;
}

function labelHTML(f, cfg) {
  const v = cfg[f.k] === undefined ? "" : cfg[f.k];
  return `<label class="f"><span>${f.label}</span>${inputHTML(f, v)}</label>`
    + (f.hint ? `<div class="hint">${f.hint}</div>` : "");
}

export function formHTML(cfg, { omit = [] } = {}) {
  return STRATEGY_FIELDS.map((g) => {
    const fs = g.fields.filter((f) => !omit.includes(f.k));
    if (!fs.length) return "";
    const body = fs.map((f) => labelHTML(f, cfg)).join("");
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

/* ==========================================================================
   GROUPS -- the same fields, arranged by what they control.

   Everything below is additive: nothing above changed shape, so views/add.js
   keeps working against formHTML / formPatch / fieldByKey unchanged.
   ========================================================================== */

export const FIELD_GROUPS = [
  { id: "sizing", title: "Sizing", icon: "◧",
    lead: "How big one rung is, and how many of them there may be.",
    keys: ["shares_per_lot", "max_lots", "size_mode", "lot_dollars", "risk_dollars",
           "atr_stop_mult", "atr_period", "min_shares", "max_shares", "fractional",
           "f_ladder", "n_target"] },

  { id: "entry", title: "Entry", icon: "▶",
    lead: "What has to happen before the FIRST lot of a ladder opens.",
    keys: ["first_entry", "entry_ma", "entry_ma_period", "bias_source", "bar_size"] },

  { id: "adds", title: "Adds", icon: "≡",
    lead: "Where the next rung sits and how it is filled.",
    keys: ["add_mode", "add_distance", "add_percent", "add_k", "add_floor",
           "add_trigger", "add_anchor", "add_depth"] },

  { id: "exits", title: "Exits", icon: "◀",
    lead: "Every way a lot can leave. There is no stop loss on an unarmed lot.",
    keys: ["take_profit", "exit_mode", "trail_amount", "trail_use_broker_stop",
           "reversal_mode", "reverse_ttl_h", "reverse_cooldown_h", "reverse_max_spread_pct",
           "basket_chase_s", "unwind_on_gap", "gap_atr",
           "unwind_min_lots", "unwind_stage_hours", "unwind_cooldown_h",
           "basket_tp_enabled", "basket_stop_enabled", "basket_stop_atr", "ladder_max_bars"] },

  { id: "gate", title: "Strategy & gate", icon: "⚑",
    lead: "Which side may open, and whether the ladder may trade at all.",
    keys: ["strategy", "strategy_entries", "strategy_exits", "side_mode",
           "trend_filter", "trend_flat_blocks_entries", "ema_4h_period",
           "st_1h_atr", "st_1h_mult", "regime_band_atr", "vwap_hysteresis",
           "depth_by_strength"] },

  { id: "pricing", title: "Order pricing", icon: "$",
    lead: "What price an entry order is sent at, and what happens if it does not fill.",
    keys: ["entry_order_type", "entry_limit_ref", "entry_limit_offset", "cap_at_rung",
           "entry_on_timeout", "entry_fill_timeout"] },

  { id: "sessions", title: "Sessions", icon: "◷",
    lead: "The hours this ladder is allowed to open and add. America/New_York.",
    keys: ["session_mode", "trade_overnight", "trade_premarket", "trade_regular",
           "trade_afterhours", "session_start", "session_end", "use_wind_down",
           "wind_down_start", "allow_extended_hours", "fractional_sessions"] },

  { id: "risk", title: "Risk", icon: "!",
    lead: "This ladder's own circuit breakers.",
    keys: ["daily_loss_limit", "auto_reconcile", "autostart"] },
];

/* A field added to STRATEGY_FIELDS and forgotten here would silently vanish
   from the Settings tab. It lands in the last card instead. */
const GROUPED = new Set(FIELD_GROUPS.flatMap((g) => g.keys));
export const UNGROUPED = ALL.map((f) => f.k).filter((k) => !GROUPED.has(k));
if (UNGROUPED.length) FIELD_GROUPS[FIELD_GROUPS.length - 1].keys.push(...UNGROUPED);

/* The selects and switches whose value decides which OTHER fields mean
   anything. Changing one re-evaluates the whole visible set. */
export const GOVERNORS = [
  "size_mode", "fractional", "first_entry", "entry_ma", "add_mode", "add_trigger",
  "exit_mode", "reversal_mode", "unwind_on_gap", "basket_stop_enabled", "strategy",
  "entry_order_type", "session_mode", "use_wind_down",
];

/* a form hands back ""/"true"; the server hands back real booleans; the two
   string switches hand back "on"/"off" */
const on = (v) => v === true || v === 1 || v === "true" || v === "on" || v === "1";
const str = (v, d = "") => (v === undefined || v === null || v === "" ? d : String(v));
const num = (v, d = 0) => { const n = Number(v); return Number.isFinite(n) ? n : d; };
const dol = (v, dp = 2) => "$" + num(v).toFixed(dp);
const words = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight",
               "nine", "ten"];
const count = (n) => (words[n] !== undefined ? words[n] : String(n));

/* ------------------------------------------------------------- visibility */
/* Every key that is INERT for the modes `v` is in. Roughly half the form on a
   typical ticker. Hidden, never greyed: a greyed field still has to be read
   before it can be ignored. */
export function hiddenKeys(v) {
  const h = new Set();
  const hide = (...ks) => ks.forEach((k) => h.add(k));

  // ---- sizing: only the divisor of the mode in use ----
  const size = str(v.size_mode, "fixed");
  if (size !== "dollars") hide("lot_dollars");
  if (size !== "atr_risk") hide("risk_dollars", "atr_stop_mult", "atr_period");
  // min_shares / max_shares clamp EVERY mode (engine._lot_shares), fixed
  // included, so they are never hidden.
  if (!on(v.fractional)) hide("fractional_sessions");

  // ---- entry ----
  if (str(v.first_entry, "red_bar") !== "with_trend") hide("entry_ma", "entry_ma_period");
  if (str(v.entry_ma, "vwap") !== "ema") hide("entry_ma_period");

  // ---- adds: ONE encoding of the rung distance, never four ----
  const am = str(v.add_mode, "points");
  if (am !== "points") hide("add_distance");
  if (am !== "percent") hide("add_percent");
  if (am !== "atr") hide("add_k", "add_floor");
  if (str(v.add_trigger, "touch") !== "touch") hide("add_depth");

  // ---- exits ----
  if (str(v.exit_mode, "limit") !== "trail") hide("trail_amount", "trail_use_broker_stop");
  const rm = str(v.reversal_mode, "off");
  if (rm !== "flatten") hide("unwind_min_lots", "unwind_stage_hours", "unwind_cooldown_h");
  if (rm !== "reverse") hide("reverse_ttl_h", "reverse_cooldown_h", "reverse_max_spread_pct");
  // the gap trigger and the chase timer belong to BOTH staged modes: a reverse
  // ladder inside its cooldown falls through to the same stage-1 path
  if (rm === "off") hide("basket_chase_s", "unwind_on_gap", "gap_atr");
  if (!on(v.unwind_on_gap)) hide("gap_atr");
  if (!on(v.basket_stop_enabled)) hide("basket_stop_atr");

  // ---- strategy ----
  if (!str(v.strategy).trim()) hide("strategy_entries", "strategy_exits");

  // ---- order pricing: nothing computes a limit price for a market order ----
  if (str(v.entry_order_type, "limit") === "market") {
    hide("entry_limit_ref", "entry_limit_offset", "cap_at_rung");
  }

  // ---- sessions: three mutually exclusive sub-modes, rendered one at a time ----
  const sm = str(v.session_mode, "times");
  const boxes = ["trade_overnight", "trade_premarket", "trade_regular", "trade_afterhours"];
  const clock = ["session_start", "session_end", "use_wind_down", "wind_down_start",
                 "allow_extended_hours"];
  if (sm === "always") hide(...boxes, ...clock);
  else if (sm === "sessions") hide(...clock);
  else hide(...boxes);
  if (sm !== "times" || !on(v.use_wind_down)) hide("wind_down_start");

  return h;
}

/* Read the live form rather than the config, so the visible set follows what
   the user has just picked and not what the server last said. Reads the
   elements, NOT FormData: applyVisibility disables what it hides, and a
   governing select that is itself hidden (use_wind_down under session_mode)
   still has to be readable or its dependants never come back. */
export function readValues(form) {
  const v = {};
  for (const e of form.elements) if (e.name) v[e.name] = e.value;
  return v;
}

/* ------------------------------------------------------------- rendering */
/* One group's fields. Each is wrapped so the label, the input and the hint
   hide together. */
export function groupHTML(group, cfg, { omit = [] } = {}) {
  return group.keys
    .filter((k) => !omit.includes(k))
    .map((k) => {
      const f = fieldByKey(k);
      if (!f) return "";
      return `<div class="fld" data-k="${k}">${labelHTML(f, cfg)}</div>`;
    }).join("");
}

/* Toggle, never re-render: the DOM keeps every value the user has typed in
   fields that are not changing, which is exactly what a re-render loses.
   Returns { shown, hidden } per group id so a card can say how much it is
   holding back. */
export function applyVisibility(root, v) {
  const h = hiddenKeys(v);
  const out = {};
  for (const g of FIELD_GROUPS) out[g.id] = { shown: 0, hidden: 0 };
  for (const node of root.querySelectorAll(".fld[data-k]")) {
    const k = node.dataset.k;
    const off = h.has(k);
    if (node.hidden !== off) {
      node.hidden = off;
      /* DISABLED, not merely hidden. Two reasons, both load-bearing:
         a hidden control still takes part in HTML5 constraint validation, and
         one that fails it blocks submit() with nothing on screen to fix
         ("An invalid form control is not focusable" in the console and a Save
         button that does nothing); and a disabled control is left out of
         FormData, so formPatch sends only the settings that are live in the
         modes chosen -- the server keeps the rest exactly as they were. */
      for (const c of node.querySelectorAll("input, select, textarea")) c.disabled = off;
    }
    const gid = node.closest("[data-group]");
    const rec = gid && out[gid.dataset.group];
    if (rec) rec[off ? "hidden" : "shown"] += 1;
  }
  return out;
}

/* -------------------------------------------------------------- summaries */
/* One plain-English line per group describing what the ticker does RIGHT NOW,
   so the page reads as a description of the strategy before it reads as a
   form. `side` only changes the preposition (a short ladder's rungs go up). */
export function summaries(v, { side = "long" } = {})  {
  const short = side === "short";
  const away = short ? "over" : "under";
  const toward = short ? "below" : "above";
  const s = {};

  // ---- sizing ----
  const size = str(v.size_mode, "fixed");
  const lots = num(v.max_lots, 0);
  const per = size === "dollars" ? `${dol(v.lot_dollars, 0)} of stock`
    : size === "atr_risk" ? `whatever risks ${dol(v.risk_dollars, 0)} at ${num(v.atr_stop_mult, 2)}× ATR`
    : `${num(v.shares_per_lot)} share${num(v.shares_per_lot) === 1 ? "" : "s"}`;
  s.sizing = `Each lot is ${per}, at most ${lots} lot${lots === 1 ? "" : "s"} deep`
    + (num(v.f_ladder) > 0
        ? ` and never more than ${(num(v.f_ladder) * 100).toFixed(0)}% of equity over ${num(v.n_target)} rungs`
        : "")
    + (on(v.fractional) ? ", fractional shares allowed" : "") + ".";

  // ---- entry ----
  const fe = str(v.first_entry, "red_bar");
  const bias = str(v.bias_source, "rd") === "1h"
    ? "The 1-hour SuperTrend picks the side."
    : "The side needs R and D to agree.";
  s.entry = (fe === "with_trend"
    ? `The first lot opens on the first ${str(v.bar_size, "1Min")} candle in the trend's colour, `
      + `on the trend's side of the ${str(v.entry_ma, "vwap") === "ema"
          ? `${num(v.entry_ma_period, 20)}-bar EMA` : "session VWAP"}.`
    : fe === "immediate"
      ? "The first lot opens immediately, with no candle rule."
      : `The first lot opens on the first red ${str(v.bar_size, "1Min")} close.`) + " " + bias;

  // ---- adds ----
  const am = str(v.add_mode, "points");
  const depth = num(v.add_depth, 1);
  const dist = am === "points" ? `${dol(v.add_distance)} ${away} the last fill`
    : am === "percent" ? `${num(v.add_percent)}% ${away} the last fill`
    : am === "atr" ? `${num(v.add_k, 1)}× the 15-minute ATR ${away} the last fill `
                     + `(never tighter than ${dol(v.add_floor)})`
    : `any close ${short ? "above" : "below"} the ladder average`;
  const anchor = str(v.add_anchor, "last_fill") === "last_open"
    ? "the newest open lot" : "the last fill of any kind";
  s.adds = str(v.add_trigger, "touch") === "touch"
    ? `A limit rests ${dist}, ${count(depth)} rung${depth === 1 ? "" : "s"} deep, measured from ${anchor}.`
    : `A ${str(v.bar_size, "1Min")} close past ${dist} adds a rung, measured from ${anchor}.`;

  // ---- exits ----
  const ex = [`Each lot leaves ${dol(v.take_profit)} ${toward} its own fill`];
  if (str(v.exit_mode, "limit") === "trail") {
    ex[0] += `, then trails ${dol(v.trail_amount)} from the peak`
      + (on(v.trail_use_broker_stop) ? " behind a broker stop" : " in this process only");
  } else ex[0] += ", on a resting limit at Alpaca";
  const rm = str(v.reversal_mode, "off");
  if (rm === "reverse") {
    ex.push(`On a 1-hour flip the whole ladder closes and re-opens on the other side, lot for lot`
      + (num(v.reverse_cooldown_h) > 0 ? `, at most once every ${num(v.reverse_cooldown_h)} h` : ""));
  } else if (rm === "flatten") {
    ex.push(`At ${num(v.unwind_min_lots, 4)} lots deep a 1-hour flip closes the deepest half, `
      + `the rest ${num(v.unwind_stage_hours, 4)} h later if the 4-hour regime agrees`);
  }
  if (on(v.basket_tp_enabled)) ex.push("a basket take-profit sits at the average");
  if (on(v.basket_stop_enabled)) ex.push(`a basket stop sits ${num(v.basket_stop_atr)}× ATR below it`);
  if (num(v.ladder_max_bars) > 0) ex.push(`a lot older than ${num(v.ladder_max_bars)} session bars is timed out`);
  s.exits = ex.join(". ") + ". There is no stop loss on an unarmed lot.";

  // ---- strategy & gate ----
  const slug = str(v.strategy).trim();
  const sides = { auto: "long only", long: "long only", short: "short only",
                  both: "either side" }[str(v.side_mode, "auto")] || str(v.side_mode);
  s.gate = (slug
    ? `Strategy "${slug}" decides ${[on(v.strategy_entries) && "entries",
        on(v.strategy_exits) && "exits"].filter(Boolean).join(" and ") || "nothing yet"}. `
    : "The ladder decides; no strategy document is attached. ")
    + `Opens ${sides}`
    + (on(v.trend_filter)
        ? `, and only with the three-layer trend filter agreeing`
          + (on(v.trend_flat_blocks_entries) ? " (a flat stack blocks entries)" : " (a flat stack still allows entries)")
        : ", with the trend filter off")
    + ".";

  // ---- order pricing ----
  s.pricing = str(v.entry_order_type, "limit") === "market"
    ? `Entries are market orders; an unfilled remainder after ${num(v.entry_fill_timeout, 45)} s is `
      + `${str(v.entry_on_timeout) === "market" ? "escalated" : "kept as a partial lot"}.`
    : `Entries are limit orders pegged to ${ {ask: "the ask", mid: "the mid", bid: "the bid",
         rung: "the rung"}[str(v.entry_limit_ref, "ask")] || str(v.entry_limit_ref) } `
      + `${num(v.entry_limit_offset) < 0 ? "-" : "+"} ${dol(Math.abs(num(v.entry_limit_offset)))}`
      + (on(v.cap_at_rung) ? ", never above the rung" : ", uncapped")
      + `, cancelled after ${num(v.entry_fill_timeout, 45)} s`
      + (str(v.entry_on_timeout) === "market" ? " with the rest bought at market" : "") + ".";

  // ---- sessions ----
  const sm = str(v.session_mode, "times");
  if (sm === "always") s.sessions = "Trades in any session the symbol is open in, 24/5.";
  else if (sm === "sessions") {
    const picked = [on(v.trade_overnight) && "overnight", on(v.trade_premarket) && "pre-market",
                    on(v.trade_regular) && "regular hours", on(v.trade_afterhours) && "after-hours"]
      .filter(Boolean);
    s.sessions = picked.length ? `Trades in ${picked.join(", ")}.`
      : "No session is ticked — this ladder never opens or adds.";
  } else {
    s.sessions = `Trades ${str(v.session_start, "—")}–${str(v.session_end, "—")} ET`
      + (on(v.use_wind_down) ? `, winding down from ${str(v.wind_down_start, "—")}` : "")
      + (on(v.allow_extended_hours) ? ", extended-hours orders allowed" : "") + ".";
  }
  if (on(v.fractional)) {
    s.sessions += ` Fractional lots are further confined to ${
      {regular: "09:30–16:00", extended: "04:00–20:00", all: "any session"}[
        str(v.fractional_sessions, "regular")] || str(v.fractional_sessions)}.`;
  }

  // ---- risk ----
  s.risk = (num(v.daily_loss_limit) > 0
    ? `Halts itself at ${dol(v.daily_loss_limit, 0)} of realized loss today`
    : "No daily loss limit — this ladder never halts itself")
    + (on(v.auto_reconcile) ? ", follows Alpaca when the two disagree" : ", halts when Alpaca disagrees")
    + (on(v.autostart) ? ", and starts with the dashboard." : ", and stays stopped on boot.");

  return s;
}
