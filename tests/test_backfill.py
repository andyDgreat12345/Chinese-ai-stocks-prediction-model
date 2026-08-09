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


def test_purge_phantom_sessions_removes_weekend_rows_only():
    """The fetch-clock bug filed Friday's bar as a Saturday session. Weekend rows
    are unambiguous phantoms; a flat weekday is not, so duplicates are kept."""
    import tempfile
    from oracle import db
    from oracle.backfill import purge_phantom_sessions

    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    rows = [
        {"trade_date": "2026-08-07", "symbol": "X", "sector": "broad",  # Friday
         "close": 1.0, "pct_change": 0.5, "fetched_at": "t"},
        {"trade_date": "2026-08-08", "symbol": "X", "sector": "broad",  # Saturday
         "close": 1.0, "pct_change": 0.5, "fetched_at": "t"},
        {"trade_date": "2026-08-09", "symbol": "X", "sector": "broad",  # Sunday
         "close": 1.0, "pct_change": 0.5, "fetched_at": "t"},
    ]
    db.upsert_market_close("china_close", rows, db_path=tmp)
    out = purge_phantom_sessions(tmp)
    assert out["china_close"] == 2
    left = [r["trade_date"] for r in db.close_series("china_close", symbol="X",
                                                     limit=100, db_path=tmp)]
    assert left == ["2026-08-07"]
    # idempotent
    assert purge_phantom_sessions(tmp)["china_close"] == 0


# ── history depth ─────────────────────────────────────────────────────────
def test_all_history_keeps_every_bar():
    """The 365-day trim was discarding data already downloaded: the ETF endpoints
    serve 1,559–3,557 bars per fund and we stored 371 of them."""
    from oracle.backfill import ALL_HISTORY, _trim_days

    series = [(f"2020-01-{i:02}", float(i)) for i in range(1, 29)]
    assert len(_trim_days(series, ALL_HISTORY)) == len(series)
    assert len(_trim_days(series, 0)) == len(series)
    assert len(_trim_days(series, -1)) == len(series)


def test_trim_keeps_one_extra_bar_for_the_first_pct_change():
    from oracle.backfill import _trim_days

    series = [(f"2020-01-{i:02}", float(i)) for i in range(1, 29)]
    assert len(_trim_days(series, 10)) == 11


def test_us_download_asks_for_max_when_all_history_requested(monkeypatch):
    """yfinance caps a '<n>d' period short of a full listing history. Every
    US<->China pairing is bounded by the shorter leg, so a short US side would
    cap the sweep no matter how much China history exists."""
    import sys, types

    seen = {}
    fake = types.ModuleType("yfinance")
    fake.download = lambda symbol, **kw: seen.update(kw) or None
    monkeypatch.setitem(sys.modules, "yfinance", fake)

    from oracle.backfill import _download_us

    _download_us("^GSPC", 0)
    assert seen["period"] == "max"
    _download_us("^GSPC", 365)
    assert seen["period"] == "365d"


def test_backfill_cli_accepts_all(monkeypatch):
    from oracle import backfill as bf

    got = {}
    monkeypatch.setattr(bf, "backfill", lambda days: got.setdefault("days", days))
    for word in ("all", "max", "full", "ALL"):
        got.clear()
        bf.main([word])
        assert got["days"] == bf.ALL_HISTORY, word
    got.clear()
    bf.main(["365"])
    assert got["days"] == 365


def test_prune_history_trims_to_the_window_and_spares_predictions():
    """Capping what the backfill fetches does not remove what is already stored;
    without pruning, reports keep spanning 1990 while the learner is bounded to
    ten years — two different answers to how far back the system looks."""
    import tempfile
    from oracle import db
    from oracle.backfill import prune_history

    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    rows = [{"trade_date": d, "symbol": "X", "sector": "broad", "close": 1.0,
             "pct_change": 0.1, "fetched_at": "t"}
            for d in ("1999-01-04", "2015-06-01", "2020-01-02", "2026-08-07")]
    db.upsert_market_close("china_close", rows, db_path=tmp)
    db.upsert_prediction({"trade_date": "1999-01-04", "sector": "broad",
                          "direction": "bullish", "confidence": "low",
                          "composite_score": 0.1, "us_spillover": 0.0,
                          "sentiment_score": 0.0, "macro_flag": 0,
                          "rationale": "r", "created_at": "t"}, db_path=tmp)

    out = prune_history("2016-01-01", db_path=tmp)
    assert out["china_close"] == 2
    left = [r["trade_date"] for r in db.close_series("china_close", symbol="X",
                                                     limit=100, db_path=tmp)]
    assert left == ["2020-01-02", "2026-08-07"]
    # the system's own record of what it said is an audit trail, not market data
    assert db.predictions_for_date("1999-01-04", db_path=tmp)
    assert prune_history("2016-01-01", db_path=tmp)["china_close"] == 0
