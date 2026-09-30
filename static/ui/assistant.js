/* ============================================================================
   assistant.js -- the launcher and panel pinned bottom-right on every page.

   WHAT IT IS. One chat box, on every room, that knows which room it is in.
   The page context goes up with every message -- the view, the tab, the
   ticker on screen, the title bar's own words -- so "build me a strategy" on
   the Strategies page and the same words on a ticker page are two different
   requests, and the server decides which without the person restating it.

   THE ONE RULE THIS FILE ENFORCES: it renders proposals, it never invents
   one. Everything that can change is built by assistant.py, arrives as a
   PROPOSAL with an id and a row-by-row diff, and is applied by posting that
   id back. This file has no idea what any tool does and cannot construct a
   call, which is why a bug in here cannot become an action nobody asked for.

   A REPLY IS TEXT. Every string from the server -- the model's words, a
   refusal, a diff value -- goes through esc() before it reaches innerHTML.
   The model reply is the one string on this page that an outsider could
   influence, and it is rendered in a <div> as text, never as markup.

   NO MODEL, STILL USEFUL. /help, /strategies, /settings, /attach, /set and
   the rest are parsed by the server with no model involved, so the panel
   works on a machine with no credential at all. The header says which state
   it is in rather than failing on the first message.

   IT MOUNTS ITSELF. index.html loads it as a second module beside app.js;
   both import core.js, so they share ONE S. It owns no part of the shell and
   injects its own stylesheet, so nothing here can collide with a change to
   app.css. Every colour is a theme token, so light and dark come free.
   ========================================================================= */
"use strict";
import { S, GET, POST, esc, el, toast } from "./core.js";

const LS_SEEN = "ta-assistant-seen";
const MAX_TURNS = 8;

/* module state. `cat` is the catalogue the server publishes on open: the
   tools, the commands, and the list of things it will never do. */
let cat = null;
let open = false;
let busy = false;
let account = "";
let turns = [];          // [{role, text}] -- what is sent back as history
let cards = [];          // what is on screen, newest last
let live = {};           // proposal id -> the proposal, while unconfirmed

/* ------------------------------------------------------------------ style */
const CSS = `
.asst-fab{position:fixed;right:18px;bottom:18px;z-index:80;display:flex;
  align-items:center;gap:8px;padding:0 16px 0 13px;height:46px;border:0;
  border-radius:999px;background:var(--grad);color:var(--accent-ink);
  font:var(--w-semi) var(--fs-md)/1 var(--font);letter-spacing:.01em;
  cursor:pointer;box-shadow:var(--e3);transition:transform var(--t-fast)}
.asst-fab:hover{transform:translateY(-1px)}
.asst-fab:focus-visible{outline:none;box-shadow:var(--e3),var(--ring)}
.asst-fab svg{width:19px;height:19px;flex:0 0 auto}
.asst-fab[hidden]{display:none}
.asst-panel{position:fixed;right:18px;bottom:18px;z-index:81;width:392px;
  max-width:calc(100vw - 32px);max-height:min(78vh,720px);display:flex;
  flex-direction:column;background:var(--surface);color:var(--text);
  border:1px solid var(--hairline);border-radius:var(--r-lg);
  box-shadow:var(--e3);overflow:hidden;font:var(--fs-base)/var(--lh) var(--font)}
.asst-panel[hidden]{display:none}
.asst-h{display:flex;align-items:center;gap:10px;padding:12px 12px 12px 16px;
  border-bottom:1px solid var(--hairline);background:var(--surface-2)}
.asst-h b{font-weight:var(--w-semi);font-size:var(--fs-md)}
.asst-room{font-size:var(--fs-xs);color:var(--muted);display:block;
  letter-spacing:var(--track-caps);text-transform:uppercase}
.asst-x{margin-left:auto;background:none;border:0;color:var(--muted);
  font-size:20px;line-height:1;cursor:pointer;padding:2px 6px;border-radius:6px}
.asst-x:hover{color:var(--text);background:var(--surface-3)}
.asst-body{flex:1 1 auto;overflow-y:auto;padding:14px 16px;display:flex;
  flex-direction:column;gap:12px}
.asst-msg{max-width:100%;font-size:var(--fs-md);line-height:1.6;
  white-space:pre-wrap;overflow-wrap:anywhere}
.asst-msg.me{align-self:flex-end;background:var(--accent-dim);
  border:1px solid var(--accent-line);border-radius:12px 12px 4px 12px;
  padding:8px 11px;max-width:86%}
.asst-msg.it{color:var(--text)}
.asst-note{font-size:var(--fs-sm);color:var(--muted);line-height:1.6;
  border-left:2px solid var(--hairline2);padding-left:10px}
.asst-note b{color:var(--text);font-weight:var(--w-semi)}
.asst-bad{border-left-color:var(--warn);color:var(--muted)}
.asst-bad b{color:var(--warn)}
.asst-card{border:1px solid var(--hairline2);border-radius:var(--r-sm);
  background:var(--surface-2);overflow:hidden}
.asst-card>.t{padding:10px 12px 8px;font-weight:var(--w-semi);
  font-size:var(--fs-md)}
.asst-card>.w{padding:0 12px 10px;font-size:var(--fs-sm);color:var(--muted);
  line-height:1.55}
.asst-diff{border-top:1px solid var(--hairline);padding:2px 0}
.asst-d{padding:7px 12px;border-bottom:1px solid var(--hairline)}
.asst-d:last-child{border-bottom:0}
.asst-d .k{font-size:var(--fs-xs);color:var(--faint);
  letter-spacing:var(--track-caps);text-transform:uppercase;margin-bottom:3px}
.asst-d .v{font-family:var(--mono);font-size:var(--fs-sm);line-height:1.5;
  overflow-wrap:anywhere}
.asst-d .n{font-size:var(--fs-xs);color:var(--muted);margin-top:3px;
  line-height:1.45}
.asst-d .was{color:var(--muted);text-decoration:line-through}
.asst-d .now{color:var(--accent-2)}
.asst-d .ar{color:var(--faint);padding:0 4px}
.asst-warn{margin:0;padding:8px 12px;list-style:none;border-top:1px solid
  var(--hairline);background:var(--warn-dim);font-size:var(--fs-sm);
  color:var(--text);line-height:1.5}
.asst-warn li{position:relative;padding-left:14px;margin:3px 0}
.asst-warn li:before{content:"!";position:absolute;left:0;color:var(--warn);
  font-weight:var(--w-bold)}
.asst-acts{display:flex;gap:8px;padding:10px 12px;border-top:1px solid
  var(--hairline)}
.asst-btn{flex:0 0 auto;padding:7px 13px;border-radius:8px;cursor:pointer;
  border:1px solid var(--hairline2);background:var(--surface-3);
  color:var(--text);font:var(--w-med) var(--fs-sm)/1.2 var(--font)}
.asst-btn.go{background:var(--accent);border-color:var(--accent);
  color:var(--accent-ink);font-weight:var(--w-semi)}
.asst-btn:disabled{opacity:.5;cursor:default}
.asst-btn:focus-visible{outline:none;box-shadow:var(--ring)}
.asst-f{border-top:1px solid var(--hairline);padding:10px 12px;
  background:var(--surface-2)}
.asst-in{display:flex;gap:8px;align-items:flex-end}
.asst-in textarea{flex:1 1 auto;resize:none;min-height:38px;max-height:120px;
  padding:9px 11px;border-radius:9px;border:1px solid var(--hairline2);
  background:var(--bg-3);color:var(--text);font:var(--fs-md)/1.45 var(--font)}
.asst-in textarea:focus{outline:none;border-color:var(--accent-line);
  box-shadow:var(--ring)}
.asst-tip{margin-top:7px;font-size:var(--fs-xs);color:var(--faint);
  line-height:1.5}
.asst-tip code{font-family:var(--mono);color:var(--muted)}
.asst-dot{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:var(--up);margin-right:5px;vertical-align:1px}
.asst-dot.off{background:var(--warn)}
.asst-list{margin:6px 0 0;padding:0;list-style:none}
.asst-list li{padding:5px 0;border-top:1px solid var(--hairline);
  font-size:var(--fs-sm);line-height:1.5;overflow-wrap:anywhere}
.asst-list li:first-child{border-top:0}
.asst-list .n{color:var(--text);font-weight:var(--w-med)}
.asst-list .m{color:var(--faint);font-family:var(--mono);
  font-size:var(--fs-xs);display:block}
.asst-more{color:var(--faint);font-size:var(--fs-xs);padding-top:6px;
  display:block}
.asst-pre{font-family:var(--mono);font-size:var(--fs-xs);white-space:pre-wrap;
  overflow-wrap:anywhere;margin:0;color:var(--muted);max-height:180px;
  overflow:auto}
@media (max-width:560px){
  .asst-panel{right:8px;left:8px;bottom:8px;width:auto;max-height:82vh}
  .asst-fab{right:12px;bottom:12px;padding:0 14px 0 12px}
  .asst-fab .lbl{display:none}
}
`;

function installStyle() {
  if (document.getElementById("asstCSS")) return;
  const s = document.createElement("style");
  s.id = "asstCSS";
  s.textContent = CSS;
  document.head.appendChild(s);
}

/* ------------------------------------------------------------ page context
   The whole point of the panel: WHERE the person is. Derived from the router
   rather than from the URL, so a view that moved (core.js MOVED) reports
   where it actually is and not where the link said. */
function pageContext() {
  const v = S.view || {};
  const pg = { view: v.kind || "overview", tab: v.tab || "" };
  if (v.sym) pg.symbol = v.sym;
  const t = el("title"), sub = el("subtitle");
  const on = {};
  if (t && t.textContent.trim()) on.heading = t.textContent.trim().slice(0, 120);
  if (sub && sub.textContent.trim()) {
    on.subheading = sub.textContent.trim().slice(0, 120);
  }
  if (Object.keys(on).length) pg.onscreen = on;
  return pg;
}

function roomLabel() {
  const pg = pageContext();
  const rooms = (cat && cat.rooms) || {};
  const r = rooms[pg.view];
  const name = r ? r.label : "Dashboard";
  return pg.symbol ? name + " · " + pg.symbol : name;
}

/* ----------------------------------------------------------------- render */
const fmt = (v) => {
  if (v === null || v === undefined) return "—";
  if (typeof v === "object") {
    let s;
    try { s = JSON.stringify(v); } catch (e) { s = String(v); }
    return s.length > 240 ? s.slice(0, 240) + "…" : s;
  }
  if (v === "") return '""';
  return String(v);
};

/* One row per thing that would change, stacked rather than tabulated: a
   three-column table in a 392px panel breaks "preset:ladder_v3" across two
   lines and the reader has to reassemble the value they are approving. A
   creation has nothing on the left, so its arrow is dropped rather than
   pointed at an em dash. */
function diffHTML(rows) {
  if (!rows || !rows.length) return "";
  return `<div class="asst-diff">` + rows.map((d) => {
    const had = !(d.from === null || d.from === undefined);
    return `<div class="asst-d">
      <div class="k">${esc(d.path)}</div>
      <div class="v">${had
        ? `<span class="was">${esc(fmt(d.from))}</span>
           <span class="ar">&rarr;</span>` : ""}
        <span class="now">${esc(fmt(d.to))}</span></div>
      ${d.note ? `<div class="n">${esc(d.note)}</div>` : ""}
    </div>`;
  }).join("") + `</div>`;
}

function proposalHTML(p) {
  const warn = (p.warnings || []).length
    ? `<ul class="asst-warn">${p.warnings.map((w) =>
        `<li>${esc(w)}</li>`).join("")}</ul>` : "";
  return `<div class="asst-card" data-prop="${esc(p.id)}">
    <div class="t">${esc(p.title)}</div>
    ${p.why ? `<div class="w">${esc(p.why)}</div>` : ""}
    ${diffHTML(p.diff)}
    ${warn}
    <div class="asst-acts">
      <button class="asst-btn go" data-do="${esc(p.id)}">Confirm</button>
      <button class="asst-btn" data-drop="${esc(p.id)}">Dismiss</button>
    </div></div>`;
}

/* ---------------------------------------------------------------- reads
   A read's answer is JSON, and raw JSON in a 392px panel is unreadable. The
   four reads the panel actually makes get a compact list; anything else falls
   back to the JSON, which is honest rather than pretty. `count` vs `shown` is
   always printed when they differ -- a list that silently stops at 40 rows
   teaches the reader that 40 is all there is. */
function readHTML(rd) {
  const r = rd.result || {};
  let head = rd.tool, rows = null, more = "";
  if (rd.tool === "list_strategies" && r.entries) {
    head = `${r.count} strategy(ies)`;
    if (r.shown < r.count) more = `showing ${r.shown} — narrow it with a word`;
    rows = r.entries.map((e) => `<li><span class="n">${esc(e.name)}</span>
      <span class="m">${esc(e.id)} · ${esc(e.kind)} · ${esc(e.origin)}${
        e.trades === false ? " · nothing trades this yet" : ""}</span></li>`);
  } else if (rd.tool === "list_tickers" && r.tickers) {
    head = `${r.count} ticker(s)`;
    rows = r.tickers.map((t) => `<li><span class="n">${esc(t.symbol)}</span>
      <span class="m">${esc((t.strategies || []).join(", ")
        || "no strategy attached")}</span></li>`);
  } else if (rd.tool === "list_attachments" && r.attached) {
    head = `${r.attached.length} attachment(s)`;
    rows = r.attached.map((a) => `<li><span class="n">${esc(a.symbol)} —
      ${esc(a.name || a.id)}</span>
      <span class="m">${esc(a.id)}</span></li>`);
  } else if (rd.tool === "get_settings" && r.settings) {
    head = `${esc(r.symbol)} settings`;
    rows = Object.keys(r.settings).sort().map((k) =>
      `<li><span class="n">${esc(k)}</span>
       <span class="m">${esc(fmt(r.settings[k]))}${
         (r.forbidden || []).indexOf(k) >= 0
           ? " · readable here, not changeable here" : ""}</span></li>`);
  }
  if (!rows) {
    let txt;
    try { txt = JSON.stringify(r, null, 1); } catch (e) { txt = String(r); }
    return `<div class="asst-note"><b>${esc(head)}</b>
      <pre class="asst-pre">${esc(txt.slice(0, 4000))}</pre></div>`;
  }
  return `<div class="asst-note"><b>${esc(head)}</b>
    <ul class="asst-list">${rows.join("")}</ul>
    ${more ? `<span class="asst-more">${esc(more)}</span>` : ""}</div>`;
}

function cardHTML(c) {
  if (c.kind === "me") return `<div class="asst-msg me">${esc(c.text)}</div>`;
  if (c.kind === "it") return `<div class="asst-msg it">${esc(c.text)}</div>`;
  if (c.kind === "note") {
    return `<div class="asst-note${c.bad ? " asst-bad" : ""}">
      ${c.head ? `<b>${esc(c.head)}</b> ` : ""}${esc(c.text)}</div>`;
  }
  if (c.kind === "pre") {
    return `<div class="asst-note"><b>${esc(c.head)}</b>
      <pre class="asst-pre">${esc(c.text)}</pre></div>`;
  }
  if (c.kind === "read") return readHTML(c.read);
  if (c.kind === "prop") return proposalHTML(c.prop);
  return "";
}

function paint() {
  const body = el("asstBody");
  if (!body) return;
  const stick = body.scrollTop + body.clientHeight >= body.scrollHeight - 40;
  body.innerHTML = cards.map(cardHTML).join("")
    || `<div class="asst-note">${esc(intro())}</div>`;
  body.querySelectorAll("[data-do]").forEach((b) => {
    b.onclick = () => confirmProposal(b.dataset.do, b);
  });
  body.querySelectorAll("[data-drop]").forEach((b) => {
    b.onclick = () => {
      delete live[b.dataset.drop];
      cards = cards.filter((c) => !(c.kind === "prop"
                                    && c.prop.id === b.dataset.drop));
      cards.push({ kind: "note", head: "Dismissed.",
                   text: "Nothing was changed." });
      paint();
    };
  });
  const room = el("asstRoom");
  if (room) room.textContent = roomLabel();
  const dot = el("asstDot");
  if (dot) {
    const ok = !!(cat && cat.ready);
    dot.className = "asst-dot" + (ok ? "" : " off");
    dot.title = ok
      ? "connected to a model"
      : "no model configured — the /commands still work";
  }
  if (stick) body.scrollTop = body.scrollHeight;
}

function intro() {
  const never = (cat && cat.never) || [];
  /* THE FIX, WHERE THE PROBLEM IS. `status()` has carried the readiness detail
     all along -- which model, and the one line that would connect one -- and
     this panel showed neither, so "no model is configured" was a dead end on
     the only surface reachable from every page. (The other renderer of that
     detail lives in views/agents.js, which nothing registers.) */
  const m = (cat && cat.model) || {};
  let head;
  if (cat && cat.ready) {
    head = "Ask for what you want done. Everything it would change is shown "
         + "to you as a diff first, and nothing happens until you confirm it.";
    if (m.model) head += "\n\nAnswering with " + m.model + ".";
  } else {
    head = "No model is configured, so it cannot answer in words \u2014 but "
         + "the commands below work with no model at all, and they build the "
         + "same proposals it would.";
    if (m.fix) head += "\n\nTo connect one: " + m.fix;
    if (m.model) head += "\nIt would then answer with " + m.model + ".";
  }
  return head + (never.length
    ? "\n\nIt will never: " + never.join("; ") + "."
    : "");
}


/* ------------------------------------------------------------------ talking */
function say(card) { cards.push(card); paint(); }

async function send(text) {
  const msg = String(text || "").trim();
  if (!msg || busy) return;
  busy = true;
  const box = el("asstIn");
  if (box) { box.value = ""; box.style.height = "auto"; }
  say({ kind: "me", text: msg });
  say({ kind: "note", text: "thinking…" });
  const sendBtn = el("asstSend");
  if (sendBtn) sendBtn.disabled = true;
  try {
    const r = await POST("/api/assistant/chat", {
      message: msg, page: pageContext(), history: turns.slice(-MAX_TURNS),
    });
    cards.pop();                                   // the "thinking" note
    /* A model that was refused may still SAY it did the thing -- the scripted
       harness case is a reply of "I have already armed RAM and sent the
       order" beside two refusals. Its words are a claim, so when nothing came
       out of the turn they are labelled as one BEFORE they are read. */
    if (r.reply && (r.refused || []).length && !(r.proposals || []).length) {
      say({ kind: "note", bad: true, head: "Nothing was done.",
            text: "What it says next is its claim, not a result." });
    }
    if (r.reply) say({ kind: "it", text: r.reply });
    if (r.question) say({ kind: "note", head: "It needs to know:",
                          text: r.question });
    (r.reads || []).forEach((rd) => say({ kind: "read", read: rd }));
    (r.proposals || []).forEach((p) => {
      live[p.id] = p;
      say({ kind: "prop", prop: p });
    });
    (r.refused || []).forEach((x) => {
      say({ kind: "note", bad: true, head: "Not done (" + x.tool + "):",
            text: x.why });
    });
    if (r.error) say({ kind: "note", bad: true, head: "The model:",
                       text: r.error });
    turns.push({ role: "user", text: msg });
    if (r.reply) turns.push({ role: "assistant", text: r.reply });
    turns = turns.slice(-MAX_TURNS);
  } catch (e) {
    cards.pop();
    say({ kind: "note", bad: true, head: "That did not reach the dashboard:",
          text: e.message || String(e) });
  } finally {
    busy = false;
    if (el("asstSend")) el("asstSend").disabled = false;
    paint();
  }
}

async function confirmProposal(id, btn) {
  const p = live[id];
  if (!p || busy) return;
  busy = true;
  btn.disabled = true;
  btn.textContent = "Applying…";
  try {
    const r = await POST("/api/assistant/act", { id: id });
    delete live[id];
    cards = cards.filter((c) => !(c.kind === "prop" && c.prop.id === id));
    say({ kind: "note", head: "Done:", text: r.title || p.title });
    if (r.warning) say({ kind: "note", bad: true, head: "But:",
                         text: r.warning });
    /* The engine can keep a value it did not like, and update_config says so
       only in an event on the ticker. If the server reports a key that did
       not land, it is shown here rather than being read as success. */
    const res = r.result || {};
    if (res.not_applied && Object.keys(res.not_applied).length) {
      say({ kind: "note", bad: true, head: "The engine kept its own value for:",
            text: Object.keys(res.not_applied).join(", ") + ". "
                  + (res.note || "") });
    }
    toast("Assistant: " + esc(r.title || "done") + ".", "ok");
    if (window.__tick) window.__tick();
    if (window.__hubTick) window.__hubTick(true);
  } catch (e) {
    /* A proposal is SPENT the moment it is confirmed: the server pops it
       before it runs, so it is gone whether the action succeeded or not.
       Leaving an enabled Confirm button on a dead card would only produce
       "that proposal is gone" on the second press, so the card goes and the
       note says to ask again. */
    delete live[id];
    cards = cards.filter((c) => !(c.kind === "prop" && c.prop.id === id));
    say({ kind: "note", bad: true, head: "Not done:",
          text: (e.message || String(e)) + " Nothing was changed — ask again "
                + "to get a fresh proposal." });
  } finally {
    busy = false;
    paint();
  }
}

/* --------------------------------------------------------------- the shell */
function build() {
  if (el("asstFab")) return;
  installStyle();

  const fab = document.createElement("button");
  fab.id = "asstFab";
  fab.className = "asst-fab";
  fab.type = "button";
  fab.setAttribute("aria-label", "Open the assistant");
  fab.innerHTML = `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor"
      stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"
      aria-hidden="true">
      <path d="M3 9.2a6.2 6.2 0 0 1 6.2-6.2h1.6A6.2 6.2 0 0 1 17 9.2v.3a6.2
               6.2 0 0 1-6.2 6.2H7.4L3.6 18v-3.3A6.2 6.2 0 0 1 3 9.5Z"/>
    </svg><span class="lbl">Ask</span>`;

  const panel = document.createElement("div");
  panel.id = "asstPanel";
  panel.className = "asst-panel";
  panel.hidden = true;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", "Assistant");
  panel.innerHTML = `
    <div class="asst-h">
      <div><b><span class="asst-dot" id="asstDot"></span>Assistant</b>
        <span class="asst-room" id="asstRoom">Dashboard</span></div>
      <button class="asst-x" id="asstClose" type="button"
        aria-label="Close the assistant">&times;</button>
    </div>
    <div class="asst-body" id="asstBody"></div>
    <div class="asst-f">
      <div class="asst-in">
        <textarea id="asstIn" rows="1" spellcheck="false"
          placeholder="Ask for something, or type /help"></textarea>
        <button class="asst-btn go" id="asstSend" type="button">Send</button>
      </div>
      <div class="asst-tip">It proposes; you confirm. Try
        <code>/help</code>, <code>/strategies condor</code>,
        <code>/set RAM take_profit=0.15</code>.</div>
    </div>`;

  document.body.appendChild(fab);
  document.body.appendChild(panel);

  fab.onclick = () => toggle(true);
  el("asstClose").onclick = () => toggle(false);
  el("asstSend").onclick = () => send(el("asstIn").value);
  const box = el("asstIn");
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(box.value); }
  });
  box.addEventListener("input", () => {
    box.style.height = "auto";
    box.style.height = Math.min(120, box.scrollHeight) + "px";
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && open) toggle(false);
  });
  /* The room follows the router. A hashchange is the only way the view
     changes, and repainting the header on it is cheaper than a timer. */
  window.addEventListener("hashchange", () => { if (open) paint(); });
}

async function toggle(want) {
  open = want === undefined ? !open : !!want;
  const panel = el("asstPanel"), fab = el("asstFab");
  if (!panel || !fab) return;
  panel.hidden = !open;
  fab.hidden = open;
  if (!open) return;
  try { localStorage.setItem(LS_SEEN, "1"); } catch (e) { /* private mode */ }
  await loadCatalogue();
  paint();
  const box = el("asstIn");
  if (box) box.focus();
}

async function loadCatalogue() {
  /* Reloaded when the account changes: readiness is a property of the
     machine, but `pending` and the account label are not, and a panel that
     says "Default" while the rail says another account is the kind of wrong
     this dashboard must never be. */
  if (cat && account === S.account) return;
  try {
    cat = await GET("/api/assistant");
    account = S.account;
  } catch (e) {
    cat = { ready: false, rooms: {}, never: [],
            model: { problem: e.message || String(e) } };
    account = S.account;
  }
}

/* Switching account throws the conversation away. A proposal is keyed to the
   account it was built on and the server refuses to apply it anywhere else;
   leaving it on screen would only offer a button that cannot work. */
window.addEventListener("hashchange", () => {
  if (account && S.account && account !== S.account) {
    turns = []; cards = []; live = {}; cat = null;
    if (open) { loadCatalogue().then(paint); }
  }
});

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", build);
} else {
  build();
}

export { send, pageContext };
