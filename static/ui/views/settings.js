/* ============================================================================
   Settings -- this account: its keys, its account-wide numbers, and the
   processes that act on it.

   Three tabs, three jobs:
     Account   which Alpaca account this is, its label and keys, the theme,
               and restarting the server
     Engine    the numbers that apply to every ladder in it -- grouped by what
               they touch, searchable, and each one saying what it does, what
               it affects, and what it can COST
     Agents    the scheduled work and the audit log

   The pass of 28 Sep 2026 added the third of those three. Poll seconds and the
   account daily loss limit used to sit in one undifferentiated column at the
   same visual weight, and one of them halts every ladder on the account. Every
   row now carries its IMPACT tier from fields.js -- the same table the
   per-ticker settings read, so a key cannot be a money field on one page and a
   plain number on another -- and the Save names what it is about to loosen.

   Three cards are gone rather than moved: the fleet controls (Portfolio's,
   byte for byte), the balances stats (Portfolio's and the topbar strip's),
   and the ticker list (the rail's, on every page). The Risk page's read-only
   mirror of the guardrails is gone too -- the "used" figures it existed to
   show are on the guardrail fields themselves now, where they can be acted on.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, POST, DEL, act, ask, toast, el, esc, panel,
  money, go, toggleTheme, curAccount, acctLabel, acctNumber,
  loadAccounts, setAccount, pickAccount, segmented, wireSegmented,
} from "../core.js";
import { mountAgents, paintAgents } from "./agents.js";
/* the one bar this page draws, from the library that draws every other bar */
import { ratiobar } from "../viz.js";
/* One classification of what a setting can cost, one search, one badge --
   shared with the per-ticker settings tab rather than re-invented here. */
import {
  ensureFieldStyles, impactOf, impactBadge, applySearch, IMPACT,
} from "../fields.js";

let renaming = false;   // the label is being edited; the poll must not repaint it
let acctMsg = "";       // the last Test-keys answer, survives the poll repaint

const TABS = [["account", "Account"], ["engine", "Engine"], ["agents", "Agents"]];
const SUB = {
  account: () => "which Alpaca account this is, and the server that runs it",
  engine: () => "applies to every ladder in this account",
  agents: () => "scheduled work, and every action taken",
};

VIEWS.settings = {
  title: () => "Settings",
  sub: (ov, v) => (SUB[v.tab || "account"] || SUB.account)(),
  tabs: TABS,

  mount(v) {
    const tab = v.tab || "account";
    if (tab === "agents") return mountAgents();
    if (tab === "engine") return mountEngine();
    mountAccount();
  },

  paint(v) {
    const tab = v.tab || "account";
    if (tab === "agents") return paintAgents();
    if (tab === "engine") return paintEngine();
    paintAccount();
  },
};

/* =============================================================== account */
/* TWO PANELS, not five cards. What this account IS, and the process that runs
   it. Appearance was a card containing one button; "Starting and stopping" was
   a card containing one paragraph pointing somewhere else. Both are lines now.

   The three actions keep their own impact tier, because "Remove account" reads
   like a housekeeping button and is the loudest thing on the page: it stops
   the ladder managing positions that stay open at Alpaca. */
function mountAccount() {
  ensureFieldStyles();          // the impact badges below come from fields.js
  ensureSettingsStyles();
  renaming = false;
  acctMsg = "";
  el("view").innerHTML = `
    ${panel("This account", `
      <div class="acct-top">
        <div id="acctName" class="acct-id">
          <div class="acct-label" id="acctLabelTxt">—</div>
          <div class="faint" id="acctMeta"></div>
        </div>
        <div class="row-btns">
          <button class="btn sm" id="acctRename" title="Changes the name on
            the rail, the title and every confirmation. Nothing at Alpaca is
            touched.">Rename <span class="imp imp-safe">display</span></button>
          <button class="btn sm" id="acctTest" title="One read of the account.
            It places nothing and changes nothing.">Test keys <span
            class="imp imp-safe">display</span></button>
          <button class="btn sm danger" id="acctRemove" title="Deletes the keys
            and the fleet FROM THIS SERVER. Positions and resting orders at
            Alpaca are left exactly as they are — which is why it is the loud
            one: the ladder stops managing them and they stay open.">Remove
            account <span class="imp imp-money">trades money</span></button>
        </div>
      </div>
      <div class="tip" id="acctMsg"></div>`,
      { titleHint: "one Alpaca key pair · one fleet" })}

    ${panel("The server", `
      <div class="srv">
        <div class="srv-a">
          <button class="btn primary" id="sRestart" title="Relaunches the
            server so new code and settings take effect. No order is placed or
            cancelled and nothing is armed that was not armed before — but this
            is ONE process for every account, so all of their fleets stop for
            the duration. Take-profits resting at Alpaca are the broker's
            orders and stay live throughout.">Restart dashboard <span
            class="imp imp-guard">limit</span></button>
          <button class="btn" id="bTheme">Toggle light / dark</button>
        </div>
        <div class="srv-b">
          <div class="tip" id="sRestartNote" hidden></div>
          <div class="tip" id="sHealth"></div>
        </div>
      </div>`, { titleHint: "one process for every account. Start all, Stop "
        + "all, Disarm all and Panic are on Portfolio, beside the ladders they "
        + "act on — one copy of them, not two." })}`;

  el("bTheme").onclick = toggleTheme;
  el("sRestart").onclick = doRestart;
  el("acctRename").onclick = startRename;
  el("acctTest").onclick = testKeys;
  el("acctRemove").onclick = removeAccount;
  paintAccount();
}

/* ================================================================== groups
   Every account-wide setting, grouped by WHAT IT TOUCHES and carrying three
   things each: what it does, what it affects, and what it can cost.

   The third is the one that was missing. Poll seconds and the account daily
   loss limit used to sit in one undifferentiated column at the same weight,
   and one of them halts every ladder on the account. Each row now renders its
   IMPACT tier from fields.js -- the same classification the per-ticker
   settings use, from the same one table, so a key cannot be a money field on
   the ticker page and a plain number here.

   `used` is how a guardrail measures itself against what the account is doing
   right now, so the form says "using 12% of this" on the field that sets it
   rather than on a read-only table somewhere else. (p = ov.portfolio,
   ov = the overview, set = the value currently in the box.)
   ========================================================================= */
const SET_GROUPS = [
  {
    id: "data", title: "Market data",
    lead: "How this account sees prices. Nothing here sends an order — but "
        + "every engine decides on what these two settings fetch.",
    fields: [
      { k: "feed", label: "Data feed", t: "sel",
        opts: [["auto", "auto — Blue Ocean overnight, SIP otherwise"],
               ["sip", "sip — the consolidated tape"],
               ["iex", "iex — one venue only, thinner"],
               ["boats", "boats — Blue Ocean, the overnight venue"]],
        hint: "The SIP tape is <b>dark 20:00–04:00 ET</b>. On <b>auto</b> the fleet "
            + "switches to Blue Ocean overnight by itself and back at 04:00.",
        affects: "Every quote, bar and indicator every ladder in this account reads." },
      { k: "poll_seconds", label: "Poll seconds", t: "num", step: 0.5, min: 1,
        hint: "How often the fleet reads Alpaca <b>once for all tickers</b>. "
            + "Positions, orders, quotes and bars are batched, so adding tickers "
            + "costs almost nothing here.",
        affects: "The 200 requests/minute this account gets, which the ladders "
               + "also place orders through. Below 1s it starts competing with "
               + "them; far above it every engine decides on stale prices." },
    ],
  },
  {
    id: "guards", title: "Portfolio guardrails",
    lead: "Limits measured across the whole account — the checks no single "
        + "engine can make for itself. 0 turns one off entirely.",
    fields: [
      { k: "max_total_exposure", label: "Max total exposure ($)", t: "num",
        step: 1000, min: 0, unit: "$",
        hint: "Cost basis across <b>every</b> ladder. A ladder that would push "
            + "past this stops adding — it is not halted and nothing is sold.",
        affects: "Whether a new rung may open. Existing lots and their resting "
               + "take-profits are untouched.",
        used: (p) => p.deployed || 0 },
      { k: "reserve_cash", label: "Cash reserve ($)", t: "num", step: 1000, min: 0,
        unit: "$",
        hint: "Buying power the fleet will never spend, whatever any ladder wants.",
        affects: "Every entry order in this account. The bar fills as buying "
               + "power falls toward the reserve.",
        used: (p, ov, set) => Math.max(0, set - (p.buying_power || 0)) },
      { k: "account_daily_loss_limit", label: "Account daily loss limit ($)",
        t: "num", step: 100, min: 0, unit: "$",
        hint: "Measured on the <b>account</b>, not one ladder. Hitting it halts "
            + "every ladder at once.",
        affects: "Every engine in this account, simultaneously. Raising it is "
               + "the single loosest change on this page.",
        used: (p) => Math.max(0, -(p.today_pl != null ? p.today_pl : p.made_today || 0)) },
      { k: "max_running_tickers", label: "Max running tickers", t: "num",
        step: 1, min: 0, unit: "n",
        hint: "Engines allowed to be running at once in this account.",
        affects: "How many ladders may be started. It does not stop one that "
               + "is already running.",
        used: (p, ov) => (ov.tickers || []).filter((t) => t.running).length },
    ],
  },
  {
    id: "dash", title: "This dashboard",
    lead: "What the browser does. No order path reads anything here.",
    fields: [
      { k: "ui_refresh_ms", label: "Dashboard refresh (ms)", t: "num",
        step: 500, min: 500,
        hint: "How often this page asks the server for the overview.",
        affects: "This browser tab only. The fleet's own decisions are on "
               + "<b>Poll seconds</b> above and are not affected by it." },
    ],
  },
];

const SET_ALL = SET_GROUPS.flatMap((g) => g.fields);
const setByKey = (k) => SET_ALL.find((f) => f.k === k);

let setQ = "";          // the settings search box
/* The impact filter: "" | money | guard | safe. It is a SECOND axis over the
   same rows, not a second list -- "show me only the things that can cost me
   money" is the question this page exists to answer quickly. */
let setImp = "";

/* What the SERVER last put in the boxes, captured the moment paintEngine fills
   them. Every "has this changed" question is asked against this and never
   against `ov.global` directly, because a setting the server does not send at
   all comes back `undefined` there -- and `String(undefined) !== "auto"`, so
   the feed select counted as a pending edit on a form nobody had touched. A
   baseline taken from the rendered form cannot have that bug: it is by
   definition what is on screen before anyone types. */
let setBase = null;

/* One row. `.fld` + `data-k` is deliberate: that is the selector fields.js's
   applySearch() walks, so this page's search and the per-ticker settings tab's
   search are the same function over the same markup. */
function setRowHTML(f) {
  const ctl = f.t === "sel"
    ? `<select name="${f.k}">${f.opts.map(([v, l]) =>
        `<option value="${esc(v)}">${esc(l)}</option>`).join("")}</select>`
    : `<input name="${f.k}" type="number" step="${f.step}" min="${f.min}">`;
  /* TWO COLUMNS: what it is on the left, what it does to your money on the
     right. The old row stacked six blocks of prose under every control and
     that stack IS the "endless widgets" complaint -- at three groups and
     seven settings the page was four screens long. */
  /* The two sentences, as ONE tooltip on ONE mark. `plain` strips the <b>
     tags they were written with, because a title attribute renders them as
     literal angle brackets. */
  const plain = (h) => String(h || "").replace(/<[^>]*>/g, "");
  const tip = plain(f.hint) + " Affects: " + plain(f.affects);
  return `<div class="fld set-row" data-k="${f.k}" data-impact="${impactOf(f.k)}"
    data-tip="${esc(tip)}">
    <div class="fld-h"><span class="fld-l">${esc(f.label)}</span>${impactBadge(f.k)}
      <span class="fld-key mono">${esc(f.k)}</span>
      <button type="button" class="set-q-m" tabindex="-1"
        aria-label="what ${esc(f.label)} does" title="${esc(tip)}">i</button></div>
    ${ctl}
    ${f.used ? `<div class="use" data-use="${f.k}"></div>` : ""}
  </div>`;
}

/* The impact filter offers only the tiers that are ACTUALLY on this page, with
   their counts. A "Trades money" button that can never match anything is not a
   filter, it is a dead control -- and the fact behind it (nothing account-wide
   changes what is bought directly) is worth SAYING rather than leaving to be
   discovered by clicking. */
const TIER_WORD = { money: "Trades money", guard: "Limits", safe: "Display",
                    unknown: "Unclassified" };

function impactCounts() {
  const out = {};
  for (const f of SET_ALL) {
    const t = impactOf(f.k);
    out[t] = (out[t] || 0) + 1;
  }
  return out;
}

function impactOptions() {
  const c = impactCounts();
  const opts = [["", `All ${SET_ALL.length}`]];
  for (const t of ["money", "guard", "safe", "unknown"]) {
    if (!c[t]) continue;
    opts.push([t, `${TIER_WORD[t]} ${c[t]}`, IMPACT[t].blurb]);
  }
  return opts;
}

/* ROUND 6: THE LEGEND IS GONE. It restated, as four lines of type under the
   toolbar, what each badge's own tooltip already says and what the impact
   filter's buttons already say -- and it did it on a page with seven
   settings. What it said is not lost: `impactOptions()` below carries each
   tier's blurb as the filter button's title, and the "nothing here places an
   order" fact, which IS worth stating, is the filter control's own label. */
function scopeHint() {
  const c = impactCounts();
  return c.money
    ? "Every setting on this page is measured across the whole account."
    : "Nothing on this page places or prices an order: every setting here is "
      + "a limit or a display preference. The settings that change what is "
      + "bought are a ticker's own, on that ticker's Settings tab.";
}

/* ================================================================ engine */
/* ONE ROOM, not a wall of panes.

   What was here: three cards stacked in a column, two prose cards in a rail
   beside them, and a search box in a sixth card above the lot -- six framed
   boxes for seven settings. The owner's words were "endless windows and
   widgets". So: one toolbar, one sticky rail of group links, one column of
   two-column rows, one sticky Save. The prose that was in the rail is one
   line at the bottom, because it is a footnote and it was being rendered at
   the same weight as the account's daily loss limit.

   NOTHING ABOUT THE FORM ITSELF CHANGED. Same `#gform`, same `name=` per key,
   same `.fld[data-k]` markup the search walks, same baseline capture, same
   save. This is layout; the ladder's real settings flow through this form and
   they are live money. */
function mountEngine() {
  setBase = null;
  ensureFieldStyles();
  ensureSettingsStyles();
  el("view").innerHTML = `
    <div class="set-tools" title="${esc(scopeHint())}">
      <input id="setQ" class="set-q" placeholder="Search settings — try “loss”, “feed”, “limit”"
             value="${esc(setQ)}" spellcheck="false" autocomplete="off">
      ${segmented({ id: "setImp", value: setImp, size: "sm", label: "impact",
        options: impactOptions() })}
      <span class="faint" id="setQn"></span>
    </div>
    <form id="gform">
      <div id="setCol"></div>
      <div class="set-save">
        <button type="submit" class="btn primary" id="gsave">Save settings</button>
        <div class="tip" id="gmsg" style="margin:0"></div>
      </div>
    </form>`;

  /* ROUND 6: THE STICKY GROUP NAV IS GONE. It was a jump list for three
     groups holding seven settings between them; with the prose out of the
     rows the whole form is one screen, and a table of contents for one screen
     is a control that costs a column and saves nothing.
     THE GROUP LEAD PARAGRAPHS ARE GONE TOO. "Market data", "Portfolio
     guardrails" and "This dashboard" are what they said. The one fact in them
     that is not in a title -- that 0 turns a guardrail off -- is drawn on the
     field itself, in red, the moment it is 0. */
  el("setCol").innerHTML = SET_GROUPS.map((g) => `
    <section class="set-g" id="setg-${g.id}">
      <header class="set-g-h">
        <h3 class="set-g-t" title="${esc(g.lead.replace(/<[^>]*>/g, ""))}">${
          esc(g.title)}</h3>
        <span class="faint set-count" data-setcount="${g.id}"></span>
      </header>
      <div class="set-fields" data-setgroup="${g.id}">
        ${g.fields.map(setRowHTML).join("")}</div>
    </section>`).join("");

  const f = el("gform");
  f.addEventListener("input", () => { S.touched = true; paintEngine(); });
  f.addEventListener("submit", onSaveEngine);

  const q = el("setQ");
  q.oninput = () => { setQ = q.value; runSetSearch(); };
  wireSegmented("setImp", (v) => { setImp = v; runSetSearch(); });
  runSetSearch();
  paintEngine();
}

/* Search hides rows by CLASS and never by `disabled`, so a filtered form still
   saves every setting it holds. Filtering a form and then silently sending
   only the visible half is the kind of bug nobody finds until a guardrail
   comes back as 0. */
function runSetSearch() {
  const f = el("gform");
  if (!f) return;
  applySearch(f, setQ);
  /* The impact filter hides by its OWN class, never by `disabled` and never by
     touching `q-out`, so it composes with the text search instead of fighting
     it -- and, like the search, a hidden row is still in the form and still
     saved. Filtering a form and then silently sending only the visible half is
     the kind of bug nobody finds until a guardrail comes back as 0. */
  let shownAll = 0, hiddenAll = 0;
  for (const row of f.querySelectorAll(".fld[data-k]")) {
    const off = !!setImp && row.dataset.impact !== setImp;
    row.classList.toggle("i-out", off);
    if (off || row.classList.contains("q-out")) hiddenAll += 1; else shownAll += 1;
  }
  const r = { shown: shownAll, hidden: hiddenAll };
  const live = (x) => !x.classList.contains("q-out")
                   && !x.classList.contains("i-out");
  for (const host of f.querySelectorAll("[data-setgroup]")) {
    const rows = [...host.querySelectorAll(".fld")];
    const shown = rows.filter(live).length;
    const tag = f.querySelector(`[data-setcount="${host.dataset.setgroup}"]`);
    if (tag) {
      tag.textContent = (setQ || setImp)
        ? `${shown}/${rows.length}`
        : `${rows.length}`;
    }
    const sec = host.closest(".set-g");
    if (sec) sec.hidden = (setQ || setImp) ? shown === 0 : false;
  }
  const n = el("setQn");
  if (n) {
    const what = [setQ ? `“${esc(setQ)}”` : "",
                  setImp ? `<b>${esc(IMPACT[setImp].badge)}</b>` : ""]
      .filter(Boolean).join(" + ");
    n.innerHTML = (setQ || setImp)
      ? (r.shown
          ? `${r.shown} of ${r.shown + r.hidden} settings match ${what}`
          : `<span class="warn">Nothing matches ${what}.</span>`)
      : "";
  }
}

/* The save names what it is about to loosen. Every field on this page is a
   limit or a feed: there is no "are you sure" theatre for the refresh
   interval, and there is one for the number that halts every ladder. */
async function onSaveEngine(e) {
  e.preventDefault();
  const f = el("gform");
  const base = setBase || {};
  const patch = {};
  const changed = [];
  for (const [k, v] of new FormData(f).entries()) {
    if (String(v).trim() === "") continue;
    patch[k] = v;
    if (String(base[k] === undefined ? "" : base[k]) !== String(v)) changed.push(k);
  }
  if (!changed.length) { toast("Nothing changed.", ""); return; }

  /* A guardrail is LOOSENED when its number goes up, or when it goes to 0 --
     and 0 is the dangerous one, because it reads like "none" and means
     "unlimited". Both are called out by name. */
  const loosened = changed.filter((k) => {
    const fd = setByKey(k);
    if (!fd || !fd.used) return false;
    const was = Number(base[k]), now = Number(patch[k]);
    if (!Number.isFinite(was) || !Number.isFinite(now)) return false;
    return now === 0 ? was !== 0 : now > was;
  });

  const line = (k) => {
    const fd = setByKey(k) || { label: k };
    const was = base[k] === undefined || base[k] === "" ? "—" : String(base[k]);
    const now = String(patch[k]);
    return `<div><b>${esc(fd.label)}</b>
      <span class="faint">${esc(was)}</span> → <b>${esc(now)}</b>
      ${Number(now) === 0 && fd.used ? `<span class="down">— off, nothing caps
        this any more</span>` : ""}</div>`;
  };

  if (!await ask({
    title: `Save ${changed.length} setting${changed.length === 1 ? "" : "s"}?`,
    ok: "Save", danger: loosened.length > 0,
    body: `<div class="set-diff">${changed.map(line).join("")}</div>
      ${loosened.length
        ? `<br><b class="down">${loosened.length} guardrail${loosened.length === 1
            ? " is" : "s are"} being loosened</b> on
           <b>${esc(acctLabel())}</b> (${esc(acctNumber() || "—")}).
           Nothing is sold and nothing is armed by this — but the checks that
           stop a ladder adding get further away.`
        : `<br>None of these sends an order or arms anything.`}`,
  })) return;

  await act(async () => {
    await POST("/api/settings", patch);
    S.touched = false;
    toast("Settings saved.", "ok");
    el("gmsg").innerHTML = `<span class="up">Saved.</span>`;
    setTimeout(() => { const m = el("gmsg"); if (m) m.textContent = ""; }, 3500);
  });
}

function paintEngine() {
  const ov = S.ov;
  const f = el("gform");
  if (!ov || !f) return;
  if (!S.touched) {
    for (const [k, v] of Object.entries(ov.global || {})) {
      const e = f.elements[k];
      if (e && e.type !== "submit") e.value = v;
    }
    /* the baseline is taken AFTER the fill and only while the form is
       untouched, so a poll that lands mid-edit cannot move the goalposts */
    setBase = {};
    for (const [k, v] of new FormData(f).entries()) setBase[k] = v;
  }
  /* The Save button says how many settings moved, so pressing it is never a
     guess about whether anything is pending. */
  const base = setBase || {};
  let n = 0;
  for (const [k, v] of new FormData(f).entries()) {
    if (String(v).trim() === "") continue;
    if (String(base[k] === undefined ? "" : base[k]) !== String(v)) n += 1;
  }
  const b = el("gsave");
  if (b) {
    b.textContent = n ? `Save ${n} change${n === 1 ? "" : "s"}` : "Save settings";
    b.disabled = !n;
  }

  const p = ov.portfolio || {};
  for (const g of SET_ALL) {
    if (!g.used) continue;
    const host = f.querySelector(`[data-use="${g.k}"]`);
    if (!host) continue;
    const set = Number((f.elements[g.k] || {}).value) || 0;
    const raw = g.used(p, ov, set);
    const used = Number.isFinite(Number(raw)) ? Math.max(0, Number(raw)) : null;
    host.innerHTML = ratiobar({
      value: used, cap: set, unit: g.unit === "$" ? "usd" : "count", dp: 0,
      label: g.label,
      why: set ? "" : "this limit is set to 0, which turns it off — nothing "
                    + "caps it",
    });
  }
}

/* Injected here rather than added to app.css, which is the shell agent's file
   while several agents are in this tree. Tokens only, so both themes follow
   theme.css. Hoist at merge. */
function ensureSettingsStyles() {
  if (document.getElementById("setCSS")) return;
  const s = document.createElement("style");
  s.id = "setCSS";
  s.textContent = [
    /* ROUND 6. Two things changed in this block and both are the same fix.

       FIRST, EVERY LITERAL PIXEL SIZE IS GONE. This sheet measured nine type
       sizes -- 11, 11.5, 12, 12.5, 13, 19px and a 9.5px badge in riskmath --
       on a page with seven settings. A scale with nine steps that a file may
       add a tenth to is not a scale; theme.css has six (--fs-micro .. --fs-xl)
       and every rule below now names one of them.

       SECOND, THE RULES FOR THE BLOCKS THAT NO LONGER EXIST ARE DELETED
       rather than left behind: .set-legend, .set-nav, .set-nav-b, .set-room,
       .set-row-a/-b, .set-foot and .set-acts. A stylesheet that still styles
       a block nobody renders is how the next agent puts the block back. */
    /* the toolbar: a search, an impact filter, and a count */
    ".set-tools{display:flex;gap:12px;align-items:center;flex-wrap:wrap;",
    "background:var(--surface);border:1px solid var(--hairline);",
    "border-radius:var(--radius);padding:12px 14px;margin-bottom:var(--s5)}",
    ".set-q{flex:1;min-width:200px;font-size:var(--fs-md);padding:9px 12px}",
    /* one group */
    ".set-g{background:var(--surface);border:1px solid var(--hairline);",
    "border-radius:var(--radius);padding:14px 18px;margin-bottom:var(--s4);",
    "scroll-margin-top:var(--s4)}",
    ".set-g[hidden]{display:none}",
    ".set-g-h{display:flex;align-items:baseline;gap:10px}",
    ".set-g-t{margin:0;font-size:var(--fs-md);font-weight:var(--w-semi)}",
    ".set-count{font-size:var(--fs-micro);margin-left:auto}",
    /* THE ROWS GO SIDE BY SIDE. With the prose out of them a row is a label,
       a control and a bar -- roughly 240px of content -- so stacking them one
       per line was three screens of white space. */
    ".set-fields{display:grid;gap:var(--s4) var(--s5);margin-top:12px;",
    "grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}",
    ".set-row{min-width:0}",
    ".set-row.i-out{display:none !important}",
    ".set-row input,.set-row select{width:100%}",
    ".set-row .use{margin-top:7px}",
    /* the one mark that replaced two paragraphs per row */
    ".set-q-m{margin-left:auto;flex:none;width:15px;height:15px;padding:0;",
    "border-radius:50%;border:1px solid var(--hairline2);background:none;",
    "color:var(--faint);font:inherit;font-size:var(--fs-micro);",
    "font-style:italic;line-height:1;cursor:help}",
    ".set-q-m:hover{color:var(--text);border-color:var(--muted)}",
    /* the save bar. SOLID, not a gradient: a translucent sticky bar over a
       form reads as a button floating on top of a field -- measured at 400px,
       where the Save sat across the Data feed row and the gradient's
       transparent half let the control show through it. */
    ".set-save{position:sticky;bottom:0;z-index:5;display:flex;gap:14px;",
    "align-items:center;flex-wrap:wrap;padding:14px 0 16px;margin-top:6px;",
    "background:var(--bg);border-top:1px solid var(--hairline)}",
    ".set-save .btn{min-width:190px}",
    ".set-diff{display:flex;flex-direction:column;gap:7px;",
    "font-size:var(--fs-sm)}",
    /* the account panel */
    ".acct-top{display:flex;align-items:center;gap:14px;flex-wrap:wrap}",
    ".acct-id{flex:1;min-width:240px}",
    ".acct-label{font-size:var(--fs-xl);font-weight:var(--w-semi);",
    "letter-spacing:-.01em}",
    ".acct-id .faint{font-size:var(--fs-sm);margin-top:2px}",
    ".row-btns .btn .imp,.srv-a .btn .imp{margin-left:6px}",
    /* An impact badge on a FILLED button has to borrow the button's ink: the
       badge's own --warn on a saturated purple was measured at a contrast the
       eye reads as decoration rather than as a word. */
    ".btn.primary .imp{color:inherit;border-color:currentColor;",
    "background:transparent;opacity:.8}",
    ".srv{display:grid;gap:var(--s4);grid-template-columns:minmax(0,220px) ",
    "minmax(0,1fr);align-items:start}",
    ".srv-a{display:flex;flex-direction:column;gap:8px}",
    ".srv-a .btn{width:100%}",
    ".srv-b .tip{margin-top:0}",
    ".srv-b .tip[hidden]{display:none}",
    "@media (max-width:900px){.srv{grid-template-columns:1fr}}",
    "@media (max-width:620px){",
    ".set-save{gap:8px}.set-save .btn{min-width:0;width:100%}}",
  ].join("");
  document.head.appendChild(s);
}

/* ------------------------------------------------------------- account */
function paintAccount() {
  if (!el("acctLabelTxt")) return;
  const a = curAccount() || {};
  const ov = S.ov;
  if (!renaming) {
    el("acctLabelTxt").textContent = a.label || a.id || S.account || "—";
    const paper = a.paper !== undefined ? a.paper : (ov ? ov.paper : true);
    const feed = a.feed || (ov && ov.feed) || "";
    el("acctMeta").innerHTML = [
      esc(a.account_number || acctNumber() || "—"),
      paper ? "Paper" : `<span class="down">LIVE</span>`,
      feed ? `${esc(feed)} feed` : "",
      a.key_last4 ? `key ending <span class="mono">${esc(a.key_last4)}</span>` : "",
      a.is_default ? "default account (from .env)" : "",
      a.connected === false ? `<span class="down">keys not answering</span>` : "",
    ].filter(Boolean).join(" · ");
  }

  const rm = el("acctRemove");
  if (rm) {
    const why = a.is_default
      ? "The default account is the one seeded from the server's .env; it "
        + "cannot be removed here, though its label can be changed."
      : "Deletes the keys and the fleet FROM THIS SERVER. Nothing at Alpaca "
        + "is cancelled or sold, and it is refused while anything in this "
        + "account is running, armed or still holds lots.";
    rm.disabled = !!why;
    rm.title = why;
  }
  /* ROUND 6: this used to print a standing paragraph about removal on every
     load. The two sentences it carried are the Remove button's own tooltip
     (and, for the default account, its disabled reason), so the slot now
     holds only what is NEWS: the answer to a Test keys click. */
  const m = el("acctMsg");
  if (m && !renaming) m.innerHTML = acctMsg || "";

  /* the one health line that belongs to the server rather than to a ladder */
  const h = el("sHealth");
  if (h && ov) {
    h.innerHTML = `Market data ${ov.snap_error
      ? `<span class="down">failing: ${esc(ov.snap_error)}</span>`
      : `<span class="up">healthy</span>, ${ov.snap_age}s old`} · session
      <b>${esc(ov.session)}</b> on the <b>${esc(ov.feed)}</b> feed.`;
  }
  /* THE ONE THING THAT IS STILL PRINTED HERE: a restart that cannot work.
     That is a warning, not a definition, so it is shown rather than hovered
     -- and the slot is hidden whenever it has nothing to report. */
  if (ov && ov.supervised === false && el("sRestartNote")) {
    el("sRestartNote").hidden = false;
    el("sRestartNote").innerHTML = `<span class="warn">Restart is
      unavailable</span> — this server was not launched by
      <code>start_bot.bat</code>, so nothing would bring it back up.`;
    el("sRestart").disabled = true;
  }
}

function startRename() {
  if (renaming) return;
  const a = curAccount() || {};
  renaming = true;
  el("acctName").innerHTML = `
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input id="acctNewLabel" value="${esc(a.label || a.id || "")}" maxlength="60"
             style="width:280px;max-width:100%" spellcheck="false" autocomplete="off">
      <button class="btn sm primary" id="acctSaveLabel">Save</button>
      <button class="btn sm" id="acctCancelLabel">Cancel</button>
    </div>`;
  const i = el("acctNewLabel");
  i.focus(); i.select();
  i.onkeydown = (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveRename(); }
    if (e.key === "Escape") endRename();
  };
  el("acctSaveLabel").onclick = saveRename;
  el("acctCancelLabel").onclick = endRename;
}

function endRename() {
  renaming = false;
  const host = el("acctName");
  if (!host) return;
  host.innerHTML = `
    <div class="acct-label" id="acctLabelTxt">—</div>
    <div class="faint" id="acctMeta"></div>`;
  paintAccount();
}

async function saveRename() {
  const i = el("acctNewLabel");
  const label = (i ? i.value : "").trim();
  if (!label) { toast("A label is required.", "err"); return; }
  const b = el("acctSaveLabel");
  if (b) b.disabled = true;
  try {
    const r = await POST(`/api/accounts/${encodeURIComponent(S.account)}/rename`, { label });
    const upd = r.account || { label };
    S.accounts = S.accounts.map((a) => a.id === S.account ? { ...a, ...upd } : a);
    toast(`Renamed to <b>${esc(upd.label || label)}</b>.`, "ok");
    endRename();
    window.__render(false);              // the rail, brand line and title carry the label
  } catch (e) {
    toast(esc(e.message), "err", 9000);
    if (b) b.disabled = false;
  }
}

async function testKeys() {
  const b = el("acctTest");
  b.disabled = true;
  b.innerHTML = `<span class="spin"></span> Testing…`;
  acctMsg = `<span class="faint">asking Alpaca…</span>`;
  el("acctMsg").innerHTML = acctMsg;
  try {
    const r = await POST(`/api/accounts/${encodeURIComponent(S.account)}/test`, {});
    acctMsg = `<span class="up">Keys work</span> — account <b>${esc(r.account_number || "—")}</b>, `
      + `equity <b>${money(r.equity)}</b>${r.feed ? `, <b>${esc(r.feed)}</b> feed` : ""}.`;
  } catch (e) {
    acctMsg = `<span class="down">Keys failed:</span> ${esc(e.message)}`;
  } finally {
    b.disabled = false;
    b.textContent = "Test keys";
    const m = el("acctMsg");
    if (m) m.innerHTML = acctMsg;
  }
}

async function removeAccount() {
  const a = curAccount() || { id: S.account };
  if (a.is_default) return;
  const label = a.label || a.id;
  if (!await ask({
    title: `Remove ${esc(label)}?`, danger: true, ok: "Remove account", requireWord: "REMOVE",
    body: `The keys for <b>${esc(label)}</b> (${esc(a.account_number || "—")}) are deleted
      from this server together with its tickers, settings and agent schedules.<br><br>
      <b>Nothing at Alpaca is cancelled or sold.</b> Positions and resting orders in
      that account are left exactly as they are.<br><br>
      The server refuses while anything in it is running, armed or still holds lots —
      stop, disarm and flatten first.`,
  })) return;
  try { await DEL(`/api/accounts/${encodeURIComponent(a.id)}`); }
  catch (e) {
    // a 409 carries the server's reason; show it as it came
    await ask({ title: "Not removed", ok: "OK", body: esc(e.message) });
    return;
  }
  toast(`Removed <b>${esc(label)}</b>.`, "ok", 8000);
  try { await loadAccounts(); }
  catch (e) { S.accounts = S.accounts.filter((x) => x.id !== a.id); }
  S.accounts = S.accounts.filter((x) => x.id !== a.id);
  const next = pickAccount();
  if (next) go({ kind: "overview", account: next });
  else { setAccount(""); go({ kind: "addaccount", account: "" }); }
}

/* ------------------------------------------------------------- restart */
export async function doRestart() {
  const ov = S.ov;
  if (ov && ov.supervised === false) {
    await ask({ title: "Restart is not available here", ok: "OK",
      body: "This server was not launched by <code>start_bot.bat</code>, so nothing "
          + "would bring it back after it exits.<br><br>Close the console window and "
          + "start it again with <b>start_bot.bat</b>." });
    return;
  }
  const running = (ov ? ov.tickers.filter((t) => t.running) : []);
  const armed = running.filter((t) => !t.dry_run).map((t) => t.symbol);
  const list = running.map((t) => t.symbol);
  const me = acctLabel();
  const others = S.accounts.filter((a) => a.id !== S.account);
  const othersTxt = others.map((a) =>
    `<b>${esc(a.label || a.id)}</b> (${a.running || 0} running, ${a.armed || 0} armed)`).join(", ");

  const r = await ask({
    title: "Restart the dashboard?", ok: "Restart", danger: true,
    body: `The server stops and relaunches — that is how new code and settings take
      effect.<br><br><b class="down">This is one process for every account.</b> Every
      account's fleet restarts, not only <b>${esc(me)}</b>${others.length
        ? ` — also ${othersTxt}` : ""}.<br><br>
      <b>Take-profits resting at Alpaca stay live throughout.</b> They are the
      broker's orders, not this process's.<br><br>
      ${list.length ? `Running in ${esc(me)} now: <b>${list.join(", ")}</b>.
        ${armed.length ? `<span class="down">${armed.join(", ")} armed.</span>` : ""}`
        : `No engines are running in ${esc(me)}, so nothing of its is interrupted.`}`,
    checkbox: (list.length || others.some((a) => a.running)) ? {
      checked: true,
      label: `Start the engines that were running again once it is back.`
        + (armed.length ? ` <b class="down">${armed.join(", ")} would resume
           transmitting live orders.</b>` : ""),
    } : null,
  });
  if (!r) return;

  showRestarting();
  try {
    await POST("/api/restart", { confirm: "RESTART", resume: !!(r && r.checked) });
  } catch (e) { hideRestarting(); toast(esc(e.message), "err", 9000); return; }
  waitForServer();
}

function showRestarting() {
  if (el("rveil")) return;
  const d = document.createElement("div");
  d.className = "veil"; d.id = "rveil";
  d.innerHTML = `<div class="modal" style="text-align:center">
    <h3>Restarting…</h3>
    <div class="body" id="rmsg">Stopping every account's engines and relaunching.</div>
    <div class="tip">Your resting take-profits are untouched at Alpaca throughout.</div>
  </div>`;
  document.body.appendChild(d);
}
function hideRestarting() { const d = el("rveil"); if (d) d.remove(); }

async function waitForServer() {
  const t0 = Date.now();
  const msg = (t) => { const m = el("rmsg"); if (m) m.innerHTML = t; };
  await new Promise((r) => setTimeout(r, 2500));   // let the old process exit
  for (let i = 0; i < 90; i++) {
    try {
      // a shared route: it answers whichever account this page was on
      const r = await fetch("/api/accounts", { cache: "no-store" });
      if (r.ok) { msg("Back up — reloading."); setTimeout(() => location.reload(), 600); return; }
    } catch (e) { /* still down, expected */ }
    msg(`Waiting for the server… ${Math.round((Date.now() - t0) / 1000)}s`);
    await new Promise((r) => setTimeout(r, 700));
  }
  msg(`<span class="down">Not back after 70s.</span><br>Check the console window.`);
}
