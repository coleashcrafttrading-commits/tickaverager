/* ============================================================================
   tkstyle.js -- the few rules the ticker pages need that the design system
   does not already carry, injected once.

   Almost nothing is here on purpose. The shell's kit in core.js and theme.css
   owns the tiles, the panels, the tables, the chips, the segmented control and
   the empty states, and this file must never grow a second version of any of
   them -- two stylesheets for one component is how the Overview came to look
   different on two accounts in the first place.

   What is left is genuinely the instrument page's own: the big price header
   with its day and 52-week range bars, the per-strategy card, and the frame
   around the small canvas in tkseries.js. Every value is a theme token from
   theme.css, so light and dark both work without a second rule.
   ========================================================================= */
"use strict";

const CSS = `
/* -- the instrument header -------------------------------------------- */
.tkx-head{display:flex;flex-wrap:wrap;gap:var(--s5) var(--s7);align-items:flex-start}
.tkx-id{min-width:0;flex:1 1 220px}
.tkx-sym{font-size:var(--fs-4xl);font-weight:var(--w-bold);
  letter-spacing:var(--track-tight);line-height:var(--lh-tight)}
.tkx-name{color:var(--muted);font-size:var(--fs-md);margin-top:var(--s1);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tkx-tags{display:flex;gap:var(--s1);flex-wrap:wrap;margin-top:var(--s3)}
.tkx-px{text-align:right;flex:0 0 auto}
.tkx-px-v{font-size:var(--fs-3xl);font-weight:var(--w-semi);
  letter-spacing:var(--track-tight);font-variant-numeric:tabular-nums;
  line-height:var(--lh-tight)}
.tkx-px-c{font-size:var(--fs-lg);font-weight:var(--w-med);margin-top:var(--s1);
  font-variant-numeric:tabular-nums}

/* -- the low / high bands --------------------------------------------- */
/* 140px, not 200: the shell's rail does not fully collapse at 375px, so a
   view gets about 233px to work in. A 200px track plus the grid gap made this
   block 200px wide inside a 175px header and it hung over the panel's own
   edge -- measured in a browser at 375, invisible at 1280. min-width:0 is the
   other half: without it a grid inside a flex item refuses to shrink below
   its tracks whatever the basis says. */
.tkx-rngs{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:var(--s4) var(--s6);flex:1 1 260px;min-width:0}
.tkx-rng-k{color:var(--faint);font-size:var(--fs-micro);
  letter-spacing:var(--track-caps);text-transform:uppercase;margin-bottom:var(--s2)}
.tkx-rng-t{position:relative;height:5px;border-radius:var(--radius-pill);
  background:var(--surface-3)}
.tkx-rng-f{position:absolute;inset:0;border-radius:var(--radius-pill);
  background:var(--grad);opacity:.62}
.tkx-rng-m{position:absolute;top:-4px;width:3px;height:13px;border-radius:2px;
  background:var(--text);transform:translateX(-1.5px)}
.tkx-rng-e{display:flex;justify-content:space-between;margin-top:6px;
  color:var(--faint);font-size:var(--fs-xs);font-variant-numeric:tabular-nums}
.tkx-why{color:var(--faint);font-size:var(--fs-xs);line-height:var(--lh)}

/* -- the Market pane's marks (tkmkt.js) -------------------------------- */
/* Every rule here replaces a metric tile. The pane is one rail column wide at
   1280 and the whole page wide at 375, so nothing carries a fixed width and
   every track is a percentage of its own box. */
.tkx-mk{display:flex;flex-direction:column;gap:var(--s4)}
.tkx-mk-track{position:relative;height:6px;border-radius:var(--radius-pill);
  background:var(--surface-3)}
.tkx-mk-track.wide{height:8px;margin:var(--s2) 0 var(--s3)}
.tkx-mk-day{position:absolute;top:0;bottom:0;border-radius:var(--radius-pill);
  background:var(--grad);opacity:.85;min-width:3px}
.tkx-mk-pin{position:absolute;top:-4px;width:3px;height:14px;border-radius:2px;
  background:var(--text);transform:translateX(-1.5px)}
.tkx-mk-ends{display:flex;justify-content:space-between;margin-top:6px;
  color:var(--faint);font-size:var(--fs-xs);font-variant-numeric:tabular-nums}
.tkx-mk-k{font-size:var(--fs-micro);letter-spacing:var(--track-caps);
  text-transform:uppercase}
.tkx-mk-dot{position:absolute;top:50%;width:10px;height:10px;border-radius:50%;
  transform:translate(-5px,-5px);border:2px solid var(--surface)}
.tkx-mk-dot.iv{background:var(--accent)}
.tkx-mk-dot.rv{background:var(--muted)}

.tkx-mk-q{display:flex;align-items:center;gap:var(--s2)}
.tkx-mk-qv{font-size:var(--fs-sm);font-variant-numeric:tabular-nums;
  font-weight:var(--w-med)}
/* NO justify-content HERE. Centring the segment was what made the marker's
   position a decoration: the offset is computed from the quote and set as a
   margin-left, and a flex rule that re-centres it silently overrides the
   measurement. */
.tkx-mk-qt{flex:1 1 auto;min-width:24px;height:6px;border-radius:var(--radius-pill);
  background:var(--surface-3);display:block;position:relative}
.tkx-mk-qs{font-size:var(--fs-micro);color:var(--ink-3);
  font-variant-numeric:tabular-nums;flex:1 1 auto;text-align:center}
.tkx-mk-qt i{display:block;height:100%;border-radius:var(--radius-pill);
  background:var(--warn);opacity:.8}

.tkx-mk-vol{display:flex;flex-direction:column;gap:6px}
.tkx-mk-vt{position:relative;display:flex;align-items:flex-end;gap:2px;
  height:44px}
.tkx-mk-vt i{flex:1 1 0;min-width:2px;border-radius:2px 2px 0 0;
  background:var(--surface-3)}
.tkx-mk-vt i.on{background:var(--accent)}
.tkx-mk-avg{position:absolute;left:0;right:0;height:1px;background:var(--faint);
  opacity:.65}
.tkx-mk-vl{display:flex;justify-content:space-between;font-size:var(--fs-xs);
  font-variant-numeric:tabular-nums;color:var(--muted)}

.tkx-mk-vol2{display:flex;flex-direction:column}
.tkx-mk-chips{display:flex;flex-wrap:wrap;gap:var(--s1)}
.tkx-mk-chip{display:inline-flex;align-items:center;gap:5px;
  padding:3px var(--s2);border-radius:var(--radius-pill);
  background:var(--surface-3);color:var(--muted);font-size:var(--fs-micro);
  letter-spacing:var(--track-caps);text-transform:uppercase;white-space:nowrap}
.tkx-mk-chip b{font-size:var(--fs-sm);letter-spacing:normal;
  text-transform:none;color:var(--text);font-variant-numeric:tabular-nums}
.tkx-mk-chip.iv b{color:var(--accent-2)}
.tkx-mk-chip.warn{background:var(--warn-dim);color:var(--warn)}
.tkx-mk-chip.warn b{color:var(--warn)}
.tkx-mk-line{display:block;width:100%;height:22px;margin-top:var(--s3);
  color:var(--accent)}

.tkx-mk-news{list-style:none;margin:0;padding:0;display:flex;
  flex-direction:column;gap:var(--s3)}
.tkx-mk-news li{display:flex;flex-direction:column;gap:2px;min-width:0}
.tkx-mk-news a{color:var(--text);font-size:var(--fs-sm);line-height:var(--lh);
  text-decoration:none}
.tkx-mk-news a:hover{color:var(--accent-2);text-decoration:underline}
.tkx-mk-news span{color:var(--faint);font-size:var(--fs-micro);
  text-transform:uppercase;letter-spacing:var(--track-caps)}
.tkx-mk-err{margin:0}

/* -- one strategy on this ticker -------------------------------------- */
/* minmax(min(260px, 100%), 1fr), not minmax(260px, 1fr): the bare form makes
   the TRACK 260px wide whatever the container is, so at phone width the card
   hung 85px past the panel it lives in. min() lets the track fall back to the
   container. Measured at 375; nothing shows at 1280. */
.tkx-strats{display:grid;gap:var(--s4);
  grid-template-columns:repeat(auto-fit,minmax(min(260px,100%),1fr))}
.tkx-sc{border:1px solid var(--hairline);border-radius:var(--r-md);
  background:var(--surface-2);padding:var(--s4);min-width:0;overflow-wrap:anywhere}
.tkx-sc-h{display:flex;align-items:center;gap:var(--s2);flex-wrap:wrap;
  margin-bottom:var(--s3)}
.tkx-sc-l{font-size:var(--fs-lg);font-weight:var(--w-semi)}
.tkx-sc-k{color:var(--faint);font-size:var(--fs-micro);text-transform:uppercase;
  letter-spacing:var(--track-caps)}
.tkx-sc-h .chip{margin-left:auto}
.tkx-kv{display:grid;grid-template-columns:1fr auto;gap:6px var(--s4);
  font-size:var(--fs-sm);margin:0}
.tkx-kv dt{color:var(--muted);min-width:0}
.tkx-kv dd{margin:0;text-align:right;font-variant-numeric:tabular-nums;
  font-weight:var(--w-med)}
.tkx-sc-a{display:flex;gap:var(--s2);flex-wrap:wrap;margin-top:var(--s4);
  padding-top:var(--s3);border-top:1px solid var(--hairline)}
.tkx-ref{margin-left:auto;align-self:center;color:var(--faint);
  font-size:var(--fs-micro);font-family:var(--mono)}

/* -- the small canvas in tkseries.js ---------------------------------- */
.tkx-ms{position:relative;width:100%}
.tkx-ms canvas{display:block}
.tkx-ms-tip{position:absolute;top:6px;pointer-events:none;display:flex;
  flex-direction:column;gap:2px;background:var(--surface-3);color:var(--text);
  border:1px solid var(--hairline2);border-radius:var(--r-sm);padding:7px 10px;
  font-size:var(--fs-xs);box-shadow:var(--e2);font-variant-numeric:tabular-nums}
.tkx-ms-tip span{color:var(--faint)}
/* display:flex on these BEATS the user agent's [hidden] rule, so setting
   el.hidden left an empty 22x16 grey box floating over the chart. Seen in a
   browser, invisible in the source. Specificity, restated. */
.tkx-ms-tip[hidden],.tkx-ms-note[hidden],.tkx-ms-empty[hidden]{display:none}
.tkx-ms-note{position:absolute;left:0;bottom:-4px;color:var(--warn);
  font-size:var(--fs-xs);line-height:var(--lh);max-width:60ch}
.tkx-ms-empty{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;color:var(--faint);font-size:var(--fs-sm);
  text-align:center;padding:0 var(--s5);line-height:var(--lh)}

/* -- the add flow ------------------------------------------------------ */
.tkx-att{display:flex;gap:var(--s3);flex-wrap:wrap;align-items:flex-end}
.tkx-att label{flex:1 1 200px;margin:0;min-width:0}

@media (max-width:820px){
  .tkx-px{text-align:left}
  .tkx-sym{font-size:var(--fs-3xl)}
  .tkx-px-v{font-size:var(--fs-2xl)}
}
`;

export function ensureCSS() {
  if (document.getElementById("tkx-css")) return;
  const s = document.createElement("style");
  s.id = "tkx-css";
  s.textContent = CSS;
  document.head.appendChild(s);
}
