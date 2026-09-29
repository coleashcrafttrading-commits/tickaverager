/* ============================================================================
   motion.js -- the movement layer, and the only file in this repo that is
   allowed to move anything.

   The complaint this answers, verbatim: "the dashboard looks like it was
   python coded like it doesnt look smooth if that makes sense". What makes a
   screen feel coded rather than built is that nothing acknowledges a change:
   a figure that has ticked $40 replaces itself between two frames, a page you
   navigate to appears whole out of nothing, and a pane that is waiting is
   blank. Three things, and this file does those three.

   FOUR RULES IT KEEPS, because a trading screen is not a marketing page:

   1. NOTHING IS INVENTED. A figure only counts when the OLD and the NEW text
      each hold exactly one number and the characters around that number are
      identical -- same currency mark, same sign, same suffix. "1/1" holds
      two numbers and never animates. A dash never animates, because there is
      no number on either side of it and the reason it is a dash is the
      point. "+$252.00" going to "-$14.00" does not animate either: the sign
      lives in the prefix, the prefix changed, and easing a green number
      through zero into a red one would show money that never existed. On any
      of those the new text is written straight in.

   2. NOTHING COUNTS UP FROM NOTHING. A value is only ever eased between two
      numbers that were both really on screen. The first time a figure is
      seen it is written, not animated -- a page that flies every balance up
      from zero on load is showing you six wrong numbers to look lively.

   3. MOTION IS SHORT AND IT IS ONE CURVE. 180-220ms, ease-out, and every
      duration comes from a token in theme.css so there is one dial.

   4. prefers-reduced-motion TURNS ALL OF IT OFF, here as well as in the CSS.
      REDUCED is read live, so toggling it in the OS takes effect without a
      reload.

   ------------------------------------------------------------- the traps
   * THE FEEDBACK LOOP. Every view in this app repaints by assigning
     innerHTML to a named container every two seconds -- measured on the
     Trading hub: 40 childList records and 125 replaced nodes in 7 seconds.
     So this file observes childList ONLY, and writes its tween frames into
     `firstChild.nodeValue`, which is a characterData change. An `el.
     textContent = x` would replace the text NODE, which is a childList
     record, which would wake the observer, which would tween again. That is
     an infinite loop and it is the reason for the split.

   * THE ELEMENT IS NOT THE SAME ELEMENT. Because the container is rebuilt,
     the node holding "$94,388.10" is destroyed and a different node holding
     "$94,412.30" takes its place. There is nothing to compare, so the
     previous value is remembered against a KEY built from where the figure
     sits (its index path up to the nearest id) plus the label beside it. If
     either moves, the key misses, and a miss writes rather than animates.

   * THE STALE REVEAL. The entrance animation is armed by a ROUTE change and
     disarmed the moment it runs. Armed by "the view re-rendered" instead, it
     would replay every two seconds forever.

   Public, for the views (none of it is required -- the file works with no
   caller at all):
       Motion.reduced                 live prefers-reduced-motion
       Motion.reveal(root)            stagger the blocks under root
       Motion.skeleton(spec)          HTML for a placeholder of that shape
       Motion.count(el, text)         write text, easing the digits if honest
   ========================================================================= */

/* ------------------------------------------------------------- the dials */
const DUR_NUM = 220;      /* a figure easing to its new value */
const DUR_IN = 200;       /* a block arriving */
const STAGGER = 26;       /* between one block and the next */
const MAX_TWEENS = 48;    /* above this the page CHANGED; snap it all */
const SKEL_AFTER = 140;   /* ms of empty pane before a placeholder is worth it */

const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
export const Motion = {
  get reduced() { return mq.matches; },
  reveal, skeleton, count,
};
window.Motion = Motion;

/* =========================================================== the numbers */

/* One numeric run, with the characters on either side of it. Returns null
   for "no number", for "more than one number" and for the empty string --
   all three of which mean "do not animate this", which is why they are one
   answer rather than three. */
const RUN = /-?\d[\d,]*(?:\.\d+)?/g;

export function parseNum(text) {
  const s = String(text == null ? "" : text);
  RUN.lastIndex = 0;
  const first = RUN.exec(s);
  if (!first) return null;
  if (RUN.exec(s)) return null;          /* two numbers: "1/1", "3 of 5" */
  const raw = first[0];
  const value = Number(raw.replace(/,/g, ""));
  if (!isFinite(value)) return null;
  const dot = raw.indexOf(".");
  return {
    pre: s.slice(0, first.index),
    post: s.slice(first.index + raw.length),
    value,
    decimals: dot < 0 ? 0 : raw.length - dot - 1,
    grouped: raw.indexOf(",") >= 0,
  };
}

/** Put the thousands separators back. Written out rather than left to
 *  toLocaleString, because that would follow the BROWSER's locale and a
 *  figure the server formatted as 94,388.10 would ease through 94.388,10 on
 *  a German machine and land back on the server's format. */
function group(digits) {
  const dot = digits.indexOf(".");
  const whole = dot < 0 ? digits : digits.slice(0, dot);
  const rest = dot < 0 ? "" : digits.slice(dot);
  let out = "";
  for (let i = 0; i < whole.length; i++) {
    if (i > 0 && (whole.length - i) % 3 === 0) out += ",";
    out += whole[i];
  }
  return out + rest;
}

export function formatLike(shape, value) {
  const neg = value < 0;
  let digits = Math.abs(value).toFixed(shape.decimals);
  if (shape.grouped) digits = group(digits);
  return shape.pre + (neg ? "-" : "") + digits + shape.post;
}

/** May these two strings be eased into one another? The whole honesty rule
 *  in one function, so the test can hold it.
 *
 *  CROSSING A THOUSAND IS NOT A CHANGE OF SHAPE, and treating it as one was
 *  the single worst thing this file did. `grouped` only records whether the
 *  string HAPPENED to contain a comma, so "$999.12" -> "$1,002.44" read as
 *  two different shapes and the most-watched figure on the dashboard -- the
 *  account value going through a round thousand -- was the one figure that
 *  snapped. It is measured either side of that boundary by the same
 *  formatter; nothing about the currency mark, the sign, the suffix or the
 *  decimals moved. So one crossing is allowed, and ONLY that one: the
 *  ungrouped side must really be under a thousand and the grouped side at or
 *  over it. "1002" -> "1,003" is not a crossing, it is two renderers
 *  disagreeing about the same magnitude, and that still snaps.
 *
 *  The separators then go on for the WHOLE flight, in both directions, so a
 *  figure falling out of the thousands shows "$1,001.20" on the way down
 *  rather than growing a comma at the last frame. The published string is
 *  still written byte for byte at the end -- `runTween` holds it -- so the
 *  value that lands is the one the server sent, grouped its way. */
export function tweenable(fromText, toText) {
  const a = parseNum(fromText), b = parseNum(toText);
  if (!a || !b) return null;
  if (a.pre !== b.pre || a.post !== b.post) return null;
  if (a.decimals !== b.decimals) return null;
  if (a.value === b.value) return null;
  let grouped = b.grouped;
  if (a.grouped !== b.grouped) {
    const bare = a.grouped ? b : a;
    const comma = a.grouped ? a : b;
    if (Math.abs(bare.value) >= 1000) return null;
    if (Math.abs(comma.value) < 1000) return null;
    grouped = true;
  }
  return {
    from: a.value, to: b.value,
    shape: { pre: b.pre, post: b.post, decimals: b.decimals, grouped },
  };
}

const easeOut = (t) => 1 - Math.pow(1 - t, 3);

/** Write `text` into `el`, easing the digits when that is honest. Safe to
 *  call on anything; it degrades to an assignment. */
export function count(el, text, dur) {
  const node = el && el.firstChild;
  if (!node || node.nodeType !== 3 || el.children.length) {
    if (el) el.textContent = String(text);
    return;
  }
  const plan = (Motion.reduced || document.hidden)
    ? null : tweenable(node.nodeValue, text);
  /* The published value is written FIRST so that runTween captures it as the
     string it was given, byte for byte, rather than re-deriving it from the
     shape. runTween then winds the node back to the old value and plays it
     forwards -- with a timer holding the published string the whole time, so
     losing the frames costs the ease and never the figure. */
  node.nodeValue = String(text);
  if (plan) runTween(el, plan, dur);
}

/* ======================================================== where a figure is
   A figure's identity is its position plus the words next to it. Position
   alone would move a Cash tween onto Invested the day a tile is inserted;
   the label alone would collide the moment two panels both say "Open P/L",
   which this dashboard does. Both together miss safely: a miss writes. */

const LABELS = ".tile-k,.hb-k,.kpi-k,.stat-k,.tick-sym,.dt-k,.panel-t";

function labelOf(el) {
  let n = el;
  for (let i = 0; n && i < 5; i++) {
    const lab = n.querySelector ? n.querySelector(LABELS) : null;
    if (lab && lab.textContent) return lab.textContent.trim().slice(0, 40);
    n = n.parentElement;
  }
  return "";
}

function keyOf(el) {
  const parts = [];
  let n = el;
  for (let i = 0; n && n !== document.body && i < 14; i++) {
    if (n.id) { parts.push("#" + n.id); break; }
    const p = n.parentElement;
    if (!p) break;
    let idx = 0;
    for (let c = p.firstElementChild; c && c !== n; c = c.nextElementSibling) {
      idx++;
    }
    parts.push(n.tagName + idx);
    n = p;
  }
  return parts.reverse().join(">") + "|" + labelOf(el);
}

/* The cache is per ROUTE. Crossing to another page and back should write the
   figures, not animate them off whatever they happened to be last time.
   A null-prototype object rather than a Map, so this file runs unchanged in
   the Duktape that test_motion.py compiles it into -- Duktape 2.x has no Map,
   and a bare {} would answer to "toString" and "constructor" as though a
   figure had been seen there before. */
const SEEN = Object.create(null);
let route = null;

/** Every leaf `.num` that is actually on screen. `.num` is core.js's own
 *  wrapper for a formatted figure, so this needs nothing from the views.
 *  A modal, the command bar and anything opted out are skipped: a dialog is
 *  a decision, not a readout. */
function figures() {
  const out = [];
  const all = document.querySelectorAll(".num");
  for (let i = 0; i < all.length; i++) {
    const el = all[i];
    if (el.children.length) continue;               /* a wrapper, not a leaf */
    if (el.closest(".veil,.cmdk,[data-nomotion]")) continue;
    if (!el.offsetParent && el.offsetHeight === 0) continue;   /* hidden tab */
    out.push(el);
  }
  return out;
}

function sweepNumbers() {
  const els = figures();
  const plans = [];
  /* A HIDDEN TAB STILL HAS TO HOLD THE RIGHT NUMBER. requestAnimationFrame
     does not fire in a background tab, so a tween started there would write
     its first frame -- the OLD value -- and then wait, and the page would sit
     on a stale figure until someone looked at it. The cache below is still
     kept up to date, so coming back does not then animate a whole page. */
  const blind = document.hidden;
  for (let i = 0; i < els.length; i++) {
    const el = els[i];
    const text = el.textContent;
    const key = keyOf(el);
    const was = SEEN[key];
    SEEN[key] = text;
    if (was === undefined || was === text) continue;
    if (Motion.reduced || blind) continue;
    const plan = tweenable(was, text);
    if (plan) plans.push([el, plan]);
  }
  /* A whole page arriving is not eight figures ticking. Above the cap this is
     a navigation or a first load, and easing all of it would be a light show
     over data nobody asked to watch move. */
  if (plans.length > MAX_TWEENS) return;
  for (let i = 0; i < plans.length; i++) runTween(plans[i][0], plans[i][1]);
}

function runTween(el, plan, dur) {
  const node = el.firstChild;
  if (!node || node.nodeType !== 3) return;
  const final = node.nodeValue;
  const ms = dur || DUR_NUM;
  const t0 = performance.now();
  /* THE SAFETY NET, and it is not optional.
     A frame chain stops for reasons this file does not control -- the tab
     goes to the background mid-tween, the compositor stalls, the page is
     hidden behind another window. Measured in a real browser: a tween of
     "$71,240" to "$71,980" lost its frames at about 170ms and the figure
     stayed on $71,756 -- a number that was never true and that nothing on
     the page would ever correct, because the view had already written the
     value it believed was on screen. A timer runs in a background tab where
     a frame does not, so this writes the published value whatever happened
     to the frames. */
  const settle = setTimeout(() => {
    if (el.isConnected && el.firstChild === node) node.nodeValue = final;
  }, ms + 500);
  const step = (now) => {
    if (!el.isConnected || el.firstChild !== node) {
      clearTimeout(settle);                                  /* repainted */
      return;
    }
    const k = Math.min(1, (now - t0) / ms);
    node.nodeValue = k >= 1 ? final
      : formatLike(plan.shape, plan.from + (plan.to - plan.from) * easeOut(k));
    if (k < 1) requestAnimationFrame(step);
    else clearTimeout(settle);
  };
  /* Start from where it WAS, not from where it landed, or the first frame is
     the final value and the ease is invisible. */
  node.nodeValue = formatLike(plan.shape, plan.from);
  requestAnimationFrame(step);
}

/* ========================================================== the entrance */

/** Stagger the blocks under `root` in. Called once per route by the sweep;
 *  exported so a view that mounts something big later can ask for it. */
export function reveal(root) {
  if (Motion.reduced || !root) return;
  /* CSS animations are paused in a background tab exactly as rAF is, and
     `animation: both` holds the FROM state while they are -- so revealing a
     page nobody is looking at would leave up to eighteen blocks sitting at
     opacity 0 until the timer below took the class off again. The content is
     already correct; it just must not be hidden to celebrate arriving. */
  if (document.hidden) return;
  const blocks = [];
  for (let c = root.firstElementChild; c; c = c.nextElementSibling) {
    /* A grid of tiles reads as its tiles, not as one slab: the tiles are the
       objects the eye counts, so they are what arrives. */
    if (c.classList.contains("tile-grid")) {
      for (let t = c.firstElementChild; t; t = t.nextElementSibling) {
        blocks.push(t);
      }
    } else {
      blocks.push(c);
    }
    if (blocks.length > 18) break;
  }
  for (let i = 0; i < blocks.length && i < 18; i++) {
    const b = blocks[i];
    b.style.setProperty("--mo-i", String(i));
    b.classList.add("mo-in");
    /* Take the class off once it has played. Left on, a block that is later
       re-parented would replay, and an `animation: both` would keep holding
       the FROM state on anything the browser decided not to run. */
    setTimeout(() => b.classList.remove("mo-in"), DUR_IN + STAGGER * i + 120);
  }
}

/* ========================================================= the empty pane

   A pane that is waiting should say it is waiting in the SHAPE of what is
   coming. `Motion.skeleton("tiles:4 panel panel")` is that sentence; a view
   can drop the string into a container and the placeholder matches the
   layout that replaces it, so nothing jumps when the data lands.

   The automatic case below is only a backstop, for the pane that mounts with
   nothing in it at all. It is removed by the view's own innerHTML write, so
   there is no teardown to get wrong. */

const SKEL_ONE = {
  tiles: (n) => `<div class="tile-grid">${repeat(n || 4,
    `<div class="tile skel-tile"><div class="skel skel-k"></div>
      <div class="skel skel-v"></div><div class="skel skel-s"></div></div>`)
    }</div>`,
  panel: (n) => `<div class="panel skel-panel"><div class="panel-h">
      <div class="panel-tt"><div class="skel skel-t"></div>
      <div class="skel skel-s"></div></div></div><div class="panel-b">${
    repeat(n || 4, `<div class="skel skel-row"></div>`)}</div></div>`,
  lines: (n) => repeat(n || 3, `<div class="skel skel-row"></div>`),
};

/* a loop, not Array.from: Duktape has no Array.from and test_motion.py runs
   this file there rather than a paraphrase of it */
function repeat(n, html) {
  let out = "";
  for (let i = 0; i < n; i++) out += html;
  return out;
}

export function skeleton(spec) {
  const words = String(spec || "tiles:4 panel").split(/\s+/).filter(Boolean);
  let html = "";
  for (let i = 0; i < words.length; i++) {
    const bits = words[i].split(":");
    const make = SKEL_ONE[bits[0]];
    if (make) html += make(bits.length > 1 ? parseInt(bits[1], 10) : 0);
  }
  return `<div class="skel-wrap" aria-hidden="true">${html}</div>`;
}

let skelTimer = null;

function maybeSkeleton() {
  const host = document.getElementById("view");
  if (!host) return;
  const real = host.querySelector(":scope > *:not(.skel-wrap)");
  if (real) { return; }
  if (skelTimer) return;
  skelTimer = setTimeout(() => {
    skelTimer = null;
    const h = document.getElementById("view");
    if (!h || h.querySelector(":scope > *:not(.skel-wrap)")) return;
    /* `data-skel` lets a view declare its own shape before it has data. With
       no attribute the generic shape is the one every page here starts with:
       a row of tiles over a panel. */
    h.innerHTML = skeleton(h.getAttribute("data-skel") || "tiles:4 panel");
  }, SKEL_AFTER);
}

/* ============================================================== the pump

   ONE observer, ONE frame of work per batch. childList only -- see the trap
   at the top of this file. */

let queued = false;
let revealArmed = true;

function pump() {
  queued = false;
  const now = location.hash.split("?")[0];
  if (now !== route) {
    route = now;
    for (const k in SEEN) delete SEEN[k];
    revealArmed = true;
  }
  maybeSkeleton();
  const host = document.getElementById("view");
  if (revealArmed && host && host.querySelector(":scope > *:not(.skel-wrap)")) {
    revealArmed = false;
    reveal(host);
  }
  sweepNumbers();
}

function schedule() {
  if (queued) return;
  queued = true;
  /* A background tab never gets a frame, so scheduling on rAF alone would
     leave `queued` stuck true for as long as the tab was hidden and this file
     would be dead when it came back. The timer is not for animating -- the
     sweep refuses to animate a hidden page -- it is so the cache of what each
     figure last said keeps up while nobody is looking. */
  if (document.hidden) { setTimeout(pump, 120); return; }
  requestAnimationFrame(pump);
}

const obs = new MutationObserver(schedule);

function boot() {
  route = location.hash.split("?")[0];
  obs.observe(document.body, { childList: true, subtree: true });
  window.addEventListener("hashchange", schedule);
  document.addEventListener("visibilitychange", schedule);
  if (mq.addEventListener) mq.addEventListener("change", schedule);
  schedule();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
