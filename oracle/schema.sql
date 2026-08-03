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
    pct_change   REAL,                   -- vs prior close
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS china_close (
    trade_date   TEXT NOT NULL,
    symbol       TEXT NOT NULL,          -- index code or ticker
    sector       TEXT,
    close        REAL,
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
    created_at    TEXT NOT NULL,
    UNIQUE (trade_date, sector)
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

-- Seed default weights (equal-ish start; tuned later by the reflection loop).
INSERT OR IGNORE INTO weights (signal, current_weight, suggested_weight, updated_at)
VALUES
    ('us_spillover', 0.45, 0.45, '1970-01-01'),
    ('sentiment',    0.35, 0.35, '1970-01-01'),
    ('macro',        0.20, 0.20, '1970-01-01');
