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
    llm_analyst.py # optional AI research desk: LLM buy/sell/hold calls (§4.2)
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
    llm.py         #   optional reflection LLM (Claude / DeepSeek, off by default)
  api/
    server.py      # FastAPI: dashboard + /api/prediction /heatmap /history
                   #   /accuracy /leaderboard /news-impact /reflections
                   #   /weights /markets (§5, §4b)
  dashboard/       # terminal UI (index.html + static/{styles.css,app.js})
  backfill.py      # one-shot historical loader (US + China) for the backtest
  backtest.py      # evaluation engine: replay history, baselines, Sharpe, p-value
  costsim.py       # cost-aware net returns: friction, drawdown, break-even
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

## Evaluating prediction ability (before any real money)

Real depth is proven by measurement, not asserted. The backtest engine
(`oracle/backtest.py`) replays whatever historical data is in the DB through the
*current* model and reports it **against naive baselines** so you can see if
there's a real edge:

```bash
python -m oracle.backfill 180      # load ~180 days of real US + China history
python -m oracle.backtest          # then judge the model against the baselines
```

It reports, per strategy (model vs. always-bullish / US-direction / persistence):
directional accuracy, **paper-trading return + annualized Sharpe**, and a
**binomial p-value** (is a >50% hit-rate real or luck?), plus a model
**calibration** table (does "high confidence" actually track accuracy?). The
model only earns its keep if it beats the baselines on bet-accuracy *and* Sharpe
with a small p-value. This is the honest gate before trusting it with money —
and it makes every future data source or research agent a measured experiment.

### Cost-aware net returns (`oracle/costsim.py`)

Gross accuracy is not money. The cost-aware layer replays the same bets through
a friction model — commission + slippage (default 15 bps round trip), equal-weight
position sizing, and **compounded** daily returns — and reports what actually
matters: **net** return, **net** Sharpe, **maximum drawdown**, and the
**break-even friction** (how expensive trading can get before the edge is eaten;
a small number means a fragile edge). It runs automatically as a section of
`python -m oracle.backtest`, or standalone to stress the cost assumptions:

```bash
python -m oracle.costsim 2.5 5 1.0   # commission_bps  slippage_bps  gross_exposure
```

Sector calls are labeled with the foreign-accessible instrument they approximate
(`config.SECTOR_TRADEABLE_ETF` → ASHR/MCHI/FXI/KWEB), with the explicit caveat
that the P&L series comes from the A-share sector-ETF proxy we ingest, not the
US-listed fund itself.

## The AI research desk (optional — `oracle/analysis/llm_analyst.py`)

An optional LLM analyst that reads the same day's data — US closes, overnight
news, macro calendar, and the reflection log (what has worked before) — and
produces a structured per-sector **buy/sell/hold call** with conviction, key
drivers, and the foreign-tradeable ETF (ASHR/KWEB/…) each call maps to.

- **Provider-agnostic, off by default.** DeepSeek (its OpenAI-compatible
  endpoint) or Claude (Anthropic SDK). Enable with two env vars; unset = the
  rule-based pipeline runs alone.

  ```bash
  export ORACLE_ANALYST_PROVIDER=deepseek   # or claude
  export DEEPSEEK_API_KEY=sk-...            # or ANTHROPIC_API_KEY
  python -m oracle.run run_llm_analysis      # produce today's AI calls
  ```

  On GitHub Actions, set repo **variable** `ORACLE_ANALYST_PROVIDER` + the
  matching key **secret** — the daily `morning` phase then runs it automatically.

- **Recorded separately and scored.** The LLM's calls land in their own
  `llm_calls` table alongside the rule-based `predictions`, and the backtest
  grades them as a distinct strategy — `llm (recorded)` — against the rule-based
  model and the naive baselines, in both the gross table and the cost-aware
  (net/Sharpe/drawdown/break-even) section. It's scored **only on the dates it
  actually called** (a forward, out-of-sample read — the LLM isn't replayed over
  history), so treat it as signal only once its bet count is large enough for the
  p-value to mean anything. A confident LLM pick is a hypothesis to be scored,
  not a fact.
- **Fail-soft + honest.** Any error or refusal leaves the rule-based prediction
  as the sole record. Output is a probabilistic signal with the standard
  disclaimer — not a buy/sell instruction, never auto-executed.

- **Multi-pass reasoning chain (optional).** Instead of one shallow call, run the
  inputs through three chained passes the way a real desk works — and pay ~3× the
  tokens for it (shown on the spend meter, which is why it's opt-in):

  1. **Macro thesis** — a strategist reads the whole tape + news + web results into
     a regime view (risk-on/off), key risks, and a per-sector bias;
  2. **Sector deep-dive** — an analyst builds an explicit bull *and* bear case per
     sector against that thesis, free to disagree where the evidence warrants;
  3. **Devil's advocate / risk** — a risk officer stress-tests each call, downgrades
     thin-evidence conviction, and emits the final calls.

  ```bash
  export ORACLE_ANALYST_MODE=chain               # enable the chain (default: single)
  export ORACLE_ANALYST_THESIS_MODEL=deepseek-reasoner   # optional: reason pass 1
  python -m oracle.run run_llm_analysis
  ```

  On GitHub Actions, set repo **variable** `ORACLE_ANALYST_MODE=chain`. Each pass is
  metered separately (`analyst-thesis` / `analyst-deepdive` / `analyst-risk`) so you
  can see exactly what the added depth costs.

- **Live web search (optional).** DeepSeek's *API* has no web search (that's an
  app-only feature), so the system runs the search itself and feeds fresh results
  into the analyst's prompt — the standard search-augmented pattern. Off by
  default; enable with a provider + key:

  ```bash
  export ORACLE_SEARCH_PROVIDER=tavily   # or brave
  export TAVILY_API_KEY=tvly-...         # or BRAVE_API_KEY
  ```

  On GitHub Actions, set repo **variable** `ORACLE_SEARCH_PROVIDER` + the matching
  key **secret**. Each run searches one market-wide query plus one per sector
  (bounded by `ORACLE_SEARCH_MAX_QUERIES`), de-duplicates, and hands the snippets
  to DeepSeek as extra provided context. The search is **metered** in the spend
  tracker (`ORACLE_SEARCH_PRICE_PER_QUERY`, 0 on a free tier), so its cost shows up
  next to token cost.

### AI research spend meter (`oracle/usage.py`)

Every AI call records its token counts (and, for search, its query count) plus an
**estimated USD cost** in the `llm_usage` table, priced from `config.LLM_PRICES`
(override via env `ORACLE_LLM_PRICES`). See it on the dashboard's *AI Research
Spend* panel, in the weekly digest, or from the CLI:

```bash
python -m oracle.usage      # today / last 7d / all-time cost + tokens, per model
```

It's an **estimate** to keep deeper research honest — the provider invoice is the
source of truth.

## The daily action report (`oracle/report.py`)

Right after the US 4 pm close, the morning job produces a single plain-language
**outlook for the coming China session** and posts it as a GitHub Issue that
`@`-mentions you — so GitHub emails it straight to your inbox (the same
zero-credential path as the weekly digest; no SMTP secret). It merges the two
engines into one read per sector:

- **✅ Leaning constructive** — candidates to research / consider (the read points up),
- **⛔ Leaning cautious** — reasons to hold off / avoid adding (the read points down),
- **👀 Mixed or flat** — the rule-based model and the AI analyst disagree (no edge).

Conviction is highest where both engines *independently agree*; a split demotes
the sector to "watch" instead of feigning an edge. Each line names the
foreign-tradeable proxy (ASHR / KWEB / FXI) and the key drivers behind the call.
Generate it by hand any time:

```bash
python -m oracle.report            # today's outlook (plain text)
python -m oracle.report --md       # markdown (what the issue/email contains)
```

It's a probabilistic lean to weigh and size to your own risk — **not** a
buy/sell instruction, not a guarantee, never auto-executed.

## Deeper, market-specific prediction

Three layers make the analysis specific and technical instead of broad:

- **Technical indicators** (`analysis/technicals.py`) — trend, Wilder RSI, MACD, and
  momentum computed on each sector ETF's own price history and fed to the analyst,
  so calls are grounded in the actual technical state ("KWEB oversold, RSI 27").
- **Single-name picks** (`config.SECTOR_STOCKS`) — the analyst names one specific
  stock to *watch* per sector from a vetted, liquid universe (with its US-ADR/HK
  tradeable form), validated against the list so it can't invent a ticker. A
  research watchlist, not a buy list.
- **US-follows-vs-diverges** (`analysis/divergence.py`) — the self-improvement loop
  measures, per sector, whether it actually tracks the US overnight (correlation of
  the US-spillover signal vs. the realized China move over all history) and labels
  it *follows US* / *diverges* / *weak-independent*, gated by
  `MIN_CORRELATION_SAMPLE`. The analyst is told to weight the US move heavily for a
  follower and to lean on domestic technicals/news for a diverger (e.g. AI-type
  themes that move on their own catalysts). Surfaced per sector in the daily report.

## The learning loop (`oracle/learning/` — how accuracy actually improves)

The reflection log describes what went wrong; this *fixes* it. Every afternoon,
after the day's result is scored, `autotune` refits the model's parameters —
the per-signal weights **and** the abstain threshold — **per sector**, and adopts
a change only when it is measurably better on days the search never saw.

```bash
python -m oracle.learning.autotune              # fit, judge, adopt-or-refuse
python -m oracle.learning.autotune --report     # current params + learning ledger
python -m oracle.learning.autotune --rollback semis   # undo a sector's last change
```

**The protocol** (`walkforward.py`), designed so improvement can't be faked:

1. the most recent `LEARNING_HOLDOUT_DAYS` (45) are cut off and **never touched
   by the search**;
2. **walk-forward folds** over the rest — expanding train window, next slice as
   validation, always predicting forward;
3. the winner is **blended** only `LEARNING_STEP` (½) of the way from the
   incumbent, so one noisy fit can't throw the model across the space;
4. the *blended* set — exactly what would be stored — is re-scored against the
   incumbent **on the untouched holdout**;
5. it is adopted only if it clears `LEARNING_MIN_IMPROVEMENT`, with enough bets
   behind it, and the sector isn't in its post-adoption **cooldown** (the holdout
   only refreshes as days pass; adopting daily would burn it in).

Every attempt — adopted or refused — is written to `learning_log` with
before/after holdout hit-rates, so the accuracy curve is auditable and any
regression is traceable and reversible. **Most runs correctly refuse**; that's
the mechanism working, not failing.

**What it learns from.** The scorer weights six components, and the tuner only
ever puts weight on signals that actually *vary* in the data (a weight on a dead
signal is a phantom parameter that silently rescales the composite). Alongside
US spillover and news sentiment it can now weight the sector's **own technical
state** — `rsi_signal` (mean reversion), `momentum_signal` (trend following, the
deliberate opposite bet), and `trend_signal` — so it can discover which
hypothesis pays per sector rather than being told.

> **No lookahead.** Technicals feeding a prediction *about* day *d* are computed
> only from closes **strictly before** *d* (`db.close_series(before=…)`). In a
> replay the target day's close is already in the DB, so using it would hand the
> model the answer and silently inflate every accuracy number downstream.

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
