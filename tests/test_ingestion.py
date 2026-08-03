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
