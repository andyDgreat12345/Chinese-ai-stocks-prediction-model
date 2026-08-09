"""Tests for the pure ingestion transforms + retry helper (spec §6.2)."""
import pytest

from oracle.ingestion._retry import with_retries
from oracle.ingestion import us_market, china_market, news


def test_retry_succeeds_after_transient_failures():
    calls = {"n": 0}

    @with_retries(attempts=4, base_delay=0, sleep=lambda _: None)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("flake")
        return "ok"

    assert flaky() == "ok"
    assert calls["n"] == 3


def test_retry_reraises_after_exhausting_attempts():
    @with_retries(attempts=3, base_delay=0, sleep=lambda _: None)
    def always_fails():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        always_fails()


def test_us_normalize_computes_pct_change():
    rows = us_market.normalize(
        {"^IXIC": [100.0, 110.0], "XLE": [50.0, 49.0]},
        fetched_at="2026-08-02T04:15:00Z", trade_date="2026-08-02",
    )
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["^IXIC"]["pct_change"] == 10.0
    assert by_sym["^IXIC"]["sector"] == "tech"
    assert by_sym["XLE"]["pct_change"] == -2.0


def test_us_normalize_handles_single_close():
    rows = us_market.normalize(
        {"^GSPC": [4200.0]}, fetched_at="t", trade_date="2026-08-02"
    )
    assert rows[0]["pct_change"] is None
    assert rows[0]["close"] == 4200.0


def test_us_normalize_skips_empty_series():
    rows = us_market.normalize({"SOXX": []}, fetched_at="t", trade_date="d")
    assert rows == []


def test_pick_close_series_english_index_columns():
    records = [{"date": "d1", "close": 100.0}, {"date": "d2", "close": 110.0}]
    assert china_market.pick_close_series(records) == [100.0, 110.0]


def test_pick_close_series_chinese_etf_columns():
    # akshare ETF endpoint returns Chinese column names.
    records = [{"日期": "d1", "收盘": 50.0}, {"日期": "d2", "收盘": 49.0}]
    assert china_market.pick_close_series(records) == [50.0, 49.0]


def test_pick_close_series_no_close_column():
    assert china_market.pick_close_series([{"date": "d1", "volume": 10}]) == []


def test_china_normalize_uses_sector_tags():
    rows = china_market.normalize(
        {"512480": [100.0, 103.0]}, fetched_at="t", trade_date="2026-08-02",
        sector_tags={"512480": "semis"},
    )
    assert rows[0]["sector"] == "semis"
    assert rows[0]["pct_change"] == 3.0


def test_sina_etf_symbol_prefix():
    assert china_market.sina_etf_symbol("510300") == "sh510300"  # Shanghai
    assert china_market.sina_etf_symbol("512480") == "sh512480"
    assert china_market.sina_etf_symbol("159915") == "sz159915"  # Shenzhen
    assert china_market.sina_etf_symbol(159930) == "sz159930"    # int tolerated


def test_download_etf_falls_back_to_sina_when_eastmoney_fails(monkeypatch):
    calls = []

    def em_reset(code):
        calls.append("eastmoney")
        raise ConnectionError("Remote end closed connection without response")

    def sina_ok(code):
        calls.append("sina")
        return [{"date": "2026-08-01", "close": 1.0}]  # non-empty -> usable

    monkeypatch.setattr(china_market, "ETF_SOURCES",
                        (("eastmoney", em_reset), ("sina", sina_ok)))
    df = china_market.download_etf_history("512480")
    assert df == [{"date": "2026-08-01", "close": 1.0}]
    assert calls == ["eastmoney", "sina"]   # tried EM first, then Sina


def test_download_etf_skips_empty_source_and_tries_next(monkeypatch):
    # a source that returns an empty frame is treated as a miss, not a success
    monkeypatch.setattr(china_market, "ETF_SOURCES", (
        ("eastmoney", lambda code: []),
        ("sina", lambda code: [{"date": "d", "close": 2.0}]),
    ))
    assert china_market.download_etf_history("510300") == [{"date": "d", "close": 2.0}]


def test_download_etf_raises_when_all_sources_fail(monkeypatch):
    def boom(code):
        raise ConnectionError("reset")

    monkeypatch.setattr(china_market, "ETF_SOURCES",
                        (("eastmoney", boom), ("sina", boom)))
    with pytest.raises(ConnectionError):
        china_market.download_etf_history("512800")


def test_parse_feed_tags_sentiment_and_category():
    fixture = {"entries": [
        {"title": "Fed signals rate cut, stocks rally", "summary": "<p>Markets surge.</p>"},
        {"title": "", "summary": "no title should be skipped"},
    ]}
    rows = news.parse_feed("reuters", fixture, fetched_at="t", trade_date="2026-08-02")
    assert len(rows) == 1
    assert rows[0]["category"] == "fed_policy"
    assert rows[0]["sentiment"] > 0
    assert "<" not in rows[0]["summary"]  # HTML stripped


# ── sector universe consistency ───────────────────────────────────────────
def test_every_sector_is_fully_configured():
    """A half-added sector fails silently: it would be predicted but never
    scored, or scored but never traded. Every map must cover every sector."""
    from oracle import config
    from oracle.analysis.pipeline import (CHINA_SECTORS, CHINA_SPILLOVER_SOURCES,
                                          SECTOR_NEWS_CATEGORIES)

    for name, mapping in (("CHINA_SECTOR_ETFS", config.CHINA_SECTOR_ETFS),
                          ("SECTOR_TRADEABLE_ETF", config.SECTOR_TRADEABLE_ETF),
                          ("SECTOR_STOCKS", config.SECTOR_STOCKS),
                          ("CHINA_SPILLOVER_SOURCES", CHINA_SPILLOVER_SOURCES),
                          ("SECTOR_NEWS_CATEGORIES", SECTOR_NEWS_CATEGORIES)):
        missing = [s for s in CHINA_SECTORS if s not in mapping]
        assert not missing, f"{name} missing: {missing}"


def test_etf_codes_are_unique_per_sector():
    """Two sectors sharing a code would be the same instrument twice — the
    simulator would treat them as independent positions."""
    from oracle import config

    codes = list(config.CHINA_SECTOR_ETFS.values())
    assert len(codes) == len(set(codes))


def test_every_spillover_source_is_actually_ingested():
    """A China sector mapped to a US tag we never fetch gets a permanently zero
    spillover signal — dead on arrival, and invisible."""
    from oracle.analysis.pipeline import CHINA_SPILLOVER_SOURCES
    from oracle.ingestion.us_market import SECTOR_TAGS

    available = set(SECTOR_TAGS.values())
    for sector, tags in CHINA_SPILLOVER_SOURCES.items():
        unknown = [t for t in tags if t not in available]
        assert not unknown, f"{sector} maps to un-ingested US tag(s): {unknown}"


# ── ETF code verification ─────────────────────────────────────────────────
def test_verify_reports_dead_codes(monkeypatch):
    from oracle.ingestion import china_market as cm

    def fake(code):
        if code == "BAD":
            raise OSError("no such fund")
        return [{"close": 1.0}, {"close": 2.0}]

    monkeypatch.setattr(cm, "download_etf_history", fake)
    monkeypatch.setattr(cm, "_to_records", lambda df: df)
    res = cm.verify_sector_etfs({"good": "510300", "broken": "BAD"})
    assert res["good"]["ok"] and res["good"]["bars"] == 2
    assert not res["broken"]["ok"] and "no such fund" in res["broken"]["error"]
    report = cm.format_verification(res)
    assert "UNRESOLVED" in report and "broken" in report


def test_verify_flags_a_code_that_resolves_but_is_empty(monkeypatch):
    """The nastier case: the endpoint answers, but with nothing in it — exactly
    how the dead news feeds passed as healthy."""
    from oracle.ingestion import china_market as cm

    monkeypatch.setattr(cm, "download_etf_history", lambda code: [])
    monkeypatch.setattr(cm, "_to_records", lambda df: [])
    res = cm.verify_sector_etfs({"hollow": "999999"})
    assert not res["hollow"]["ok"]
    assert "no usable closes" in res["hollow"]["error"]


# ── trade_date comes from the data, not the wall clock ────────────────────
def test_china_normalize_prefers_the_sources_own_bar_date():
    """Live bug: the job ran at 15:15 CST and stamped the UTC date, so a Saturday
    run filed Friday's bar as a Saturday session."""
    from oracle.ingestion.china_market import normalize

    rows = normalize(
        {"510300": {"closes": [4.70, 4.75], "ohlc": {"close": 4.75},
                    "bar_date": "2026-08-07"}},
        "t", "2026-08-08", {"510300": "broad"})
    assert rows[0]["trade_date"] == "2026-08-07", "must use the bar's own date"


def test_china_normalize_falls_back_when_the_source_gives_no_date():
    from oracle.ingestion.china_market import normalize

    rows = normalize({"510300": {"closes": [1.0, 2.0], "ohlc": {}}},
                     "t", "2026-08-08", {"510300": "broad"})
    assert rows[0]["trade_date"] == "2026-08-08"


def test_us_normalize_uses_per_symbol_bar_dates():
    from oracle.ingestion.us_market import normalize

    rows = normalize({"^GSPC": [100.0, 101.0], "XLE": [50.0, 51.0]}, "t",
                     "2026-08-08", {"^GSPC": "2026-08-07"})
    by = {r["symbol"]: r["trade_date"] for r in rows}
    assert by["^GSPC"] == "2026-08-07"
    assert by["XLE"] == "2026-08-08"      # no bar date -> fallback


def test_us_bar_dates_are_per_symbol_not_per_frame():
    """yfinance pads every symbol to a shared index, so a symbol that did not
    print today carries a trailing NaN — its real last bar is earlier. Using the
    frame's final index date for everything would re-date those stale bars."""
    from oracle.ingestion.us_market import extract_bar_dates

    class F:
        index = ["2026-08-05", "2026-08-06", "2026-08-07"]

    closes = {"fresh": [1.0, 2.0, 3.0], "stale": [1.0, 2.0, None], "empty": []}
    dates = extract_bar_dates(F(), ["fresh", "stale", "empty"], closes)
    assert dates["fresh"] == "2026-08-07"
    assert dates["stale"] == "2026-08-06"      # not re-dated to today
    assert "empty" not in dates


def test_us_bar_dates_degrade_safely_without_an_index():
    from oracle.ingestion.us_market import extract_bar_dates

    assert extract_bar_dates(object(), ["A"], {"A": [1.0]}) == {}
