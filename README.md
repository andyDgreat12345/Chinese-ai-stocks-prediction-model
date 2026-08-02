# China Market Oracle

A background system that, on a schedule, ingests U.S. market closes + world
news, runs an analysis/prediction pass, and surfaces a directional read on
Chinese equity markets in a terminal-style dashboard.

> **Not investment advice.** Output is a probabilistic signal for you to weigh
> yourself — not a buy/sell instruction and not a guarantee of returns.

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the system runs — operating
model, hosting, backend, and data flow. To deploy, pick one:

- **[GITHUB_PAGES.md](GITHUB_PAGES.md)** — zero-server: GitHub Actions runs the
  jobs on a schedule and publishes the dashboard to GitHub Pages. $0, no box to
  manage (public dashboard).
- **[DEPLOY.md](DEPLOY.md)** — an always-on host (small VPS) with systemd; keeps
  the dashboard private and gives precise job timing.

## Top priority

The **self-improvement / reflection loop** (spec §4b) — comparing each
prediction against the actual result, measuring US→China influence
empirically, and accumulating that into a growing reflection log the system
actually uses — matters more than any single day's prediction. This scaffold
is built so that loop is a **first-class citizen from day one**: predictions
already store their component signals, and the schema already carries the
correlation leaderboard, news-impact table, calibration scores, reflection log,
and a human-in-the-loop weights table.

## Layout

```
oracle/
  config.py        # schedule, data universe, reflection-loop guards, disclaimer
  schema.sql       # SQLite schema — reflection loop (§4b) is first-class
  db.py            # connection + idempotent schema init
  scheduler.py     # APScheduler wiring, 6 cron jobs pinned to CST, SQLite jobstore
  jobs/            # the 6 scheduled jobs (§2); ingestion jobs now wired
  analysis/
    scoring.py     # pure, testable weighted-signal scoring (§4)
    sentiment.py   # lexicon sentiment + category classifier (§4.2, v1)
    pipeline.py    # build_signals (US→China + sentiment) -> predictions (§4)
  ingestion/       # yfinance / akshare / RSS pulls with retries (§3)
    _retry.py      #   exponential-backoff retry helper
    us_market.py   #   US indices/ETFs/metals -> us_close
    china_market.py#   China broad indices + sector ETFs -> china_close
    news.py        #   RSS -> tagged headlines -> news
    macro.py       #   hand-maintained macro calendar -> macro_events
  reflection/      # self-improvement loop (§4b, top priority)
    stats.py       #   pure math: pearson, best-fit lag, calibration/Brier
    scoring.py     #   (i)   prediction scoring vs actual + calibration
    correlation.py #   (ii)  rolling US→China correlation + news-impact table
    reflect.py     #   (iii) reflection log (rule-based; LLM-swappable)
  api/
    server.py      # FastAPI: dashboard + /api/prediction /heatmap /history
                   #   /accuracy /leaderboard /news-impact /reflections
                   #   /weights /markets (§5, §4b)
  dashboard/       # terminal UI (index.html + static/{styles.css,app.js})
app.py             # `python app.py` -> backend + dashboard on localhost:8000
tests/             # fixture-driven tests (scoring, sentiment, ingestion)
```

## Quick start

```bash
pip install -r requirements.txt
python -m oracle.db          # create data/oracle.db
python -m pytest -q          # run tests
cp examples/macro_events.sample.json data/macro_events.json   # optional: seed macro calendar
python app.py                # start backend + terminal dashboard → localhost:8000
python -m oracle.scheduler   # (separately) run the daily cron jobs
```

The dashboard (`oracle/dashboard/`) is a vanilla-JS terminal UI served by the
same FastAPI app: 9 draggable panels (prediction summary, score-colored sector
heatmap, accuracy tracker, US→China leaderboard, weights diff, reflection log,
global markets, precious metals, world-session clocks). It polls the API and
does no compute of its own; panel layout persists in `localStorage`.

## Build roadmap (spec §6)

| Phase | What | Status |
|---|---|---|
| 1 | Scaffold: structure, APScheduler, SQLite schema | **done** |
| 2 | Ingestion: `fetch_us_close` / `fetch_world_news` / `fetch_china_close` with retries | **done** |
| 3 | Analysis engine: weighted scoring on live data | **done** (pipeline wired US→China + sentiment → predictions) |
| 4 | Local FastAPI: `/api/prediction`, `/api/heatmap`, `/api/history`, `/api/accuracy` | **done** |
| 5 | Dashboard: terminal UI, fetch from local API, 2 new panels | **done** (9 panels, drag-reorder, localStorage) |
| 6 | Scheduler wiring: connect jobs to real functions, mock time-shifted runs | **done** (all 6 jobs wired; live cron untested) |
| 7 | **Reflection loop (§4b)**: scoring + calibration, correlation engine, reflection log | **done** (top priority) |
| 8 | Disclaimers: persistent banner + per-prediction caveat | **done** (banner in UI + in every API payload) |

## The reflection loop (§4b — top priority)

Runs at 15:15 CST once the actual China close is in (`reflect_and_update`), as
three separable, individually auditable sub-systems:

1. **Prediction scoring** (`reflection/scoring.py`) — directional hit/miss per
   sector plus a Brier term, so *calibration* is measurable, not just accuracy.
   Sectors with no actual data are left unscored rather than guessed.
2. **US→China influence** (`reflection/correlation.py`) — rolling correlation +
   best-fit lag for every US↔China symbol pair, gated by a ≥30-observation
   guard so noise can't masquerade as a "strong correlation"; plus a
   news-category → China-sector impact table with sample size and variance.
3. **Reflection log** (`reflection/reflect.py`) — a structured daily entry
   (which signals worked/missed, likely reason, suggested weight adjustment) in
   the spec's JSON schema, appended to a persistent JSONL + markdown log. The
   generator is deterministic/offline by default and accepts an `llm` callable
   to upgrade it. **Suggested weight adjustments are logged for review, not
   auto-applied** — `config.AUTO_APPLY_WEIGHT_ADJUSTMENTS` (default off) gates
   that, and `run_analysis` retrieves recent reflections before each prediction.

## Scheduled jobs (all CST — `Asia/Shanghai`)

| Time | Job | Purpose |
|---|---|---|
| 04:15 | `fetch_us_close` | US index/sector closes |
| 04:30 | `fetch_world_news` | overnight RSS headlines |
| 05:00 | `run_analysis` | combine signals → prediction |
| 09:15 | `pre_open_refresh` | re-check breaking news |
| 15:05 | `fetch_china_close` | log actual close vs prediction |
| 15:15 | `reflect_and_update` | self-improvement pass (§4b) |

## Open decisions before Phase 2 (spec §7)

- Always-on host: own machine vs. small VPS (VPS needed for overnight runs).
- News access: Reuters/Caixin RSS are free; Bloomberg may need a workaround.
- v1 sentiment: lexicon-based (fastest) vs. an LLM call per headline batch.
