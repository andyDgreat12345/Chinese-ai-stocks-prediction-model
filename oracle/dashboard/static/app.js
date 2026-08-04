/* China Market Oracle — terminal dashboard front-end (spec §5).
 * Vanilla JS, no build step, no external libs. Polls the local FastAPI backend;
 * does no compute of its own. Panel order persists in localStorage. */
"use strict";

const LS_ORDER = "cmo.panelOrder.v1";
const POLL_MS = 60_000;

// ── helpers ────────────────────────────────────────────────────────────
const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pct = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${Number(v).toFixed(2)}%`);
const signClass = (v) => (v == null ? "dim" : v > 0 ? "pos" : v < 0 ? "neg" : "dim");
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// Live: hit the FastAPI /api/<name> endpoints. Static (GitHub Pages): fetch the
// prebuilt api/<name>.json snapshots written by the site build.
const STATIC = !!window.CMO_STATIC;
const apiUrl = (name) => (STATIC ? `api/${name}.json` : `/api/${name}`);

async function getJSON(name) {
  const r = await fetch(apiUrl(name), { cache: "no-store" });
  if (!r.ok) throw new Error(`${name} ${r.status}`);
  return r.json();
}

// color a score in [-1,1] from red (bearish) through neutral to green (bullish)
function scoreColor(s) {
  s = clamp(Number(s) || 0, -1, 1);
  const t = Math.abs(s);
  const hue = s >= 0 ? 150 : 356;
  return `hsl(${hue}, ${30 + t * 50}%, ${14 + t * 14}%)`;
}

function tag(direction) {
  const d = (direction || "neutral").toLowerCase();
  return `<span class="tag ${d}">${esc(d)}</span>`;
}
function emptyNote(msg) { return `<div class="empty">${esc(msg)}</div>`; }

// Daily-report direction glyph, colored by sign.
function dirSpan(dir) {
  const label = { bullish: "▲ up", bearish: "▼ down", neutral: "► flat" }[dir] || "—";
  const cls = dir === "bullish" ? "pos" : dir === "bearish" ? "neg" : "dim";
  return `<span class="${cls}">${label}</span>`;
}
function reportItem(m) {
  const dir = m.consensus == null
    ? `mixed: rule ${dirSpan(m.rule_dir)} / AI ${dirSpan(m.llm_dir)} → no clear edge`
    : `${dirSpan(m.consensus)} · <span class="dim">${esc(m.conviction)} (${esc(m.source)})</span>`;
  const drivers = (m.drivers && m.drivers.length)
    ? `<div class="rationale">${m.drivers.map(esc).join(" · ")}</div>` : "";
  const row = el("div", "pred");
  row.innerHTML =
    `<div class="pred-top">
       <span class="sector">${esc(m.label)}</span>
       <span class="badge">${esc(m.etf)}</span>
       <span class="repdir">${dir}</span>
     </div>${drivers}`;
  return row;
}

// ── panel registry ─────────────────────────────────────────────────────
// Each panel: { id, title, wide?, render(bodyEl) -> Promise }
const PANELS = [
  {
    id: "prediction", title: "Prediction Summary", wide: true,
    async render(body) {
      const d = await getJSON("prediction");
      if (!d.predictions.length) return void (body.innerHTML = emptyNote("No prediction yet — run the analysis job."));
      const head = el("div", "dim", `China session ${esc(d.trade_date || "—")}`);
      body.innerHTML = "";
      body.appendChild(head);
      for (const p of d.predictions) {
        const row = el("div", "pred");
        row.innerHTML =
          `<div class="pred-top">
             <span class="sector">${esc(p.sector)}</span>
             ${tag(p.direction)}
             <span class="conf ${esc(p.confidence)}">${esc(p.confidence)} conf</span>
             <span class="score">${Number(p.composite_score).toFixed(2)}</span>
           </div>
           <div class="rationale">${esc(p.rationale)}</div>`;
        body.appendChild(row);
      }
    },
  },
  {
    id: "report", title: "Daily Action Report", wide: true,
    async render(body) {
      const d = await getJSON("report");
      const groups = [
        ["consider", "✅ Leaning constructive — candidates to consider", "pos"],
        ["avoid", "⛔ Leaning cautious — hold off / avoid adding", "neg"],
        ["watch", "👀 Mixed or flat — watch, no clear edge", "dim"],
      ];
      const total = groups.reduce((n, [k]) => n + ((d[k] || []).length), 0);
      if (!total) return void (body.innerHTML = emptyNote("No outlook yet — the report is produced right after the US close."));
      body.innerHTML = "";
      body.appendChild(el("div", "dim", `China session ${esc(d.trade_date || "—")} · ${esc(d.us_summary || "")}`));
      if (!d.analyst_enabled) body.appendChild(el("div", "dim", "AI analyst off — rule-based read only."));
      for (const [key, label, cls] of groups) {
        const items = d[key] || [];
        if (!items.length) continue;
        body.appendChild(el("div", "rep-head " + cls, esc(label)));
        for (const m of items) body.appendChild(reportItem(m));
      }
    },
  },
  {
    id: "llm-usage", title: "AI Research Spend",
    async render(body) {
      const d = await getJSON("llm-usage");
      const money = (v) => `$${Number(v || 0).toFixed(4)}`;
      const tok = (v) => Number(v || 0).toLocaleString();
      body.innerHTML =
        `<div class="bigstat">
           <span class="pct">${money(d.all_time.cost_usd)}</span>
           <span class="lbl">all-time · ${tok(d.all_time.tokens)} tokens · ${d.all_time.calls} calls</span>
         </div>
         <table>
           <tr><th>window</th><th class="num">cost</th><th class="num">tokens</th><th class="num">calls</th></tr>
           <tr><td>today</td><td class="num">${money(d.today.cost_usd)}</td><td class="num dim">${tok(d.today.tokens)}</td><td class="num dim">${d.today.calls}</td></tr>
           <tr><td>last 7d</td><td class="num">${money(d.last_7d.cost_usd)}</td><td class="num dim">${tok(d.last_7d.tokens)}</td><td class="num dim">${d.last_7d.calls}</td></tr>
         </table>`;
      if (d.by_model && d.by_model.length) {
        const t = el("table");
        t.innerHTML = "<tr><th>model</th><th class='num'>cost</th><th class='num'>calls</th></tr>";
        for (const m of d.by_model) {
          t.insertAdjacentHTML("beforeend",
            `<tr><td>${esc(m.provider)}/${esc(m.model)}</td><td class="num">${money(m.cost_usd)}</td><td class="num dim">${m.calls}</td></tr>`);
        }
        body.appendChild(t);
      }
      if (!d.all_time.calls) body.appendChild(el("div", "empty", "No AI calls yet — enable the analyst to start metering."));
      body.appendChild(el("div", "rationale", "Estimated from published rates; the provider invoice is truth."));
    },
  },
  {
    id: "heatmap", title: "Sector Heatmap (predicted)",
    async render(body) {
      const d = await getJSON("heatmap");
      if (!d.cells.length) return void (body.innerHTML = emptyNote("No prediction data."));
      const grid = el("div", "heatmap");
      for (const c of d.cells) {
        const cell = el("div", "cell");
        cell.style.background = scoreColor(c.score);
        cell.innerHTML =
          `<span class="s">${esc(c.sector)}</span>
           <span class="v">${Number(c.score).toFixed(2)}</span>
           <span class="d">${esc(c.direction)} · ${esc(c.confidence)}</span>`;
        grid.appendChild(cell);
      }
      body.innerHTML = "";
      body.appendChild(grid);
    },
  },
  {
    id: "accuracy", title: "Accuracy Tracker",
    async render(body) {
      const d = await getJSON("accuracy");
      const o = d.overall;
      if (!o.scored) return void (body.innerHTML = emptyNote("No scored predictions yet — accuracy appears after the reflection job runs."));
      const rate = o.hit_rate == null ? 0 : o.hit_rate;
      body.innerHTML =
        `<div class="bigstat"><span class="pct">${(rate * 100).toFixed(0)}%</span>
           <span class="lbl">${o.hits}/${o.scored} directional calls correct</span></div>
         <div class="bar"><i style="width:${(rate * 100).toFixed(0)}%"></i></div>`;
      const rows = Object.entries(d.by_sector || {});
      if (rows.length) {
        const t = el("table");
        t.innerHTML = "<tr><th>sector</th><th class='num'>hit-rate</th><th class='num'>n</th></tr>";
        for (const [s, v] of rows) {
          t.insertAdjacentHTML("beforeend",
            `<tr><td>${esc(s)}</td><td class="num">${(v.hit_rate * 100).toFixed(0)}%</td><td class="num dim">${v.scored}</td></tr>`);
        }
        body.appendChild(el("div", null, "").appendChild(t).parentNode);
      }
    },
  },
  {
    id: "leaderboard", title: "US → China Influence",
    async render(body) {
      const d = await getJSON("leaderboard");
      if (!d.rows.length) return void (body.innerHTML = emptyNote("Correlations appear once enough daily data accumulates."));
      const t = el("table");
      t.innerHTML = "<tr><th>US</th><th>→ China</th><th class='num'>corr</th><th class='num'>lag</th><th class='num'>n</th><th></th></tr>";
      for (const r of d.rows.slice(0, 12)) {
        const est = r.established
          ? `<span class="badge est">established</span>`
          : `<span class="badge noisy">n&lt;${d.min_sample}</span>`;
        t.insertAdjacentHTML("beforeend",
          `<tr class="${r.established ? "" : "muted-row"}">
             <td>${esc(r.us_symbol)}</td><td>${esc(r.china_symbol)}</td>
             <td class="num ${signClass(r.correlation)}">${Number(r.correlation).toFixed(2)}</td>
             <td class="num dim">${r.best_lag}d</td>
             <td class="num dim">${r.sample_size}</td><td>${est}</td>
           </tr>`);
      }
      body.innerHTML = "";
      body.appendChild(t);
    },
  },
  {
    id: "weights", title: "Model Weights (vs suggested)",
    async render(body) {
      const d = await getJSON("weights");
      body.innerHTML = "";
      for (const r of d.rows) {
        const cur = Number(r.current_weight), sug = r.suggested_weight;
        const diff = sug == null ? 0 : sug - cur;
        const arrow = Math.abs(diff) < 1e-4 ? "=" : diff > 0 ? "▲" : "▼";
        const cls = Math.abs(diff) < 1e-4 ? "dim" : diff > 0 ? "pos" : "neg";
        const row = el("div", "wrow");
        row.innerHTML =
          `<span class="sig">${esc(r.signal)}</span>
           <span class="bar" style="flex:1"><i style="width:${(cur * 100).toFixed(0)}%"></i></span>
           <span class="num">${cur.toFixed(2)}</span>
           <span class="arrow ${cls}">${arrow} ${sug == null ? "—" : Number(sug).toFixed(2)}</span>`;
        body.appendChild(row);
      }
      body.appendChild(el("div", "rationale",
        `Auto-apply: <b>${d.auto_apply ? "on" : "off (review only)"}</b>`));
    },
  },
  {
    id: "reflections", title: "Reflection Log", wide: true,
    async render(body) {
      const d = await getJSON("reflections");
      if (!d.rows.length) return void (body.innerHTML = emptyNote("Reflections appear after the daily self-improvement pass."));
      body.innerHTML = "";
      for (const r of d.rows.slice(0, 6)) {
        let worked = [], missed = [], adj = {};
        try { worked = JSON.parse(r.signals_that_worked || "[]"); } catch {}
        try { missed = JSON.parse(r.signals_that_missed || "[]"); } catch {}
        try { adj = JSON.parse(r.suggested_adjustment || "{}"); } catch {}
        const card = el("div", "pred");
        card.innerHTML =
          `<div class="pred-top"><span class="sector">${esc(r.trade_date)}</span>
             <span class="conf ${esc(r.reflection_confidence)}">${esc(r.reflection_confidence)} conf</span></div>
           <div class="rationale">
             <span class="pos">✓ ${worked.map(esc).join(", ") || "—"}</span> &nbsp;
             <span class="neg">✗ ${missed.map(esc).join(", ") || "—"}</span><br>
             ${esc(r.likely_reason_for_miss || "")}<br>
             <span class="dim">suggests:</span> ${esc(adj.signal || "—")} → ${esc(adj.direction || "none")} (${esc(adj.magnitude || "")})
           </div>`;
        body.appendChild(card);
      }
    },
  },
  {
    id: "markets", title: "Global Markets (US close)",
    async render(body) {
      const d = await getJSON("markets");
      const all = [...d.indices, ...d.sectors];
      if (!all.length) return void (body.innerHTML = emptyNote("No US market data yet."));
      const t = el("table");
      t.innerHTML = "<tr><th>symbol</th><th>sector</th><th class='num'>close</th><th class='num'>chg</th></tr>";
      for (const r of all) {
        t.insertAdjacentHTML("beforeend",
          `<tr><td>${esc(r.symbol)}</td><td class="dim">${esc(r.sector)}</td>
             <td class="num">${r.close == null ? "—" : Number(r.close).toFixed(2)}</td>
             <td class="num ${signClass(r.pct_change)}">${pct(r.pct_change)}</td></tr>`);
      }
      body.innerHTML = "";
      body.appendChild(t);
    },
  },
  {
    id: "metals", title: "Precious Metals",
    async render(body) {
      const d = await getJSON("markets");
      if (!d.metals.length) return void (body.innerHTML = emptyNote("No metals data yet."));
      const t = el("table");
      t.innerHTML = "<tr><th>metal</th><th class='num'>close</th><th class='num'>chg</th></tr>";
      const label = { "GC=F": "Gold", "SI=F": "Silver" };
      for (const r of d.metals) {
        t.insertAdjacentHTML("beforeend",
          `<tr><td>${esc(label[r.symbol] || r.symbol)}</td>
             <td class="num">${r.close == null ? "—" : Number(r.close).toFixed(2)}</td>
             <td class="num ${signClass(r.pct_change)}">${pct(r.pct_change)}</td></tr>`);
      }
      body.innerHTML = "";
      body.appendChild(t);
    },
  },
  {
    id: "clocks", title: "World Sessions",
    render(body) {
      renderClocks(body);   // synchronous; also ticks itself every second
      return Promise.resolve();
    },
  },
];

// ── world clocks (client-side, no backend) ─────────────────────────────
const CITIES = [
  { city: "New York", tz: "America/New_York" },
  { city: "London", tz: "Europe/London" },
  { city: "Shanghai", tz: "Asia/Shanghai" },
];
function shanghaiSession() {
  const parts = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai", weekday: "short", hour: "2-digit",
    minute: "2-digit", hour12: false,
  }).formatToParts(new Date());
  const get = (t) => parts.find((p) => p.type === t)?.value;
  const wd = get("weekday");
  const mins = parseInt(get("hour")) * 60 + parseInt(get("minute"));
  if (["Sat", "Sun"].includes(wd)) return ["closed", "weekend"];
  if (mins >= 570 && mins < 690) return ["open", "morning session"];   // 09:30–11:30
  if (mins >= 690 && mins < 780) return ["lunch", "lunch break"];       // 11:30–13:00
  if (mins >= 780 && mins < 900) return ["open", "afternoon session"];  // 13:00–15:00
  return ["closed", "closed"];
}
function renderClocks(body) {
  const wrap = el("div", "clocks");
  const now = new Date();
  for (const c of CITIES) {
    const t = new Intl.DateTimeFormat("en-GB", {
      timeZone: c.tz, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(now);
    const clk = el("div", "clk");
    let extra = "";
    if (c.city === "Shanghai") {
      const [st, lbl] = shanghaiSession();
      extra = `<div class="session ${st}">A-share: ${lbl}</div>`;
    }
    clk.innerHTML = `<div class="city">${c.city}</div><div class="t">${t}</div>${extra}`;
    wrap.appendChild(clk);
  }
  body.innerHTML = "";
  body.appendChild(wrap);
}

// ── layout: build panels, drag-to-reorder, persist order ───────────────
function savedOrder() {
  try { return JSON.parse(localStorage.getItem(LS_ORDER)) || []; } catch { return []; }
}
function saveOrder() {
  const ids = [...document.querySelectorAll(".panel")].map((p) => p.dataset.id);
  localStorage.setItem(LS_ORDER, JSON.stringify(ids));
}
function orderedPanels() {
  const order = savedOrder();
  const byId = Object.fromEntries(PANELS.map((p) => [p.id, p]));
  const seen = new Set();
  const out = [];
  for (const id of order) if (byId[id] && !seen.has(id)) { out.push(byId[id]); seen.add(id); }
  for (const p of PANELS) if (!seen.has(p.id)) out.push(p);   // new panels append
  return out;
}

let dragId = null;
function buildGrid() {
  const grid = $("#grid");
  grid.innerHTML = "";
  for (const panel of orderedPanels()) {
    const node = el("section", "panel" + (panel.wide ? " wide" : ""));
    node.dataset.id = panel.id;
    node.innerHTML =
      `<div class="phead" draggable="true">
         <span>${esc(panel.title)}</span><span class="grip">⠿</span>
       </div>
       <div class="pbody">…</div>`;
    grid.appendChild(node);

    const head = $(".phead", node);
    head.addEventListener("dragstart", () => { dragId = panel.id; node.classList.add("dragging"); });
    head.addEventListener("dragend", () => { node.classList.remove("dragging"); saveOrder(); });
    node.addEventListener("dragover", (e) => { e.preventDefault(); node.classList.add("drag-over"); });
    node.addEventListener("dragleave", () => node.classList.remove("drag-over"));
    node.addEventListener("drop", (e) => {
      e.preventDefault();
      node.classList.remove("drag-over");
      const dragged = document.querySelector(`.panel[data-id="${dragId}"]`);
      if (dragged && dragged !== node) {
        const rect = node.getBoundingClientRect();
        const after = e.clientY > rect.top + rect.height / 2;
        node.parentNode.insertBefore(dragged, after ? node.nextSibling : node);
        saveOrder();
      }
    });
  }
}

// ── refresh cycle ──────────────────────────────────────────────────────
async function refreshData() {
  const conn = $("#conn");
  const byId = Object.fromEntries(PANELS.map((p) => [p.id, p]));
  let ok = true;
  await Promise.all([...document.querySelectorAll(".panel")].map(async (node) => {
    const panel = byId[node.dataset.id];
    const body = $(".pbody", node);
    try { await panel.render(body); }
    catch (e) { ok = false; body.innerHTML = `<div class="empty">unavailable — ${esc(e.message)}</div>`; }
  }));
  conn.textContent = ok ? "● live" : "● degraded";
  conn.className = "conn " + (ok ? "ok" : "bad");
  $("#updated").textContent = "updated " + new Date().toLocaleTimeString();
  try {
    const h = await getJSON("health");
    if (h.disclaimer) $("#disclaimer").textContent = h.disclaimer;
  } catch {}
}

function tickClock() {
  $("#clock").textContent = new Date().toLocaleTimeString();
  const clkPanel = document.querySelector('.panel[data-id="clocks"] .pbody');
  if (clkPanel) renderClocks(clkPanel);
}

// ── boot ───────────────────────────────────────────────────────────────
$("#reset").addEventListener("click", () => {
  localStorage.removeItem(LS_ORDER);
  buildGrid();
  refreshData();
});
buildGrid();
refreshData();
tickClock();
setInterval(tickClock, 1000);
setInterval(refreshData, POLL_MS);
