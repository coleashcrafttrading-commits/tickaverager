/* ============================================================================
   btread.js -- how to READ a backtest. Pure data, no DOM, no templates.

   CLAUDE.md's "Reading a backtest" section is a list of ways this repo has
   already been lied to by its own results page. Every one of them is a
   function here rather than a paragraph in a card, so the rule is applied to
   every row of every sweep instead of being read once and forgotten:

     total_pl, never net_profit alone   -> decompose()
     a 100% win rate is "no stop loss"  -> caveats() no_stop
     profit_factor is null, not inf     -> caveats() pf_null
     sortino is null under 3 down days  -> caveats() sortino_null
     max_lots_held == cap bound it      -> caveats() cap_bound
     rank by P/L per $ of drawdown      -> plPerDD() + rank()

   It is a separate module from the view for two reasons. It can be compiled
   and run offline (test_btview.py does exactly that, which a file full of
   template literals cannot be -- Duktape's Babel overflows its C stack on
   them), and the Bank room and the Backtest room must rank by the SAME
   function or the leaderboard and the results table disagree about which
   run was better.

   NOTHING HERE RETURNS 0 FOR A NUMBER NOBODY MEASURED. A missing value comes
   back null with a reason beside it and the view draws a dash. This repo has
   been burned by a rendered 0.00 more than once.
   ========================================================================= */
"use strict";

/* Below this, a result is listed but never ranked. riskbank.leaderboard uses
   the same number for the same reason: three lucky trades beat a hundred good
   ones on every ratio ever invented. */
export const MIN_TRADES_TO_RANK = 10;

/* A window shorter than this is a hypothesis, not a finding. Stated in days
   because that is the unit the owner picks in the form. */
export const THIN_WINDOW_DAYS = 30;

export const measured = (v) =>
  v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));

export const num = (v, dflt = null) => (measured(v) ? Number(v) : dflt);

/* ------------------------------------------------------------- the headline
   net_profit and open_pl are DIFFERENT NUMBERS and the difference is the whole
   story. The repo's own example: +$2,190 realised, -$2,577 open, -$387 what
   the account would actually show. A page that prints the first one large and
   the second one small IS the lie, so the view is handed all three at once
   with the arithmetic already checked. */
export function decompose(s) {
  s = s || {};
  const net = num(s.net_profit);
  const open = num(s.open_pl);
  const total = num(s.total_pl);
  const both = net !== null && open !== null && total !== null;
  return {
    net, open, total,
    open_count: num(s.open_at_end, 0),
    /* The backend adds these up itself. If they ever stop adding up, say so on
       screen rather than picking whichever one flatters the run. */
    adds_up: both ? Math.abs(net + open - total) < 0.011 : null,
    /* The specific shape the honesty rule is about: banked winners with a
       bigger unbooked loss sitting behind them. */
    realised_hides_loss: both && net > 0 && total < 0,
  };
}

/* ------------------------------------------------------------- the ranking
   P/L per dollar of drawdown, never profit. Ranking by profit selects for
   whichever run took the most risk, which is the opposite of the question.

   A drawdown of exactly zero returns null, NOT Infinity and not a huge
   number: "this never went underwater" is real, but it is not comparable
   against a run that did, so it sorts last and says why. */
export function plPerDD(s) {
  s = s || {};
  const pl = num(s.total_pl);
  const dd = Math.abs(num(s.max_drawdown, 0));
  if (pl === null) {
    return { value: null, reason: "no total P/L was reported for this run" };
  }
  if (!dd) {
    return { value: null,
             reason: "no drawdown was measured, so there is nothing to divide "
                   + "by -- real, but not comparable" };
  }
  return { value: pl / dd, reason: "" };
}

/* The ranking number AS IT MAY BE SHOWN. plPerDD is the arithmetic; this adds
   the sample-size floor, because a ratio over two trades and a ratio over
   ninety drawn in the same column, in the same weight, IS the comparison the
   floor exists to prevent. Every place that prints the score calls this one,
   so the headline card, the comparison table and the sweep table cannot
   disagree about whether a run is rankable. */
export function score(s) {
  const n = num((s || {}).total_trades, 0);
  if (n < MIN_TRADES_TO_RANK) {
    return { value: null,
             reason: `${n} closed trade${n === 1 ? "" : "s"} is under `
                   + `${MIN_TRADES_TO_RANK} -- listed, never ranked` };
  }
  return plPerDD(s);
}

/* One row of a sweep, ranked or excluded, with the reason it was excluded.
   Excluded rows are still RETURNED -- dropping them silently is how a sweep
   comes to look like it had six combinations when it ran two hundred. */
export function rank(rows, opt) {
  const minTrades = (opt && opt.min_trades) || MIN_TRADES_TO_RANK;
  const ranked = [];
  const excluded = [];
  (rows || []).forEach((r, i) => {
    const row = { i, row: r, score: null, reason: "" };
    if (r && r.error) {
      row.reason = "the combination failed";
      excluded.push(row);
      return;
    }
    const n = num((r || {}).total_trades, 0);
    if (n < minTrades) {
      row.reason = n === 0
        ? "it closed no trades"
        : `only ${n} closed trade${n === 1 ? "" : "s"} -- under ${minTrades}`;
      excluded.push(row);
      return;
    }
    const sc = plPerDD(r);
    row.score = sc.value;
    row.reason = sc.reason;
    ranked.push(row);
  });
  /* null score last, not first: "no drawdown" must never outrank a measured
     ratio just because a null sorts high in a naive comparator. */
  ranked.sort((a, b) => {
    if ((a.score === null) !== (b.score === null)) return a.score === null ? 1 : -1;
    return (b.score || 0) - (a.score || 0);
  });
  return { ranked, excluded, min_trades: minTrades };
}

/* -------------------------------------------------------------- the caveats
   Everything the numbers do not say by themselves. level is "bad" when the
   headline number is misleading without it, "warn" when it bounds what may be
   concluded, "info" when it only explains a dash. */
export function caveats(s, ctx) {
  s = s || {};
  ctx = ctx || {};
  const out = [];
  const trades = num(s.total_trades, 0);
  const d = decompose(s);

  if (ctx.look_ahead) {
    out.push({ code: "look_ahead", level: "bad",
               text: "The runner caught this strategy reading a future bar. "
                   + "The result is not a result." });
  }

  if (d.realised_hides_loss) {
    out.push({ code: "realised_hides_loss", level: "bad",
               text: "Realised P/L is positive and the run still lost money: "
                   + "the open inventory it never closed is bigger than "
                   + "everything it banked. Total P/L is the number." });
  }

  if (d.adds_up === false) {
    out.push({ code: "does_not_add_up", level: "bad",
               text: "Realised plus open does not equal total. Two of these "
                   + "three numbers came from different places -- do not "
                   + "reconcile them on screen, find out which is wrong." });
  }

  /* The one the repo states twice, in capitals, in two files. */
  if (trades && num(s.win_rate, 0) >= 95 && !num(s.losing_trades, 0)) {
    out.push({ code: "no_stop", level: "bad",
               text: "Every closed trade is a winner. That is what a strategy "
                   + "with NO STOP LOSS always looks like -- losers are simply "
                   + "never closed. Read max drawdown and open P/L, not the "
                   + "win rate." });
  }

  if (ctx.hit_max_lots || s.hit_max_lots) {
    out.push({ code: "cap_bound", level: "warn",
               text: "Max lots held equals the cap, so the CAP bound this "
                   + "result. You are comparing caps, not the parameter you "
                   + "swept." });
  }

  const fill = num(s.fill_rate_pct);
  if (fill !== null && fill < 100) {
    out.push({ code: "entries_missed", level: "warn",
               text: `Only ${fill.toFixed(1)}% of the entries this strategy `
                   + `asked for could fill on these bars`
                   + (num(s.entries_missed, 0)
                      ? ` (${num(s.entries_missed, 0)} missed).` : ".")
                   + " The rest never happened and are not in any number here." });
  }

  if (trades && trades < MIN_TRADES_TO_RANK) {
    out.push({ code: "thin_sample", level: "warn",
               text: `${trades} closed trade${trades === 1 ? "" : "s"} is too `
                   + `few to rank. Listed, never ranked.` });
  }

  const days = num(s.span_days);
  if (days !== null && days < THIN_WINDOW_DAYS) {
    out.push({ code: "short_window", level: "warn",
               text: `${days.toFixed(0)} day${days < 1.5 ? "" : "s"} of one `
                   + `symbol is a hypothesis, not a finding. Say which you have.` });
  }

  if (trades && s.profit_factor === null) {
    out.push({ code: "pf_null", level: "info",
               text: "No losing trade in this window, so there is no profit "
                   + "factor. Null, never infinity: a fact about the window." });
  }

  if (s.sortino === null || s.sortino === undefined) {
    out.push({ code: "sortino_null", level: "info",
               text: "Fewer than three down days, so Sortino is not "
                   + "meaningful and is not printed." });
  }

  if (!trades && !num(s.open_at_end, 0)) {
    out.push({ code: "no_trades", level: "warn",
               text: "This run never opened a position. Every number below is "
                   + "the absence of a strategy, not the result of one." });
  }
  return out;
}

export const worstLevel = (cs) =>
  (cs || []).some((c) => c.level === "bad") ? "bad"
  : (cs || []).some((c) => c.level === "warn") ? "warn"
  : (cs || []).length ? "info" : "";

/* ------------------------------------------------------------- benchmarks
   btstats computes THREE comparisons and the old page drew one. The passive
   twin is the one that is actually fair -- signed, so a short strategy is not
   benchmarked against a long position, and time-weighted, so a strategy in the
   market 12% of the time is compared against 12% of a position. Buy-and-hold
   is the blunt one everybody knows and is always LONG.  */
export function benchmarks(s) {
  s = s || {};
  const mine = num(s.total_pl);
  const rows = [];
  const bh = num(s.buy_hold_dollars);
  if (bh !== null) {
    rows.push({
      key: "buy_hold", label: "Bought 100 shares and held",
      value: bh, pct: num(s.buy_hold_pct),
      edge: num(s.vs_buy_hold, mine === null ? null : mine - bh),
      note: "always long, whatever side the strategy took",
    });
  }
  const twin = num(s.twin_dollars);
  if (twin !== null) {
    rows.push({
      key: "twin", label: "The passive twin",
      value: twin, pct: null,
      edge: num(s.edge_vs_twin, mine === null ? null : mine - twin),
      shares: num(s.twin_shares), drawdown: num(s.twin_drawdown),
      note: "the same average exposure, signed and time-weighted, held "
          + "through the whole window with no timing at all",
    });
  }
  return { mine, rows };
}

/* -------------------------------------------------------- series -> candles
   A candle needs a high and a low, and an equity curve is one number per bar.
   Bucketing CONTIGUOUS ranges of samples gives a real open/high/low/close for
   the bucket -- it is the same operation a daily chart performs on minutes,
   and it is honest as long as nobody is told the wick came from anywhere else.

   Buckets are assigned by INDEX, never by wall-clock time. That is the fix for
   the gap the owner sees when he zooms in: spacing candles by timestamp leaves
   a hole across every night, weekend and halt, and on a 1-minute ladder most
   of the axis is hole. Ordinal slots, real timestamps on the labels.  */
export function bucketOHLC(values, stamps, buckets) {
  const v = values || [];
  const n = v.length;
  const out = { points: [], samples: n, per: 0, dojis: 0, reason: "" };
  if (!n) {
    out.reason = "there is no series to draw";
    return out;
  }
  let b = Math.max(1, Math.min(Math.round(buckets || n), n));
  out.per = n / b;
  for (let k = 0; k < b; k++) {
    const a = Math.floor((k * n) / b);
    const z = Math.max(a + 1, Math.floor(((k + 1) * n) / b));
    let o = null, h = -Infinity, l = Infinity, c = null, count = 0;
    for (let i = a; i < z && i < n; i++) {
      /* measured() first, then Number(). Number(null) is 0 and
         Number.isFinite(0) is true, so a null in the series would become a
         real sample at zero -- a low the account never touched, drawn as a
         wick. That is this repo's oldest bug wearing a chart. */
      if (!measured(v[i])) continue;
      const x = Number(v[i]);
      if (o === null) o = x;
      if (x > h) h = x;
      if (x < l) l = x;
      c = x;
      count++;
    }
    if (o === null) continue;
    if (h === l) out.dojis++;
    out.points.push({
      t: (stamps || [])[Math.min(n - 1, z - 1)] || "",
      t0: (stamps || [])[a] || "",
      o, h, l, c, v: count, i0: a, i1: z - 1,
    });
  }
  /* The same note hub.py appends for the same reason. One sample per bucket
     means every candle is a doji, and drawing a field of crosses instead of
     saying "there is one number per slot here" is a chart pretending to know
     more than it does. */
  if (out.points.length && out.dojis / out.points.length >= 0.5) {
    out.reason = `${out.dojis} of ${out.points.length} buckets hold a single `
      + `sample, so those candles are dojis -- line or bar is the honest form `
      + `at this detail.`;
  }
  return out;
}

/* Per-trade P/L as its own series. Bar form is what this is for; candle form
   buckets several trades into one, which is still a real open/high/low/close
   over that run of trades. */
export function tradeSeries(trades) {
  const pnl = [], stamps = [];
  for (const t of trades || []) {
    pnl.push(Number(t.pnl) || 0);
    stamps.push(t.exit_t || t.entry_t || "");
  }
  return { values: pnl, stamps };
}

/* A sweep line -> {param: [values]}. Kept here rather than in the view so the
   count shown beside the box and the count the server expands are the same
   arithmetic. */
export function parseSweep(txt) {
  const out = {};
  for (const line of String(txt || "").split(/[\n;]/)) {
    const t = line.trim();
    if (!t || t.startsWith("#") || !t.includes("=")) continue;
    const at = t.indexOf("=");
    const k = t.slice(0, at).trim();
    const vals = t.slice(at + 1).split(",").map((x) => x.trim()).filter(Boolean)
      .map((x) => {
        if (/^-?\d+$/.test(x)) return parseInt(x, 10);
        if (/^-?\d*\.\d+$/.test(x)) return parseFloat(x);
        if (x === "true") return true;
        if (x === "false") return false;
        return x;
      });
    if (k && vals.length) out[k] = vals;
  }
  return out;
}

export const combos = (sw) =>
  Object.values(sw || {}).reduce((a, v) => a * ((v || []).length || 1), 1);

/* Which subsystem a swept key reaches. The sweep box accepts five prefixes
   and a typo in one of them is silently a ladder setting that does not exist,
   so the box says what it read rather than waiting for the run to say
   nothing. */
export function sweepKinds(sw) {
  const out = [];
  for (const k of Object.keys(sw || {})) {
    let kind = "run setting";
    if (k.startsWith("p.")) kind = "coded PARAMS value";
    else if (k.startsWith("ind.")) kind = "indicator inside the document";
    else if (k.startsWith("target.")) kind = "strategy document target";
    else if (k.startsWith("stop.")) kind = "strategy document stop";
    out.push({ key: k, kind, n: (sw[k] || []).length });
  }
  return out;
}
