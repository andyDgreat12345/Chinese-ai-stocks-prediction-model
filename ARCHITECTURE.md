# Architecture & Operations

How China Market Oracle runs — the operating model, hosting, data flow, and
backend. Companion to the [README](README.md) (build roadmap) and the project
spec (`chinamarketoraclespec.md`).

> **Not investment advice.** Every output is a probabilistic signal to weigh
> yourself, never a buy/sell instruction or a guarantee.

---

## 1. What it is

A **batch system on a clock**, not a live app. On a fixed daily schedule it
ingests U.S. market closes + world news, produces a directional read on Chinese
equity sectors, then — after the China close — scores itself and writes a
reflection it uses to improve. No live compute in the request path.

The design priority (spec §4b) is the **self-improvement loop**: measuring
US→China influence empirically and accumulating a reflection log the system
actually reads back. That is what makes month 6 better than month 1.

---

## 2. One day in the life (all times CST)

```
04:00  (US markets close)
04:15  fetch_us_close      yfinance: indices + sector ETFs + metals   → us_close
04:30  fetch_world_news    RSS: headline + 1st paragraph, tag         → news
05:00  run_analysis        read us_close+news+macro, pull recent
                           reflections, score each China sector       → predictions
09:15  pre_open_refresh    (STUB) re-check breaking news, nudge confidence
09:30  (China opens) ─────────────────────────────────────────────
15:00  (China closes)
15:05  fetch_china_close   akshare: broad indices + sector ETFs       → china_close
15:15  reflect_and_update  (i)  score vs actual + calibration         → prediction_scores
                           (ii) rolling US→China correlation +
                                news-impact table                     → correlations,
                                                                        news_impact
                           (iii) structured reflection                → reflection_log
                                                                        (+ JSONL + .md)
```

The loop is closed: tomorrow's `run_analysis` reads the reflections written
today (spec §4b-iii).

---

## 3. Where it runs

Everything is **local to one host** — no cloud services, no managed database.
The stack is Python + SQLite + a web server.

| Host option | Notes |
|---|---|
| Laptop, always-on | Free, but the US close is ~04:00 CST — the machine must be awake overnight. Fragile. |
| **Small VPS (~$5–6/mo)** | **Recommended.** Runs through the overnight window, survives reboots. SQLite on one node is ample. |

The **GitHub repo is the source of truth**; the running instance is a clone on
that host. Deploy: `git clone` → `pip install -r requirements.txt` → run the two
long-lived processes (below) under `systemd` so they auto-restart.

---

## 4. Backend — process model

Two processes share one SQLite file (`data/oracle.db`):

```
┌───────────────────────────┐         ┌───────────────────────────┐
│ Process A: SCHEDULER       │  write  │ Process B: API server     │
│ python -m oracle.scheduler │ ──────► │ uvicorn oracle.api.server │
│ APScheduler runs 6 jobs;   │ SQLite  │ FastAPI, READ-ONLY;       │
│ persistent SQLite jobstore │ ◄────── │ serves /api/* JSON        │
│ (survives restarts)        │  read   └─────────────┬─────────────┘
└────────────────────────────┘                       │ HTTP poll
                                                      ▼
                                          ┌───────────────────────┐
                                          │ Dashboard (static     │
                                          │ front — Phase 5 TODO) │
                                          └───────────────────────┘
```

- **SQLite is the contract.** The scheduler is the only writer; the API is a
  pure reader — no compute in the request path (spec §5).
- **No queue / Redis / external DB** — deliberate; a solo project doesn't need
  the orchestration overhead.
- Jobs **fail soft**: a flaky feed logs and returns 0 rather than crashing the
  scheduler. Network calls retry with exponential backoff (2/4/8/16s).

---

## 5. Capabilities ("skills")

Each is an isolated, independently testable module.

| Capability | Module |
|---|---|
| US-market ingestion | `oracle/ingestion/us_market.py` |
| China ingestion (indices + sector ETFs) | `oracle/ingestion/china_market.py` |
| News ingestion | `oracle/ingestion/news.py` |
| Sentiment + categorisation | `oracle/analysis/sentiment.py` |
| Signal scoring | `oracle/analysis/scoring.py`, `pipeline.py` |
| US→China influence engine | `oracle/reflection/correlation.py`, `stats.py` |
| Self-scoring + calibration | `oracle/reflection/scoring.py` |
| Reflection log | `oracle/reflection/reflect.py` |

**On LLMs (spec §7b):** the system uses **zero external LLM calls** today —
fully deterministic and offline. LLMs are deliberately kept *out* of the
prediction hot path (no model "inventing" stock research). The one place an LLM
is worth it is the daily *reflection*; `generate_reflection(llm=...)` is the
seam to drop in a Sonnet-tier call later. Cheap high-throughput fan-out
(DeepSeek-style) only matters if per-ticker research across many stocks is added
— not needed for v1.

---

## 6. Input → Output

**Inputs** (always pulled by code, never invented by a model):

| Data | Source |
|---|---|
| US indices / sector ETFs / metals | `yfinance` |
| China broad indices + sector ETFs | `akshare` |
| World news (headline + 1st paragraph) | RSS via `feedparser` |
| Macro events (Fed/CPI/PMI) | manual / `akshare` (table present, ingestion TODO) |

**Outputs** — served from SQLite via the API, plus files on disk:

| Endpoint | Content |
|---|---|
| `GET /api/prediction` | per-sector direction + confidence + rationale + disclaimer |
| `GET /api/heatmap` | sectors coloured by *predicted* score (not raw price) |
| `GET /api/accuracy` | rolling hit-rate (honest `null` until scored) |
| `GET /api/leaderboard` | measured US→China correlations (established vs noisy) |
| `GET /api/news-impact` | news-category → typical China-sector move + variance |
| `GET /api/weights` | current vs. suggested weights (self-correction diff) |
| `GET /api/reflections` | daily reflection entries |
| `GET /api/history` | past predictions joined with actual outcomes |

Files: `data/reflection_log.jsonl` + `data/reflections/<date>.md` — the
human-readable accumulation.

---

## 7. News pipeline

**Sources:** Reuters + Caixin RSS (free, configured). Bloomberg needs a
workaround → deferred. Caixin/Sina are strong free Chinese-language options.

**Flow:** headline + first paragraph → lexicon sentiment (−1..1, with negation)
+ category (fed / chip-export / tariffs / stimulus / earnings / macro). This
feeds two distinct things:

1. **Same-day signal** — sentiment per sector enters `run_analysis` scoring.
2. **Empirical learning** — the `news_impact` table accumulates "when category X
   appeared, China sector Y moved Z% on average," with sample size + variance.

v1 sentiment is lexicon-based (fast, free, offline). Upgrade path: swap an
LLM-per-batch classifier behind `analyze()`.

---

## 8. The self-improvement loop (spec §4b — top priority)

Runs at 15:15 as three separable, individually auditable sub-systems:

1. **Prediction scoring** — directional hit/miss per sector + a Brier term, so
   *calibration* is measurable, not just accuracy. Sectors with no actual data
   are left unscored, never guessed.
2. **US→China influence** — rolling Pearson correlation + best-fit lag
   (same-day vs next-day) for every US↔China symbol pair, gated by a
   ≥30-observation guard so noise can't pose as a strong correlation; plus the
   news-impact table.
3. **Reflection log** — a structured daily entry (signals worked/missed, likely
   reason, suggested weight adjustment, confidence) in the spec's JSON schema.
   **Suggested weight adjustments are logged for review, not auto-applied** —
   `config.AUTO_APPLY_WEIGHT_ADJUSTMENTS` (default off) gates that.

---

## 9. Known gaps

- **`pre_open_refresh` (09:15) is a stub** — the breaking-news re-check isn't
  implemented yet.
- **Macro-event ingestion** is unwired (the table + scoring hook exist).
- **Dashboard (Phase 5)** — the API serves everything the panels need; the
  terminal UI itself is not built.
- **Sector-ETF codes** in `config.CHINA_SECTOR_ETFS` should be verified against
  live akshare on first run; ingestion fails soft, so a wrong code just leaves
  that sector unscored until corrected.
