/* ============================================================================
   Scanner -- the momentum screen, and the one thing it can do about it.

   What the screen would have shown at 09:30 on a given day, using only what
   was knowable then, with an "Add to fleet" on every candidate. That button
   is the only reason this page is part of this product: without it the page
   is a research note about somebody else's method, and nothing on it touches
   the fleet.

   The watchlist is deliberately shown as a FUNNEL rather than a final list.
   "Nine names" tells you nothing; "two hundred gapped, fifty-four had a small
   enough float" tells you which criterion is actually doing the selecting, and
   that is the number that moves when the market changes.

   The Replication tab is gone (123 lines, a third hand-rolled equity chart).
   It replayed a DIFFERENT strategy on a hypothetical $2,000 account and could
   never inform a decision about this fleet; the replay itself lives on in
   research/ on disk, where a research artefact belongs.
   ========================================================================= */
"use strict";
import {
  S, VIEWS, GET, POST, act, ask, toast, el, esc, card, stat, tableHTML,
  money, money0, pct, go,
} from "../core.js";

let scan = null;
let pickedDate = "";

const num = (n) => (n == null ? "—" : Number(n).toLocaleString());
const flo = (n) => {
  if (n == null) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + "K";
  return num(n);
};

VIEWS.scanner = {
  title: () => "Scanner",
  sub: () => "five criteria, point-in-time — and a way into the fleet",

  mount() {
    el("view").innerHTML = `
      <div class="grid main">
        <div>
          ${card("Watchlist", `
            <div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap">
              <select id="scDate" style="width:auto"></select>
              <span class="faint" id="scNote" style="align-self:center"></span>
            </div>
            <div class="stats" id="scStats"></div>
            <div id="scTable" style="margin-top:16px"></div>`)}
        </div>
        <div>
          ${card("The funnel", `<div id="scFunnel"></div>`,
            "where candidates are lost", { flush: true })}
          ${card("The five criteria", `<div id="scCrit"></div>`,
            "as configured", { flush: true })}
          ${card("Adding one", `<div class="tip" style="margin-top:0">
            <b>Add to fleet</b> creates a ladder on that symbol with the default
            strategy — the plain $0.10 ladder — <b>stopped and in dry run</b>.
            Nothing transmits until you start it and arm it, and every setting
            is editable on the ticker's own page afterwards.<br><br>
            These names are picked by a momentum screen, not by anything the
            ladder cares about. A small float and a big gap is exactly the shape
            that can keep falling after the ladder is full, and the ladder has
            <b>no stop loss</b>.</div>`)}
          ${card("Float", `
            <div class="tip" style="margin-top:0">Float comes from SEC filings,
              keyed on the date each figure was <b>filed</b> rather than the
              period it covers — a share count cannot be used before it was
              public. Where no public float was ever reported the figure is
              shares outstanding, which is an <b>upper bound</b>, and the
              quality column says so.</div>`)}
        </div>
      </div>`;
    const sel = el("scDate");
    if (sel) {
      sel.onchange = async (e) => {
        pickedDate = e.target.value;
        await load();
        paintList();
      };
    }
    load().then(paintList);
  },

  paint() { if (scan) paintList(); },
};

async function load() {
  try {
    scan = await GET("/api/scanner" + (pickedDate ? "?date=" + pickedDate : ""));
  } catch (e) {
    scan = { error: String(e.message || e) };
  }
}

/* The only thing on this page that touches the fleet. It goes through the
   same POST /api/tickers the Add-a-ticker page uses, with no config, so the
   new ladder gets the server's defaults plus the default preset -- stopped,
   dry run, nothing transmitted. */
function addToFleet(sym, price) {
  return act(async () => {
    const held = ((S.ov && S.ov.tickers) || []).some((t) => t.symbol === sym);
    if (held) {
      toast(`${esc(sym)} is already in the fleet.`, "err");
      return;
    }
    if (!await ask({
      title: `Add ${esc(sym)} to the fleet?`, ok: "Add ticker",
      body: `A new ladder on <b>${esc(sym)}</b> with its own ledger, on the
        default strategy (the plain $0.10 ladder).<br><br>
        It arrives <b>stopped and in dry run</b> — nothing transmits until you
        start it and arm it.<br><br>
        ${price ? `The screen saw it open at <b>${money(price)}</b> on
          ${esc(scan.date || "that day")}; it will size against today's price,
          not that one.<br><br>` : ""}
        This symbol was chosen by a momentum screen, which is not the thing
        this ladder is good at. Check its settings before arming it.`,
    })) return;
    await POST("/api/tickers", { symbol: sym });
    toast(`<b>${esc(sym)}</b> added — stopped and in dry run.`, "ok", 8000);
    await window.__tick();
    go({ kind: "ticker", sym, tab: "settings" });
  });
}

function paintList() {
  if (!scan || !el("scTable")) return;
  if (scan.error) {
    el("scTable").innerHTML = `<div class="empty">${esc(scan.error)}</div>`;
    return;
  }
  const sel = el("scDate");
  if (sel && !sel.options.length) {
    sel.innerHTML = (scan.dates || [])
      .slice()
      .reverse()
      .map((d) => `<option value="${d}">${d}</option>`)
      .join("");
    sel.value = scan.date;
  }
  const r = scan.regime || {};
  el("scNote").textContent = r.basis || "";
  el("scStats").innerHTML =
    stat("Gapped in", num(scan.gapped), "price, gap, liquidity") +
    stat("Survived float", num(scan.survived), "the fifth criterion") +
    stat("Market", r.regime || "—", "sets the float ceiling") +
    stat("Float ceiling", flo(scan.float_max), "shares");

  const inFleet = new Set(((S.ov && S.ov.tickers) || []).map((t) => t.symbol));
  const rows = (scan.candidates || []).map((c) => {
    const open = Number(c.open_raw != null ? c.open_raw : c.open);
    return `
    <tr>
      <td><b>${esc(c.symbol)}</b></td>
      <td class="num">$${open.toFixed(2)}</td>
      <td class="num ${c.gap_pct >= 0 ? "up" : "down"}">${pct(c.gap_pct, 1)}</td>
      <td class="num">${flo(c.float_shares)}</td>
      <td><span class="faint">${esc(c.float_quality || "—")}</span></td>
      <td class="num">${c.float_turnover == null ? "—" : c.float_turnover + "x"}</td>
      <td class="num">${flo(c.avg_vol_30)}</td>
      <td>${inFleet.has(c.symbol)
        ? `<button class="btn sm" data-go="ticker" data-sym="${esc(c.symbol)}"
             data-tab="live">In the fleet</button>`
        : `<button class="btn sm" data-add="${esc(c.symbol)}"
             data-px="${open}">Add to fleet</button>`}</td>
    </tr>`;
  });
  el("scTable").innerHTML = tableHTML(
    ["Symbol", "Open (as traded)", "Gap", "Float", "Quality", "Turnover",
     "30d avg vol", ""],
    rows,
    "Nothing gapped into range that morning."
  );
  el("scTable").querySelectorAll("[data-add]").forEach((b) => {
    b.onclick = () => addToFleet(b.dataset.add, Number(b.dataset.px) || 0);
  });

  const d = scan.dropped || {};
  el("scFunnel").innerHTML = tableHTML(
    ["Stage", "Lost"],
    [
      ["no SEC filing at all", d.no_filing],
      ["float figure implausible", d.implausible],
      ["over the float ceiling", d.too_big],
      ["over 100M shares", d.hard_reject],
    ].map(
      ([k, v]) => `<tr><td>${k}</td><td class="num">${num(v || 0)}</td></tr>`
    )
  );

  const c = scan.criteria || {};
  el("scCrit").innerHTML = tableHTML(
    ["Criterion", "Value"],
    [
      ["price", `$${c.min_price} – $${c.max_price}`],
      ["gap at the open", `≥ ${c.min_gap_pct}%`],
      ["up on the day", `≥ ${c.min_change_pct}%`],
      ["relative volume", `≥ ${c.min_rvol}x`],
      ["float", `≤ ${flo(scan.float_max)}`],
    ].map(([k, v]) => `<tr><td>${k}</td><td class="num">${v}</td></tr>`)
  );
}
