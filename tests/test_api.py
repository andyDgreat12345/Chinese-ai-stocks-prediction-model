"""Smoke tests for the FastAPI endpoints (spec §5, Phase 4).

Skipped cleanly if fastapi/httpx aren't installed, so the core suite still runs
in a minimal environment.
"""
import tempfile

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from oracle import config, db  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db()
    db.upsert_market_close("us_close", [
        {"trade_date": "2026-08-02", "symbol": "SOXX", "sector": "semis",
         "close": 100.0, "pct_change": 2.5, "fetched_at": "t"},
    ])
    db.insert_news([
        {"trade_date": "2026-08-02", "source": "r", "category": "chip_export",
         "headline": "chips surge", "summary": "", "sentiment": 0.7, "fetched_at": "t"},
    ])
    from oracle.analysis.pipeline import run_analysis
    run_analysis("2026-08-02")

    from oracle.api import server
    return TestClient(server.app)


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_prediction_has_disclaimer_and_signals(client):
    body = client.get("/api/prediction").json()
    assert body["disclaimer"]                       # spec §0/§5: always present
    assert len(body["predictions"]) == 5
    semis = next(p for p in body["predictions"] if p["sector"] == "semis")
    assert semis["direction"] == "bullish"
    assert "us_spillover" in semis["signals"]       # component signals exposed


def test_heatmap_cells(client):
    cells = client.get("/api/heatmap").json()["cells"]
    assert {c["sector"] for c in cells} >= {"semis", "energy", "broad"}


def test_accuracy_empty_before_scoring(client):
    # No actuals scored yet -> hit_rate is None, not a fabricated number.
    assert client.get("/api/accuracy").json()["overall"]["hit_rate"] is None


def test_dashboard_shell_and_static_serve(client):
    root = client.get("/")
    assert root.status_code == 200
    assert "China Market Oracle" in root.text
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_markets_splits_indices_sectors_metals(client):
    # seed a couple of US rows across sector tags
    db.upsert_market_close("us_close", [
        {"trade_date": "2026-08-02", "symbol": "^GSPC", "sector": "broad",
         "close": 5200.0, "pct_change": 1.0, "fetched_at": "t"},
        {"trade_date": "2026-08-02", "symbol": "GC=F", "sector": "gold",
         "close": 2400.0, "pct_change": 0.3, "fetched_at": "t"},
    ])
    m = client.get("/api/markets").json()
    assert any(r["symbol"] == "^GSPC" for r in m["indices"])
    assert any(r["symbol"] == "GC=F" for r in m["metals"])
