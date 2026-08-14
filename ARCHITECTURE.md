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
04:30  fetch_world_news    RSS: headline + 1st paragraph, tag; load
                           macro calendar (Fed/CPI/PMI)               → news, macro_events
05:00  run_analysis        read us_close+news+macro, pull recent
                           reflections, score each China sector       → predictions
09:15  pre_open_refresh    re-fetch breaking news, adjust prediction
                           confidence (direction preserved)           → predictions
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

**On LLMs (spec §7b):** the system is **fully deterministic and offline by
default** — no LLM in the prediction hot path (no model "inventing" stock
research). The one place an LLM is worth it is the daily *reflection*, and that
seam is now wired: `reflection/llm.py` provides an **optional, provider-agnostic
backend** (Claude via the official SDK, or DeepSeek via its OpenAI-compatible
endpoint) selected by `ORACLE_LLM_PROVIDER`. It only *interprets* the real
predicted-vs-actual data it's handed — it never invents numbers — and any
failure (or no key) falls back to the deterministic rule-based generator, so the
loop never depends on the LLM. Cheap high-throughput fan-out (DeepSeek across
many tickers) still only matters if per-ticker research is added later.

**Enable it** by setting `ORACLE_LLM_PROVIDER=claude` (+ `ANTHROPIC_API_KEY`) or
`=deepseek` (+ `DEEPSEEK_API_KEY`). Optional `ORACLE_LLM_MODEL` overrides the
model — Claude defaults to `claude-opus-5`; the spec notes Sonnet-tier is
enough, so `ORACLE_LLM_MODEL=claude-sonnet-5` is a cheaper choice.

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

**Sources:** six feeds, all verified live by item count and pubDate rather than
HTTP status — three English (FT markets, SCMP economy, Guardian business) and
three Chinese-language (Eastmoney market + stock, Chinanews finance).

The Chinese feeds carry most of the weight, and the reason is structural:
A-shares are ~97% domestically owned and >80% retail by volume, and domestic
investors read domestic media. The English feeds describe this market to the
wrong audience.

> The original two feeds (Reuters, Caixin Global) were both **dead** — Reuters
> retired its public RSS endpoint and the Caixin URL 404s. Because ingestion
> fails soft per source, nothing errored; the news table simply sat at zero rows
> for months while the job reported success. Verify a feed by what it returns,
> never by whether the request succeeded.

**Flow:** headline + first paragraph → `analyze_any()` routes by script →
lexicon sentiment (−1..1) + category. Chinese scoring is segmentation-free:
substring matching, longest-first with position consumption so 涨停 is never also
counted as 涨, with negation and intensity read positionally from the characters
before a match. Both engines emit **one shared category vocabulary**, including
the `general` fallback — a divergence there silently splits one bucket in two.

This feeds two things:

1. **Same-day signal** — sentiment per sector enters `run_analysis` scoring,
   gated by the learner's coverage floor so a new feed cannot steer predictions
   before it has history.
2. **Empirical learning** — the `news_impact` table accumulates "when category X
   appeared, China sector Y moved Z% on the NEXT session" (next, not same — the
   same-day join was a lookahead bug, since news stamped D is fetched after
   china_close[D] has printed).

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

## 9. Measurement discipline

The distinguishing property of this system is not its predictions — it is that
it can tell when they are wrong, and has repeatedly done so at its own expense.

Guards that exist because something got past them once:

| Guard | What it caught |
| --- | --- |
| `_prior_us_rows` / `_prior_news_rows` | three lookahead bugs pairing a session with data that postdated it |
| `live_signals` coverage gate | a signal non-zero on 1 day of 369 counting as weightable |
| `pin_dead_signals` | a hand-set weight that would have steered live calls the moment its feed woke |
| `order_sensitivity` | a +65% headline that was an artifact of config ordering |
| `implausible_moves` + per-board limits | unadjusted share conversions read as −74% market moves |
| bar-date stamping | Saturday "sessions" invented by the fetch clock |
| `TRADEABLE_FROM_OPEN` | a 71% hit rate on a segment no entry can capture |

The pattern worth knowing when extending this: **a component that fails soft
fails silently.** Every one of the data bugs above sat behind a job that reported
success. When adding a source, add the check that proves it produced something —
item count and freshness, never HTTP status.

## 10. Status & remaining work

**The spine is complete and running.** Ingest → predict → refresh → score →
measure → learn → reflect → display, on GitHub Actions, with 418 tests in CI.

Remaining items, honestly stated:

- **The tradeable edge is unproven.** 52% bet-accuracy is significant but sits
  in the overnight gap; the segment actually traded is 49.3%. The mean-reversion
  rule is the strongest candidate and needs forward evidence (`oracle/paper.py`).
- **The Chinese news layer is measured but not wired.** It enters the learner
  automatically once coverage clears 5%.
- **The debate analyst is off** pending its head-to-head verdict.
- **Macro ingestion is file-based.**

