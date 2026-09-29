/* ============================================================================
   tickeropts.js -- the OPTIONS pane on one ticker's page.

   This file is where the Plays room went. The owner deleted that room by
   description: "we have a lot of waste with 'plays' and 'data' on the options
   ... the plays are just a conglomerate of all the tickers when i can just go
   to them myself and see it ... I should be able to have a simple pane on the
   ticker that says 'options strategy' and from the dropdown I can choose
   one." Everything the room did is here, keyed by the symbol that is already
   on screen.

   ITS OWN FILE, and that is not an arbitrary split. static/ui/views/options.js
   is compiled by test_optview.py through the Babel that ships inside dukpy,
   which overflows its C stack on that bundle; three of its functions are
   already excluded from it. Putting this pane there would have made that
   worse. Here it gets a fresh compile stack and its own test.

   ------------------------------------------------------------------- writes
   THIS FILE CAN SEND ONE ORDER, and it is a CLOSING one. Everything else it
   POSTs writes a local file. Exactly what it calls, and nothing else:

     POST /api/optlab/plays/arm      writes state/options/PLAYS_ARMED. ADDITIVE
                                     -- one row's Arm adds its key and leaves
                                     the rest of the set alone.
     POST /api/optlab/plays/disarm   with `keys`, so one row's Disarm does not
                                     switch off the other eight. Never gated,
                                     never rate-limited: the stop button has to
                                     work when everything else does not.
     POST /api/optlab/plays/enable   the assignment's own switch.
     POST /api/optlab/plays/close    THE ONE ORDER. A closing order only, not
                                     gated by the arm, because a human asking
                                     to get out must never be refused for being
                                     disarmed. It is the only route under
                                     /api/optlab that reaches the broker and
                                     test_optboard.py asserts that.

   ATTACHING IS NOT HERE. It is the one bank's dropdown on the Strategies tab,
   which is the same control for a ladder, an indicator document, a banked
   option structure and a tailored play -- that is the whole point of there
   being one bank. This pane links to it rather than growing a second one: two
   dropdowns that both attach are two ways for the ticker and the bank to
   disagree.

   ---------------------------------------------------------------- the rules
   Three states are kept visibly apart, because collapsing any two of them is
   how a row comes to look live when nothing will ever send it:

     ENABLED   the assignment's own switch
     ARMED     the arm file permits OPENING this SYMBOL:play key
     TRADES    any engine sends this kind of thing at all

   A banked structure is attached, may be enabled, has no arm key, and does
   not trade: 231 structures are a library and `optengine` knows two plays.
   The pane says so on the row rather than leaving it implied.

   A number nobody measured is a dash carrying its reason. Never a zero: on a
   P/L pane those two look identical and only one of them is information.

   Data: GET /api/optlab/ticker/{sym} -- optticker.report(), passed through.
   Every figure is {value, n, unit, reason, thin}.
   ========================================================================= */
"use strict";
import {
  GET, POST, act, ask, el, esc, panel, tile, tileGrid, dataTable, emptyState,
  stateChip, chip, mv, measured, mreason, money, sgn, go, toast,
} from "./core.js";

/* One mount token, the same device views/options.js uses: a timer stamped
   with the mount that started it dies as soon as a newer mount exists. The
   router has no unmount hook, so without this, leaving the tab and coming
   back leaves two pollers running against one host. */
let MOUNT = 0;
let D = null;                    // the last payload
let ERR = "";
let SYM = "";

const POLL_MS = 12000;

/* ------------------------------------------------------------ formatting */
const DASH = `<span class="faint">—</span>`;
const has = (v) => v != null && Number.isFinite(Number(v));
const n0 = (v) => (has(v) ? Number(v).toLocaleString() : DASH);
const dol = (v, dp = 2) => (has(v) ? "$" + Number(v).toFixed(dp) : DASH);

/* A metric's value, or a dash that CARRIES ITS REASON on hover. The reason is
   the whole point: "no closed trade yet" and "these positions cannot be
   priced" are different answers and both render as an empty cell otherwise. */
function m$(metric, fmt) {
  if (measured(metric)) return (fmt || money)(mv(metric));
  return `<span class="faint" title="${esc(mreason(metric) || "not measured")
    }">—</span>`;
}
const mSub = (metric, noun) => {
  const n = metric && metric.n;
  if (!measured(metric)) return mreason(metric) || "not measured";
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
};
const pct1 = (v) => (has(v) ? (Number(v) * 100).toFixed(1) + "%" : DASH);
/* An ISO instant, shortened to what a person reads. Kept as the ORIGINAL
   string on hover, because the arm's expiry is an audited fact and the full
   stamp is the one in the file. */
const when = (iso) => {
  const s = String(iso || "");
  if (!s) return DASH;
  return `<span title="${esc(s)}">${esc(s.slice(0, 10))} ${
    esc(s.slice(11, 16))} UTC</span>`;
};

/* ------------------------------------------------------------------ style
   One <style>, written once, in theme tokens only so both themes follow for
   free. app.css belongs to another agent and views/options.js injects its own
   sheet the same way. */
const CSS = `
.tko-state { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap;
             padding: 12px 14px; border-radius: 10px; margin-bottom: 14px;
             border: 1px solid var(--hairline); border-left-width: 5px; }
.tko-state b { font-size: 15px; letter-spacing: .06em; white-space: nowrap; }
.tko-state span { color: var(--muted); font-size: 12.5px; flex: 1 1 260px;
                  min-width: 0; }
.tko-state.on    { border-left-color: var(--up);
                   background: color-mix(in srgb, var(--up) 10%, transparent); }
.tko-state.on b  { color: var(--up); }
.tko-state.off   { border-left-color: var(--faint); background: var(--surface); }
.tko-state.off b { color: var(--muted); }
.tko-state.froze { border-left-color: var(--down);
                   background: color-mix(in srgb, var(--down) 12%, transparent); }
.tko-state.froze b { color: var(--down); }

.tko-row { border: 1px solid var(--hairline); border-radius: 10px;
           padding: 11px 13px; margin-bottom: 9px; background: var(--surface); }
.tko-row.off { opacity: .62; }
.tko-h { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.tko-name { font-size: 14px; font-weight: 640; }
.tko-btns { display: flex; gap: 6px; margin-left: auto; flex-wrap: wrap; }
.tko-nums { display: grid; gap: 8px; margin-top: 10px;
            grid-template-columns: repeat(auto-fit, minmax(108px, 1fr)); }
.tko-n { display: flex; flex-direction: column; gap: 2px; min-width: 0;
         padding: 5px 7px; border-radius: 7px; background: var(--surface-2); }
.tko-n i { font-style: normal; font-size: 9.5px; letter-spacing: .05em;
           text-transform: uppercase; color: var(--faint); }
.tko-n b { font-size: 13px; font-weight: 620;
           font-variant-numeric: tabular-nums; }
.tko-why { margin-top: 8px; font-size: 11.5px; color: var(--muted);
           line-height: 1.5; }
.tko-arm { display: flex; gap: 8px; flex-wrap: wrap; align-items: flex-end;
           margin-bottom: 12px; }
.tko-arm label { display: flex; flex-direction: column; gap: 4px;
                 font-size: 11px; color: var(--faint); flex: 1 1 260px; }
.tko-arm input { background: var(--surface-2); color: var(--text);
                 border: 1px solid var(--hairline); border-radius: 8px;
                 padding: 6px 9px; font: inherit; font-size: 12.5px; }
.tko-arm .w { flex: 0 0 96px; }
.tko-scroll { overflow-x: auto; }
.tko-scroll table { min-width: 720px; }
.tko-legs { font-size: 11px; color: var(--muted); line-height: 1.5; }
.tko-note { font-size: 12px; line-height: 1.55; color: var(--muted);
            border: 1px solid var(--hairline2); border-radius: 8px;
            padding: 9px 11px; margin-bottom: 9px; }
.tko-note.bad { color: var(--down);
                border-color: color-mix(in srgb, var(--down) 45%, transparent); }
.tko-note.warn { color: var(--warn);
                 border-color: color-mix(in srgb, var(--warn) 45%, transparent); }
.tko-facts { display: grid; gap: 7px;
             grid-template-columns: repeat(auto-fill, minmax(96px, 1fr)); }
@media (max-width: 560px) {
  .tko-btns { margin-left: 0; width: 100%; }
  .tko-btns .btn { font-size: 11px; padding: 3px 9px; }
}
`;

function ensureStyle() {
  if (document.getElementById("tkoCss")) return;
  const s = document.createElement("style");
  s.id = "tkoCss";
  s.textContent = CSS;
  document.head.appendChild(s);
}

/* ------------------------------------------------------------- the blocks */
/* THE ARM STATE, and FROZEN drawn as outranking it. An armed playbook with
   state/FROZEN present opens nothing, so a green "armed" over a frozen
   account is a lie -- the frozen case wins the strip outright. */
function armStrip(d) {
  const st = d.state || {};
  if (st.frozen) {
    return `<div class="tko-state froze"><b>FROZEN</b>
      <span>state/FROZEN exists. Nothing opens on any strategy, whatever the
      arm file says. Only a human removes it.</span></div>`;
  }
  const mine = st.keys || [];
  if (mine.length) {
    return `<div class="tko-state on"><b>ARMED</b>
      <span>${esc(mine.join(", "))} may OPEN until
      ${when(st.expires)}. ${esc(st.reason || "")}
      Closing is never gated by this.</span></div>`;
  }
  return `<div class="tko-state off"><b>NOT ARMED</b>
    <span>Nothing on ${esc(d.symbol)} will open.
    ${esc(st.arm_why || "no key for this ticker is in the arm file")}.
    Open positions stay managed to their target, stop and the assignment
    guard.</span></div>`;
}

/* The reason and the expiry the arm buttons use. In the PANEL rather than in
   the confirm dialog on purpose: the reason goes in the audit log and is the
   only record six months from now of why this account was armed, so it is
   typed deliberately and visibly rather than into a box that vanishes. */
function armForm() {
  return `<div class="tko-arm">
    <label>Reason — this goes in the audit log
      <input id="tkoWhy" placeholder="why are you arming this?"
        autocomplete="off"></label>
    <label class="w">Days
      <input id="tkoDays" type="number" min="1" max="30" value="7"></label>
  </div>`;
}

const ORIGIN = { personal: ["accent", "you built or specified this one"],
                 standard: ["mute", "shipped with the bank"] };

function stratRow(s) {
  const o = ORIGIN[s.origin] || ["mute", ""];
  const gate = s.trades.ok
    ? "" : `<div class="tko-note warn">Attached, and <b>nothing sends it</b>:
        ${esc(s.trades.why)}</div>`;
  const armBtn = s.play
    ? (s.armed
        ? `<button class="btn sm" data-disarm="${esc(s.arm_key)}">Disarm</button>`
        : `<button class="btn sm primary" data-arm="${esc(s.arm_key)}">Arm</button>`)
    : "";
  const onBtn = s.play
    ? `<button class="btn sm" data-enable="${esc(s.play)}"
         data-on="${s.enabled ? "0" : "1"}">${s.enabled ? "Switch off" : "Switch on"}</button>`
    : "";
  return `<div class="tko-row${s.enabled ? "" : " off"}">
    <div class="tko-h">
      <span class="tko-name">${esc(s.name || s.id)}</span>
      ${chip(esc(s.origin || "?"), o[0], o[1])}
      ${stateChip(s.state || (s.enabled ? "live" : "idle"))}
      ${s.play ? chip(s.armed ? "armed" : "not armed",
                      s.armed ? "up" : "mute", esc(s.arm_why)) : ""}
      ${s.trades.ok ? "" : chip("does not trade", "warn", esc(s.trades.why))}
      <span class="tko-btns">${armBtn}${onBtn}</span>
    </div>
    ${gate}
    <div class="tko-nums">
      <div class="tko-n"><i>Open</i><b>${n0(s.open)}</b></div>
      <div class="tko-n"><i>Closed</i><b>${n0(s.closed)}</b></div>
      <div class="tko-n"><i>Realized</i><b>${m$(s.realized, sgn)}</b></div>
      <div class="tko-n"><i>Open P/L</i><b>${m$(s.open_pl, sgn)}</b></div>
      <div class="tko-n"><i>At risk</i><b>${m$(s.at_risk)}</b></div>
      <div class="tko-n"><i>Contracts</i><b>${n0(s.contracts)}</b></div>
    </div>
    ${s.summary ? `<div class="tko-why">${esc(s.summary)}</div>` : ""}
    ${s.why ? `<div class="tko-why">${esc(s.why)}</div>` : ""}
    <div class="tko-why">from ${esc(s.source || "?")}</div>
  </div>`;
}

function stratBlock(d) {
  const rows = d.strategies || [];
  const body = rows.length
    ? armForm() + rows.map(stratRow).join("")
    : emptyState({
        title: `No options strategy on ${esc(d.symbol)}`,
        body: `The one bank holds the two tailored plays and 231 standard
          structures. Attaching is on the Strategies tab, where a ladder, an
          indicator document and an option structure all go on through the
          same dropdown — and a ticker may carry several.`,
        action: `<button class="btn primary" id="tkoGoBank">Attach a strategy</button>`,
      });
  return panel(`Options strategy on ${esc(d.symbol)}`, body, {
    sub: "attached, switched on, armed and whether anything sends it are four "
       + "different facts",
    actions: `<button class="btn sm" id="tkoGoBank2">Attach / detach</button>`,
  });
}

/* ------------------------------------------------------------- the money */
function plBlock(d) {
  const p = d.pl || {};
  return panel("What this ticker's options have done", tileGrid([
    tile({ label: "Realized", metric: p.realized, signed: true,
           sub: mSub(p.realized, "closed trade") }),
    tile({ label: "Open P/L", metric: p.open_pl, signed: true,
           sub: mSub(p.open_pl, "priced position") }),
    tile({ label: "Realized + open", metric: p.total, signed: true,
           sub: mSub(p.total, "position") }),
    tile({ label: "At risk now", metric: p.at_risk,
           sub: mSub(p.at_risk, "open position") }),
    tile({ label: "Win rate", html: measured(p.win_rate)
             ? pct1(mv(p.win_rate)) : DASH,
           sub: mSub(p.win_rate, "closed trade") }),
    tile({ label: "Expectancy", metric: p.expectancy, signed: true,
           sub: "per closed trade" }),
  ], { cols: 6 }), {
    sub: "realized and open are never added up unless EVERY open position "
       + "here could be priced",
  });
}

/* ---------------------------------------------------------- the positions */
const COVER = {
  resting: ["up", "a GTC take-profit is resting at Alpaca"],
  resting_day: ["warn", "the resting exit is a DAY order -- it is gone at the "
              + "close and this process has to put it back"],
  guard_only: ["warn", "adopted: the assignment guard is the whole of its "
             + "cover, because no thresholds were ever set for it"],
  pending: ["mute", "the entry has not filled yet"],
  closing: ["mute", "a closing order is working"],
  loop: ["warn", "the broker refused the resting exit, so the loop owns the "
       + "target -- and only while the loop is running"],
  none: ["down", "OPEN WITH NO WAY OUT: no resting order and no recorded "
       + "refusal. Nothing closes this if the loop stops."],
  closed: ["mute", "closed"],
};

function legText(p) {
  return (p.legs || []).map((l) =>
    `${esc(String(l.side || "").toUpperCase())} ${esc(String(l.right || ""))}`
    + `${l.strike != null ? " " + esc(String(l.strike)) : ""}`).join(" / ");
}

function posRows(d) {
  return (d.positions || []).filter((p) => p.is_open).map((p) => {
    const c = COVER[p.exit_cover] || ["mute", p.exit_cover || ""];
    return [
      `<b>${esc(p.play || "?")}</b>${p.orphan
        ? `<br>${chip("orphan", "warn", "the strategy that opened this is no "
          + "longer attached; nothing will open another and this one is still "
          + "managed")}` : ""}`,
      `${n0(p.contracts)}${p.partial
        ? `<br>${chip("partial", "warn", "the broker filled fewer than were "
          + "requested; the exit must be sized for what filled")}` : ""}`,
      `<span class="tko-legs">${legText(p)}<br>${esc(p.expiry || "")}</span>`,
      has(p.dte) ? `${p.dte}d` : DASH,
      dol(p.entry_net),
      p.mark == null
        ? `<span class="faint" title="${esc(p.mark_error
            || "no mark has ever been taken -- neither the profit target nor "
             + "the stop can trip on a position with no price")}">—</span>`
        : dol(p.mark),
      p.open_pl == null
        ? `<span class="faint" title="${esc(p.open_pl_reason || "not priced")
          }">—</span>`
        : sgn(p.open_pl),
      chip(esc(p.exit_cover || "?"), c[0], c[1]),
      `<button class="btn sm danger" data-close="${esc(p.id)}">Close</button>`,
    ];
  });
}

function posBlock(d) {
  const rows = posRows(d);
  return panel("Open structures", `<div class="tko-scroll">${dataTable({
    cols: ["Play", { label: "Contracts", num: true }, "Legs",
           { label: "DTE", num: true }, { label: "Entry", num: true },
           { label: "Mark", num: true }, { label: "Open P/L", num: true },
           "Way out", ""],
    rows,
    empty: `Nothing is open on ${esc(d.symbol)}.`,
  })}</div>`, {
    sub: "contracts are what the BROKER confirmed, never what was requested; "
       + "a short leg goes off the book at 2 DTE by the calendar rule",
  });
}

/* --------------------------------------------------------- what to act on */
const SEV = { critical: "bad", warn: "warn", note: "" };

function noteList(rows, title, sub, empty) {
  if (!rows || !rows.length) return "";
  return panel(title, rows.map((a) =>
    `<div class="tko-note ${SEV[a.severity] || ""}">
       ${a.id ? `<b>${esc(a.id)}</b> — ` : ""}${esc(a.message)}</div>`).join("")
    || `<div class="tko-note">${esc(empty)}</div>`, { sub });
}

function whyBlock(d) {
  const w = d.why_not || {};
  if (w.why) {
    return panel("Why nothing opened", `<div class="tko-note">${esc(w.why)}</div>`,
                 { sub: "from the decision log" });
  }
  const body = (w.refusals || []).map((g) =>
    `<div class="tko-note">${n0(g.n)} × <b>${esc(g.class)}</b> —
       ${esc(g.example)}</div>`).join("");
  return panel("Why nothing opened", body
    || `<div class="tko-note">Every proposal in the last ${n0(w.window_h)}
        hours was accepted.</div>`, {
    sub: `${n0(w.proposals)} proposal(s) in ${n0(w.window_h)}h — ${n0(w.ok)}
          opened, ${n0(w.refused)} refused`,
  });
}

/* ------------------------------------------------------------- the facts
   The Data room, per ticker. Read out of the board cache: this page never
   starts a measurement, because a refresh spends the same 200/min trading
   budget the live share ladders do. */
const FACT_LABEL = {
  spot: "Spot", iv30: "IV 30d", iv_rank: "IV rank", rv20: "RV 20d",
  vrp: "VRP", term_slope: "Term", skew25: "Skew 25d", liquidity: "Liquidity",
  next_expiry: "Next expiry", earnings_days: "Earnings in",
  ex_div_days: "Ex-div in",
};

function factCell(name, f) {
  const lab = FACT_LABEL[name] || name;
  if (!f || f.value == null) {
    return `<div class="tko-n" style="background:transparent;border:1px dashed
      var(--hairline)" title="${esc((f && f.reason) || "nobody measured this")}">
      <i>${esc(lab)}</i><b class="faint">—</b></div>`;
  }
  const v = typeof f.value === "number" ? Number(f.value).toFixed(
    Math.abs(f.value) < 2 ? 3 : 2) : String(f.value);
  return `<div class="tko-n" title="${esc(f.source
    ? "supplied by " + f.source : "")}"><i>${esc(lab)}</i><b>${esc(v)}</b></div>`;
}

function factsBlock(d) {
  const b = d.board || {};
  if (!b.watched || !b.measured) {
    return panel(`What we know about ${esc(d.symbol)}`,
      `<div class="tko-note">${esc(b.why || "not measured")}</div>`,
      { sub: "the options watchlist, read and never measured from this page" });
  }
  const cells = (b.order || []).map((k) => factCell(k, (b.facts || {})[k]));
  return panel(`What we know about ${esc(d.symbol)}`,
    `<div class="tko-facts">${cells.join("")}</div>
     ${b.regime ? `<div class="tko-why"><b>${esc(b.regime)}</b> —
        ${esc(b.regime_reason || "")}</div>` : ""}
     ${b.shares_conflict ? `<div class="tko-note bad">${esc(b.conflict_reason
        || "the share ladder trades this name too")}</div>` : ""}`, {
      sub: `measured ${has(b.age_s) ? Math.round(b.age_s) + "s ago" : "at "
            + esc(String(b.as_of || "?"))}${b.stale ? " — stale" : ""}`,
    });
}

/* ------------------------------------------------------------ the writes */
function armReason() {
  const w = el("tkoWhy");
  return w ? String(w.value || "").trim() : "";
}
function armDays() {
  const d = el("tkoDays");
  const n = d ? parseInt(d.value, 10) : 7;
  return Number.isFinite(n) && n > 0 ? Math.min(n, 30) : 7;
}

async function doArm(key) {
  const why = armReason();
  if (!why) {
    toast("A reason is required — it goes in the audit log and it is the only "
          + "record of why.", "err");
    const w = el("tkoWhy");
    if (w) w.focus();
    return;
  }
  const days = armDays();
  if (!await ask({
    title: `Arm ${esc(key)}?`, ok: "Arm", requireWord: "ARM",
    body: `<b>${esc(key)} will open real positions</b> on this paper account
      for the next ${days} day${days === 1 ? "" : "s"}, whenever its own rules
      fire.<br><br>Reason, which goes in the audit log:
      <b>${esc(why)}</b><br><br>This is ADDITIVE: nothing already armed is
      switched off. <code>state/FROZEN</code> still outranks it.`,
  })) return;
  const r = await POST("/api/optlab/plays/arm",
                       { keys: [key], reason: why, days, by: "ticker-pane" });
  toast(r.warning || `${key} is armed.`, r.warning ? "err" : "ok");
  await load(SYM);
}

async function doDisarm(key) {
  if (!await ask({
    title: `Disarm ${esc(key)}?`, ok: "Disarm",
    body: `${esc(key)} stops OPENING. <b>Nothing is closed and nothing is
      cancelled</b> — open positions stay under management to their target,
      their stop and the assignment guard, which is the point: a disarm that
      also stopped the exits would make the stop button the thing that strands
      a short leg into expiry.<br><br>Every other armed key is left alone.`,
  })) return;
  const r = await POST("/api/optlab/plays/disarm", { keys: [key] });
  toast(r.note || `${key} disarmed.`, "ok");
  await load(SYM);
}

async function doEnable(play, on) {
  await POST("/api/optlab/plays/enable",
             { symbol: SYM, play, enabled: !!on });
  toast(`${play} on ${SYM} is switched ${on ? "on" : "off"}.`, "ok");
  await load(SYM);
}

async function doClose(id) {
  const p = (D && (D.positions || []).find((x) => x.id === id)) || {};
  if (!await ask({
    title: `Close ${esc(id)} now?`, danger: true, ok: "Send the closing order",
    requireWord: "CLOSE",
    body: `<b>This places a REAL closing order</b> on
      ${esc(String(p.contracts || "?"))} contract(s) of
      ${esc(p.play || "this structure")} — ${esc(legText(p))}, expiring
      ${esc(p.expiry || "?")}.<br><br>It is the only order this page can send,
      it can only close, and it is deliberately not gated by the arm. Any
      resting take-profit is cancelled first and the cancel is confirmed
      before the close goes out: two fills against one position is a naked
      leg.`,
  })) return;
  const r = await POST("/api/optlab/plays/close",
                       { id, reason: "closed by hand from the ticker pane" });
  toast((r.errors && r.errors.length)
    ? r.errors.join("; ") : `Closing order sent for ${id}.`,
    (r.errors && r.errors.length) ? "err" : "ok");
  await load(SYM);
}

/* -------------------------------------------------------------- the mount */
function hosts() {
  return `<div id="tkoRoot">
    <div id="tkoHead"></div>
    <div id="tkoAttn"></div>
    <div id="tkoPL"></div>
    <div id="tkoStrat"></div>
    <div id="tkoPos"></div>
    <div id="tkoWhy2"></div>
    <div id="tkoFacts"></div>
    <div id="tkoReads"></div>
  </div>`;
}

const put = (id, html) => {
  const n = el(id);
  if (!n) return false;
  n.innerHTML = html;
  return true;
};

function paint() {
  if (!el("tkoRoot")) return;
  if (ERR) {
    put("tkoHead", `<div class="tko-note bad"><b>${esc(ERR)}</b><br>
      The ledger, the arm file and the play store are all local files, so this
      is the dashboard and not the account.</div>`);
    return;
  }
  const d = D;
  if (!d) { put("tkoHead", `<div class="faint">Loading…</div>`); return; }
  put("tkoHead", armStrip(d) + (d.disagreements || []).map((x) =>
    `<div class="tko-note ${SEV[x.severity] || ""}">${esc(x.message)}</div>`
  ).join(""));
  put("tkoAttn", noteList(d.attention, "Needs acting on",
                          "every one of these is a state this account has "
                          + "actually been in", ""));
  put("tkoPL", plBlock(d));
  put("tkoStrat", stratBlock(d));
  put("tkoPos", posBlock(d));
  put("tkoWhy2", whyBlock(d));
  put("tkoFacts", factsBlock(d));
  put("tkoReads", panel("Where these numbers came from",
    (d.reads || []).map((r) =>
      `<div class="tko-why"><b>${esc(r.what)}</b> — ${esc(r.where)}</div>`
    ).join(""), { sub: "Alpaca first, then the ledgers, then config — and "
                     + "where two disagree it is said above, not resolved" }));
}

async function load(sym) {
  try {
    D = await GET("/api/optlab/ticker/" + encodeURIComponent(sym));
    ERR = "";
  } catch (e) {
    ERR = e.message || String(e);
  }
  paint();
}

/* Every timer is stamped with the mount that started it and stops as soon as
   a newer mount exists or its host leaves the document. */
function every(ms, fn) {
  const mine = MOUNT;
  const id = setInterval(() => {
    if (mine !== MOUNT || !el("tkoRoot")) { clearInterval(id); return; }
    fn();
  }, ms);
}

export function mountOptions(sym) {
  ensureStyle();
  MOUNT += 1;
  if (SYM !== sym) { D = null; ERR = ""; }
  SYM = sym;
  el("view").innerHTML = hosts();
  paint();

  /* ONE delegated listener on the wrapper innerHTML replaced, not on #view:
     delegating from #view stacks a new listener on the same element every
     time the tab is opened, and the third visit fires three confirmations for
     one click. views/ticker.js learned this the same way. */
  el("tkoRoot").addEventListener("click", (e) => {
    const b = e.target.closest("[data-arm],[data-disarm],[data-enable],"
                               + "[data-close]");
    if (b) {
      if (b.dataset.arm) return act(() => doArm(b.dataset.arm));
      if (b.dataset.disarm) return act(() => doDisarm(b.dataset.disarm));
      if (b.dataset.enable) {
        return act(() => doEnable(b.dataset.enable, b.dataset.on === "1"));
      }
      return act(() => doClose(b.dataset.close));
    }
    if (e.target.closest("#tkoGoBank, #tkoGoBank2")) {
      go({ kind: "ticker", sym, tab: "strategies" });
    }
  });

  load(sym);
  /* Twelve seconds. The ledger and the arm file are local files and this route
     spends no trading call of its own, but the marks only change when the
     WORKER cycles -- every 20 s -- so polling faster would redraw the same
     numbers. */
  every(POLL_MS, () => load(sym));
}

/* Whether the tab is worth offering at all. hub's ticker payload already says
   whether this account holds options on the symbol; a ticker with neither an
   options strategy nor an option position does not need the tab, and offering
   it everywhere would put an empty room on every ladder page. */
export function hasOptions(d) {
  if (!d) return false;
  if (d.options && Number(d.options.contracts)) return true;
  return (d.strategies || []).some((s) => s.kind === "options");
}
