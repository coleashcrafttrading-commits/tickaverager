/* ============================================================================
   Risk -- one question: am I about to lose more than I can afford?

   Dollar settings hide the thing that matters: a $0.10 target on a symbol that
   ranges $0.02 a minute is a completely different trade from the same target on
   one that ranges $0.15. Everything here is shown in both units.

   ------------------------------------------------- what this page became
   It was a wall of two tables. It is now a room, and each block answers one
   question in a picture with the table that produced it underneath:

     WHERE IS THE MONEY      a donut, by ticker and by strategy, off the two
                             sources that actually know -- /api/risk for the
                             ladders' cost basis and /api/hub/strategies for
                             every strategy including the options book. They
                             are DIFFERENT DENOMINATORS and the page says so
                             rather than adding them.
     HOW CONCENTRATED        ranked bars, plus the top-1 / top-3 share and the
                             HHI stated as "equivalent equal positions".
     HOW FAR HAVE I FALLEN   drawdown from the account's own high-water mark,
                             drawn off perf.daily()'s equity prints -- the
                             ACCOUNT curve, not the strategy logs, because a
                             log that records only take-profits cannot fall.
     WHAT CAPS ME            every guardrail as a gauge, sorted, with the one
                             that would stop the next order named. A cap set
                             to 0 is drawn OFF, never as 0% used.
     WHAT COULD BE DELIVERED assignment exposure: the shares a short option leg
                             hands this account if it finishes in the money.
                             Nothing else on this dashboard says that number.

   ------------------------------------------------------------------ rules
   Nothing here places an order, arms anything or writes anything: five GETs.
   A number nobody measured is a dash carrying its reason, never a zero -- and
   a drawdown nobody could measure is the reason, never a flat line along the
   bottom, because a flat line is a claim that this account has never fallen.

   The three things this page used to carry and no longer does (an Account stat
   row duplicating the topbar strip, a read-only mirror of the Settings
   guardrails, and the Profiles/Bank research tabs) are still where they went:
   the topbar, Settings -> Engine, and Research.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, el, esc, money, money0, sgn, px, qty, hashFor,
  panel, tile, tileGrid, dataTable, segmented, wireSegmented, chip,
  mfmt, mv, unmeasured,
} from "../core.js";
/* The CHARTS are the visuals agent's components -- one donut in this
   dashboard, not two. What riskmath.js adds is the arithmetic underneath them
   and the one control viz.js has no shape for: a LIMIT, which can be off, and
   off is not zero. */
import { donut, area, vizEmpty, dotscale, ratiobar } from "../viz.js";
/* the disclosure layer. This room's dashes carry the reason they are dashes,
   and a reason behind a hover is unreadable on a phone and from the keyboard. */
import { wireReasons } from "../reason.js";
import {
  concentration, drawdown, capRow, binding, ensureCapStyles,
} from "../riskmath.js";

/* Every read this page makes, and what it is the truth about. Five sources
   that can disagree; when they do, the page says which said what. */
let D = {
  risk: null,      // /api/risk           ladder exposure + the account's caps
  perf: null,      // /api/perf/report    the ACCOUNT equity curve and its risk
  strat: null,     // /api/hub/strategies exposure by strategy, options included
  plays: null,     // /api/optlab/plays   open option positions, for assignment
};
let ERR = {};
let forAccount = "";
let at = 0;                  // epoch ms of the last completed load
let busy = false;
let splitBy = "ticker";      // the donut's segmented control

/* /api/hub/strategies and /api/perf/report both spend from the 200/min
   TRADING budget the live ladders place orders through, and both are cached
   server-side (20 s and 60 s). Polling faster than the cache can answer
   differently buys nothing and costs the ladders. */
const MIN_MS = 20000;

VIEWS.risk = {
  title: () => "Risk",
  sub: () => "what a move against you costs, in dollars, in ATR and in equity",

  mount() {
    ensureCapStyles();
    ensureRiskStyles();
    if (forAccount !== S.account) {
      D = { risk: null, perf: null, strat: null, plays: null };
      ERR = {}; at = 0; forAccount = S.account;
    }
    el("view").innerHTML = `
      <div id="rkNotes"></div>
      <div id="rkHero"></div>
      <div class="grid main">
        <div>
          <div id="rkSplit"></div>
          <div id="rkDraw"></div>
        </div>
        <div>
          <div id="rkCaps"></div>
          <div id="rkAssign"></div>
        </div>
      </div>
      <div id="rkLadders"></div>
      <div id="rkStress"></div>`;
    load(true);
  },

  paint() {
    if (D.risk) render();
    load(false);
  },
};

async function load(force) {
  if (busy) return;
  if (!force && at && Date.now() - at < MIN_MS) return;
  busy = true;
  /* Each read is settled on its own. One failing source must not blank the
     other four -- a risk page that goes empty because the options ledger
     404'd is a risk page nobody can use during the incident that matters. */
  const jobs = [
    ["risk", "/api/risk"],
    ["perf", "/api/perf/report"],
    ["strat", "/api/hub/strategies"],
    ["plays", "/api/optlab/plays"],
  ];
  const got = await Promise.all(jobs.map(async ([k, url]) => {
    try { return [k, await GET(url), ""]; }
    catch (e) { return [k, null, e.message || String(e)]; }
  }));
  for (const [k, data, err] of got) {
    if (data) { D[k] = data; ERR[k] = ""; } else { ERR[k] = err; }
  }
  at = Date.now();
  busy = false;
  render();
}

/* ------------------------------------------------------------- the reads */
const ladders = () => (D.risk && D.risk.tickers) || [];
const acct = () => (D.risk && D.risk.account) || {};
const limits = () => (D.risk && D.risk.limits) || {};
const pf = () => (D.perf && D.perf.portfolio) || null;
const strategies = () => (D.strat && D.strat.strategies) || [];
const openPlays = () => ((D.plays && D.plays.positions) || [])
  .filter((p) => p.is_open !== false && p.state !== "closed");

/* The one place a source's failure turns into a MARK.

   ROUND 6: THIS USED TO BE A PARAGRAPH, AND IT WAS PRINTED ONCE PER BLOCK.
   /api/risk feeds five blocks on this page, so a single 500 from it drew five
   byte-identical 40-word strips down the page plus the note at the top --
   six copies of one fact, which reads as six problems. The reason is now
   stated ONCE, at the top, by renderNotes(); each stranded block draws a
   short struck-through bar carrying the same reason on hover, which keeps
   the block's height and says "this is empty because something failed"
   without re-explaining it.

   Nothing is drawn from the last read, and that has not changed: a risk
   figure that cannot be refreshed is not a risk figure. */
function stranded(key, what) {
  if (!ERR[key]) return "";
  return `<div class="rk-out" title="${esc(what + " could not be read: "
    + ERR[key] + ". Nothing is drawn here rather than drawn from the last "
    + "read -- a risk figure that cannot be refreshed is not a risk "
    + "figure.")}"><span></span>${esc(what.toLowerCase())} is not answering</div>`;
}

function render() {
  if (!el("rkHero")) return;
  renderNotes();
  renderHero();
  renderSplit();
  renderDrawdown();
  renderCaps();
  renderAssignment();
  renderLadders();
  renderStress();
  wireReasons(el("view"));
}

/* ------------------------------------------------------------- the notes */
function renderNotes() {
  const host = el("rkNotes");
  if (!host) return;
  const a = acct(), L = limits(), T = ladders();
  const n = [];
  for (const [k, what] of [["risk", "Ladder exposure"],
                           ["perf", "The account's performance"],
                           ["strat", "The strategy list"],
                           ["plays", "The options ledger"]]) {
    if (!ERR[k]) continue;
    n.push(`<div class="note bad"><b>${esc(what)} could not be read.</b>
      ${esc(ERR[k])} Everything below that comes from it is missing rather
      than stale.</div>`);
  }
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
  if (D.risk && noLimits) {
    n.push(`<div class="note warn"><b>No portfolio guardrails are set.</b>
      Nothing caps total exposure, protects a cash reserve, or halts the fleet
      on an account-level loss. Each ladder is limited only by its own max lots
      — <a href="${hashFor({ kind: "settings", tab: "engine" })}">set them</a>.</div>`);
  }
  if (D.risk && D.risk.worst_case > a.equity) {
    n.push(`<div class="note bad"><b>Worst case exceeds the account.</b> Every
      ladder filling every rung would need <b>${money0(D.risk.worst_case)}</b>
      against <b>${money0(a.equity)}</b> of equity. That state is unreachable —
      the ladders would stop filling — but it means your caps are not the thing
      limiting you, your buying power is.</div>`);
  }
  const conc = concentration(T.map((t) => ({ label: t.symbol,
                                             value: t.cost_basis })));
  if (conc.ok && conc.n > 1 && conc.top1 > 0.5) {
    n.push(`<div class="note warn"><b>${(conc.top1 * 100).toFixed(0)}% of what
      the ladders hold is in one name</b> — ${esc(conc.words)}. A move against
      that ticker is a move against the book.</div>`);
  }
  host.innerHTML = n.join("");
}

/* -------------------------------------------------------------- the hero */
function renderHero() {
  const host = el("rkHero");
  if (!host) return;
  const a = acct(), T = ladders(), p = pf();
  const atr1 = T.reduce((s, t) => s + (Number(t.loss_1atr) || 0), 0);
  const held = T.reduce((s, t) => s + (Number(t.cost_basis) || 0), 0);
  const asn = assignment();

  host.innerHTML = tileGrid([
    tile({ label: "Deployed", big: true,
           html: (D.risk
             ? `<span class="num">${a.deployed_pct}%</span>`
             : unmeasured(ERR.risk || "the exposure read has not answered"))
             + (D.risk ? `<div class="rk-mk">${ratiobar({
                 value: a.deployed, cap: a.equity, unit: "usd", dp: 0,
                 label: "cost basis of what the ladders hold, over equity",
               })}</div>` : ""),
           /* ROUND 7: NO HINT. The ratiobar directly underneath this
              figure already carries "cost basis of what the ladders hold,
              over equity" as its own label -- the tile said it again, one
              line above, for a reader who could hover. */ }),
    tile({ label: "A 1× ATR move against you",
           html: D.risk ? sgn(atr1) : unmeasured(),
           hint: `one average bar's range on the ${money0(held)} held` }),
    tile({ label: "If every rung fills",
           html: (D.risk ? `<span class="num">${money0(D.risk.worst_case)}</span>`
                         : unmeasured(ERR.risk))
             + (D.risk && a.equity ? `<div class="rk-mk">${ratiobar({
                 value: D.risk.worst_case, cap: a.equity, unit: "usd", dp: 0,
                 label: "against account equity",
               })}</div>` : ""),
           /* the only half worth keeping: it is a COMMITMENT, not a
              forecast. "Every ladder at max_lots" is what the label says. */
           hint: "at max_lots. The size of the commitment, not a forecast." }),
    /* ONE TILE, ONE CURVE. Where the account is standing is a POSITION
       between its worst fall and its own peak, and that is a dot on a track.
       As two tiles it was two dollar figures and two percentages and the
       reader did the subtraction. */
    tile({ label: "Drawdown",
           metric: p ? p.current_drawdown : null, signed: true,
           html: p ? undefined : unmeasured(ERR.perf
             || "the performance read has not answered"),
           sub: `<div class="rk-mk">${dotscale({
             value: p ? mv(p.current_drawdown) : null,
             min: p ? mv(p.max_drawdown) : null, max: 0,
             unit: "usd", dp: 0, signed: true, tone: "down",
             lo: p && mv(p.max_drawdown) != null
               ? "worst " + money0(mv(p.max_drawdown)) : "worst —",
             hi: "peak",
             aria: "where the account is standing between its worst fall and "
                 + "its own high-water mark",
             why: ERR.perf || "no account equity curve has been measured",
           })}</div>`,
           /* ROUND 7: the MEASURED pair and nothing else. The 22 words in
              front of them defined drawdown and named the curve, and the
              Drawdown panel below carries that curve's basis already -- this
              tile was the second copy of it. */
           hint: "worst "
               + (p ? mfmt(p.max_drawdown_pct, { unit: "pct" }) : "not measured")
               + ", now "
               + (p ? mfmt(p.current_drawdown_pct, { unit: "pct" })
                    : "not measured") }),
    tile({ label: "Assignment exposure",
           html: asn.ok
             ? `<span class="num">${money0(asn.notional)}</span>`
             : unmeasured(asn.why),
           /* the count is measured and stays; what a short leg DELIVERS is
              on the Assignment exposure panel's own title, once. */
           hint: asn.ok
             ? `${asn.shares.toLocaleString()} shares across ${asn.legs} short `
               + `leg${asn.legs === 1 ? "" : "s"}`
             : "" }),
  ], { cols: 5 });
}

/* ------------------------------------------------- where the money sits */
/* TWO DENOMINATORS, and they are not the same one.

   By ticker is the LADDERS' cost basis: what this fleet paid for what it
   holds. By strategy is every strategy's VALUE off hub -- the options book
   included -- which is what the broker marks it at today. Cost and mark are
   different numbers about different things, so they are never summed, never
   put on one ring, and each says which it is. */
function renderSplit() {
  const host = el("rkSplit");
  if (!host) return;
  const T = ladders();
  const byTicker = T.map((t) => ({ label: t.symbol, key: t.symbol,
                                   value: t.cost_basis, upl: t.unrealized }));
  const byStrategy = strategies().map((r) => ({
    label: r.label || r.id, key: r.id, value: mv(r.value),
    upl: mv(r.open_pl),
  }));
  const rows = splitBy === "ticker" ? byTicker : byStrategy;
  const err = splitBy === "ticker" ? stranded("risk", "Ladder exposure")
                                   : stranded("strat", "The strategy list");
  /* ROUND 6: THE CONCENTRATION PANEL IS FOLDED IN HERE.
     The ring above and the ranked bars in that panel were THE SAME NUMBERS --
     each ladder's cost basis -- drawn one above the other, each with its own
     paragraph. The ring already ranks them and already prints each one's
     dollars and share, so the bars were a third rendering of a split the eye
     had just read.

     What the bars had and the ring does not is the ANSWER to "how
     concentrated", and that is one mark: the biggest name's share on a
     0-100% track with the top three ticked. Only over the TICKER split --
     concentration() measures the ladders' cost basis and has nothing to say
     about the strategy ring's market values. */
  const c = concentration(rows.map((r) => ({ label: r.label, value: r.value })));
  const conc = (splitBy === "ticker" && c.ok)
    ? `<div class="rk-conc">${dotscale({
        value: c.top1, min: 0, max: 1, unit: "pct", dp: 1,
        lo: "biggest name 0%", hi: "100%",
        tone: c.top1 > 0.5 ? "down" : "flat",
        marks: [{ at: c.top3, label: "top three" }],
        aria: "share of the ladders' book held in the single biggest name, "
            + "with the top three marked",
      /* ROUND 7: the caption's tooltip READ THE MARK BACK -- both
         percentages are the dot and the tick beside it, and `c.words` is
         printed in the caption itself. What a tooltip can add here is only
         what is NOT in the picture: the names the split left out. */
      /* AND THE CAPTION IS A FIGURE, NOT A SENTENCE. riskmath's `c.words`
         reads "as concentrated as 2.0 equal-sized positions, out of 2" -- nine
         words, printed HERE and, whenever the top name is over half the book,
         printed again verbatim inside the warning at the top of this page. The
         warning keeps every word of its copy; a warning is not where a
         subtraction round saves anything. This one becomes the number it was
         wrapped around, which is what the mark beside it needed all along.
         `equivalent` and `n` are riskmath's own fields -- the same two the
         sentence was built from, so nothing is recomputed here. */
      })}<span class="rk-conc-w"${c.why ? ` data-why title="${
        esc("left out of the split: " + c.why)}"` : ""
      }><b>${c.equivalent.toFixed(1)}</b> of ${c.n} effective</span></div>`
    : "";
  /* The ring is viz.js's, and it REFUSES a negative share rather than drawing
     |value| -- which is why EXPOSURE is what is split here and P/L is a signed
     figure in the tables underneath. */
  const body = err || `
    ${donut({
      slices: rows.map((r) => ({
        label: r.label, value: r.value,
        why: (r.value === null || r.value === undefined)
          ? "the broker has not priced this" : "",
      })),
      unit: "usd", dp: 0, size: 156,
      centerSub: splitBy === "ticker" ? "cost basis" : "market value",
      aria: splitBy === "ticker" ? "exposure by ticker" : "exposure by strategy",
      empty: splitBy === "ticker"
        ? "No ladder holds anything" : "No strategy holds anything",
    })}
    ${conc}`;

  host.innerHTML = panel("Where the money is", body, {
    /* ROUND 7: 40 words to 6. The two halves it spelled out are ON the two
       buttons that select them, where a reader is already looking, and the
       "By strategy" button keeps the sentence that matters -- NOT the same
       quantity as the ticker ring. */
    titleHint: "two different quantities, never summed",
    actions: segmented({
      id: "rkSplitSeg", value: splitBy, size: "sm", label: "split by",
      options: [["ticker", "By ticker",
                 "the share ladders' cost basis — what they paid"],
                ["strategy", "By strategy",
                 "every strategy's market value today, options included. NOT "
                 + "the same quantity as the ticker ring."]],
    }),
  });
  wireSegmented("rkSplitSeg", (v) => { splitBy = v; renderSplit(); });
}

/* ----------------------------------------------------------- drawdown */
function renderDrawdown() {
  const host = el("rkDraw");
  if (!host) return;
  const p = pf();
  const days = (D.perf && D.perf.daily) || [];
  const err = stranded("perf", "The account's performance");
  const dd = drawdown(days);
  const cav = p && p.caveats && p.caveats.length
    ? `<div class="note warn" style="margin:12px 0 0">${
        p.caveats.map((c) => `<div>${esc(c)}</div>`).join("")}</div>`
    : "";
  /* NEVER A FLAT LINE. `drawdown` comes back ok:false with its reason when
     there is no curve, and a straight line along the bottom of a risk page
     reads as "this account has never fallen" -- a claim nobody measured. */
  const chart = dd.ok
    ? area({
        series: [{ label: "Below the high", tone: "down",
                   points: dd.rows.map((r) => ({ t: r.date, v: r.dd })) }],
        h: 150, unit: "usd", dp: 0, zeroLine: false,
        aria: "drawdown from the account own high-water mark",
      })
    : vizEmpty({ h: 150, title: "No drawdown curve", body: dd.why });
  /* THE DEEPEST FALL AS A CHIP, not a sentence. Everything the sentence
     carried beyond those two figures -- the peak date, the trough date,
     whether it recovered -- is the chip's tooltip. The skipped-day count is
     the one part that reports something WRONG with the picture, so it stays
     printed as a warning. */
  const marks = dd.ok
    ? `<div class="rk-chips"><span class="rk-chip down" title="${esc(
        "deepest fall" + (dd.peakAt ? ", from the high on " + dd.peakAt : "")
        + (dd.troughAt ? " to " + dd.troughAt : "")
        + (dd.recoveredAt ? ", back at the high on " + dd.recoveredAt
                          : ", and it has NOT recovered"))}">deepest ${
        money(dd.maxDd, 0)} · ${(dd.maxDdPct * 100).toFixed(2)}%</span>${
        dd.recoveredAt ? `<span class="rk-chip" title="${esc(
          "the account climbed back to its high-water mark on "
          + dd.recoveredAt)}">recovered</span>`
        : `<span class="rk-chip warn" title="the account has not been back to
           that high since">not recovered</span>`}</div>
       ${/* NOT SHORTENED. Round 7 trimmed this to "skipped, not drawn flat"
            and test_rooms.py section 7 caught it: "a subtraction round must
            not eat a warning". It is right. This is a note that says the
            PICTURE is incomplete, the last clause is what makes the reason
            legible, and six words is not where this round's savings are. */
         dd.skipped ? `<div class="note warn" style="margin:10px 0 0">${
         dd.skipped} day(s) carried no equity print and were skipped rather
         than drawn flat — a holiday is not a flat day.</div>` : ""}`
    : "";
  const body = err || `
    ${chart}
    ${marks}
    ${tileGrid([
      /* "Max drawdown" left this grid: the hero tile above is now the whole
         drawdown story, worst and now on one track, and printing the same
         dollar figure twice on one page is what the owner keeps calling
         clutter. What is left here is the five RATIOS, and a ratio's
         definition is a tooltip. */
      tile({ label: "Longest drawdown",
             metric: p ? p.longest_drawdown_days : null, unit: "days",
             hint: p && p.drawdown_peak_date
               ? `the longest run below a high-water mark, from `
                 + `${p.drawdown_peak_date}`
               : "the longest run below a high-water mark" }),
      /* ROUND 7: a ratio's DEFINITION is not a tooltip, it is a textbook.
         What is kept on each is the part that is this repo's own: how it is
         annualised, and when it comes back null. Calmar keeps nothing --
         "return over max drawdown" is the name of the ratio. */
      tile({ label: "Sharpe", metric: p ? p.sharpe : null, unit: "ratio",
             hint: "daily returns, annualised by root-252" }),
      tile({ label: "Sortino", metric: p ? p.sortino : null, unit: "ratio",
             hint: "down days only; null below three of them" }),
      tile({ label: "Calmar", metric: p ? p.calmar : null, unit: "ratio" }),
      tile({ label: "Exposure", metric: p ? p.exposure : null, unit: "pct",
             hint: "the share of days with something open" }),
    ], { cols: 5, cls: "plain" })}
    ${cav}`;
  host.innerHTML = panel("Drawdown", body, {
    /* 56 words to 21. The opening sentence defined the word in the heading.
       What is left is the REASON the curve is the one it is, which is the
       whole reason this panel exists: a drawdown drawn from the realised log
       would read $0.00 and be true. */
    titleHint: "ACCOUNT equity, not the realised log — every closed row there "
      + "is a win, so a curve from it cannot fall and would read $0.00",
  });
}

/* ------------------------------------------------------ the caps that bind */
function renderCaps() {
  const host = el("rkCaps");
  if (!host) return;
  const L = limits(), a = acct(), T = ladders();
  const ov = S.ov || {};
  const p = ov.portfolio || {};
  const err = stranded("risk", "The account's caps");

  /* `used` for each cap is measured the SAME WAY the Settings field measures
     it -- one definition per guardrail, so the bar on the field that sets it
     and the gauge here can never tell two stories. */
  const today = p.today_pl !== undefined && p.today_pl !== null
    ? p.today_pl : p.made_today;
  const caps = [
    { key: "max_total_exposure", label: "Max total exposure",
      cap: L.max_total_exposure, used: a.deployed,
      fmt: (v) => money0(v),
      why: "Blocks a new rung. Nothing is sold and nothing is halted." },
    { key: "reserve_cash", label: "Cash reserve",
      cap: L.reserve_cash,
      used: L.reserve_cash
        ? Math.max(0, L.reserve_cash - (Number(p.buying_power) || 0)) : 0,
      fmt: (v) => money0(v),
      why: "Buying power the fleet never spends.",
      unmeasured: "the overview has not reported buying power yet" },
    { key: "account_daily_loss_limit", label: "Account daily loss limit",
      cap: L.account_daily_loss_limit,
      used: today === null || today === undefined
        ? NaN : Math.max(0, -Number(today)),
      fmt: (v) => money0(v),
      why: "Measured on the ACCOUNT. Hitting it halts every ladder at once.",
      unmeasured: "today's account P/L has not been measured" },
    { key: "max_running_tickers", label: "Max running tickers",
      cap: L.max_running_tickers,
      used: (ov.tickers || []).filter((t) => t.running).length,
      fmt: (v) => String(v),
      why: "Does not stop one already running." },
  ];
  /* The ladders' own max_lots is a cap too, and on this fleet it is usually
     the one that actually bites -- so the fullest ladder is shown beside the
     account-wide four rather than left on a table further down. */
  let fullest = null;
  for (const t of T) {
    if (!t.max_lots) continue;
    const f = t.lots_open / t.max_lots;
    if (!fullest || f > fullest.f) fullest = { t, f };
  }
  if (fullest) {
    caps.push({ key: "max_lots", label: `${fullest.t.symbol} max lots`,
                cap: fullest.t.max_lots, used: fullest.t.lots_open,
                fmt: (v) => String(v),
                why: "The fullest ladder. At the cap it holds — and there "
                   + "is no stop loss under it." });
  }
  const b = binding(caps);
  const rows = caps.slice().sort((x, y) => {
    const fx = x.cap ? (Number(x.used) / x.cap) : -1;
    const fy = y.cap ? (Number(y.used) / y.cap) : -1;
    return (Number.isNaN(fy) ? -1 : fy) - (Number.isNaN(fx) ? -1 : fx);
  });

  const body = err || `
    <div class="caps">${rows.map((c) =>
      capRow({ ...c, binding: !!(b && b.key === c.key) })).join("")}</div>${
    /* THE ONLY THING THAT STAYED IS THE ONE THAT REPORTS A STATE, not a
       definition: every cap off means nothing on this account can stop an
       order, and that is a warning. The binding cap is already drawn as the
       marked row, so naming it underneath was saying it twice. */
    b ? "" : `<div class="note warn" style="margin:10px 0 0">No cap on this
      account can bind — every one of them is off, so nothing here stops the
      next order.</div>`}`;
  host.innerHTML = panel("The caps that bind", body, {
    /* ROUND 7: NO HINT AT ALL. Both of its sentences are PRINTED on the rows
       they are about: a 0 cap draws the words "off — nothing caps this", and
       the nearest to binding is the row labelled BINDS FIRST. Twenty-five
       words describing two labels that are already on screen. */
    actions: `<a class="btn sm" href="${
      hashFor({ kind: "settings", tab: "engine" })}">Set them</a>`,
  });
}

/* ------------------------------------------------- assignment exposure --
   The one number on this dashboard that only this block says. CLAUDE.md:
   these are AMERICAN options on shares, a short leg that finishes in the
   money delivers or takes 100 shares per contract the account never sized
   for, and a PARTIAL-ITM expiry can lose MORE than the structure's stated max
   loss. So the figure is not "max loss" -- it is what could be delivered. */
function assignment() {
  if (ERR.plays && !D.plays) {
    return { ok: false, why: "the options ledger could not be read: "
                             + ERR.plays, shares: 0, notional: 0, legs: 0 };
  }
  if (!D.plays) {
    return { ok: false, why: "the options ledger has not answered yet",
             shares: 0, notional: 0, legs: 0 };
  }
  const rows = [];
  let shares = 0, notional = 0, unpriced = 0;
  for (const p of openPlays()) {
    for (const leg of (p.legs || [])) {
      if (String(leg.side || "").toLowerCase() !== "sell") continue;
      const ct = Number(p.contracts) || 0;
      const sh = ct * 100;
      const k = Number(leg.strike);
      const val = Number.isNaN(k) ? null : k * sh;
      if (val === null) unpriced += 1; else notional += val;
      shares += sh;
      rows.push({ symbol: p.symbol, play: p.play, occ: leg.symbol,
                  right: leg.right, strike: k, contracts: ct, shares: sh,
                  notional: val, expiry: p.expiry, dte: p.dte,
                  adopted: !!p.adopted });
    }
  }
  return { ok: true, why: "", rows, shares, notional, legs: rows.length,
           unpriced, positions: openPlays().length };
}

function renderAssignment() {
  const host = el("rkAssign");
  if (!host) return;
  const a = assignment();
  const eq = Number(acct().equity) || 0;
  let body;
  if (!a.ok) {
    body = `<div class="note warn" style="margin:0">Assignment exposure is not
      shown: ${esc(a.why)}. It is not zero — nobody measured it.</div>`;
  } else if (!a.legs) {
    body = `<div class="rk-chips"><span class="rk-chip" title="${esc(
      a.positions
        ? a.positions + " option position" + (a.positions === 1 ? " is" : "s are")
          + " open and every leg of " + (a.positions === 1 ? "it is" : "them is")
          + " long, so nothing can be assigned to this account."
        : "no option position is open at all")}">no short leg open</span></div>`;
  } else {
    body = `
      ${dataTable({
        dense: true,
        cols: ["Ticker", { label: "Leg" }, { label: "Strike", num: true },
               { label: "Contracts", num: true }, { label: "Shares", num: true },
               { label: "If assigned", num: true }, "Expires"],
        rows: a.rows.map((r) => [
          `<b>${esc(r.symbol)}</b>${r.adopted
            ? ` ${chip("adopted", "warn", "this position was not opened by a play")}`
            : ""}`,
          `<span class="mono" style="font-size:var(--fs-xs)">${esc(r.occ || "")}</span>
           ${r.right ? chip(esc(r.right), "mute") : ""}`,
          Number.isNaN(r.strike) ? unmeasured("no strike on this leg")
                                 : px(r.strike),
          String(r.contracts),
          r.shares.toLocaleString(),
          r.notional === null ? unmeasured("no strike, so no figure")
                              : money0(r.notional),
          `${esc(r.expiry || "—")}${r.dte !== undefined && r.dte !== null
            ? ` <span class="faint">${r.dte}d</span>` : ""}`,
        ]),
      })}
      <div class="rk-chips">
        <span class="rk-chip warn">${money0(a.notional)} would be
          <b>delivered</b>, not lost</span>${eq
          ? `<span class="rk-chip">${Math.round(100 * a.notional / eq)}% of
             ${money0(eq)} equity</span>` : ""}
      </div>
      <details class="rk-more"><summary>why a delivery is not a loss</summary>
        These are American options on shares. A short leg $0.01 in the money at
        expiry is auto-exercised, and a PARTIAL-ITM expiry — the short assigned
        while the long expires worthless — can lose more than the structure's
        stated max loss and leaves naked stock overnight. The account is
        options level 3, so every short leg here is defined-risk or covered;
        that limits the loss, not the delivery.
      </details>`;
  }
  host.innerHTML = panel("Assignment exposure", body, {
    titleHint: "what a short option leg hands this account if it finishes in "
      + "the money",
  });
}

/* --------------------------------------------------------- per ladder */
function renderLadders() {
  const host = el("rkLadders");
  if (!host) return;
  const T = ladders();
  /* add_mode is not on /api/risk, and it decides whether the server could
     compute a ladder depth at all -- join it from the fleet snapshot rather
     than printing a bare dash and letting it look like zero risk. */
  const modeOf = {};
  for (const t of ((S.ov && S.ov.tickers) || [])) modeOf[t.symbol] = t.add_mode;

  const err = stranded("risk", "Ladder exposure");
  const body = err || dataTable({
    dense: true,
    cols: ["Ticker", { label: "Price", num: true }, { label: "ATR(14)", num: true },
           { label: "ATR %", num: true }, { label: "Target", num: true },
           { label: "in ATR", num: true, title: "take profit measured in average bars" },
           { label: "Add", num: true }, { label: "in ATR", num: true },
           { label: "Lots", num: true }, { label: "Held", num: true },
           { label: "Cost", num: true }, { label: "Max exposure", num: true },
           { label: "Open P/L", num: true }],
    empty: "No ticker is configured on this account.",
    rows: T.map((t) => {
      const tpAtr = t.tp_in_atr;
      const warnTp = tpAtr != null && tpAtr < 0.5;
      const atrRungs = (modeOf[t.symbol] || "points") !== "points";
      return `<tr class="click" data-go="ticker" data-sym="${esc(t.symbol)}">
        <td><b>${esc(t.symbol)}</b>${t.armed
          ? ` ${chip("armed", "down", "this ladder transmits real orders")}` : ""}</td>
        <td class="dt-n num">${px(t.price)}</td>
        <td class="dt-n num">${t.atr ? "$" + t.atr.toFixed(3)
          : unmeasured("not enough bars to measure ATR(14)")}</td>
        <td class="dt-n num faint">${t.atr_pct ? t.atr_pct.toFixed(2) + "%" : "—"}</td>
        <td class="dt-n num">$${t.take_profit.toFixed(2)}</td>
        <td class="dt-n num ${warnTp ? "warn" : ""}"${warnTp
          ? ` title="the target is inside half an average bar: noise reaches it"`
          : ""}>${tpAtr == null
            ? unmeasured("no ATR, so the target cannot be expressed in bars")
            : tpAtr.toFixed(2) + "×"}</td>
        <td class="dt-n num">${atrRungs ? `<span class="faint">1× ATR</span>`
          : "$" + t.add_distance.toFixed(2)}</td>
        <td class="dt-n num">${atrRungs ? `<span class="faint">1.00×</span>`
          : (t.add_in_atr == null ? unmeasured("no ATR") : t.add_in_atr.toFixed(2) + "×")}</td>
        <td class="dt-n num">${t.lots_open}<span class="faint">/${t.max_lots}</span></td>
        <td class="dt-n num">${qty(t.shares_held)}</td>
        <td class="dt-n num">${money0(t.cost_basis)}</td>
        <td class="dt-n num faint">${money0(t.max_exposure)}</td>
        <td class="dt-n num">${sgn(t.unrealized)}</td></tr>`;
    }),
  });
  host.innerHTML = panel("Per ladder", body, {
    titleHint: "click a row for that ticker",
    flush: true,
  });
}

/* ------------------------------------------------------------- stress */
function renderStress() {
  const host = el("rkStress");
  if (!host) return;
  const T = ladders();
  const modeOf = {};
  for (const t of ((S.ov && S.ov.tickers) || [])) modeOf[t.symbol] = t.add_mode;
  const anyAtr = T.some((t) => (modeOf[t.symbol] || "points") !== "points");
  const err = stranded("risk", "Ladder exposure");

  const body = err || `${dataTable({
    dense: true,
    cols: ["Ticker", { label: "Held", num: true },
           /* ROUND 7. 90 words across four headers became 44, and the
              split is the same one everywhere in this round: the sentence
              that DESCRIBES the column goes, the sentence that says how to
              read a DASH or how rough a figure is stays. "How far price must
              fall for every rung to fill" is what "Full ladder depth" means;
              "an ATR ladder has no fixed depth, so read it as unknown, never
              zero" is the contract, and every cell that is a dash carries its
              own reason through `unmeasured` besides. */
           { label: "A 1× ATR fall", num: true,
             title: "one average bar's range on what is held" },
           { label: "Full ladder depth", num: true,
             title: "add distance × max lots. A dash means an ATR "
                  + "ladder has no fixed depth — unknown, never zero." },
           { label: "Cost to fill it", num: true,
             title: "max_exposure: filling every rung" },
           { label: "Est. loss at the bottom", num: true,
             title: "assumes the average lot half the depth underwater: an "
                  + "order of magnitude, not a figure" }],
    empty: "No ticker is configured on this account.",
    rows: T.map((t) => {
      const atrRungs = (modeOf[t.symbol] || "points") !== "points";
      const noDepth = atrRungs
        ? unmeasured("the server only measures ladder depth for fixed-dollar "
                   + "rungs; an ATR ladder has no fixed depth")
        : unmeasured("no depth was computed for this ladder");
      return [
        `<b>${esc(t.symbol)}</b>`,
        qty(t.shares_held),
        sgn(t.loss_1atr),
        t.ladder_depth
          ? `$${t.ladder_depth.toFixed(2)} <span class="faint">(${
              t.ladder_depth_pct}%)</span>`
          : noDepth,
        `<span class="faint">${money0(t.max_exposure)}</span>`,
        t.loss_full_ladder ? sgn(t.loss_full_ladder) : noDepth,
      ];
    }),
  })}
  <div class="note warn" style="margin:12px 16px 2px">There is <b>no stop
    loss</b>. None of these figures includes the case where price keeps falling
    after the ladder is full — there the loss is unbounded until price
    recovers.</div>`;
  host.innerHTML = panel("If it goes against you", body, {
    /* ROUND 7: the "no stop loss" half is deleted, not quietened -- the
       panel's own footer prints it in full, as a warning, directly under this
       table. The ATR half is a REASON for a dash and stays. */
    titleHint: (anyAtr ? "some ladders run ATR rungs, which have no fixed "
                       + "depth, so those depth cells are dashes and not zeroes"
                       : ""),
    flush: true,
  });
}


/* ================================================== this room's own CSS ====
   Injected from here, once, rather than appended to app.css: several agents
   edit that stylesheet in the same week and it is the easiest place in a repo
   to collide silently. viz.js and overview.js do the same. Every value is a
   TOKEN from theme.css, so both themes carry through.

   All five classes below exist because round 6 replaced a paragraph with a
   mark, and a mark needs somewhere to sit. */
function ensureRiskStyles() {
  if (typeof document === "undefined" || !document
      || document.getElementById("ta-risk-css")) return;
  const st = document.createElement("style");
  st.id = "ta-risk-css";
  st.textContent = `
/* a block whose source failed: the height of the block, the reason on hover,
   and NOT the 40-word paragraph that used to be printed once per block */
.rk-out { display: flex; align-items: center; gap: var(--s3);
  padding: var(--s4) 0; color: var(--down); font-size: var(--fs-sm); }
.rk-out span { flex: 1; height: 2px; border-radius: 2px;
  background: repeating-linear-gradient(90deg, var(--down) 0 5px,
    transparent 5px 10px); opacity: .55; }

/* a mark inside a metric tile, where the tile's caption used to be */
.rk-mk { margin-top: 6px; }

/* concentration: the dot on a 0-100% track, with the HHI beside it */
.rk-conc { display: flex; align-items: center; gap: var(--s4);
  margin-top: var(--s3); }
.rk-conc .viz-dsc { flex: 1; min-width: 120px; }
.rk-conc-w { flex: none; font-size: var(--fs-xs); color: var(--muted);
  max-width: 22ch; }

/* the chips that replaced a sentence built out of figures */
.rk-chips { display: flex; flex-wrap: wrap; gap: var(--s2);
  margin-top: var(--s3); }
.rk-chip { font-size: var(--fs-xs); padding: 3px 9px;
  border-radius: var(--radius-pill); background: var(--bg-3);
  color: var(--muted); border: 1px solid var(--hairline);
  font-variant-numeric: tabular-nums; }
.rk-chip b { color: var(--text); font-weight: var(--w-semi); }
.rk-chip.warn { color: var(--warn); border-color: var(--warn); }
.rk-chip.down { color: var(--down); border-color: var(--down); }

/* the disclosure that holds the assignment warning, shut by default */
.rk-more { margin-top: var(--s3); font-size: var(--fs-sm); color: var(--muted);
  line-height: 1.6; }
.rk-more > summary { cursor: pointer; color: var(--accent-2);
  font-size: var(--fs-xs); list-style: none; }
.rk-more > summary::-webkit-details-marker { display: none; }
.rk-more > summary::before { content: "\\25B8 "; }
.rk-more[open] > summary::before { content: "\\25BE "; }
.rk-more[open] { padding-bottom: var(--s2); }

@media (max-width: 620px) {
  .rk-conc { flex-direction: column; align-items: stretch; gap: var(--s2); }
  .rk-conc-w { max-width: none; }
}`;
  document.head.appendChild(st);
}
