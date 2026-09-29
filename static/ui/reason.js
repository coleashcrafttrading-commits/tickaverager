/* ============================================================================
   reason.js -- a REASON you can reach without a mouse.

   ROUND 7, and the other half of round 6. Round 6 was asked to take the prose
   off the dashboard and it did: the Hub went from 858 visible words to 356.
   But most of that prose was not deleted, it was moved into `title=`, and the
   Hub ended the round carrying 80 title attributes holding 1,113 words behind
   356 visible ones. Counting both, it had fallen 2%.

   A `title` is not a delete. It is also, and this is the part that matters,
   NOT READABLE:

     * on a phone there is no hover, so it never appears at all;
     * from the keyboard there is no hover either, so a Tab user never sees it;
     * a screen reader announces it inconsistently and, on a <div>, often not.

   Round 7 deleted the glossary. What it KEPT is the class of sentence this
   product exists for -- why a number is missing, why a figure is thin, what a
   control will cost -- and those may not stay behind a hover. This file is how
   they stop being.

   WHAT IT DOES

     wireReasons(root)   every mark inside `root` that carries a reason becomes
                         a real disclosure: focusable, announced, and openable
                         by tap, by click and by Enter/Space. Hover still works
                         -- the `title` is left exactly where it was, so a mouse
                         user sees the native tooltip and nothing regressed.

   A mark opts IN with `data-why`; `.unmeasured[title]` (core.js's dash-with-a-
   reason) is opted in for it, because that dash is the single most important
   sentence on any of these pages and it is rendered by a helper this file is
   not allowed to edit.

   IT ADDS NO WORDS. The popover shows the text that was already in the title
   attribute; nothing new is written and nothing is duplicated into the DOM
   until it is opened. Measured as "visible words plus every title= word",
   which is the bar round 7 is held to, this file is zero.

   ONE POPOVER, NOT ONE PER MARK, and every class name is prefixed `rsn-`.
   Five agents inject stylesheets into this one document, and round 6 shipped
   a ten-minute bug where viz.js's new `.ds-track` collided with returns.js's
   -- a 6px track rendered as a 74px white slab. A new global class name is a
   merge hazard, so these are namespaced and there is exactly one element.
   ========================================================================= */
"use strict";

/* the marks this file upgrades. `data-why` is the opt-in; `.unmeasured` is
   core.js's dash-with-its-reason, opted in here because that is the one
   sentence the product's contract is made of. */
const SEL = "[data-why][title], .unmeasured[title]";

/* elements that are already in the tab order; giving these a tabindex would
   be a no-op at best and would reorder the page at worst */
const NATIVE = new Set(["BUTTON", "A", "INPUT", "SELECT", "TEXTAREA", "SUMMARY"]);

let styled = false;
let pop = null;          // the one popover
let owner = null;        // the mark it is currently showing
let shownAt = 0;         // when, so a mouse click does not undo its own focus

export function ensureReasonStyles() {
  if (styled) return;
  styled = true;
  const css = [
    /* the mark itself: it must LOOK like something you can open, or a
       focusable div is just a trap for the Tab key */
    ".rsn-m{cursor:help}",
    ".rsn-m:focus-visible{outline:2px solid var(--accent,#6c8cff);",
    "outline-offset:2px;border-radius:3px}",
    /* the popover. position:fixed against the mark's own rect, so it does not
       care what its ancestor's overflow or transform is doing. */
    ".rsn-pop{position:fixed;z-index:9999;max-width:min(320px,calc(100vw - 24px));",
    "padding:8px 10px;border-radius:var(--radius,6px);",
    "background:var(--surface,#191b20);color:var(--text,#e8e8ea);",
    "border:1px solid var(--hairline2,#3a3d45);",
    "box-shadow:0 6px 24px rgba(0,0,0,.36);",
    "font-size:var(--fs-sm,12px);line-height:1.45;white-space:pre-line}",
    ".rsn-pop[hidden]{display:none}",
  ].join("");
  const s = document.createElement("style");
  s.id = "rsn-css";
  s.textContent = css;
  document.head.appendChild(s);
}

function popover() {
  if (pop && pop.isConnected) return pop;
  pop = document.createElement("div");
  pop.className = "rsn-pop";
  pop.id = "rsn-pop";
  pop.setAttribute("role", "tooltip");
  pop.hidden = true;
  document.body.appendChild(pop);
  return pop;
}

function hide() {
  if (owner) {
    owner.setAttribute("aria-expanded", "false");
    owner.removeAttribute("aria-describedby");
  }
  owner = null;
  if (pop) pop.hidden = true;
}

function show(mark, toggle) {
  const text = (mark.getAttribute("title") || "").trim();
  if (!text) return hide();
  if (owner === mark && pop && !pop.hidden) {
    /* A SECOND TAP SHUTS IT -- but a mouse click on a <button> fires focusin
       FIRST and click second, so without this the popover would open on the
       focus and be closed again by the same click, 12 ms later, and the
       control would look dead. That is the exact defect this file was written
       to remove, reintroduced by its own fix. 300 ms is longer than any
       browser takes between those two events and far shorter than a
       deliberate second press. */
    if (toggle && Date.now() - shownAt > 300) return hide();
    return;
  }
  hide();
  const p = popover();
  p.textContent = text;
  p.hidden = false;
  owner = mark;
  shownAt = Date.now();
  mark.setAttribute("aria-expanded", "true");
  mark.setAttribute("aria-describedby", "rsn-pop");
  /* measure, THEN place. A popover placed before it has been laid out is
     placed against a zero-height box and lands over the mark it explains. */
  const r = mark.getBoundingClientRect();
  const b = p.getBoundingClientRect();
  const gap = 8;
  let top = r.bottom + gap;
  if (top + b.height > window.innerHeight - 8) {
    const above = r.top - gap - b.height;
    top = above >= 8 ? above : Math.max(8, window.innerHeight - 8 - b.height);
  }
  let left = r.left;
  if (left + b.width > window.innerWidth - 8) left = window.innerWidth - 8 - b.width;
  p.style.top = Math.round(top) + "px";
  p.style.left = Math.round(Math.max(8, left)) + "px";
}

/* one set of listeners per root, and the root is #view, which app.js refills
   with innerHTML rather than replacing -- so these survive every repaint and
   are never stacked up twice. */
function wireRoot(r) {
  if (r.dataset.rsnRoot) return;
  r.dataset.rsnRoot = "1";
  r.addEventListener("focusin", (e) => {
    const m = e.target.closest && e.target.closest(".rsn-m");
    if (m) show(m); else if (owner) hide();
  });
  r.addEventListener("focusout", (e) => {
    /* focus moving inside the same mark is not leaving it */
    if (owner && (!e.relatedTarget || !owner.contains(e.relatedTarget))) hide();
  });
  /* TOUCH. A tap fires click and, on a div, no focus at all -- which is why
     `title` is invisible on a phone and why this listener is the whole point
     of the file. The event is NOT stopped: a mark inside a row that navigates
     still navigates, and the popover goes with the page. */
  r.addEventListener("click", (e) => {
    const m = e.target.closest && e.target.closest(".rsn-m");
    if (m) show(m, true);
    else if (owner) hide();
  });
  r.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
    const m = e.target.closest && e.target.closest(".rsn-m");
    if (!m || NATIVE.has(m.tagName)) return;   // a real button gets its own click
    e.preventDefault();
    show(m);
  });
}

let global = false;
function wireGlobal() {
  if (global) return;
  global = true;
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
  window.addEventListener("scroll", () => { if (owner) hide(); }, true);
  window.addEventListener("resize", () => { if (owner) hide(); });
}

/* wireReasons(root) -- call it after the HTML is in the DOM, on every paint.
   It is idempotent: a mark already upgraded is skipped, and a repaint that
   replaced the markup simply upgrades the new marks. */
export function wireReasons(root) {
  const r = typeof root === "string" ? document.getElementById(root) : root;
  if (!r) return;
  ensureReasonStyles();
  wireGlobal();
  wireRoot(r);
  if (owner && !owner.isConnected) hide();     // its mark was repainted away
  for (const m of r.querySelectorAll(SEL)) {
    if (m.dataset.rsnOn) continue;
    const t = (m.getAttribute("title") || "").trim();
    if (!t) continue;
    m.dataset.rsnOn = "1";
    m.classList.add("rsn-m");
    if (!NATIVE.has(m.tagName)) {
      if (!m.hasAttribute("tabindex")) m.tabIndex = 0;
      /* A HEADING KEEPS ITS ROLE. `role="button"` on an <h2> takes the panel
         out of a screen reader's heading list, which is how a blind reader
         navigates a page of panels -- a worse trade than the one this file
         exists to make. It is focusable and it answers Enter; that is enough,
         and the popover is wired to it by aria-describedby when it opens. */
      if (!m.hasAttribute("role") && !/^H[1-6]$/.test(m.tagName)) {
        m.setAttribute("role", "button");
      }
    }
    if (!m.hasAttribute("aria-expanded")) m.setAttribute("aria-expanded", "false");
    /* a bare dash with a title announces as "—". The reason IS the label. */
    if (!m.hasAttribute("aria-label") && !(m.textContent || "").trim()) {
      m.setAttribute("aria-label", t);
    }
  }
}

/* for the suites: what is open right now, or "" */
export const __openReason = () => (owner && !pop.hidden ? pop.textContent : "");
