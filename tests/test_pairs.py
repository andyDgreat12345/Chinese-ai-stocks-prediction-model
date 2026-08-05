"""Tests for the US→China lead/lag pairing (and the lag-0 tradeability guard)."""
import tempfile

import pytest

from oracle import config, db
from oracle.analysis import pairs as pr


# ── the tradeability distinction (the whole point of this module) ─────────
def test_lag_zero_is_not_predictive():
    assert pr.is_predictive(0) is False
    assert pr.is_predictive(1) is True and pr.is_predictive(2) is True


def test_lag_note_explains_the_timing_for_same_day():
    note = pr.lag_note(0)
    assert "NOT tradeable" in note and "LATER" in note
    assert "leads" in pr.lag_note(1)


def test_rank_puts_tradeable_pairs_first_even_when_weaker():
    rows = [
        {"us_symbol": "SOXX", "china_symbol": "510300", "correlation": 0.64, "best_lag": 0},
        {"us_symbol": "XLE", "china_symbol": "159930", "correlation": 0.41, "best_lag": 1},
    ]
    ranked = pr.rank_pairs(rows)
    # the weaker but ACTIONABLE lag-1 pair must outrank the strong same-day one
    assert ranked[0]["us_symbol"] == "XLE"
    assert ranked[1]["best_lag"] == 0


# ── rebasing + lag alignment ──────────────────────────────────────────────
def test_rebase_is_percent_from_first_point():
    out = pr.rebase([("d1", 100.0), ("d2", 110.0), ("d3", 90.0)])
    assert [p["v"] for p in out] == [0.0, 10.0, -10.0]


def test_rebase_handles_empty_and_zero_base():
    assert pr.rebase([]) == []
    assert pr.rebase([("d1", 0.0), ("d2", 5.0)]) == []


def test_align_with_lag_shifts_us_forward():
    us = [("d1", 100.0), ("d2", 110.0), ("d3", 121.0)]
    china = [("d1", 10.0), ("d2", 11.0), ("d3", 12.0)]
    a = pr.align_with_lag(us, china, 1)
    # China d2/d3 pair with US d1/d2 -> the US series leads by one session
    assert a["dates"] == ["d2", "d3"]
    assert [p["v"] for p in a["us"]] == [0.0, 10.0]        # US d1->d2 = +10%
    assert [p["v"] for p in a["china"]] == [0.0, round((12 / 11 - 1) * 100, 4)]


def test_align_with_lag_zero_is_same_day():
    us = [("d1", 100.0), ("d2", 110.0)]
    china = [("d1", 10.0), ("d2", 11.0)]
    a = pr.align_with_lag(us, china, 0)
    assert a["dates"] == ["d1", "d2"]


def test_align_uses_only_common_dates():
    us = [("d1", 100.0), ("d2", 110.0), ("d4", 120.0)]
    china = [("d2", 10.0), ("d3", 11.0), ("d4", 12.0)]
    a = pr.align_with_lag(us, china, 1)
    assert a["dates"] == ["d4"]          # common = d2,d4 -> lag1 leaves d4


def test_align_returns_empty_when_too_short_for_the_lag():
    a = pr.align_with_lag([("d1", 1.0)], [("d1", 1.0)], 3)
    assert a == {"dates": [], "us": [], "china": []}


def test_build_pair_carries_metadata_and_flags():
    row = {"us_symbol": "XLE", "china_symbol": "159930", "correlation": 0.41,
           "best_lag": 1, "sample_size": 30, "window_days": 30}
    us = [(f"d{i}", 100.0 + i) for i in range(10)]
    cn = [(f"d{i}", 10.0 + i * 0.1) for i in range(10)]
    p = pr.build_pair(row, us, cn)
    assert p["predictive"] is True and p["best_lag"] == 1
    assert p["us_symbol"] == "XLE" and len(p["dates"]) > 0
    assert "leads" in p["lag_note"]


# ── endpoint ──────────────────────────────────────────────────────────────
def test_pairs_endpoint_ranks_and_labels(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    dates = [f"2026-06-{d:02d}" for d in range(1, 21)]
    for i, d in enumerate(dates):
        db.upsert_market_close("us_close", [
            {"trade_date": d, "symbol": "XLE", "sector": "energy",
             "close": 100.0 + i, "pct_change": 1.0, "fetched_at": "t"},
            {"trade_date": d, "symbol": "SOXX", "sector": "semis",
             "close": 50.0 + i, "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "159930", "sector": "energy",
             "close": 1.6 + i * 0.01, "pct_change": 1.0, "fetched_at": "t"},
            {"trade_date": d, "symbol": "510300", "sector": "broad",
             "close": 4.6 + i * 0.01, "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
    conn = db.connect(tmp)
    conn.executemany(
        """INSERT INTO correlations (us_symbol, china_symbol, window_days,
               correlation, best_lag, sample_size, established, computed_at)
           VALUES (?,?,?,?,?,?,1,'t')""",
        [("SOXX", "510300", 30, 0.64, 0, 30),      # strong but same-day
         ("XLE", "159930", 30, 0.41, 1, 30)])      # weaker but tradeable
    conn.commit(); conn.close()

    from oracle.api import server
    body = TestClient(server.app).get("/api/pairs").json()
    assert body["timing"]["hours_us_after_china"] == 14
    first = body["pairs"][0]
    assert first["us_symbol"] == "XLE" and first["predictive"] is True
    assert any(p["predictive"] is False for p in body["pairs"])   # lag-0 still shown
    assert first["us"] and first["china"]
