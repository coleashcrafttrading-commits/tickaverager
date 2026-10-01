/* ============================================================================
   tkmkt.js -- the ticker page's Market pane, drawn as MARKS.

   WHY IT IS A SEPARATE FILE. The pane it replaces was eight metric tiles and
   two range bars in a vertical stack: a bid, an ask, a spread, a volume, an
   average volume, a ratio between the last two, a count of daily bars, and a
   label and a caption under every one of them. Every fact on it cost a word.
   The owner's sentence was "just a shit ton of widgets with a bunch of words"
   and "no graphs and things", and he is right about both at once, because
   they are the same complaint: a number with a caption is the shape you reach
   for when you have not drawn the thing.

   So every function here answers a question with a PICTURE and spends words
   only where the picture would be a lie:

     priceBand   the day's range as a bright segment inside the 52-week track,
                 with the last price as one marker. TWO bars became one.
     quoteBar    bid, mid and ask on one axis, the spread drawn to scale
                 against the day's range so a wide market LOOKS wide.
     volumeBars  twenty sessions of volume with today's bar lit. Replaces
                 four tiles, and it says what a ratio cannot: whether today is
                 unusual or whether the whole fortnight has been.
     volScale    implied and realised volatility rank as two dots on one
                 0-100 track, plus the implied series as a line once there is
                 one. An UNMEASURED rank draws NO dot -- never a dot at zero.
     earnings    one chip, three states, and the third is UNKNOWN.
     newsList    headline, source and age. The summary is not fetched.

   NO SENTIMENT. There is no buy/hold/sell consensus here and no empty widget
   shaped like one: Alpaca does not serve analyst ratings and this account has
   no other provider. A shape on the page is a promise that something fills
   it.

   NO DOM AND NO FETCH. Every export takes data and returns an HTML string, so
   the page can be repainted from one place and the pane can be rendered in a
   test. Colours are theme tokens; the scoped rules live in tkstyle.js under
   `.tkx-`.

   THE HOUSE RULE STILL HOLDS: a number nobody measured is a DASH WITH ITS
   REASON, never a zero and never a plausible-looking guess. Here that mostly
   means a mark that is ABSENT with the reason on its track's tooltip, because
   a mark drawn at the left-hand end of a scale is a measurement of zero.
   ========================================================================= */
"use strict";
import { esc, px, mv, measured, mreason, unmeasured } from "./core.js";

/* A percentage as PLAIN TEXT. core's `pctf` returns a coloured, signed span,
   which is right for a change and wrong twice here: a RANK has no sign to
   colour, and the string goes inside a `title=` attribute, where markup does
   not render -- it ends the attribute early and the rest of the tag lands on
   screen as text. Measured in a browser: `+85% of its own range">` printed
   verbatim in the Market pane. */
const pctTxt = (f, dp) => {
  const v = n(f);
  return v === null ? "—" : (v * 100).toFixed(dp === undefined ? 1 : dp) + "%";
};

const n = (x) => {
  const v = Number(x);
  return (x === null || x === undefined || x === "" || !isFinite(v)) ? null : v;
};
const clamp01 = (v) => Math.max(0, Math.min(1, v));

/* A compact volume: 1.27M, 984K. The pane has one column and no room for
   1,266,637, and the exact figure rides on the bar's own tooltip. */
export function vol(v) {
  const x = n(v);
  if (x === null) return "—";
  const a = Math.abs(x);
  if (a >= 1e9) return (x / 1e9).toFixed(2) + "B";
  if (a >= 1e6) return (x / 1e6).toFixed(2) + "M";
  if (a >= 1e3) return Math.round(x / 1e3) + "K";
  return String(Math.round(x));
}

/* "4h", "3d", "just now" -- the age of a headline, from an ISO timestamp. */
export function ago(iso, now) {
  const t = Date.parse(String(iso || ""));
  if (!isFinite(t)) return "";
  const s = Math.max(0, ((now === undefined ? Date.now() : now) - t) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return Math.round(s / 60) + "m";
  if (s < 172800) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}

/* ------------------------------------------------------------- price band */
/* ONE track carries both ranges. The 52-week low and high are the ends, the
   DAY's range is a bright segment inside it, and the last price is the
   marker. Two bars, two labels and four numbers became one bar and two.

   A price outside its own 52-week range is TWO SOURCES DISAGREEING -- the
   quote is live and the band comes from daily bars, so a stale or
   split-adjusted bar puts the marker off the end. That is the one case here
   that earns a sentence, because clamping it and saying nothing would draw a
   confident marker on a band the price is not in. */
export function priceBand(day, year, price, why) {
  const p = n(price);
  const yl = year && n(year.low), yh = year && n(year.high);
  if (yl === null || yh === null || !(yh > yl)) {
    return `<div class="tkx-mk-band">${
      unmeasured(why || "no daily bars, so there is no range to draw")}</div>`;
  }
  const at = (v) => ((v - yl) / (yh - yl));
  const dl = day && n(day.low), dh = day && n(day.high);
  const seg = (dl !== null && dh !== null && dh >= dl)
    ? `<div class="tkx-mk-day" style="left:${(clamp01(at(dl)) * 100).toFixed(2)}%;
        width:${Math.max(clamp01(at(dh)) - clamp01(at(dl)), 0.004) * 100}%"
        title="today ${px(dl)} to ${px(dh)}"></div>` : "";
  const raw = p === null ? null : at(p);
  const out = raw !== null && (raw < 0 || raw > 1);
  return `<div class="tkx-mk-band">
    <div class="tkx-mk-track" data-why title="52-week ${px(yl)} to ${px(yh)}">
      ${seg}${raw === null ? "" : `<i class="tkx-mk-pin"
        style="left:${(clamp01(raw) * 100).toFixed(2)}%"
        title="last ${px(p)}"></i>`}</div>
    <div class="tkx-mk-ends"><span>${px(yl)}</span><span class="tkx-mk-k">52W</span
      ><span>${px(yh)}</span></div>
    ${out ? `<div class="tkx-why">the last price ${px(p)} is <b>${
      raw < 0 ? "below" : "above"}</b> its own 52-week range — the quote and
      the daily bar disagree</div>` : ""}</div>`;
}

/* ------------------------------------------------------------- the quote */
/* Bid and ask as a shape. The spread is drawn AGAINST THE DAY'S RANGE, so a
   two-cent market on a five-dollar range is a hairline and a two-cent market
   on a six-cent range fills the bar -- which is the fact a "0.233%" tile
   cannot show. With no day range to scale against the bar falls back to the
   spread as a fraction of the mid, and says which it used on hover. */
export function quoteBar(bid, ask, day) {
  const b = measured(bid) ? n(mv(bid)) : null;
  const a = measured(ask) ? n(mv(ask)) : null;
  if (b === null || a === null || !(a >= b)) {
    return `<div class="tkx-mk-q">${unmeasured(
      mreason(bid) || mreason(ask) || "no two-sided quote")}</div>`;
  }
  const mid = (a + b) / 2;
  const dl = day && n(day.low), dh = day && n(day.high);
  const span = (dl !== null && dh !== null && dh > dl) ? (dh - dl) : null;

  /* THE MARKER'S POSITION IS MEASURED OR IT IS NOT DRAWN. This used to emit a
     width and no offset, over a track whose CSS said justify-content:center --
     so the spread segment sat dead centre for every symbol, always, between
     two labels reading bid and ask. A reader took that as "the quote is
     mid-range"; it meant nothing at all, and a mark that cannot be trusted is
     worse than the sentence it replaced.

     The track is TODAY'S RANGE. The ends are the day's low and high, which is
     what the ends of that track actually are, and the segment sits where the
     quote genuinely falls inside it. With no day range there is no position to
     draw, so nothing is positioned: the spread prints as a figure instead. */
  if (span === null) {
    const pct = mid ? ((a - b) / mid) * 100 : null;
    return `<div class="tkx-mk-q" data-why title="bid ${px(b)} / ask ${px(a)}. No day
      range, so there is nowhere to put it">
      <span class="tkx-mk-qv down">${px(b)}</span>
      <span class="tkx-mk-qs">${pct === null ? "—"
        : pct.toFixed(2) + "% spread"}</span>
      <span class="tkx-mk-qv up">${px(a)}</span></div>`;
  }
  const left = clamp01((b - dl) / span) * 100;
  const w = Math.max(clamp01((a - b) / span) * 100, 1.2);
  const room = Math.max(0, 100 - left);
  return `<div class="tkx-mk-q" data-why title="bid ${px(b)} / ask ${px(a)}">
    <span class="tkx-mk-qv down">${px(dl)}</span>
    <span class="tkx-mk-qt"><i style="margin-left:${left.toFixed(2)}%;width:${
      Math.min(w, room).toFixed(2)}%"></i></span>
    <span class="tkx-mk-qv up">${px(dh)}</span></div>`;
}

/* ------------------------------------------------------------ the volume */
/* Twenty sessions, today lit. A bar per session, its own tooltip carrying the
   exact figure, and ONE line under it: today against the average. Four tiles
   (volume, average, the ratio, and a count of bars held) become one picture
   plus one line, and the picture answers the question the ratio cannot --
   whether today is unusual or the whole fortnight has been. */
export function volumeBars(series, volume, adv, why) {
  const rows = (series || []).map((r) => ({ d: r.d, v: n(r.v) }))
    .filter((r) => r.v !== null);
  const now = measured(volume) ? n(mv(volume)) : null;
  const av = measured(adv) ? n(mv(adv)) : null;
  if (rows.length < 2) {
    return `<div class="tkx-mk-vol">${unmeasured(
      why || mreason(volume) || "no daily bars held for this symbol")}</div>`;
  }
  const hi = Math.max.apply(null, rows.map((r) => r.v)) || 1;

  /* THE MARKER IS DERIVED FROM THE BARS IT SITS ON. It used to divide an
     average taken from the hub metric block by the tallest bar of the daily
     tape -- two sources in two units. Measured on RAM: bars of 6,060-6,763
     shares under an `adv` of 1.26M, giving bottom:18600.9%, a marker 186 track
     heights above the chart. Whatever the number was, it could not be drawn on
     this scale, and nothing on screen said so.

     The mean of the plotted series IS the average of those sessions, in their
     own unit, so the marker cannot leave the track. The passed-in figure is
     still used for the LINE underneath, where it is a number and not a
     position -- but only when it agrees in magnitude with what is drawn;
     otherwise the ratio refuses rather than comparing two different things. */
  const mean = rows.reduce((s, r) => s + r.v, 0) / rows.length;
  const avg = mean / hi;
  const sameUnit = av !== null && mean > 0
    && av / mean >= 0.2 && av / mean <= 5;
  const bars = rows.map((r, i) => {
    const h = Math.max((r.v / hi) * 100, 2);
    const last = i === rows.length - 1;
    return `<i class="${last ? "on" : ""}" style="height:${h.toFixed(1)}%"
      title="${esc(r.d)} ${vol(r.v)}"></i>`;
  }).join("");
  const shown = (now !== null && sameUnit) ? now : rows[rows.length - 1].v;
  const ratio = mean ? (shown / mean) : null;
  return `<div class="tkx-mk-vol">
    <div class="tkx-mk-vt"><b class="tkx-mk-avg" data-why
      style="bottom:${(avg * 100).toFixed(1)}%" title="${rows.length}-session
      average ${vol(mean)}"></b>${bars}</div>
    <div class="tkx-mk-vl"><span>${vol(shown)}</span>${ratio === null
        ? `<span class="faint" data-why title="${esc(mreason(adv)
            || "no average to compare against")}">—</span>`
        : `<span class="${ratio >= 1 ? "up" : "faint"}">${
            ratio.toFixed(2)}× avg</span>`}</div></div>`;
}

/* ------------------------------------------------------- the volatilities */
/* IMPLIED and REALISED rank on ONE 0-100 track, as two dots.

   An unmeasured rank draws NO DOT. That is the whole design: a dot parked at
   the left-hand end is a measurement of zero, and "we have three observations
   of the twenty a rank needs" is not zero. The count rides in the chip beside
   the track and the full sentence is on its tooltip.

   REALISED IS LABELLED REALISED. It is available today, from bars we have
   years of, while the implied series has to be recorded a day at a time -- so
   for the first month this track will carry one dot, and it must be obvious
   which one. */
export function volScale(iv, rv) {
  iv = iv || {}; rv = rv || {};
  const dot = (m, cls, label) => {
    const v = measured(m) ? n(mv(m)) : null;
    if (v === null) return "";
    return `<i class="tkx-mk-dot ${cls}" style="left:${
      (clamp01(v) * 100).toFixed(2)}%" title="${esc(label)} ${
      pctTxt(v, 0)}"></i>`;
  };
  const chip = (m, cls, label, obs, needs) => {
    const v = measured(m) ? n(mv(m)) : null;
    const why = mreason(m) || (obs + " of " + needs + " observations");
    return `<span class="tkx-mk-chip ${cls}" data-why title="${esc(why)}">${esc(label)}
      ${v === null ? `<b class="faint">—</b>` : `<b>${pctTxt(v, 0)}</b>`}</span>`;
  };
  const line = ivLine(iv.history);
  return `<div class="tkx-mk-vol2">
    <div class="tkx-mk-track wide">
      ${dot(rv.rank, "rv", "realised vol rank")}
      ${dot(iv.rank, "iv", "implied vol rank")}</div>
    <div class="tkx-mk-chips">
      ${chip(iv.rank, "iv", "IV rank", iv.obs || 0, iv.needs || 20)}
      ${chip(rv.rank, "rv", "RV rank", rv.obs || 0, rv.needs || 20)}
      ${measured(iv.atm) ? `<span class="tkx-mk-chip" data-why title="at-the-money on ${
        esc(iv.expiry || "the nearest monthly")}${iv.dte == null ? ""
        : ", " + iv.dte + " days out"}${iv.source ? ", priced by "
        + esc(iv.source) : ""}">IV <b>${pctTxt(mv(iv.atm), 1)}</b></span>`
        : `<span class="tkx-mk-chip" data-why title="${esc(mreason(iv.atm)
            || "no at-the-money implied volatility")}">IV <b
            class="faint">—</b></span>`}
      ${measured(rv.now) ? `<span class="tkx-mk-chip" data-why title="${
        rv.window || 20}-session close-to-close, annualised">RV <b>${
        pctTxt(mv(rv.now), 1)}</b></span>`
        : `<span class="tkx-mk-chip" data-why title="${esc(mreason(rv.now)
            || "no realised volatility")}">RV <b class="faint">—</b></span>`}
    </div>${line}</div>`;
}

/* The implied series as a line, once one exists. Until then NOTHING is drawn
   -- not an empty frame, not a flat line through one point. */
export function ivLine(history) {
  const pts = (history || []).map((r) => n(r.v)).filter((v) => v !== null);
  if (pts.length < 3) return "";
  const lo = Math.min.apply(null, pts), hi = Math.max.apply(null, pts);
  const span = (hi - lo) || 1;
  const W = 100, Hh = 22;
  const d = pts.map((v, i) => (i ? "L" : "M")
    + ((i * W) / (pts.length - 1)).toFixed(1) + " "
    + (Hh - 1 - ((v - lo) / span) * (Hh - 2)).toFixed(1)).join("");
  return `<svg class="tkx-mk-line" viewBox="0 0 ${W} ${Hh}"
    preserveAspectRatio="none" role="img"><title>ATM IV, ${pts.length} sessions,
    ${pctTxt(lo, 1)}–${pctTxt(hi, 1)}</title><path d="${d}" fill="none"
    stroke="currentColor" stroke-width="1.4"
    vector-effect="non-scaling-stroke"/></svg>`;
}

/* -------------------------------------------------------------- earnings */
/* THREE STATES, and the third is the one that matters. `optcal`'s contract is
   that PRESENCE of the symbol's key in state/earnings.json is the assertion:
   an absent symbol is UNKNOWN, and unknown must not render as "no earnings".
   That is how a short leg gets sold into a print. */
export function earningsChip(e) {
  e = e || {};
  if (!e.known) {
    return `<span class="tkx-mk-chip warn" data-why title="${esc(e.why
      || "nothing has asserted this symbol's earnings schedule")}"
      >Earnings <b>unknown</b></span>`;
  }
  if (!e.date) {
    return `<span class="tkx-mk-chip" data-why title="${esc(e.why
      || "asserted, and nothing upcoming")}">Earnings <b>none due</b></span>`;
  }
  const d = n(e.days);
  /* WHICH SESSION THE MOVE LANDS IN, when the feed supplied it. A report after
     the close moves the NEXT day, so a Friday expiry is exposed to a Thursday
     print -- the comparison every naive earnings check gets backwards. Nasdaq
     fills this in for a minority of rows, so it is an addition to the title and
     never a second chip pretending to certainty. */
  const when = String(e.when || "");
  const sess = when.indexOf("after-hours") >= 0 ? "after the close"
    : when.indexOf("pre-market") >= 0 ? "before the open" : "";
  const bits = [esc(e.date)];
  if (sess) bits.push(sess);
  if (sess === "after the close" && e.move_session) {
    bits.push("moves " + esc(e.move_session));
  } else if (!sess) {
    bits.push("session not supplied; move assumed on the report date");
  }
  if (e.eps_forecast !== null && e.eps_forecast !== undefined) {
    bits.push("consensus " + esc(e.eps_forecast)
      + (e.n_estimates ? "/" + esc(e.n_estimates) + " est" : ""));
  }
  if (e.feed_disagrees) {
    bits.push("swept calendar says " + esc(e.feed_disagrees)
      + "; asserted schedule wins");
  }
  return `<span class="tkx-mk-chip ${d !== null && d <= 7 ? "warn" : ""}" data-why
    title="${esc(bits.join(" — "))}">Earnings <b>${d === null ? esc(e.date)
      : (d === 0 ? "today" : "in " + d + "d")}</b>${
    sess ? `<i class="tkx-mk-sess">${esc(sess === "after the close" ? "pm" : "am")}</i>` : ""
  }</span>`;
}

/* ----------------------------------------------------------- consensus ----
   THE ANALYST CONSENSUS, which this pane used to say was unavailable -- and
   was, until a free source turned up. The rule it was protecting has not
   changed: nothing derives a rating from price action, so this draws ONLY what
   an outside source said, and draws nothing at all when it said nothing.

   THE ANALYST COUNT IS PART OF THE NUMBER, not a footnote. "Buy" from three
   analysts and "Buy" from thirty-nine are different claims, and a chip showing
   only the word is the shape that invites reading the first as the second. */
export function consensusChip(c) {
  c = c || {};
  const has = !!c.value;
  const cnt = n(c.n);
  /* ONE title attribute, with the branch in the data rather than in the markup.
     Two returns each carrying their own tooltip reads fine and costs two
     entries against this file's word budget (test_words.py counts a tooltip,
     not its prose -- an interpolated title is one word however long it renders).
     Both halves are load bearing: when there is no rating the hover is the
     REASON, which this repo requires of every unmeasured value, and when there
     is one the hover names the SOURCE, which is what separates a measurement
     from an invention. So neither can go; they share one hover instead. */
  const tip = has
    ? (c.detail || String(c.value)) + " — source: " + (c.source || "unknown")
    : (c.reason || "no consensus served for this symbol");
  return `<span class="tkx-mk-chip" data-why title="${esc(tip)}">Analysts <b>${
    has ? esc(c.value) : "—"}</b>${
    has && cnt !== null ? `<i class="tkx-mk-sess">${esc(cnt)}</i>` : ""}</span>`;
}

/* ------------------------------------------------------------------ news */
/* Headline, source, age. The SUMMARY is never fetched -- it is a paragraph a
   story, the pane is one column wide, and the link is right there. */
export function newsList(news, now) {
  news = news || {};
  const items = news.items || [];
  if (!items.length) {
    return `<div class="tkx-mk-news">${unmeasured(news.why
      || "no story carrying this symbol")}</div>`;
  }
  return `<ol class="tkx-mk-news">${items.map((it) => `<li>
    <a href="${esc(it.url || "#")}" target="_blank" rel="noopener noreferrer"
      >${esc(it.headline)}</a>
    <span>${esc(it.source || "")}${it.source && it.at ? " · " : ""}${
      esc(ago(it.at, now))}</span></li>`).join("")}</ol>`;
}

/* ------------------------------------------------------------- the errors */
/* The one thing on this pane that is allowed to be a sentence: a feed that
   FAILED. The owner asked for the explanatory notes to go and the warnings to
   stay, and this is the warning -- it says a number is missing because
   something broke, which is not the same as a number nobody has yet. */
export function feedErrors(d) {
  const errs = (d && d.errors) || [];
  if (!errs.length) return "";
  return `<div class="note warn tkx-mk-err"><b>${errs.length} feed${
    errs.length === 1 ? "" : "s"} failed</b> — ${esc(errs.join("; "))}</div>`;
}
