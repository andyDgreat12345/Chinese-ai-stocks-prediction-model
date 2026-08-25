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
  config.py        # schedule, 10-sector universe, feeds, learning + history guards
  schema.sql       # SQLite schema — the reflection loop (§4b) is first-class
  db.py            # connection, idempotent schema init, column/table migrations
  scheduler.py     # APScheduler wiring, cron jobs pinned to CST
  jobs/            # the scheduled jobs (§2)
  run.py           # phase entrypoints used by CI/Actions (morning/afternoon/...)

  analysis/
    scoring.py     # pure weighted-signal scoring -> direction + confidence
    pipeline.py    # build_signals (US spillover, sentiment, technicals) (§4)
    technicals.py  # Wilder RSI, MACD, SMA, momentum -> learnable signals
    segments.py    # K-line decomposition: gap / body / wicks + tradeability
    sentiment.py   # English lexicon sentiment + category classifier
    sentiment_zh.py# Chinese lexicon: substring match, positional negation
    divergence.py  # classifies sectors as follows-US / diverges / independent
    pairs.py       # lead/lag semantics; encodes the CST/UTC session clocks
    forecast.py    # measured probability cone for the dashboard
    llm_analyst.py # optional AI research desk (single pass)
    debate.py      # optional adversarial bull/bear/synthesis analyst
    variant_scoring.py # scores analyst variants head-to-head, with a kill gate

  ingestion/       # network isolated from the pure transforms, fail-soft
    us_market.py   #   US indices/ETFs/metals -> us_close
    china_market.py#   China indices + 10 sector ETFs; code verification;
                   #   per-board price limits; corporate-action neutralisation
    news.py        #   English + Chinese RSS -> tagged headlines -> news
    quotes.py      #   near-real-time snapshots for named picks
    macro.py       #   hand-maintained macro calendar -> macro_events

  learning/        # the loop that actually moves the numbers
    walkforward.py #   holdout split, folds, edge_t objective, coverage gate
    autotune.py    #   fit -> judge on holdout -> adopt only on measured gain

  reflection/      # daily self-assessment (§4b)
    scoring.py stats.py correlation.py accumulate.py reflect.py llm.py

  research/        # the statistics that keep findings honest
    sweep.py       #   Fisher-z p-values, Benjamini-Hochberg, split-half
    universe.py    #   35 US symbols, each with a written hypothesis + controls
    marginal.py    #   conditional analysis + plateau-vs-spike validation
    news_signal.py #   does Chinese news predict? + detectability table
    run.py labels.py

  simulator/       # what a disciplined human trader would have done
    trader.py engine.py tune.py ranking.py

  paper.py         # forward, out-of-sample ledger for the validated rule
  backfill.py      # historical loader (10-year rolling window) + prune/repair
  backtest.py      # replay + baselines + Sharpe + p-value (lookahead-guarded)
  costsim.py       # cost-aware net returns: friction, drawdown, break-even
  api/server.py    # FastAPI dashboard + JSON endpoints
  site_build.py    # static snapshot build for GitHub Pages
  dashboard/       # terminal-style UI
tests/             # 418 tests, all network-free
```

## What the system has actually established

Measured, not assumed. Every figure below came out of the machinery above and
several of them overturned an earlier, more flattering number.

| Finding | Evidence |
| --- | --- |
| The model has a small real directional edge | 52% bet-accuracy over 8,251 bets, p = 1.1e-05 |
| ...but it lives in the overnight **gap** | gap 71.8% vs 58.8% majority baseline; body 49.4% |
| ...which entry at the open **cannot capture** | the driving US session trades inside the gap window |
| So the tradeable segment is a coin flip | body edge_t = −0.53 |
| But the gap is the **losing** half of the day | gap −0.072%/session (t=−14.4) vs body +0.110% (t=+10.2) |
| ...so entering at the open avoids a drag, not a gain | negative overnight in 10/10 sectors, 11/11 years |
| The learner optimizes a target **no trade can earn** | scores close-to-close; a trade earns the body |
| ...so its dominant signal is empty where it counts | `us_spillover` t=+5.82 scored, **t=+0.09** tradeable |
| ...and the two objectives disagree on the best signal | rho=0.72; `rsi_signal` wins on the body |
| Simulator returns come from **exit discipline** | 49% win rate, 1.6:1 win/loss ratio — the prediction has no tradeable content to add |
| A conditional **mean-reversion** rule does survive | holdout n=332, 59.6% hit, +0.312% net, t=+2.40 |
| Holding it overnight makes it **worse** | +0.304% at t=+2.22, wider spread; 2-day hold fails FDR |
| The rule is a filter on **magnitude**, not direction | fires on 5.2% of sessions, selects 3.46× normal intraday drift |
| The premium is **not** harvestable unconditionally | +0.110% gross vs 0.15% round trip → −0.040% net |
| The edge is broad but not decisively so | positive in 28/32 buckets; best family p = 0.055 |
| Sector-edge ranking does **not** work | worse than random ordering (25th percentile) |

Three lookahead bugs, two data-integrity bugs (unadjusted corporate actions,
fetch-clock date stamping), one ordering artifact and one invalid sign test
(pooled across overlapping families, reporting p<0.0001 where the honest figure
was p=0.055) were found and fixed along the way. Each is documented in the
module that fixes it.

**The largest open risk is not statistical.** Every instrument traded is a
domestic A-share equity ETF, and mainland equities settle T+1 — shares bought
today cannot be sold until the next session. If that applies to these ETFs, the
validated rule's same-session exit cannot be placed at all. It is priced both
ways in `research/execution.py` (the constraint costs −0.014%/trade, so the rule
degrades rather than dies) and the forward ledger records both legs, but the
question itself can only be settled by a broker.

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

## Website: charts, candles, and the forecast cone

The dashboard leads with the **Daily Action Report** (the same read that's emailed),
followed by **Sector Charts + Forecast Cone**:

- **Real candlesticks** where OHLC exists (`open`/`high`/`low` are ingested and
  backfilled alongside the close). Where a source gave only closes, the chart
  draws a **line instead of inventing a bar**.
- **A forecast cone**, not a predicted candlestick. Its width is the *measured*
  10–90% range of what actually happened on past days when the model made this
  same call for this sector (`analysis/forecast.py`), so it widens honestly when
  the edge is weak. Each sector also reports `median +0.60%, 10–90% range -1.65%
  to +3.00% (n=101)`.

> **Why no predicted candlestick.** A candle encodes open/high/low/close, which
> means an intraday path — and we ingest **one close per day**, no intraday data
> at all. Drawing a future bar would fabricate three of its four numbers on top of
> a ~55%-accuracy directional signal. The cone shows the same idea using only
> numbers we have actually measured.

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

## Research modules (`oracle/research/` — the statistics that keep it honest)

Sweeping enough hypotheses always produces a significant one. Every finding in
this project has to clear the same four filters before it is reported:

1. a sample floor;
2. a Fisher-z p-value;
3. **Benjamini-Hochberg** across *every* test run, so a reported q-value already
   accounts for how wide the search was;
4. **split-half stability** — both halves of the history must agree in sign.

`research/sweep.py` applies these to US↔China pairs (including deliberate
CONTROL symbols, which is how the first wide sweep was caught measuring global
risk beta rather than China alpha). `research/marginal.py` applies them to
conditional buckets and adds a plateau-vs-spike check: a real effect survives
moving its threshold, a fitted one towers over its neighbours.

`research/news_signal.py` reports a **detectability table** — the smallest
effect the current history could resolve. It is what showed that a wide sweep
would need roughly five years to detect a realistic news-sentiment effect, which
killed a plan before it consumed months.

`research/exit_horizon.py` asks whether the rule's same-session exit is the
right one — it was never a decision, just the horizon the rule was found on.
Four exits are scored on the same holdout, each against the drift of holding
every session that long. It also carries the overnight/intraday decomposition
that explains the answer, and the premium economics showing why a decade-long
anomaly has not been arbitraged away (the premium is smaller than the friction
needed to collect it).

`research/execution.py` asks whether the rule can be executed at all: the T+1
settlement question, and how much slippage the edge absorbs before it is gone
(breakeven +0.230%, which is 8.8% of the average move on the sessions it
trades). The settlement warning prints unconditionally — a caveat that only
appears when the numbers happen to compute would be missing on exactly the run
where someone reads the report and funds something.

`learning/objective.py` asks whether the learner is optimizing something a trade
can earn. It is not: the loop scores close-to-close, a position entered at the
open earns the body, and the system's founding signal carries t=+5.82 against
the first and t=+0.09 against the second. The module deliberately stops short of
proposing a reweight — no signal reaches t≥2 on the body, so realigning the
objective would most likely reveal that the tradeable segment is unpredictable
rather than produce a better model. What changes is how the headline is read.

`research/regimes.py` asks whether the edge is broad or one lucky corner,
slicing the rule across six pre-registered families. It is deliberately **not**
a search: no bucket is ever selected to trade, and the report refuses to name a
best one, because the best of a dozen buckets is a selection effect. Its sign
test runs per family, never pooled — the families overlap, so pooling counts
every trade once per family.

## Forward evidence (`oracle/paper.py`)

The mean-reversion rule passed every retrospective test available, but all of
them reuse the same ten years. The paper ledger records each qualifying setup
**before the outcome is known** and settles it after the close, so the forward
row is the one number that could not have been fitted. It places no orders.

It records **both settlement legs** — the same-session exit as validated, and
the same trade sold at the next open — because the T+1 question is unresolved
and the forward record takes about four months to fill. Discovering at the end
of that wait that only the unexecutable leg had been kept would waste the wait.
The two already disagree: the first forward setup was a +0.0002% win at T+0 and
a −0.3002% loss at T+1.

## Open decisions

- **Timing — now measured, no longer a judgement call.** The overnight gap is
  the losing half of the day (−0.072%/session, t=−14.4), and holding the rule
  overnight earns less with a wider spread (+0.304% at t=+2.22 vs +0.317% at
  t=+2.44). Entering at the open is not forfeiting the edge; it is sitting out
  the drag. See `research/exit_horizon.py`.
- **Settlement — open, and the largest risk here.** Whether these ETFs permit
  same-session selling decides whether the validated rule is placeable. Not
  answerable from price data; ask a broker. Priced both ways meanwhile.
- **Macro ingestion is file-based** (hand-maintained JSON); a live feed is a
  clean upgrade behind `load_from_file`.
- **Analyst variants.** The adversarial debate analyst is built and off by
  default; `variant_scoring.py` decides whether it stays.
