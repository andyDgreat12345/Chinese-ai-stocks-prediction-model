# China Market Oracle

A background system that, on a schedule, ingests U.S. market closes + world
news, runs an analysis/prediction pass, and surfaces a directional read on
Chinese equity markets in a terminal-style dashboard.

> **Not investment advice.** Output is a probabilistic signal for you to weigh
> yourself — not a buy/sell instruction and not a guarantee of returns.

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
  jobs/            # the 6 scheduled jobs (§2) — Phase-1 stubs
  analysis/
    scoring.py     # pure, testable weighted-signal scoring (§4)
  ingestion/       # yfinance / akshare / RSS pulls          (Phase 2)
  reflection/      # scoring, correlation engine, LLM reflection (Phase 7)
  api/             # FastAPI endpoints for the dashboard      (Phase 4)
tests/             # fixture-driven tests (scoring covered)
```

## Quick start

```bash
pip install -r requirements.txt
python -m oracle.db          # create data/oracle.db
python -m pytest -q          # run tests
python -m oracle.scheduler   # start the scheduler (jobs are stubs for now)
```

## Build roadmap (spec §6)

| Phase | What | Status |
|---|---|---|
| 1 | Scaffold: structure, APScheduler, SQLite schema | **done** |
| 2 | Ingestion: `fetch_us_close` / `fetch_world_news` / `fetch_china_close` with retries | next |
| 3 | Analysis engine: weighted scoring on live data | scoring fn + tests done |
| 4 | Local FastAPI: `/api/prediction`, `/api/heatmap`, `/api/history` | todo |
| 5 | Dashboard: terminal UI, fetch from local API, 2 new panels | todo |
| 6 | Scheduler wiring: connect jobs to real functions, mock time-shifted runs | todo |
| 7 | **Reflection loop (§4b)**: scoring + calibration, correlation engine, reflection log | todo (top priority) |
| 8 | Disclaimers: persistent banner + per-prediction caveat | disclaimer wired in config |

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
