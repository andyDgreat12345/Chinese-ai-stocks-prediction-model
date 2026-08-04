"""Central configuration for China Market Oracle.

Times are expressed in CST (China Standard Time, UTC+8) to match the spec's
scheduling table. The scheduler is pinned to this timezone explicitly so the
job clock does not drift with the host machine's locale.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────
# Each is overridable by env var so a stateless runner (e.g. GitHub Actions)
# can point the DB + logs at a persisted state directory between runs.
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = Path(os.environ.get("ORACLE_DB") or DATA_DIR / "oracle.db")
REFLECTION_LOG = Path(os.environ.get("ORACLE_REFLECTION_LOG") or DATA_DIR / "reflection_log.jsonl")
# Hand-maintained macro calendar (Fed/CPI/PMI). JSON list of
# {event_date, category, description, weight}. See examples/macro_events.sample.json.
MACRO_CALENDAR_FILE = Path(os.environ.get("ORACLE_MACRO_FILE") or DATA_DIR / "macro_events.json")

# ── Timezone ─────────────────────────────────────────────────────────────
# China A-share market: 09:30–15:00 CST (lunch 11:30–13:00).
TIMEZONE = "Asia/Shanghai"

# ── Scheduled jobs (spec §2). (hour, minute) in CST. ─────────────────────
JOB_SCHEDULE = {
    "fetch_us_close":     (4, 15),   # S&P/Nasdaq/Dow closes + sector perf
    "fetch_world_news":   (4, 30),   # overnight RSS headlines
    "run_analysis":       (5, 0),    # combine signals -> prediction JSON
    "pre_open_refresh":   (9, 15),   # re-check breaking news, adjust confidence
    "fetch_china_close":  (15, 5),   # log actual close, compare vs prediction
    "reflect_and_update": (15, 15),  # self-improvement pass (spec §4b) — TOP PRIORITY
}

# ── Data-source universe (spec §3) ───────────────────────────────────────
US_INDICES = ["^GSPC", "^IXIC", "^DJI"]                 # S&P500, Nasdaq, Dow
US_SECTOR_ETFS = ["XLE", "XLF", "SOXX"]                 # energy, financials, semis
PRECIOUS_METALS = ["GC=F", "SI=F"]                      # gold, silver
CHINA_INDICES = {
    "SSE": "sh000001",       # SSE Composite
    "SZSE": "sz399001",      # SZSE Component
    "ChiNext": "sz399006",   # ChiNext
}

# Per-sector actuals for the accuracy scorecard. Liquid A-share sector ETFs are
# used as the sector proxy — their codes are stable, unlike obscure sector-index
# codes. {china_sector: ETF code}. Fetched via akshare's ETF history endpoint.
# NOTE: verify each code against live akshare on first run — the ingestion job
# fails soft per symbol, so a wrong/renamed code just leaves that sector
# unscored (no crash) until corrected.
CHINA_SECTOR_ETFS = {
    "broad": "510300",       # CSI 300 ETF
    "growth": "159915",      # ChiNext ETF
    "semis": "512480",       # Semiconductor ETF
    "energy": "159930",      # Energy ETF
    "financials": "512800",  # Bank/financials ETF
}

# Foreign-accessible instrument each sector maps to — what a non-mainland
# investor could actually trade (US-listed China ETFs). Used ONLY to LABEL the
# cost-aware backtest so the read is concrete ("this sector call ≈ buying KWEB").
# The return series in the backtest still comes from the A-share sector-ETF close
# we ingest (CHINA_SECTOR_ETFS): a close proxy, but NOT identical — the US-listed
# funds differ in tracking basket, trade in US hours, and carry FX exposure. Treat
# the mapping as directional context, not a claim the P&L is literally tradeable.
SECTOR_TRADEABLE_ETF = {
    "broad": "ASHR",        # CSI 300 A-shares (direct analogue); MCHI is broader
    "growth": "KWEB",       # China internet/growth
    "semis": "KWEB",        # no pure US-listed China-semis ETF — tech/internet proxy
    "energy": "FXI",        # large-cap, energy majors included (loose proxy)
    "financials": "FXI",    # large-cap, financials-heavy
}

NEWS_FEEDS = {
    "reuters": "https://feeds.reuters.com/reuters/businessNews",
    "caixin": "https://www.caixinglobal.com/rss/economics.xml",
    # Bloomberg often needs a workaround — left as a Phase-2 open decision (spec §7).
}

# ── Reflection loop guards (spec §4b) ────────────────────────────────────
# A correlation must clear this many independent observations before it is
# allowed to influence model weights — below it, "strong correlation" is noise.
MIN_CORRELATION_SAMPLE = 30
CORRELATION_WINDOWS = [30, 60, 90]  # rolling windows (trading days)

# Weight adjustments suggested by the reflection LLM are logged for review,
# NOT auto-applied, until this is flipped on (spec §4b-iii, human-in-the-loop).
AUTO_APPLY_WEIGHT_ADJUSTMENTS = False

# ── Live web search (optional — augments the AI analyst) ─────────────────
# DeepSeek's API has no web search, so we run it ourselves and feed the results
# into the analyst's prompt. OFF by default: set ORACLE_SEARCH_PROVIDER=tavily
# (or brave) + the matching key secret (TAVILY_API_KEY / BRAVE_API_KEY) to enable.
SEARCH_MAX_QUERIES = int(os.environ.get("ORACLE_SEARCH_MAX_QUERIES") or 6)
SEARCH_MAX_RESULTS = int(os.environ.get("ORACLE_SEARCH_MAX_RESULTS") or 4)
# Estimated USD per search query for the spend meter (0 on a free tier; set to
# your plan's per-query rate to track it). Override via ORACLE_SEARCH_PRICE_PER_QUERY.
SEARCH_PRICE_PER_QUERY = float(os.environ.get("ORACLE_SEARCH_PRICE_PER_QUERY") or 0.0)

# ── LLM token pricing (USD per 1,000,000 tokens) ─────────────────────────
# Used ONLY to *estimate* spend for the usage meter (oracle/analysis/pricing.py);
# the provider's invoice is the source of truth. VERIFY against current pricing —
# these move. Override without a code change via env ORACLE_LLM_PRICES, a JSON
# object merged over these defaults, e.g.:
#   ORACLE_LLM_PRICES='{"deepseek-chat":{"input":0.27,"cache_hit":0.07,"output":1.10}}'
# "input" = per-1M prompt tokens on a cache MISS; "cache_hit" = per-1M prompt
# tokens served from the provider's context cache; "output" = per-1M completion.
LLM_PRICES = {
    "deepseek-chat":     {"input": 0.27, "cache_hit": 0.07, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "cache_hit": 0.14, "output": 2.19},
    "claude-opus-5":     {"input": 5.0,  "cache_hit": 0.50, "output": 25.0},
    "claude-sonnet-5":   {"input": 3.0,  "cache_hit": 0.30, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 1.0, "cache_hit": 0.10, "output": 5.0},
}
# Used when a model isn't in the table above — conservative-ish so an unknown
# model still meters *something* rather than $0.
LLM_PRICE_FALLBACK = {"input": 0.30, "cache_hit": 0.08, "output": 1.20}

_env_prices = os.environ.get("ORACLE_LLM_PRICES")
if _env_prices:
    try:
        import json as _json
        for _model, _rate in (_json.loads(_env_prices) or {}).items():
            LLM_PRICES[_model] = {**LLM_PRICES.get(_model, LLM_PRICE_FALLBACK), **_rate}
    except (ValueError, TypeError, AttributeError):
        pass  # bad override -> keep defaults, never crash config import

DISCLAIMER = (
    "Not investment advice. Output is a probabilistic signal for you to weigh "
    "yourself — not a buy/sell instruction and not a guarantee of returns."
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
