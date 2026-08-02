"""Central configuration for China Market Oracle.

Times are expressed in CST (China Standard Time, UTC+8) to match the spec's
scheduling table. The scheduler is pinned to this timezone explicitly so the
job clock does not drift with the host machine's locale.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "oracle.db"
REFLECTION_LOG = DATA_DIR / "reflection_log.jsonl"
# Hand-maintained macro calendar (Fed/CPI/PMI). JSON list of
# {event_date, category, description, weight}. See examples/macro_events.sample.json.
MACRO_CALENDAR_FILE = DATA_DIR / "macro_events.json"

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

DISCLAIMER = (
    "Not investment advice. Output is a probabilistic signal for you to weigh "
    "yourself — not a buy/sell instruction and not a guarantee of returns."
)

DATA_DIR.mkdir(parents=True, exist_ok=True)
