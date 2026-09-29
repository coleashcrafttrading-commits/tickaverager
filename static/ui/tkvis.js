/* ============================================================================
   tkvis.js -- the few presentation rules the ticker pages need that neither
   the design system nor the shared visual kit already carries.

   WHAT IS NOT HERE, AND WHY THAT MATTERS. This file started with its own P/L
   calendar, its own histogram, its own contribution bar and its own donut.
   All four are gone: `static/ui/viz.js` and `static/ui/calendar.js` ship those
   components for the whole dashboard, `calendar.js` takes `perf.daily()`'s own
   row shape verbatim and already handles the two cases a second calendar would
   have got wrong differently (the grey day, and deriving a weekday without a
   Date object). Two calendars in one dashboard is how the Overview came to
   look different on two accounts, and the ticker page is not going to be the
   thing that reintroduces it.

   So what is left is genuinely this page's own:

     * `groupHeadHTML`, the headed band that lets the four metric groups be
       ONE panel instead of four. The owner on the old Risk and Settings
       pages: "theres just endless windows and widgets".
     * the strip above the strategy dropdown, which is the kind switch and the
       search that make a 259-entry bank usable on a ticker.

   Every value is a theme.css token, so light and dark both work from one
   rule; `test_tickerperf.py` section 8 checks there is no hex in here and
   that every selector is under `.tkv-`.
   ========================================================================= */
"use strict";
import { esc } from "./core.js";

const CSS = `
/* -- the bank picker on the Strategies tab ----------------------------- */
/* The kind switch and the search sit ABOVE the select rather than beside it:
   at 400px three controls on one row put the button under the fold, and the
   search is the control that makes a 231-entry shelf usable at all. */
.tkv-bankbar{display:flex;gap:var(--s3);flex-wrap:wrap;align-items:flex-end;
  margin-bottom:var(--s4)}
.tkv-bq{flex:1 1 220px;margin:0;min-width:0}
.tkv-bankhead{margin:var(--s6) 0 var(--s3);padding-top:var(--s4);
  border-top:1px solid var(--hairline);color:var(--faint);
  font-size:var(--fs-xs);line-height:var(--lh)}

/* -- a heading inside a panel ------------------------------------------ */
.tkv-grp{display:flex;align-items:baseline;gap:var(--s3);flex-wrap:wrap;
  margin:var(--s6) 0 var(--s3);padding-top:var(--s4);
  border-top:1px solid var(--hairline)}
.tkv-grp:first-child{margin-top:0;padding-top:0;border-top:0}
.tkv-grp-t{font-size:var(--fs-sm);font-weight:var(--w-semi);
  letter-spacing:var(--track-caps);text-transform:uppercase;color:var(--text)}
.tkv-grp-n{color:var(--faint);font-size:var(--fs-xs);line-height:var(--lh);
  flex:1 1 260px;min-width:0}
`;

export function ensureVisCSS() {
  if (document.getElementById("tkv-css")) return;
  const s = document.createElement("style");
  s.id = "tkv-css";
  s.textContent = CSS;
  document.head.appendChild(s);
}

/* A headed band inside a panel, so four metric groups are one window rather
   than four. The note is the sentence that says what the group is measured
   on, and it sits BESIDE the title: at 1280px the reader sees both without
   scrolling and at 400px it wraps to its own line. */
export function groupHeadHTML(title, note) {
  return `<div class="tkv-grp"><span class="tkv-grp-t">${esc(title)}</span>
    ${note ? `<span class="tkv-grp-n">${esc(note)}</span>` : ""}</div>`;
}
