"""Central configuration for China Market Oracle.

Times are expressed in CST (China Standard Time, UTC+8) to match the spec's
scheduling table. The scheduler is pinned to this timezone explicitly so the
job clock does not drift with the host machine's locale.
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env override ('0'/'false'/'off' disable). Absent = default."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "off", "no")

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
# energy, financials, semis + the spillover sources for the added China sectors.
US_SECTOR_ETFS = ["XLE", "XLF", "SOXX", "XLV", "XLP", "XLI"]
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
    # Added to widen the tradeable universe. Trade count is the binding
    # constraint on verifying anything here: at ~174 trades per 18 months,
    # confirming a genuine 51%->55% win rate would take about seven years.
    # Doubling the instruments roughly halves that wait.
    # Verify every code with `python -m oracle.ingestion.china_market --verify`
    # before trusting it — ingestion fails soft per symbol, so a wrong or
    # renamed code leaves that sector silently unscored rather than erroring.
    "healthcare": "512170",  # 医疗ETF — healthcare/medical devices
    "consumer": "159928",    # 消费ETF — major consumer staples
    "brokers": "512880",     # 证券ETF — brokers, the most policy-sensitive book
    "defense": "512660",     # 军工ETF — defense; domestically driven, a genuine
                             #   divergence candidate rather than a US follower
    "newenergy": "515030",   # 新能源车ETF — EV/battery chain
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
    # The added sectors have no close US-listed analogue at all — these are
    # broad-China funds standing in for a narrow domestic theme, so the label is
    # weaker here than above. Read them as "roughly this part of China", not as a
    # tracking instrument.
    "healthcare": "MCHI",   # broad China; no US-listed China-healthcare fund
    "consumer": "MCHI",     # broad China; CHIQ is discretionary-only
    "brokers": "FXI",       # large-cap financials-heavy
    "defense": "MCHI",      # no US-listed analogue; A-share defense is not
                            #   foreign-accessible in any close form
    "newenergy": "KWEB",    # loose — KWEB is internet, not EV/battery
}

# Curated single-name watchlist per sector — liquid, recognizable China names the
# analyst may pick from as "the specific name to watch" for a sector call. Each:
# {ticker (primary listing), name, tradeable (the foreign-accessible form — US ADR
# or HK line — or "" if mainland-only)}. This is a *research watchlist*, NOT a
# buy list; the disclaimer applies to every pick. Kept short and liquid on purpose.
SECTOR_STOCKS = {
    "broad": [
        {"ticker": "600519.SS", "name": "Kweichow Moutai", "tradeable": ""},
        {"ticker": "601318.SS", "name": "Ping An Insurance", "tradeable": "HK:2318"},
        {"ticker": "600036.SS", "name": "China Merchants Bank", "tradeable": "HK:3968"},
    ],
    "growth": [
        {"ticker": "BABA", "name": "Alibaba", "tradeable": "BABA"},
        {"ticker": "PDD", "name": "PDD Holdings", "tradeable": "PDD"},
        {"ticker": "JD", "name": "JD.com", "tradeable": "JD"},
        {"ticker": "0700.HK", "name": "Tencent", "tradeable": "HK:0700"},
        {"ticker": "BIDU", "name": "Baidu", "tradeable": "BIDU"},
    ],
    "semis": [
        {"ticker": "688981.SS", "name": "SMIC", "tradeable": "HK:0981"},
        {"ticker": "688256.SS", "name": "Cambricon", "tradeable": ""},
        {"ticker": "002371.SZ", "name": "NAURA Technology", "tradeable": ""},
        {"ticker": "1347.HK", "name": "Hua Hong Semiconductor", "tradeable": "HK:1347"},
    ],
    "energy": [
        {"ticker": "601857.SS", "name": "PetroChina", "tradeable": "HK:0857"},
        {"ticker": "600028.SS", "name": "Sinopec", "tradeable": "HK:0386"},
        {"ticker": "0883.HK", "name": "CNOOC", "tradeable": "HK:0883"},
    ],
    "financials": [
        {"ticker": "601398.SS", "name": "ICBC", "tradeable": "HK:1398"},
        {"ticker": "601288.SS", "name": "Agricultural Bank of China", "tradeable": "HK:1288"},
        {"ticker": "601318.SS", "name": "Ping An Insurance", "tradeable": "HK:2318"},
    ],
    "healthcare": [
        {"ticker": "600276.SS", "name": "Jiangsu Hengrui Pharma", "tradeable": "HK:1276"},
        {"ticker": "300760.SZ", "name": "Mindray Medical", "tradeable": ""},
        {"ticker": "603259.SS", "name": "WuXi AppTec", "tradeable": "HK:2359"},
    ],
    "consumer": [
        {"ticker": "600519.SS", "name": "Kweichow Moutai", "tradeable": ""},
        {"ticker": "000858.SZ", "name": "Wuliangye Yibin", "tradeable": ""},
        {"ticker": "600887.SS", "name": "Inner Mongolia Yili", "tradeable": ""},
    ],
    "brokers": [
        {"ticker": "600030.SS", "name": "CITIC Securities", "tradeable": "HK:6030"},
        {"ticker": "601211.SS", "name": "Guotai Junan Securities", "tradeable": "HK:2611"},
        {"ticker": "300059.SZ", "name": "East Money Information", "tradeable": ""},
    ],
    "defense": [
        {"ticker": "600760.SS", "name": "AVIC Shenyang Aircraft", "tradeable": ""},
        {"ticker": "000768.SZ", "name": "AVIC Xi'an Aircraft", "tradeable": ""},
        {"ticker": "600893.SS", "name": "AECC Aviation Power", "tradeable": ""},
    ],
    "newenergy": [
        {"ticker": "300750.SZ", "name": "CATL", "tradeable": ""},
        {"ticker": "002594.SZ", "name": "BYD", "tradeable": "HK:1211"},
        {"ticker": "300274.SZ", "name": "Sungrow Power", "tradeable": ""},
    ],
}

# English/global feeds — the foreign-investor view of the market.
# The previous two entries (feeds.reuters.com, caixinglobal.com) were BOTH dead:
# Reuters retired its public RSS endpoint and the Caixin Global URL 404s. Because
# ingestion fails soft per source, nothing ever errored — the news table just sat
# at zero rows. Verified live 2026-08-05; re-verify by item count, not HTTP status.
NEWS_FEEDS = {
    "ft_markets": "https://www.ft.com/markets?format=rss",
    # China economy in English — the direct replacement for Caixin Global.
    "scmp_economy": "https://www.scmp.com/rss/92/feed",
    "guardian_business": "https://www.theguardian.com/uk/business/rss",
}

# Chinese-language domestic financial media. These matter more than the English
# feeds above for A-shares: the market is ~97% domestically owned and >80% retail
# by volume, and the literature finds domestic investors rely on Chinese/state
# media while foreign investors rely on global sources. The feeds above describe
# the market to the wrong audience; these are what the actual price-setters read.
# Fail-soft per source — a blocked or dead feed is skipped, never fatal.
# Every URL here was verified live (2026-08-05): fetched, XML-parsed, item count
# >0, and pubDate within the current day. That bar exists because two weaker ones
# are easy to trip over — sina's roll feed returns HTTP 200 with a boilerplate
# body and ZERO items, and sina's focus feed returns 15 well-formed items dated
# 2018. Neither raises, so "the fetch succeeded" proves nothing; only a dated,
# non-empty item list does. Re-verify the same way before adding a source.
NEWS_FEEDS_ZH = {
    # Market/sector commentary — closest to the signal we want ("沪指涨1.47%
    # 科创50指数大涨4.78% 半导体板块爆发"). ~98 items/day.
    "eastmoney_market": "http://rss.eastmoney.com/rss_partener.xml",
    # Single-name flow: institutional visits, company announcements. ~90/day.
    "eastmoney_stock": "http://rss.eastmoney.com/rss_stock.xml",
    # Policy and macro framing from state media. ~30/day.
    # (The same publisher's scroll-news feed was trialled and dropped: 25 general
    # headlines, every one scoring neutral — volume without signal.)
    "chinanews_finance": "https://www.chinanews.com.cn/rss/finance.xml",
}

# ── Reflection loop guards (spec §4b) ────────────────────────────────────
# A correlation must clear this many independent observations before it is
# allowed to influence model weights — below it, "strong correlation" is noise.
MIN_CORRELATION_SAMPLE = 30
CORRELATION_WINDOWS = [30, 60, 90]  # rolling windows (trading days)

# Weight adjustments suggested by the reflection LLM are logged for review,
# NOT auto-applied, until this is flipped on (spec §4b-iii, human-in-the-loop).
# NOTE: this gates the *LLM's* qualitative suggestions, which carry no
# out-of-sample proof. The empirical learner below is a different mechanism: it
# only ever adopts a change that measurably beat the incumbent on held-out days.
AUTO_APPLY_WEIGHT_ADJUSTMENTS = False

# ── Empirical learning loop (oracle/learning/) ───────────────────────────
# The walk-forward tuner fits signal weights + the abstain threshold per sector
# and adopts a change ONLY when it beats the incumbent on a holdout window the
# search never saw. Every guard below exists to stop the model chasing noise.
LEARNING_ENABLED = _env_flag("ORACLE_LEARNING", True)
# Most-recent trading days reserved as the untouched judgement set. Bigger =
# a more reliable adoption decision (fewer flukes) at the cost of training data.
LEARNING_HOLDOUT_DAYS = int(os.environ.get("ORACLE_LEARNING_HOLDOUT_DAYS") or 45)
# Walk-forward folds used to select a candidate on the remaining history.
LEARNING_FOLDS = int(os.environ.get("ORACLE_LEARNING_FOLDS") or 3)
# Refuse to judge on a holdout thinner than this many scored records / bets.
LEARNING_MIN_HOLDOUT_RECORDS = int(os.environ.get("ORACLE_LEARNING_MIN_RECORDS") or 20)
LEARNING_MIN_HOLDOUT_BETS = int(os.environ.get("ORACLE_LEARNING_MIN_BETS") or 8)
# Fraction of the way to move from the incumbent toward the fitted candidate.
# Chosen a priori as a damping default ("move halfway to the evidence"), NOT
# fitted on the holdout — tuning this on holdout results would be exactly the
# overfitting the holdout exists to prevent. It changes how OFTEN the model
# adopts, never whether an adopted change was validated.
LEARNING_STEP = float(os.environ.get("ORACLE_LEARNING_STEP") or 0.5)
# Required out-of-sample edge_t gain before a change is adopted at all.
LEARNING_MIN_IMPROVEMENT = float(os.environ.get("ORACLE_LEARNING_MIN_GAIN") or 0.15)
# Days a sector must wait between adopted changes. The holdout is the most recent
# N trading days, so it only *refreshes* as time passes; adopting every day would
# repeatedly re-test against nearly the same window and slowly burn it in —
# selection pressure disguised as learning. Waiting lets genuinely new days
# accumulate before the next change is judged.
LEARNING_ADOPT_COOLDOWN_DAYS = int(
    os.environ.get("ORACLE_LEARNING_COOLDOWN_DAYS") or 5)

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
