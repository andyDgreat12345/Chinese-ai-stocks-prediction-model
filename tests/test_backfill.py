"""Tests for the historical backfill loader (pure transforms + offline e2e)."""
import tempfile

from oracle import backfill as bf
from oracle import backtest, config, db


def test_series_to_rows_computes_pct_change_first_is_null():
    rows = bf.series_to_rows("^GSPC", "broad",
                             [("2026-08-01", 100.0), ("2026-08-02", 110.0)], "t")
    assert rows[0]["pct_change"] is None      # no prior close
    assert rows[1]["pct_change"] == 10.0
    assert rows[0]["sector"] == "broad"


def test_extract_dated_series_english_and_sorts():
    recs = [{"date": "2026-08-02", "close": 2}, {"date": "2026-08-01", "close": 1}]
    assert bf.extract_dated_series(recs) == [("2026-08-01", 1.0), ("2026-08-02", 2.0)]


def test_extract_dated_series_chinese_columns():
    recs = [{"日期": "2026-08-01", "收盘": 1.5}]
    assert bf.extract_dated_series(recs) == [("2026-08-01", 1.5)]


def test_extract_dated_series_missing_columns_returns_empty():
    assert bf.extract_dated_series([{"foo": 1, "bar": 2}]) == []


def test_trim_days_keeps_window_plus_one():
    series = [(f"d{i}", float(i)) for i in range(10)]
    assert len(bf._trim_days(series, 3)) == 4   # window + 1 for the first pct baseline


def test_backfill_then_backtest_end_to_end(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)

    # Stub the network: US symbols and China indices/ETFs return synthetic series.
    monkeypatch.setattr(bf, "_download_us", lambda sym, days: sym)
    monkeypatch.setattr(bf, "_us_records", lambda df: [
        {"date": "2026-08-01", "close": 100.0}, {"date": "2026-08-02", "close": 102.0}])
    monkeypatch.setattr(bf, "_download_china_index", lambda code: [
        {"date": "2026-08-01", "close": 10.0}, {"date": "2026-08-02", "close": 10.2}])
    monkeypatch.setattr(bf, "_download_china_etf", lambda code: [
        {"日期": "2026-08-01", "收盘": 1.0}, {"日期": "2026-08-02", "收盘": 1.03}])

    n = bf.backfill(days=180)
    assert n > 0

    # rows landed in both tables
    assert db.get_rows_for_date("us_close", "2026-08-02", db_path=tmp)
    assert db.get_rows_for_date("china_close", "2026-08-02", db_path=tmp)

    # and the backtest can now run over the backfilled history
    report = backtest.run_backtest(db_path=tmp)
    assert report["n_records"] > 0
