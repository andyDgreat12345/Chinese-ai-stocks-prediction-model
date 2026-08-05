"""Tests for the news-sentiment predictive-value harness."""
import tempfile

from oracle import db
from oracle.research import news_signal as ns
from oracle.research import sweep as sw


def _seed(conn, rows):
    for r in rows:
        conn.execute(
            "INSERT INTO news (trade_date, source, category, headline, summary,"
            " sentiment, fetched_at) VALUES (?,?,?,?,?,?,?)",
            (r["d"], r["src"], r.get("cat", "general"), "h", "", r["s"], "t"))
    conn.commit()


# ── language attribution ─────────────────────────────────────────────────
def test_language_is_derived_from_the_source_list():
    from oracle import config

    for src in config.NEWS_FEEDS_ZH:
        assert ns.language_of(src) == "zh"
    for src in config.NEWS_FEEDS:
        assert ns.language_of(src) == "en"
    assert ns.language_of("something_unknown") == "en"


# ── daily aggregation ────────────────────────────────────────────────────
def test_daily_sentiment_averages_per_bucket():
    path = tempfile.mktemp(suffix=".db")
    db.init_db(path)
    conn = db.connect(path)
    _seed(conn, [{"d": "2026-01-01", "src": "eastmoney_market",
                  "cat": "chip_export", "s": v} for v in (1.0, 0.0, 0.5, 0.5, 0.0)])
    series = ns.daily_sentiment(path, min_headlines=5)
    assert series["zh:ALL"]["2026-01-01"] == 0.4
    assert series["zh:chip_export"]["2026-01-01"] == 0.4


def test_thin_days_are_dropped():
    """A one-headline day is not a day's sentiment."""
    path = tempfile.mktemp(suffix=".db")
    db.init_db(path)
    conn = db.connect(path)
    _seed(conn, [{"d": "2026-01-01", "src": "eastmoney_market", "s": 1.0}])
    assert ns.daily_sentiment(path, min_headlines=5) == {}


def test_languages_land_in_separate_buckets():
    path = tempfile.mktemp(suffix=".db")
    db.init_db(path)
    conn = db.connect(path)
    _seed(conn, [{"d": "2026-01-01", "src": "eastmoney_market", "s": 1.0}] * 5
               + [{"d": "2026-01-01", "src": "ft_markets", "s": -1.0}] * 5)
    series = ns.daily_sentiment(path, min_headlines=5)
    assert series["zh:ALL"]["2026-01-01"] == 1.0
    assert series["en:ALL"]["2026-01-01"] == -1.0


# ── the lookahead rule ───────────────────────────────────────────────────
def test_lag_zero_is_never_tested():
    """news[D] is fetched AFTER china_close[D], so lag 0 is lookahead — not
    merely untradeable the way it is in the price sweep."""
    assert 0 not in ns.NEWS_LAGS
    assert min(ns.NEWS_LAGS) >= 1


# ── insufficient history is reported as such, not as a null result ───────
def test_short_history_reports_insufficient_not_empty():
    path = tempfile.mktemp(suffix=".db")
    db.init_db(path)
    conn = db.connect(path)
    _seed(conn, [{"d": "2026-01-01", "src": "eastmoney_market", "s": 0.5}] * 5)
    result = ns.run(path)
    assert result["status"] == "insufficient_history"
    assert result["days_short"] == sw.MIN_PAIRS - 1
    # It must not read as "we measured and found nothing".
    report = ns.format_report(result)
    assert "Not measured yet" in report
    assert result["power"], "the power table is the point when data is short"


# ── detectability maths ──────────────────────────────────────────────────
def test_min_detectable_r_falls_as_history_grows():
    a = ns.min_detectable_r(40)
    b = ns.min_detectable_r(250)
    assert a > b > 0
    assert ns.min_detectable_r(3) is None


def test_correcting_for_more_tests_raises_the_bar():
    """Sweeping wider costs sensitivity — that trade must show up in the number."""
    narrow = ns.min_detectable_r(90, 0.10 / 6)
    wide = ns.min_detectable_r(90, 0.10 / 264)
    assert wide > narrow


def test_days_needed_matches_min_detectable_r():
    """The two functions invert each other, so the report cannot contradict itself."""
    n = ns.days_needed_for(0.30, 0.05)
    assert ns.min_detectable_r(n, 0.05) <= 0.30
    assert ns.days_needed_for(0.0) is None
