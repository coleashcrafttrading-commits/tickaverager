/* ============================================================================
   tkmetrics.js -- a ticker's OWN record, as arithmetic and nothing else.

   No DOM, no fetch, no template literals: this file is what test_tickerview.py
   runs through the Babel inside dukpy, so the numbers on the ticker page are
   proved against real inputs rather than eyeballed in a browser.

   Two rules carried over from the Python side and not re-invented here:

     * the envelope is {value, n, unit, reason, thin} -- optperf's shape, the
       one hub.metric() emits. A number that renders has to be able to say how
       much data is behind it, so there is ONE shape on the wire and on screen.
     * a value nobody measured is null with a reason. NEVER 0. This repo has
       shipped a confident zero more than once and it is the reason the page
       renders a dash and the sentence that explains it.

   `unit: "pct"` is a FRACTION -- 0.0123 is +1.23% -- because that is what
   hub.py and optperf.py put on the wire. Rendering is the caller's job.

   Everything here is a FALLBACK. `fromHub()` is asked first for every figure,
   so the day hub.py grows a win rate this file stops computing one and the
   page reads the server's number with no edit here.
   ========================================================================= */
"use strict";

/* The same thresholds optperf.py uses, and for the same reason: a win rate
   over four trades is not a win rate, and expectancy needs more than a rate
   does because it is carried by the tail. If these ever disagree with
   optperf.MIN_TRADES_FOR_RATE / MIN_TRADES_FOR_EXPECTANCY, the Python is
   right and this is stale. */
export var MIN_N_RATE = 20;
export var MIN_N_EXPECTANCY = 30;

/* -------------------------------------------------------------- envelope */
export function envelope(value, n, unit, reason, thin) {
  var v = (value === undefined ? null : value);
  return {
    value: v,
    n: Number(n) || 0,
    unit: unit || "usd",
    // reason rides along only when it is going to be read: for a dash, or as
    // a caveat on a number too thin to trust. On a solid number it is noise.
    reason: (v === null || thin) ? (reason || null) : null,
    thin: !!thin,
  };
}

export function dashed(n, unit, why) {
  return envelope(null, n, unit, why, false);
}

/* --------------------------------------------------------------- numbers */
function num(x) {
  if (x === null || x === undefined || x === "") return null;
  var v = Number(x);
  return isFinite(v) ? v : null;
}

function round2(x) {
  return x === null ? null : Math.round(x * 100) / 100;
}

/* ---------------------------------------------------------- hub deference */
/* The server's own metric for `key`, or null when it does not publish one.
   Only a real envelope counts: a bare number would lose the sample size,
   which is the half of the contract that stops a thin figure reading as a
   measured one. */
export function fromHub(d, key) {
  var M = d && d.metrics;
  if (!M || typeof M !== "object") return null;
  var m = M[key];
  if (!m || typeof m !== "object") return null;
  if (!Object.prototype.hasOwnProperty.call(m, "value")) return null;
  if (!Object.prototype.hasOwnProperty.call(m, "n")) return null;
  return m;
}

/* ---------------------------------------------------------------- trades */
/* The closed trades on this ticker, OLDEST FIRST.

   `history.trades` arrives newest-first from /api/hub/ticker/{sym} because
   that is the order a table wants; every statistic below wants the other one,
   and a curve built on the wrong order reports the drawdown backwards. */
export function closedTrades(d) {
  var h = (d && d.history) || {};
  var rows = h.trades || [];
  var out = [];
  for (var i = 0; i < rows.length; i++) {
    if (num(rows[i].realized) === null) continue;
    out.push(rows[i]);
  }
  out.sort(function (a, b) {
    var x = String(a.t || ""), y = String(b.t || "");
    return x < y ? -1 : x > y ? 1 : 0;
  });
  return out;
}

/* Why there is nothing to measure. The server's own sentence wins -- it knows
   whether the journal was unreadable or simply empty, and this file does
   not. */
function noTradeReason(d) {
  var h = (d && d.history) || {};
  if (h.why) return String(h.why);
  return "no closed trade on record for this ticker";
}

/* ---------------------------------------------------- the realised curve */
/* Cumulative realised P/L, oldest first, as [{t, n, c}].

   `history.curve` is the server's and is preferred. Rebuilding it here is the
   fallback for a payload that carries trades and no curve. */
export function realisedCurve(d) {
  var h = (d && d.history) || {};
  var c = h.curve || [];
  var out = [];
  var i;
  for (i = 0; i < c.length; i++) {
    var v = num(c[i].c);
    if (v === null) continue;
    out.push({ t: c[i].t, n: num(c[i].n) || (out.length + 1), c: v });
  }
  if (out.length) return out;
  var rows = closedTrades(d), run = 0, built = [];
  for (i = 0; i < rows.length; i++) {
    run = round2(run + (num(rows[i].realized) || 0));
    built.push({ t: rows[i].t, n: i + 1, c: run });
  }
  return built;
}

/* ------------------------------------------------------------- the stats */
function winRate(d, rows) {
  var hub = fromHub(d, "win_rate");
  if (hub) return hub;
  var n = rows.length;
  if (!n) return dashed(0, "pct", noTradeReason(d));
  var wins = 0;
  for (var i = 0; i < rows.length; i++) {
    if ((num(rows[i].realized) || 0) > 0) wins++;
  }
  // a FRACTION, the unit hub.py and optperf.py both put on the wire
  return envelope(Math.round((wins / n) * 1e6) / 1e6, n, "pct",
                  n < MIN_N_RATE
                    ? "only " + n + " closed trade" + (n === 1 ? "" : "s")
                      + " -- a rate needs " + MIN_N_RATE
                      + " before it means anything"
                    : null,
                  n < MIN_N_RATE);
}

function expectancy(d, rows) {
  var hub = fromHub(d, "expectancy");
  if (hub) return hub;
  var n = rows.length;
  if (!n) return dashed(0, "usd", noTradeReason(d));
  var sum = 0;
  for (var i = 0; i < rows.length; i++) sum += (num(rows[i].realized) || 0);
  return envelope(round2(sum / n), n, "usd",
                  n < MIN_N_EXPECTANCY
                    ? "only " + n + " closed trade" + (n === 1 ? "" : "s")
                      + " -- expectancy is carried by the tail and needs "
                      + MIN_N_EXPECTANCY
                    : null,
                  n < MIN_N_EXPECTANCY);
}

function meanOf(rows, keep, unit, empty) {
  var sum = 0, n = 0;
  for (var i = 0; i < rows.length; i++) {
    var v = num(rows[i].realized) || 0;
    if (!keep(v)) continue;
    sum += v; n++;
  }
  if (!n) return dashed(0, unit, empty);
  return envelope(round2(sum / n), n, unit, null, false);
}

/* Gross win over gross loss. A book with no loser yet has NOTHING to divide
   by -- reporting that as a big number, or worse as zero, is the exact lie
   this repo keeps having to take back out. */
function profitFactor(d, rows) {
  var hub = fromHub(d, "profit_factor");
  if (hub) return hub;
  if (!rows.length) return dashed(0, "ratio", noTradeReason(d));
  var up = 0, down = 0, nd = 0;
  for (var i = 0; i < rows.length; i++) {
    var v = num(rows[i].realized) || 0;
    if (v > 0) up += v;
    else if (v < 0) { down += -v; nd++; }
  }
  if (!nd) {
    return dashed(rows.length, "ratio",
                  "no losing trade yet, so there is nothing to divide by");
  }
  return envelope(Math.round((up / down) * 1000) / 1000, rows.length, "ratio",
                  rows.length < MIN_N_RATE
                    ? "only " + rows.length + " closed trade"
                      + (rows.length === 1 ? "" : "s") + " behind this ratio"
                    : null,
                  rows.length < MIN_N_RATE);
}

/* The deepest fall in THIS TICKER'S OWN realised curve.

   It is not this ticker's share of the account drawdown and must never be
   labelled as one: it ignores open positions entirely and it moves only when
   a lot closes. What it does answer is "how far under water did booking this
   ticker's trades ever put me", which is the question a per-ticker page is
   asked. Returned POSITIVE -- a $40 drawdown is 40, never -40 -- and the
   colour is the caller's business. */
function curveDrawdown(d) {
  var hub = fromHub(d, "drawdown");
  if (hub) return hub;
  var c = realisedCurve(d);
  if (!c.length) return dashed(0, "usd", noTradeReason(d));
  var peak = c[0].c, worst = 0, at = c[0].t;
  for (var i = 0; i < c.length; i++) {
    if (c[i].c > peak) peak = c[i].c;
    var fall = peak - c[i].c;
    if (fall > worst) { worst = fall; at = c[i].t; }
  }
  var m = envelope(round2(worst), c.length, "usd", null, false);
  m.peak_at = at;
  return m;
}

/* How long a lot is held. The journal records `hold_seconds` on every close;
   /api/hub/ticker/{sym} does not currently forward it, which is why this can
   come back as a dash that names the missing field rather than as a zero. */
function holdSeconds(d, rows) {
  var hub = fromHub(d, "hold_seconds");
  if (hub) return hub;
  var vals = [];
  for (var i = 0; i < rows.length; i++) {
    var v = num(rows[i].hold_seconds);
    if (v !== null && v > 0) vals.push(v);
  }
  if (!vals.length) {
    return dashed(0, "seconds", rows.length
      ? "the trade rows carry no hold_seconds -- /api/hub/ticker/{sym} does "
        + "not forward the journal's own field yet"
      : noTradeReason(d));
  }
  var sum = 0;
  for (var j = 0; j < vals.length; j++) sum += vals[j];
  var m = envelope(Math.round(sum / vals.length), vals.length, "seconds",
                   null, false);
  vals.sort(function (a, b) { return a - b; });
  m.median = vals[Math.floor(vals.length / 2)];
  m.max = vals[vals.length - 1];
  return m;
}

function bestWorst(rows, want) {
  if (!rows.length) return null;
  var pick = rows[0];
  for (var i = 1; i < rows.length; i++) {
    var v = num(rows[i].realized) || 0, p = num(pick.realized) || 0;
    if (want === "best" ? v > p : v < p) pick = rows[i];
  }
  return pick;
}

/* ------------------------------------------------------------ the bundle */
/* Everything the ticker page's record card renders, in one call.

   `source` is part of the answer and not decoration: `/api/hub/ticker/{sym}`
   builds `history` from journal.jsonl, which is the LADDER's log. An options
   play's realised P/L lives on its own strategy card and the two must not be
   added up on screen -- CLAUDE.md says so and so does the hub contract. */
export function tickerMetrics(d) {
  var rows = closedTrades(d);
  var h = (d && d.history) || {};
  var n = rows.length;
  var sum = 0;
  for (var i = 0; i < rows.length; i++) sum += (num(rows[i].realized) || 0);
  return {
    trades: envelope(n, n, "count", n ? null : noTradeReason(d), false),
    realized: n ? envelope(round2(sum), n, "usd", null, false)
                : dashed(0, "usd", noTradeReason(d)),
    win_rate: winRate(d, rows),
    expectancy: expectancy(d, rows),
    avg_win: meanOf(rows, function (v) { return v > 0; }, "usd",
                    n ? "no winning trade yet" : noTradeReason(d)),
    avg_loss: meanOf(rows, function (v) { return v < 0; }, "usd",
                     n ? "no losing trade yet" : noTradeReason(d)),
    profit_factor: profitFactor(d, rows),
    drawdown: curveDrawdown(d),
    hold: holdSeconds(d, rows),
    best: bestWorst(rows, "best"),
    worst: bestWorst(rows, "worst"),
    curve: realisedCurve(d),
    source: h.source || "journal.jsonl (ladder trades only)",
  };
}

/* --------------------------------------------------------------- caveats */
/* What a renderer needs to know about an envelope before it draws it, with no
   opinion about HTML. `dash` means print the em dash and the reason;
   `caveat` means print the number AND the reason. */
export function caveatOf(m) {
  if (!m || typeof m !== "object") {
    return { dash: true, caveat: "no metric was supplied", n: 0 };
  }
  var v = m.value;
  if (v === null || v === undefined) {
    return { dash: true, caveat: m.reason || "not measured", n: m.n || 0 };
  }
  return { dash: false, caveat: m.thin ? (m.reason || "thin sample") : null,
           n: m.n || 0 };
}
