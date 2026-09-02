/* ============================================================================
   ind.js -- the browser mirror of indicators.py.

   Deliberately the same formulas as the Python library so a line on the chart
   is the same number the backtest traded on. If these two ever disagree, the
   chart is lying about the strategy.

   Every function returns a full series aligned to the bars, with nulls where a
   value has not formed. Never 0.0 -- a zero gets read as a real level.
   ========================================================================= */
"use strict";

const F = (xs) => xs.map(Number);

export function sma(v, period) {
  v = F(v);
  const out = new Array(v.length).fill(null);
  if (period <= 0 || v.length < period) return out;
  let s = 0;
  for (let i = 0; i < v.length; i++) {
    s += v[i];
    if (i >= period) s -= v[i - period];
    if (i >= period - 1) out[i] = s / period;
  }
  return out;
}

export function ema(v, period) {
  v = F(v);
  const out = new Array(v.length).fill(null);
  if (period <= 0 || v.length < period) return out;
  const k = 2 / (period + 1);
  let prev = v.slice(0, period).reduce((a, b) => a + b, 0) / period;
  out[period - 1] = prev;
  for (let i = period; i < v.length; i++) {
    prev = v[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

export function wilder(v, period) {
  v = F(v);
  const out = new Array(v.length).fill(null);
  if (period <= 0 || v.length < period) return out;
  let prev = v.slice(0, period).reduce((a, b) => a + b, 0) / period;
  out[period - 1] = prev;
  for (let i = period; i < v.length; i++) {
    prev = (prev * (period - 1) + v[i]) / period;
    out[i] = prev;
  }
  return out;
}

export function trueRange(h, l, c) {
  const out = [];
  for (let i = 0; i < h.length; i++) {
    out.push(i === 0 ? h[i] - l[i]
      : Math.max(h[i] - l[i], Math.abs(h[i] - c[i - 1]), Math.abs(l[i] - c[i - 1])));
  }
  return out;
}

export const atr = (h, l, c, period = 14) => wilder(trueRange(h, l, c), period);

export function rsi(c, period = 14) {
  c = F(c);
  const out = new Array(c.length).fill(null);
  if (c.length <= period) return out;
  const g = [], ls = [];
  for (let i = 1; i < c.length; i++) {
    const d = c[i] - c[i - 1];
    g.push(Math.max(0, d)); ls.push(Math.max(0, -d));
  }
  const ag = wilder(g, period), al = wilder(ls, period);
  for (let i = 0; i < ag.length; i++) {
    if (ag[i] == null || al[i] == null) continue;
    out[i + 1] = al[i] === 0 ? 100 : 100 - 100 / (1 + ag[i] / al[i]);
  }
  return out;
}

export function macd(c, fast = 12, slow = 26, signal = 9) {
  const ef = ema(c, fast), es = ema(c, slow);
  const line = c.map((_, i) =>
    (ef[i] == null || es[i] == null) ? null : ef[i] - es[i]);
  const solid = line.filter((x) => x != null);
  const sv = ema(solid, signal);
  const sig = new Array(c.length).fill(null);
  let j = 0;
  for (let i = 0; i < c.length; i++) {
    if (line[i] == null) continue;
    sig[i] = sv[j++];
  }
  const hist = c.map((_, i) =>
    (line[i] == null || sig[i] == null) ? null : line[i] - sig[i]);
  return { macd: line, signal: sig, hist };
}

export function bollinger(c, period = 20, mult = 2) {
  c = F(c);
  const mid = sma(c, period);
  const upper = new Array(c.length).fill(null);
  const lower = new Array(c.length).fill(null);
  for (let i = 0; i < c.length; i++) {
    if (mid[i] == null) continue;
    const w = c.slice(i + 1 - period, i + 1);
    const m = mid[i];
    const sd = Math.sqrt(w.reduce((a, x) => a + (x - m) ** 2, 0) / period);
    upper[i] = m + mult * sd; lower[i] = m - mult * sd;
  }
  return { upper, mid, lower };
}

export function keltner(h, l, c, period = 20, mult = 2) {
  const mid = ema(c, period), a = atr(h, l, c, period);
  return {
    upper: mid.map((m, i) => (m == null || a[i] == null) ? null : m + mult * a[i]),
    mid,
    lower: mid.map((m, i) => (m == null || a[i] == null) ? null : m - mult * a[i]),
  };
}

export function stoch(h, l, c, kPeriod = 14, dPeriod = 3) {
  const k = new Array(c.length).fill(null);
  for (let i = 0; i < c.length; i++) {
    if (i + 1 < kPeriod) continue;
    const hh = Math.max(...h.slice(i + 1 - kPeriod, i + 1));
    const ll = Math.min(...l.slice(i + 1 - kPeriod, i + 1));
    k[i] = hh === ll ? 50 : 100 * (c[i] - ll) / (hh - ll);
  }
  const d0 = sma(k.map((x) => x == null ? 0 : x), dPeriod);
  return { k, d: k.map((x, i) => x == null ? null : d0[i]) };
}

export function adx(h, l, c, period = 14) {
  const n = c.length;
  const plus = [0], minus = [0];
  for (let i = 1; i < n; i++) {
    const up = h[i] - h[i - 1], dn = l[i - 1] - l[i];
    plus.push(up > dn && up > 0 ? up : 0);
    minus.push(dn > up && dn > 0 ? dn : 0);
  }
  const a = wilder(trueRange(h, l, c), period);
  const ps = wilder(plus, period), ms = wilder(minus, period);
  const pdi = new Array(n).fill(null), mdi = new Array(n).fill(null);
  const dx = [], idx = [];
  for (let i = 0; i < n; i++) {
    if (!a[i] || ps[i] == null || ms[i] == null) continue;
    pdi[i] = 100 * ps[i] / a[i];
    mdi[i] = 100 * ms[i] / a[i];
    const tot = pdi[i] + mdi[i];
    dx.push(tot === 0 ? 0 : 100 * Math.abs(pdi[i] - mdi[i]) / tot);
    idx.push(i);
  }
  const av = wilder(dx, period);
  const out = new Array(n).fill(null);
  idx.forEach((i, j) => { out[i] = av[j]; });
  return { adx: out, plus_di: pdi, minus_di: mdi };
}

export function supertrend(h, l, c, period = 10, mult = 3) {
  const n = c.length;
  const a = atr(h, l, c, period);
  const line = new Array(n).fill(null), dir = new Array(n).fill(null);
  let fu = null, fl = null, d = 1;
  for (let i = 0; i < n; i++) {
    if (a[i] == null) continue;
    const hl2 = (h[i] + l[i]) / 2;
    const bu = hl2 + mult * a[i], bl = hl2 - mult * a[i];
    if (fu == null) { fu = bu; fl = bl; }
    else {
      fu = (bu < fu || c[i - 1] > fu) ? bu : fu;
      fl = (bl > fl || c[i - 1] < fl) ? bl : fl;
    }
    if (c[i] > fu) d = 1; else if (c[i] < fl) d = -1;
    dir[i] = d; line[i] = d === 1 ? fl : fu;
  }
  return { line, dir };
}

export function vwap(h, l, c, v, keys) {
  const out = new Array(c.length).fill(null);
  let pv = 0, vol = 0, prev = null;
  for (let i = 0; i < c.length; i++) {
    const k = keys ? keys[i] : null;
    if (keys && k !== prev) { pv = 0; vol = 0; prev = k; }
    const tp = (h[i] + l[i] + c[i]) / 3;
    pv += tp * v[i]; vol += v[i];
    out[i] = vol ? pv / vol : null;
  }
  return out;
}

export function donchian(h, l, period = 20) {
  const upper = new Array(h.length).fill(null);
  const lower = new Array(h.length).fill(null);
  for (let i = 0; i < h.length; i++) {
    if (i + 1 < period) continue;
    upper[i] = Math.max(...h.slice(i + 1 - period, i + 1));
    lower[i] = Math.min(...l.slice(i + 1 - period, i + 1));
  }
  return { upper, lower };
}

export function psar(h, l, step = 0.02, max = 0.2) {
  const n = h.length;
  const out = new Array(n).fill(null);
  if (n < 3) return out;
  let bull = true, af = step, ep = h[0], sar = l[0];
  for (let i = 1; i < n; i++) {
    sar = sar + af * (ep - sar);
    if (bull) {
      if (l[i] < sar) { bull = false; sar = ep; ep = l[i]; af = step; }
      else if (h[i] > ep) { ep = h[i]; af = Math.min(max, af + step); }
    } else {
      if (h[i] > sar) { bull = true; sar = ep; ep = h[i]; af = step; }
      else if (l[i] < ep) { ep = l[i]; af = Math.min(max, af + step); }
    }
    out[i] = sar;
  }
  return out;
}

export function hma(c, period = 20) {
  const wma = (v, p) => {
    const out = new Array(v.length).fill(null);
    const denom = (p * (p + 1)) / 2;
    for (let i = p - 1; i < v.length; i++) {
      let s = 0;
      for (let j = 0; j < p; j++) s += v[i - j] * (p - j);
      out[i] = s / denom;
    }
    return out;
  };
  const half = wma(c, Math.max(1, Math.round(period / 2)));
  const full = wma(c, period);
  const raw = c.map((_, i) =>
    (half[i] == null || full[i] == null) ? 0 : 2 * half[i] - full[i]);
  const sq = wma(raw, Math.max(1, Math.round(Math.sqrt(period))));
  return c.map((_, i) => (full[i] == null ? null : sq[i]));
}

/* --------------------------------------------------------------- catalog
   What the chart's indicator picker offers. `panel` means it cannot share the
   price axis (RSI is 0-100; drawing it over price is meaningless). */
export const CATALOG = {
  ema:        { label: "EMA", params: { period: 20 }, panel: false,
                run: (b, p) => ({ EMA: ema(b.c, p.period) }) },
  sma:        { label: "SMA", params: { period: 20 }, panel: false,
                run: (b, p) => ({ SMA: sma(b.c, p.period) }) },
  hma:        { label: "Hull MA", params: { period: 20 }, panel: false,
                run: (b, p) => ({ HMA: hma(b.c, p.period) }) },
  vwap:       { label: "VWAP", params: {}, panel: false,
                run: (b) => ({ VWAP: vwap(b.h, b.l, b.c, b.v, b.day) }) },
  bollinger:  { label: "Bollinger", params: { period: 20, mult: 2 }, panel: false,
                run: (b, p) => bollinger(b.c, p.period, p.mult) },
  keltner:    { label: "Keltner", params: { period: 20, mult: 2 }, panel: false,
                run: (b, p) => keltner(b.h, b.l, b.c, p.period, p.mult) },
  donchian:   { label: "Donchian", params: { period: 20 }, panel: false,
                run: (b, p) => donchian(b.h, b.l, p.period) },
  supertrend: { label: "SuperTrend", params: { period: 10, mult: 3 }, panel: false,
                run: (b, p) => ({ ST: supertrend(b.h, b.l, b.c, p.period, p.mult).line }) },
  psar:       { label: "Parabolic SAR", params: { step: 0.02, max: 0.2 }, panel: false,
                run: (b, p) => ({ PSAR: psar(b.h, b.l, p.step, p.max) }) },
  rsi:        { label: "RSI", params: { period: 14 }, panel: true,
                run: (b, p) => ({ RSI: rsi(b.c, p.period) }) },
  macd:       { label: "MACD", params: { fast: 12, slow: 26, signal: 9 }, panel: true,
                run: (b, p) => macd(b.c, p.fast, p.slow, p.signal) },
  stoch:      { label: "Stochastic", params: { kPeriod: 14, dPeriod: 3 }, panel: true,
                run: (b, p) => stoch(b.h, b.l, b.c, p.kPeriod, p.dPeriod) },
  adx:        { label: "ADX", params: { period: 14 }, panel: true,
                run: (b, p) => adx(b.h, b.l, b.c, p.period) },
  atr:        { label: "ATR", params: { period: 14 }, panel: true,
                run: (b, p) => ({ ATR: atr(b.h, b.l, b.c, p.period) }) },
};

export function cols(bars) {
  return {
    o: bars.map((b) => b.o), h: bars.map((b) => b.h),
    l: bars.map((b) => b.l), c: bars.map((b) => b.c),
    v: bars.map((b) => b.v || 0),
    day: bars.map((b) => String(b.t).slice(0, 10)),
  };
}
