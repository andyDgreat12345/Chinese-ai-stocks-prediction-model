"""Tests for OHLC storage, the calibrated forecast, and the charts endpoint."""
import tempfile

import pytest

from oracle import backfill, config, db
from oracle.analysis import forecast as fc


# ── OHLC extraction + storage ─────────────────────────────────────────────
def test_extract_dated_ohlc_english_and_chinese_columns():
    en = backfill.extract_dated_ohlc([
        {"date": "2026-08-03", "open": 1.0, "high": 1.5, "low": 0.9, "close": 1.2}])
    assert en[0][1] == {"close": 1.2, "open": 1.0, "high": 1.5, "low": 0.9}
    cn = backfill.extract_dated_ohlc([
        {"日期": "2026-08-03", "开盘": 1.0, "最高": 1.5, "最低": 0.9, "收盘": 1.2}])
    assert cn[0][1]["high"] == 1.5


def test_extract_dated_ohlc_tolerates_close_only_source():
    out = backfill.extract_dated_ohlc([{"date": "2026-08-03", "close": 1.2}])
    assert out[0][1] == {"close": 1.2, "open": None, "high": None, "low": None}


def test_ohlc_to_rows_computes_pct_from_prev_close():
    series = [("2026-08-03", {"close": 10.0, "open": 9.8, "high": 10.2, "low": 9.7}),
              ("2026-08-04", {"close": 11.0, "open": 10.1, "high": 11.2, "low": 10.0})]
    rows = backfill.ohlc_to_rows("512480", "semis", series, "t")
    assert rows[0]["pct_change"] is None            # no prior close
    assert rows[1]["pct_change"] == 10.0
    assert rows[1]["high"] == 11.2


def test_ohlc_round_trips_and_close_only_upsert_preserves_it():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    db.upsert_market_close("china_close", [
        {"trade_date": "2026-08-04", "symbol": "512480", "sector": "semis",
         "close": 10.0, "open": 9.5, "high": 10.4, "low": 9.4,
         "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
    row = db.close_series("china_close", symbol="512480", db_path=tmp)[0]
    assert (row["open"], row["high"], row["low"]) == (9.5, 10.4, 9.4)
    # a later close-only write (e.g. the legacy path) must not wipe the OHLC
    db.upsert_market_close("china_close", [
        {"trade_date": "2026-08-04", "symbol": "512480", "sector": "semis",
         "close": 10.5, "pct_change": 2.0, "fetched_at": "t2"}], db_path=tmp)
    row = db.close_series("china_close", symbol="512480", db_path=tmp)[0]
    assert row["close"] == 10.5 and row["high"] == 10.4      # updated, not nulled


def test_init_db_migrates_ohlc_columns_onto_an_old_table():
    tmp = tempfile.mktemp(suffix=".db")
    conn = db.connect(tmp)
    conn.executescript(
        """CREATE TABLE china_close (trade_date TEXT, symbol TEXT, sector TEXT,
             close REAL, pct_change REAL, fetched_at TEXT,
             PRIMARY KEY (trade_date, symbol))""")
    conn.commit(); conn.close()
    db.init_db(tmp)
    cols = {r["name"] for r in db.connect(tmp).execute("PRAGMA table_info(china_close)")}
    assert {"open", "high", "low"} <= cols


# ── calibrated forecast (no invented precision) ───────────────────────────
def _recs(moves, sector="semis", direction="bullish"):
    return [{"sector": sector, "model_dir": direction, "actual_move": m} for m in moves]


def test_percentile_interpolates():
    assert fc.percentile([0.0, 10.0], 0.5) == 5.0
    assert fc.percentile([1.0], 0.9) == 1.0
    assert fc.percentile([], 0.5) is None


def test_distribution_reports_measured_range_and_hit_rate():
    moves = [-2.0, -1.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 0.2, 0.8, 1.1, 1.3]
    d = fc.outcome_distribution(_recs(moves), "semis", "bullish", min_sample=5)
    assert d["enough"] and d["n"] == 12
    assert d["p10_move_pct"] < d["median_move_pct"] < d["p90_move_pct"]
    assert d["hit_rate"] == round(10 / 12, 4)          # 10 of 12 moves were up


def test_distribution_refuses_below_min_sample():
    d = fc.outcome_distribution(_recs([1.0, 2.0]), "semis", "bullish", min_sample=12)
    assert d["enough"] is False and d["n"] == 2
    assert "median_move_pct" not in d                   # no number is offered at all


def test_bearish_call_signed_return_flips():
    d = fc.outcome_distribution(_recs([-1.0] * 12, direction="bearish"),
                                "semis", "bearish", min_sample=5)
    assert d["median_move_pct"] == -1.0                 # raw market move
    assert d["median_signed_return_pct"] == 1.0         # gain to a bearish position


def test_format_range_is_honest_when_thin():
    assert "insufficient history" in fc.format_range({"enough": False, "n": 3})


# ── charts endpoint ───────────────────────────────────────────────────────
def test_charts_endpoint_shape(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    for i in range(5):
        d = f"2026-08-0{i + 1}"
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "512480", "sector": "semis",
             "close": 10.0 + i, "open": 9.9 + i, "high": 10.4 + i, "low": 9.7 + i,
             "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
    from oracle.api import server
    body = TestClient(server.app).get("/api/charts?days=30").json()
    semis = body["sectors"]["semis"]
    assert semis["symbol"] == "512480"
    assert semis["has_ohlc"] is True
    assert semis["bars"][0]["o"] is not None and semis["bars"][-1]["c"] == 14.0
    assert body["disclaimer"]


def test_charts_marks_close_only_history(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    db.upsert_market_close("china_close", [
        {"trade_date": "2026-08-01", "symbol": "512480", "sector": "semis",
         "close": 10.0, "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
    from oracle.api import server
    body = TestClient(server.app).get("/api/charts").json()
    # no OHLC -> the front-end draws a line instead of fabricating candles
    assert body["sectors"]["semis"]["has_ohlc"] is False
