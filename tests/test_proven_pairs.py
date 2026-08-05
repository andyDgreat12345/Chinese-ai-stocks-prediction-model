"""Tests for labels, proven-pair registration, and the reflection refresh."""
import tempfile

import pytest

from oracle import config, db
from oracle.research import labels as lb


# ── labels: every displayed symbol resolves to something readable ─────────
def test_china_and_us_symbols_are_labelled():
    a = lb.label("512480")
    assert a["name"] == "Semiconductor ETF" and a["sector"] == "semis"
    b = lb.label("^VIX")
    assert b["sector"] == "volatility" and b["kind"] == "volatility"


def test_company_specific_instruments_carry_the_company():
    baba = lb.label("BABA")
    assert baba["company"] == "Alibaba Group" and baba["kind"] == "adr"
    # an index is NOT company-specific
    assert lb.label("^GSPC")["company"] == ""
    assert lb.company_of("510300") == ""


def test_watchlist_stocks_resolve_from_config():
    smic = lb.label("688981.SS")
    assert "SMIC" in smic["name"] and smic["sector"] == "semis"
    assert smic["kind"] == "stock" and smic["company"]


def test_unknown_symbol_degrades_instead_of_raising():
    out = lb.label("NOT_A_TICKER")
    assert out["symbol"] == "NOT_A_TICKER" and out["sector"] == "unknown"
    assert out["display"] == "NOT_A_TICKER"


def test_display_string_includes_sector():
    assert lb.short_label(lb.label("XLE")) == "Energy sector · energy"


# ── proven pairs: register, refresh, rank ─────────────────────────────────
def _seed(tmp):
    db.init_db(tmp)
    db.upsert_proven_pair({
        "us_symbol": "XLE", "china_symbol": "159930", "lag": 1,
        "r_discovered": 0.293, "q_value": 0.0, "n_discovered": 353,
        "discovered_on": "2026-08-05"}, db_path=tmp)
    return tmp


def test_register_and_read_back():
    tmp = _seed(tempfile.mktemp(suffix=".db"))
    rows = db.proven_pairs(tmp)
    assert len(rows) == 1
    assert rows[0]["r_discovered"] == 0.293 and rows[0]["refresh_count"] == 0
    assert rows[0]["current_r"] is None          # not yet refreshed


def test_refresh_updates_factor_and_increments_count():
    tmp = _seed(tempfile.mktemp(suffix=".db"))
    db.refresh_proven_pair("XLE", "159930", 1, 0.251, 360, "2026-08-06", tmp)
    db.refresh_proven_pair("XLE", "159930", 1, 0.240, 361, "2026-08-07", tmp)
    row = db.proven_pairs(tmp)[0]
    assert row["current_r"] == 0.240 and row["current_n"] == 361
    assert row["refresh_count"] == 2 and row["refreshed_on"] == "2026-08-07"
    # discovery stats are preserved — decay is visible against them
    assert row["r_discovered"] == 0.293


def test_rediscovery_preserves_refresh_counters():
    tmp = _seed(tempfile.mktemp(suffix=".db"))
    db.refresh_proven_pair("XLE", "159930", 1, 0.25, 360, "2026-08-06", tmp)
    db.upsert_proven_pair({                       # a later sweep re-finds it
        "us_symbol": "XLE", "china_symbol": "159930", "lag": 1,
        "r_discovered": 0.310, "q_value": 0.0, "n_discovered": 400,
        "discovered_on": "2026-09-01"}, db_path=tmp)
    row = db.proven_pairs(tmp)[0]
    assert row["r_discovered"] == 0.310           # discovery stats updated
    assert row["refresh_count"] == 1              # ...counters not reset


def test_reflection_refresh_recomputes_and_accumulates(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed(tmp)
    dates = [f"2026-06-{d:02d}" for d in range(1, 26)]
    for i, d in enumerate(dates):
        # China day t tracks US day t-1 -> a real lag-1 relationship
        us_pct = 1.0 if i % 2 == 0 else -1.0
        cn_pct = 1.0 if (i - 1) % 2 == 0 else -1.0
        db.upsert_market_close("us_close", [
            {"trade_date": d, "symbol": "XLE", "sector": "energy", "close": 100.0,
             "pct_change": us_pct, "fetched_at": "t"}], db_path=tmp)
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "159930", "sector": "energy", "close": 1.6,
             "pct_change": cn_pct, "fetched_at": "t"}], db_path=tmp)

    from oracle.reflection.correlation import refresh_proven_pairs
    assert refresh_proven_pairs(db_path=tmp) == 1
    row = db.proven_pairs(tmp)[0]
    assert row["current_r"] is not None and row["refresh_count"] == 1
    # the reading also lands in the accumulation history
    obs = db.correlation_observations("XLE", "159930", 1, db_path=tmp)
    assert len(obs) == 1 and obs[0]["correlation"] == row["current_r"]


def test_refresh_is_noop_and_safe_with_no_proven_pairs():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    from oracle.reflection.correlation import refresh_proven_pairs
    assert refresh_proven_pairs(db_path=tmp) == 0


# ── the hub endpoint groups by US symbol and labels everything ────────────
def test_proven_hubs_groups_by_us_symbol(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    for cn in ("159930", "512480"):               # one US hub, two China legs
        db.upsert_proven_pair({
            "us_symbol": "XLE", "china_symbol": cn, "lag": 1, "r_discovered": 0.29,
            "q_value": 0.0, "n_discovered": 353, "discovered_on": "2026-08-05"},
            db_path=tmp)
    dates = [f"2026-06-{d:02d}" for d in range(1, 21)]
    for i, d in enumerate(dates):
        db.upsert_market_close("us_close", [
            {"trade_date": d, "symbol": "XLE", "sector": "energy",
             "close": 100.0 + i, "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
        for cn in ("159930", "512480"):
            db.upsert_market_close("china_close", [
                {"trade_date": d, "symbol": cn, "sector": "energy",
                 "close": 1.6 + i * 0.01, "pct_change": 1.0, "fetched_at": "t"}],
                db_path=tmp)

    from oracle.api import server
    body = TestClient(server.app).get("/api/proven-hubs").json()
    assert body["n_pairs"] == 2
    hub = body["hubs"][0]
    assert hub["us_symbol"] == "XLE" and hub["n_counterparts"] == 2   # grouped, not repeated
    assert hub["us_label"]["name"] == "Energy sector"
    assert {l["china_symbol"] for l in hub["legs"]} == {"159930", "512480"}
    assert all(l["china_label"]["sector"] for l in hub["legs"])       # labelled
