/* China Market Oracle — terminal dashboard front-end (spec §5).
 * Vanilla JS, no build step, no external libs. Polls the local FastAPI backend;
 * does no compute of its own. Panel order persists in localStorage. */
"use strict";

const LS_ORDER = "cmo.panelOrder.v2";
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

// ── candlestick chart (inline SVG, no libraries) ───────────────────────
const SVG_NS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs) => {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  return n;
};

/* Draw price history + (when present) the forecast cone.
 * Candlesticks are drawn only where real OHLC exists; otherwise a close line —
 * we never fabricate a bar. The cone's width comes from the MEASURED p10/p90 of
 * past outcomes under the same call, so it widens honestly when the edge is weak. */
function drawChart(bars, opts) {
  const W = 640, H = 200, PAD_L = 4, PAD_R = 54, PAD_T = 8, PAD_B = 16;
  const svg = svgEl("svg", {
    viewBox: `0 0 ${W} ${H}`, class: "chart", preserveAspectRatio: "none",
  });
  if (!bars.length) return svg;

  const cone = opts && opts.forecast && opts.forecast.enough ? opts.forecast : null;
  const last = bars[bars.length - 1].c;
  // Value range includes the cone so it never clips.
  let lo = Infinity, hi = -Infinity;
  for (const b of bars) {
    lo = Math.min(lo, b.l != null ? b.l : b.c);
    hi = Math.max(hi, b.h != null ? b.h : b.c);
  }
  if (cone) {
    lo = Math.min(lo, last * (1 + cone.p10_move_pct / 100));
    hi = Math.max(hi, last * (1 + cone.p90_move_pct / 100));
  }
  const span = hi - lo || 1;
  lo -= span * 0.05; hi += span * 0.05;
  // Reserve the last ~8% of the x-axis for the forecast step.
  const plotW = W - PAD_L - PAD_R;
  const bodyW = cone ? plotW * 0.92 : plotW;
  const n = bars.length;
  const x = (i) => PAD_L + (n <= 1 ? bodyW / 2 : (i / (n - 1)) * bodyW);
  const y = (v) => PAD_T + (1 - (v - lo) / (hi - lo)) * (H - PAD_T - PAD_B);
  const step = n > 1 ? bodyW / (n - 1) : 6;
  const cw = Math.max(1.2, Math.min(6, step * 0.62));

  if (opts && opts.hasOhlc) {
    for (let i = 0; i < n; i++) {
      const b = bars[i];
      if (b.o == null || b.h == null || b.l == null) continue;
      const up = b.c >= b.o;
      const cls = up ? "up" : "down";
      svg.appendChild(svgEl("line", {                    // wick
        x1: x(i), x2: x(i), y1: y(b.h), y2: y(b.l), class: `wick ${cls}`,
      }));
      const top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c));
      svg.appendChild(svgEl("rect", {                    // body
        x: x(i) - cw / 2, y: top, width: cw,
        height: Math.max(1, bot - top), class: `candle ${cls}`,
      }));
    }
  } else {
    svg.appendChild(svgEl("path", {
      d: bars.map((b, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(b.c).toFixed(1)}`).join(""),
      class: "priceline",
    }));
  }

  if (cone) {
    const xNow = x(n - 1), xEnd = W - PAD_R;
    const yHi = y(last * (1 + cone.p90_move_pct / 100));
    const yLo = y(last * (1 + cone.p10_move_pct / 100));
    const yMid = y(last * (1 + cone.median_move_pct / 100));
    const yNow = y(last);
    svg.appendChild(svgEl("path", {                      // p10–p90 cone
      d: `M${xNow},${yNow} L${xEnd},${yHi} L${xEnd},${yLo} Z`,
      class: `cone ${cone.direction}`,
    }));
    svg.appendChild(svgEl("line", {                      // median path
      x1: xNow, y1: yNow, x2: xEnd, y2: yMid, class: `cone-mid ${cone.direction}`,
    }));
  }
  return svg;
}

/* Two rebased % series on one axis (US vs China), already lag-aligned server-side
 * so a genuine lead shows up as the lines tracking each other. */
function drawPair(us, china) {
  const W = 640, H = 150, PAD = 6, PAD_B = 14;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart pairchart",
                             preserveAspectRatio: "none" });
  const all = [...us.map((p) => p.v), ...china.map((p) => p.v)];
  if (!all.length) return svg;
  let lo = Math.min(...all), hi = Math.max(...all);
  const span = (hi - lo) || 1;
  lo -= span * 0.08; hi += span * 0.08;
  const n = Math.max(us.length, china.length);
  const x = (i) => PAD + (n <= 1 ? 0 : (i / (n - 1)) * (W - PAD * 2));
  const y = (v) => PAD + (1 - (v - lo) / (hi - lo)) * (H - PAD - PAD_B);
  const path = (pts, cls) => {
    if (!pts.length) return;
    svg.appendChild(svgEl("path", {
      d: pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(""),
      class: cls,
    }));
  };
  // zero line, so "which one is up on the window" reads instantly
  if (lo < 0 && hi > 0) {
    svg.appendChild(svgEl("line", { x1: PAD, x2: W - PAD, y1: y(0), y2: y(0),
                                    class: "zeroline" }));
  }
  path(us, "usline");
  path(china, "cnline");
  return svg;
}

/* Hub chart: one US series (thick amber) with every China series it leads,
 * each in its own hue so they're distinguishable, all rebased to % and already
 * lag-aligned server-side. */
const HUB_HUES = [190, 145, 275, 25, 330, 95, 215, 55];

function drawHub(usSeries, legs) {
  const W = 640, H = 190, PAD = 6, PAD_B = 14;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart hubchart",
                             preserveAspectRatio: "none" });
  const all = [...usSeries.map((p) => p.v)];
  for (const l of legs) for (const p of l.china) all.push(p.v);
  if (!all.length) return svg;
  let lo = Math.min(...all), hi = Math.max(...all);
  const span = (hi - lo) || 1;
  lo -= span * 0.08; hi += span * 0.08;
  const n = Math.max(usSeries.length, ...legs.map((l) => l.china.length));
  const x = (i) => PAD + (n <= 1 ? 0 : (i / (n - 1)) * (W - PAD * 2));
  const y = (v) => PAD + (1 - (v - lo) / (hi - lo)) * (H - PAD - PAD_B);
  const draw = (pts, attrs) => {
    if (!pts.length) return;
    svg.appendChild(svgEl("path", {
      d: pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.v).toFixed(1)}`).join(""),
      fill: "none", ...attrs,
    }));
  };
  if (lo < 0 && hi > 0) {
    svg.appendChild(svgEl("line", { x1: PAD, x2: W - PAD, y1: y(0), y2: y(0),
                                    class: "zeroline" }));
  }
  legs.forEach((l, i) => draw(l.china, {
    stroke: `hsl(${HUB_HUES[i % HUB_HUES.length]}, 70%, 60%)`, "stroke-width": 1.4,
  }));
  draw(usSeries, { class: "usline", "stroke-width": 2.4 });   // the lead, on top
  return svg;
}

/* Equity curve: a filled area against the starting-capital baseline, so time
 * spent underwater is visible rather than hidden by autoscaling. */
function drawEquity(curve, start) {
  const W = 640, H = 150, PAD = 6, PAD_B = 12;
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart",
                             preserveAspectRatio: "none" });
  if (!curve.length) return svg;
  const vals = curve.map((p) => p[1]);
  let lo = Math.min(...vals, start), hi = Math.max(...vals, start);
  const span = (hi - lo) || 1;
  lo -= span * 0.08; hi += span * 0.08;
  const n = curve.length;
  const x = (i) => PAD + (n <= 1 ? 0 : (i / (n - 1)) * (W - PAD * 2));
  const y = (v) => PAD + (1 - (v - lo) / (hi - lo)) * (H - PAD - PAD_B);
  svg.appendChild(svgEl("line", { x1: PAD, x2: W - PAD, y1: y(start), y2: y(start),
                                  class: "zeroline" }));
  const d = curve.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");
  const last = vals[vals.length - 1];
  svg.appendChild(svgEl("path", {
    d: `${d} L${x(n - 1).toFixed(1)},${y(start).toFixed(1)} L${x(0).toFixed(1)},${y(start).toFixed(1)} Z`,
    fill: last >= start ? "rgba(53,255,158,.12)" : "rgba(255,95,109,.12)", stroke: "none" }));
  svg.appendChild(svgEl("path", { d, fill: "none",
    stroke: last >= start ? "var(--green)" : "var(--red)", "stroke-width": 1.6 }));
  return svg;
}

// Research endpoints all return a preformatted `report` plus structured
// fields. Rendering the report verbatim keeps one wording in one place: the
// text a reader sees on the dashboard is the same text the scheduled job wrote
// to oracle-state, so the two can never drift into telling different stories.
function researchPanel(endpoint, note) {
  return async function render(body) {
    let d;
    try { d = await getJSON(endpoint); }
    catch (e) { return void (body.innerHTML = emptyNote(`unavailable (${esc(String(e.message || e))})`)); }
    if (d.error) return void (body.innerHTML = emptyNote(`unavailable — ${esc(d.error)}`));
    const text = d.report || "";
    if (!text.trim()) return void (body.innerHTML = emptyNote("not measured yet"));
    body.innerHTML = "";
    if (note) body.appendChild(el("div", "dim", esc(note)));
    const pre = el("pre", "research");
    pre.textContent = text;              // textContent, never innerHTML
    body.appendChild(pre);
  };
}

// ── panel registry ─────────────────────────────────────────────────────
// Each panel: { id, title, wide?, render(bodyEl) -> Promise }
const PANELS = [
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
    id: "paper", title: "Forward Ledger — the one number not fitted", wide: true,
    render: researchPanel("paper",
      "Recorded before each outcome was known. Both settlement legs are kept "
      + "because the T+1 question is unresolved."),
  },
  {
    id: "exit-horizon", title: "Exit Horizon — how long to hold", wide: true,
    render: researchPanel("exit-horizon",
      "Where the market's return actually accrues, and whether holding "
      + "overnight helps. It does not."),
  },
  {
    id: "execution", title: "Execution Realism — settlement and slippage", wide: true,
    render: researchPanel("execution",
      "Whether the validated rule can be placed at all, and how much bad fill "
      + "it survives."),
  },
  {
    id: "regimes", title: "Regime Robustness — broad, or one lucky corner?", wide: true,
    render: researchPanel("regimes",
      "Not a search. No bucket here is ever selected to trade."),
  },
  {
    id: "segments", title: "K-line Segments — where the edge sits", wide: true,
    render: researchPanel("segments"),
  },
  {
    id: "simulation", title: "Trader Simulation (paper)", wide: true,
    async render(body) {
      const d = await getJSON("simulation");
      if (!d.available) return void (body.innerHTML = emptyNote(d.reason || "unavailable"));
      const r = d.rules || {};
      const pc = (v) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${Number(v).toFixed(2)}%`);
      body.innerHTML = "";
      body.appendChild(el("div", "dim",
        `conviction ≥ ${esc(r.min_conviction)} · risk ${r.risk_per_trade_pct}%/trade · ` +
        `stop ${r.stop_loss_pct}% / target ${r.take_profit_pct}% · max ${r.max_positions} positions · ` +
        `hold ≤ ${r.max_hold_days}d · ${r.cost_bps}bps round trip · ` +
        (r.allow_short ? "shorting on" : "long-only")));

      const beat = d.beat_buy_and_hold;
      body.appendChild(el("div", "bigstat",
        `<span class="pct ${d.return_pct >= 0 ? "" : "neg"}">${pc(d.return_pct)}</span>
         <span class="lbl">over ${d.sessions} sessions · buy &amp; hold ${pc(d.buy_and_hold_return_pct)}
           · <b class="${beat ? "pos" : "neg"}">${beat ? "beat" : "did not beat"} buy &amp; hold</b></span>`));

      body.appendChild(drawEquity(d.equity_curve || [], d.starting_cash));

      const t = el("table");
      t.innerHTML =
        `<tr><th>trades</th><th class="num">win rate</th><th class="num">avg win</th>
             <th class="num">avg loss</th><th class="num">profit factor</th>
             <th class="num">max DD</th></tr>
         <tr><td>${d.n_trades}</td>
             <td class="num">${d.win_rate == null ? "—" : (d.win_rate * 100).toFixed(0) + "%"}</td>
             <td class="num pos">${pc(d.avg_win_pct)}</td>
             <td class="num neg">${pc(d.avg_loss_pct)}</td>
             <td class="num">${d.profit_factor ?? "—"}</td>
             <td class="num neg">${pc(d.max_drawdown_pct)}</td></tr>`;
      body.appendChild(t);

      const ex = Object.entries(d.exit_reasons || {})
        .map(([k, v]) => `${esc(k)} ${v}`).join(" · ");
      body.appendChild(el("div", "rationale", `exits: ${ex || "—"}`));
      body.appendChild(el("div", "rationale",
        "Paper only. Inputs are lookahead-free (each session decided from the PRIOR " +
        "US close), but live fills, taxes and regime change are not modeled. " +
        "Not investment advice."));
    },
  },
  {
    id: "charts", title: "Sector Charts + Forecast Cone", wide: true,
    async render(body) {
      const d = await getJSON("charts");
      const secs = d.sectors || {};
      if (!Object.keys(secs).length) return void (body.innerHTML = emptyNote("No price history yet."));
      body.innerHTML = "";
      for (const [sector, s] of Object.entries(secs)) {
        const wrap = el("div", "chartbox");
        const f = s.forecast;
        const head = el("div", "pred-top");
        head.innerHTML =
          `<span class="sector">${esc(sector)}</span>
           <span class="badge">${esc(s.symbol)}</span>
           ${s.call ? tag(s.call) : ""}
           <span class="repdir dim">${esc(s.technical_note || "")}</span>`;
        wrap.appendChild(head);
        wrap.appendChild(drawChart(s.bars || [], { hasOhlc: s.has_ohlc, forecast: f }));
        const note = el("div", "rationale");
        if (f && f.enough) {
          note.innerHTML =
            `next session (from ${f.n} past days with this same call): median ` +
            `<b>${f.median_move_pct > 0 ? "+" : ""}${f.median_move_pct}%</b>, ` +
            `10–90% range ${f.p10_move_pct}% to ${f.p90_move_pct}%` +
            (f.hit_rate != null ? ` · ${(f.hit_rate * 100).toFixed(0)}% right` : "");
        } else {
          note.textContent = s.has_ohlc
            ? "No forecast range yet — not enough scored history for this call."
            : "Close-only history (no candles yet) — re-run the backfill to load OHLC.";
        }
        wrap.appendChild(note);
        body.appendChild(wrap);
      }
      body.appendChild(el("div", "rationale",
        "The cone is the measured 10–90% range of what actually happened on past days " +
        "with this call — not a predicted candlestick. We have no intraday data, so a " +
        "drawn future bar would be invented precision."));
    },
  },
  {
    id: "proven-hubs", title: "Proven Correlations — US hub → China", wide: true,
    async render(body) {
      const d = await getJSON("proven-hubs");
      if (!d.hubs || !d.hubs.length) {
        return void (body.innerHTML = emptyNote(
          "No proven pairs registered yet — run the research sweep (Actions → Oracle → phase: research)."));
      }
      body.innerHTML = "";
      body.appendChild(el("div", "dim",
        `${d.n_pairs} pairs survived the sweep · each refreshed every reflection round`));
      for (const hub of d.hubs) {
        const box = el("div", "chartbox");
        const ul = hub.us_label || {};
        const head = el("div", "pred-top");
        head.innerHTML =
          `<span class="sector">${esc(hub.us_symbol)}</span>
           <span class="badge">${esc(ul.name || "")}</span>
           <span class="badge noisy">${esc(ul.sector || "")}</span>
           ${ul.company ? `<span class="badge">${esc(ul.company)}</span>` : ""}
           <span class="repdir dim">leads ${hub.n_counterparts} China instrument(s)</span>`;
        box.appendChild(head);
        box.appendChild(drawHub(hub.legs[0] ? hub.legs[0].us : [], hub.legs));

        // Legend: one row per China counterpart, with its own colour + label.
        const t = el("table");
        t.innerHTML = "<tr><th></th><th>China instrument</th><th>sector</th>" +
          "<th class='num'>lag</th><th class='num'>r (now)</th>" +
          "<th class='num'>r (found)</th><th class='num'>refreshes</th></tr>";
        hub.legs.forEach((l, i) => {
          const cl = l.china_label || {};
          const hue = HUB_HUES[i % HUB_HUES.length];
          const cur = l.current_r == null ? "—" : Number(l.current_r).toFixed(3);
          t.insertAdjacentHTML("beforeend",
            `<tr>
               <td><span style="color:hsl(${hue},70%,60%)">━</span></td>
               <td>${esc(cl.name || l.china_symbol)} <span class="dim">${esc(l.china_symbol)}</span>
                   ${cl.company ? `<span class="badge">${esc(cl.company)}</span>` : ""}</td>
               <td class="dim">${esc(cl.sector || "")}</td>
               <td class="num">${l.lag}d</td>
               <td class="num ${l.current_r >= 0 ? "pos" : "neg"}">${cur}</td>
               <td class="num dim">${Number(l.r_discovered).toFixed(3)}</td>
               <td class="num dim">${l.refresh_count}</td>
             </tr>`);
        });
        box.appendChild(t);
        box.appendChild(el("div", "rationale",
          `<span class="usline-key">━</span> ${esc(hub.us_symbol)} (US lead, shifted forward by its lag) · ` +
          "China lines coloured per the legend · all rebased to % from the window start"));
        body.appendChild(box);
      }
      body.appendChild(el("div", "rationale",
        "\"r (now)\" is re-measured every reflection round — a decaying link shows up " +
        "as it falls away from \"r (found)\". Not investment advice."));
    },
  },
  {
    id: "pairs", title: "US → China Lead/Lag (paired K-lines)", wide: true,
    async render(body) {
      const d = await getJSON("pairs");
      const ps = d.pairs || [];
      if (!ps.length) return void (body.innerHTML = emptyNote("No established correlations yet."));
      body.innerHTML = "";
      const t = d.timing || {};
      body.appendChild(el("div", "dim",
        `China closes ${esc(t.china_close_utc)} UTC · US closes ${esc(t.us_close_utc)} UTC ` +
        `(~${t.hours_us_after_china}h later)`));
      for (const p of ps) {
        const box = el("div", "chartbox");
        const badge = p.predictive
          ? `<span class="badge est">lag ${p.best_lag}d · tradeable</span>`
          : `<span class="badge noisy">lag 0 · not tradeable</span>`;
        const head = el("div", "pred-top");
        head.innerHTML =
          `<span class="sector">${esc(p.us_symbol)} → ${esc(p.china_symbol)}</span>
           <span class="badge">${esc((p.us_label||{}).name || "")} → ${esc((p.china_label||{}).name || "")}</span>
           <span class="badge noisy">${esc((p.china_label||{}).sector || "")}</span>
           ${badge}
           <span class="repdir ${p.correlation >= 0 ? "pos" : "neg"}">r=${Number(p.correlation).toFixed(3)}
             <span class="dim">n=${p.sample_size}, ${p.window_days}d window</span></span>`;
        box.appendChild(head);
        box.appendChild(drawPair(p.us || [], p.china || []));
        box.appendChild(el("div", "rationale",
          `<span class="usline-key">━</span> ${esc(p.us_symbol)} (US) &nbsp; ` +
          `<span class="cnline-key">━</span> ${esc(p.china_symbol)} (China) — both rebased to % ` +
          `from the window start${p.best_lag ? `, US shifted forward ${p.best_lag}d` : ""}`));
        box.appendChild(el("div", "rationale " + (p.predictive ? "" : "warn"), esc(p.lag_note)));
        body.appendChild(box);
      }
      body.appendChild(el("div", "rationale",
        "Tradeable (lag ≥ 1) pairs are listed first on purpose: the biggest raw " +
        "correlations here are same-day, and same-day cannot be acted on."));
    },
  },
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
    id: "learning", title: "Self-Improvement (learning)", wide: true,
    async render(body) {
      const d = await getJSON("learning");
      const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(0)}%`);
      body.innerHTML = "";
      body.appendChild(el("div", "dim",
        `${d.n_adopted} of ${d.n_attempts} tuning attempts improved on a ` +
        `${d.holdout_days}-day holdout the search never saw` +
        (d.enabled ? "" : " · learning DISABLED")));

      const params = d.params || {};
      if (Object.keys(params).length) {
        const t = el("table");
        t.innerHTML = "<tr><th>sector</th><th class='num'>us</th><th class='num'>sent</th>" +
          "<th class='num'>rsi</th><th class='num'>mom</th><th class='num'>trend</th>" +
          "<th class='num'>thresh</th></tr>";
        for (const [s, p] of Object.entries(params).sort()) {
          const f = (k) => Number(p[k] || 0).toFixed(2);
          t.insertAdjacentHTML("beforeend",
            `<tr><td>${esc(s)}</td><td class="num">${f("us_spillover")}</td>
               <td class="num">${f("sentiment")}</td><td class="num">${f("rsi_signal")}</td>
               <td class="num">${f("momentum_signal")}</td><td class="num">${f("trend_signal")}</td>
               <td class="num">${Number(p.threshold || 0).toFixed(3)}</td></tr>`);
        }
        body.appendChild(t);
      } else {
        body.appendChild(el("div", "empty", "No parameters tuned yet — running on the hand-set defaults."));
      }

      for (const h of (d.history || []).slice(0, 8)) {
        const up = h.hit_after != null && h.hit_before != null && h.hit_after > h.hit_before;
        const cls = h.adopted ? "pos" : "dim";
        const row = el("div", "pred");
        row.innerHTML =
          `<div class="pred-top">
             <span class="sector">${esc(h.sector)}</span>
             <span class="${cls}">${h.adopted ? "✓ adopted" : "· kept"}</span>
             <span class="repdir ${up ? "pos" : "dim"}">holdout ${pct(h.hit_before)} → ${pct(h.hit_after)}</span>
           </div>
           <div class="rationale">${esc(h.run_date)} · n=${h.n_holdout} · ${esc(h.reason)}</div>`;
        body.appendChild(row);
      }
      body.appendChild(el("div", "rationale",
        "A change is adopted only when it beats the incumbent out-of-sample — most runs correctly refuse."));
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
