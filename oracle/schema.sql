-- China Market Oracle — SQLite schema
-- Designed so the self-improvement / reflection loop (spec §4b) is a
-- first-class citizen from day one, NOT a v2 bolt-on.

-- ─────────────────────────────────────────────────────────────────────────
-- RAW INGESTED DATA (spec §3, Phase 2)
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS us_close (
    trade_date   TEXT NOT NULL,          -- ISO date of the US session that closed
    symbol       TEXT NOT NULL,          -- ^GSPC, XLE, GC=F, ...
    sector       TEXT,                   -- logical sector tag (energy/semis/...)
    close        REAL,
    open         REAL,                   -- OHLC, for candlestick rendering
    high         REAL,
    low          REAL,
    pct_change   REAL,                   -- vs prior close
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS china_close (
    trade_date   TEXT NOT NULL,
    symbol       TEXT NOT NULL,          -- index code or ticker
    sector       TEXT,
    close        REAL,
    open         REAL,                   -- OHLC, for candlestick rendering
    high         REAL,
    low          REAL,
    pct_change   REAL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS news (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date   TEXT NOT NULL,          -- session the headline is attributed to
    source       TEXT,
    category     TEXT,                   -- fed_policy / chip_export / tariffs / stimulus / earnings / ...
    headline     TEXT NOT NULL,
    summary      TEXT,                   -- first paragraph only (spec §3)
    sentiment    REAL,                   -- lexicon score, -1..1 (Phase 3)
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS macro_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date   TEXT NOT NULL,
    category     TEXT,                   -- fed_meeting / cpi / pmi / trade_data
    description  TEXT,
    weight       REAL DEFAULT 1.0,       -- manual dominance weight (spec §4.3)
    notes        TEXT
);

-- ─────────────────────────────────────────────────────────────────────────
-- PREDICTIONS + COMPONENT SIGNALS (spec §4b-i)
-- Every prediction stores the component signals that produced it, not just the
-- final verdict, so error can later be attributed to a specific signal.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS predictions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date        TEXT NOT NULL,     -- the China session being predicted
    sector            TEXT NOT NULL,     -- sector/index the call is about
    direction         TEXT NOT NULL,     -- bullish / neutral / bearish
    confidence        TEXT NOT NULL,     -- low / med / high
    composite_score   REAL NOT NULL,
    -- component signals (auditable attribution):
    us_spillover      REAL,
    sentiment_score   REAL,
    macro_flag        INTEGER DEFAULT 0, -- 1 if a scheduled macro event applies
    rationale         TEXT,              -- one-line explanation for the dashboard
    created_at        TEXT NOT NULL,
    UNIQUE (trade_date, sector)
);

-- Directional + calibration scoring, filled at 15:15 once actuals land.
CREATE TABLE IF NOT EXISTS prediction_scores (
    prediction_id     INTEGER NOT NULL REFERENCES predictions(id),
    actual_direction  TEXT,
    actual_pct_change REAL,
    correct           INTEGER,           -- 1/0 directional hit
    confidence_p      REAL,              -- probability implied by confidence bucket
    brier             REAL,              -- (confidence_p - correct)^2, for calibration
    scored_at         TEXT NOT NULL,
    PRIMARY KEY (prediction_id)
);

-- ─────────────────────────────────────────────────────────────────────────
-- LLM RESEARCH DESK (optional AI analyst — spec §4.2/§7b)
-- The LLM analyst's per-sector buy/sell/hold calls are recorded as a SEPARATE
-- source alongside the rule-based `predictions`, never replacing them, so the
-- backtest can score the LLM's edge against the rule-based model and baselines.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT NOT NULL,          -- the China session being called
    sector        TEXT NOT NULL,
    provider      TEXT,                   -- deepseek / claude
    model         TEXT,
    direction     TEXT NOT NULL,          -- bullish / neutral / bearish
    conviction    TEXT NOT NULL,          -- low / med / high
    tradeable_etf TEXT,                   -- foreign-accessible proxy (ASHR/KWEB/...)
    key_drivers   TEXT,                   -- JSON array of strings
    rationale     TEXT,
    top_pick      TEXT,                   -- JSON {ticker,name,tradeable,note}: single name to watch
    -- Which analyst produced this call: 'single' (one pass) or 'debate'
    -- (bull/bear advocacy + synthesis). Both may exist for the same session so
    -- the two can be scored head-to-head on identical inputs; without the
    -- variant in the key, running one would overwrite the other and the
    -- comparison the debate exists to settle could never be made.
    variant       TEXT NOT NULL DEFAULT 'single',
    created_at    TEXT NOT NULL,
    UNIQUE (trade_date, sector, variant)
);

-- ─────────────────────────────────────────────────────────────────────────
-- LLM TOKEN / COST METER (§4.2 cost governance)
-- One row per LLM API call (analyst today; future research passes too), with
-- the token counts the provider reported and an *estimated* USD cost. This is
-- the running meter so deeper research never surprises the bill — the
-- provider's own invoice remains the source of truth.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date        TEXT,                 -- session the call was for (nullable)
    call_type         TEXT,                 -- analyst / reflection / research-* ...
    provider          TEXT,                 -- deepseek / claude
    model             TEXT,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cached_tokens     INTEGER DEFAULT 0,    -- prompt tokens served from cache (cheaper)
    total_tokens      INTEGER DEFAULT 0,
    cost_usd          REAL DEFAULT 0,       -- estimated from config.LLM_PRICES
    created_at        TEXT NOT NULL         -- when the call was made (spend accrues here)
);

-- ─────────────────────────────────────────────────────────────────────────
-- US → CHINA INFLUENCE MEASUREMENT (spec §4b-ii)
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS correlations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    us_symbol     TEXT NOT NULL,
    china_symbol  TEXT NOT NULL,
    window_days   INTEGER NOT NULL,      -- 30/60/90
    correlation   REAL,
    best_lag      INTEGER,               -- 0 = same day, 1 = next-day effect
    sample_size   INTEGER NOT NULL,
    established    INTEGER DEFAULT 0,    -- 1 once sample_size >= MIN_CORRELATION_SAMPLE
    computed_at   TEXT NOT NULL,
    UNIQUE (us_symbol, china_symbol, window_days)
);

-- Append-only log of every correlation reading, one row per pair/lag per day.
-- The `correlations` table above is a SNAPSHOT (it overwrites daily); this is the
-- ACCUMULATION. A relationship that has read +0.4 consistently for sixty
-- observations is worth far more than one that reads +0.6 today and -0.2 last
-- week, and only a history can tell those apart.
CREATE TABLE IF NOT EXISTS correlation_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_on   TEXT NOT NULL,         -- date the reading was taken
    us_symbol     TEXT NOT NULL,
    china_symbol  TEXT NOT NULL,
    lag           INTEGER NOT NULL,      -- 0 = same-day (untradeable), >=1 = predictive
    window_days   INTEGER NOT NULL,      -- 0 = expanding (all history to date)
    correlation   REAL,
    sample_size   INTEGER,
    UNIQUE (observed_on, us_symbol, china_symbol, lag, window_days)
);

-- Pairs that survived the wide research sweep (FDR-corrected, sign-stable,
-- tradeable lag). These are promoted to first-class citizens: the reflection
-- pass refreshes each one's correlation factor every round, so a proven link is
-- continuously re-tested rather than trusted forever on one discovery run.
CREATE TABLE IF NOT EXISTS proven_pairs (
    us_symbol      TEXT NOT NULL,
    china_symbol   TEXT NOT NULL,
    lag            INTEGER NOT NULL,
    r_discovered   REAL,               -- correlation at discovery
    q_value        REAL,               -- FDR-corrected q at discovery
    n_discovered   INTEGER,
    discovered_on  TEXT NOT NULL,
    current_r      REAL,               -- refreshed every reflection round
    current_n      INTEGER,
    refreshed_on   TEXT,
    refresh_count  INTEGER DEFAULT 0,
    PRIMARY KEY (us_symbol, china_symbol, lag)
);

-- News-category -> typical subsequent China sector move (spec §4b-ii).
CREATE TABLE IF NOT EXISTS news_impact (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    category      TEXT NOT NULL,
    china_sector  TEXT NOT NULL,
    avg_move      REAL,
    variance      REAL,
    sample_size   INTEGER NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (category, china_sector)
);

-- ─────────────────────────────────────────────────────────────────────────
-- REFLECTION LOG + WEIGHT REVIEW (spec §4b-iii)
-- The single most valuable artifact of the project. LLM-generated daily
-- structured reflection; weight adjustments are logged for review, not
-- silently auto-applied.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reflection_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date            TEXT NOT NULL UNIQUE,
    predicted_json        TEXT,
    actual_json           TEXT,
    signals_that_worked   TEXT,          -- JSON array
    signals_that_missed   TEXT,          -- JSON array
    likely_reason_for_miss TEXT,
    suggested_adjustment  TEXT,          -- JSON: {signal, direction, magnitude}
    reflection_confidence TEXT,          -- low / med / high
    created_at            TEXT NOT NULL
);

-- Current model weights + the reflection loop's proposed adjustments, so the
-- dashboard can show a suggested-vs-actual diff (human-in-the-loop).
CREATE TABLE IF NOT EXISTS weights (
    signal          TEXT PRIMARY KEY,    -- us_spillover / sentiment / macro
    current_weight  REAL NOT NULL,
    suggested_weight REAL,
    updated_at      TEXT NOT NULL
);

-- ─────────────────────────────────────────────────────────────────────────
-- LEARNED MODEL PARAMETERS + LEARNING LEDGER (§4b — the self-improvement core)
-- The walk-forward tuner (oracle/learning/) fits signal weights AND the
-- abstain threshold, PER SECTOR, on past data and validates them on a holdout
-- window the search never saw. Adopted parameter sets land here; every run —
-- adopted or rejected — is appended to learning_log so the accuracy curve is
-- auditable and a bad change can be traced and rolled back.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS model_params (
    sector      TEXT PRIMARY KEY,     -- china sector, or '*' for the global default
    params      TEXT NOT NULL,        -- JSON {us_spillover, sentiment, macro, threshold}
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date       TEXT NOT NULL,
    sector         TEXT NOT NULL,
    params_before  TEXT,              -- JSON
    params_after   TEXT,              -- JSON (== before when not adopted)
    score_before   REAL,              -- incumbent objective on the holdout
    score_after    REAL,              -- candidate objective on the SAME holdout
    hit_before     REAL,              -- holdout directional hit-rate, incumbent
    hit_after      REAL,              -- holdout directional hit-rate, candidate
    n_holdout      INTEGER,           -- scored records in the holdout window
    adopted        INTEGER NOT NULL,  -- 1 = the change was applied
    reason         TEXT,              -- why adopted / why refused
    created_at     TEXT NOT NULL
);

-- Seed default weights (equal-ish start; tuned later by the reflection loop).
INSERT OR IGNORE INTO weights (signal, current_weight, suggested_weight, updated_at)
VALUES
    ('us_spillover', 0.45, 0.45, '1970-01-01'),
    ('sentiment',    0.35, 0.35, '1970-01-01'),
    ('macro',        0.20, 0.20, '1970-01-01');

-- ─────────────────────────────────────────────────────────────────────────
-- PAPER STRATEGY LEDGER
-- Forward, out-of-sample record for a research rule that has passed
-- retrospective validation but has never been tested on data that did not
-- exist when it was found. Every retrospective test reuses the same ten years;
-- only this table accumulates evidence that cannot have been fitted.
--
-- A row is written the moment a setup fires (entry known, outcome NULL) and
-- filled in once the session closes. Writing it up front is what makes it
-- honest: a ledger that only records entries it later likes is a backtest.
-- ─────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS paper_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT NOT NULL,          -- the China session traded
    sector        TEXT NOT NULL,
    strategy      TEXT NOT NULL,          -- rule identifier
    -- The conditions as measured at entry, so a later reader can check the
    -- setup really qualified rather than trusting that it did.
    prior_body    REAL,
    gap           REAL,
    entry_price   REAL,
    -- Filled at the close. NULL while the session is open.
    exit_price    REAL,
    body_pct      REAL,                   -- open -> close, the segment traded
    net_pct       REAL,                   -- body minus modelled round-trip cost
    outcome       TEXT,                   -- win / loss / open
    recorded_at   TEXT NOT NULL,
    settled_at    TEXT,
    UNIQUE (trade_date, sector, strategy)
);
