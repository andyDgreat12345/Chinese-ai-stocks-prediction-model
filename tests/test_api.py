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
    from oracle.analysis.pipeline import CHINA_SECTORS
    # Bound to the configured universe, not a literal — the sector list grows.
    assert len(body["predictions"]) == len(CHINA_SECTORS)
    semis = next(p for p in body["predictions"] if p["sector"] == "semis")
    assert semis["direction"] == "bullish"
    assert "us_spillover" in semis["signals"]       # component signals exposed


def test_report_buckets_and_disclaimer(client):
    body = client.get("/api/report").json()
    assert body["disclaimer"]
    assert body["trade_date"] == "2026-08-02"
    assert all(k in body for k in ("consider", "avoid", "watch"))
    # strong positive US semis spillover + chip sentiment -> semis leans constructive
    assert "semis" in {m["sector"] for m in body["consider"]}


def test_llm_usage_meter_shape(client):
    body = client.get("/api/llm-usage").json()
    assert all(k in body for k in ("today", "last_7d", "all_time", "by_model"))
    assert body["all_time"]["calls"] == 0        # nothing metered in this fixture
    assert body["disclaimer"]


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


# ── research endpoints on the dashboard ───────────────────────────────────
def test_paper_endpoint_always_shows_the_holdout_beside_the_forward_row(client):
    """The forward number must never be readable on its own — it is the whole
    point that it is being compared against the retrospective claim."""
    r = client.get("/api/paper")
    assert r.status_code == 200
    body = r.json()
    assert body["holdout_reference"]["n"] == 332
    assert "forward" in body
    assert body["disclaimer"]
    assert body["rule"]["cost_pct"] > 0, "costs must be visible, not implied"


def test_paper_endpoint_survives_an_empty_database(client):
    body = client.get("/api/paper").json()
    assert body["forward"].get("n", 0) >= 0


def test_segments_endpoint_reports_capturability_not_just_accuracy(client):
    """A 71% hit rate on a segment no entry can reach is not an edge, so the
    flag has to travel with the number."""
    r = client.get("/api/segments")
    assert r.status_code == 200
    body = r.json()
    assert "segments" in body
    for row in body["segments"]:
        assert "tradeable_from_open" in row


def test_execution_endpoint_carries_the_settlement_question(client):
    """The T+1 question decides whether the rule is placeable at all, so it must
    reach the dashboard rather than living in a commit message."""
    r = client.get("/api/execution")
    assert r.status_code == 200
    body = r.json()
    assert "disclaimer" in body
    if "error" not in body:
        assert "settlement" in body and "slippage" in body
        assert "VERIFY THIS WITH THE BROKER" in body["report"]


def test_exit_horizon_endpoint_reports_a_verdict_not_just_numbers(client):
    r = client.get("/api/exit-horizon")
    assert r.status_code == 200
    body = r.json()
    assert "disclaimer" in body
    if "error" not in body:
        assert "verdict" in body
        assert "keep_current_exit" in body["verdict"]


def test_regimes_endpoint_never_pools_the_sign_test(client):
    """Pooling overlapping families manufactures significance; the endpoint must
    expose per-family agreement and no pooled p-value."""
    r = client.get("/api/regimes")
    assert r.status_code == 200
    body = r.json()
    assert "disclaimer" in body
    if "error" not in body and body["result"].get("status") == "measured":
        assert "agreement" in body["result"]
        assert "sign_p" not in body["result"]


def test_research_endpoints_fail_soft_on_a_broken_database(client, monkeypatch):
    """A research result must never take the dashboard down."""
    from oracle.research import exit_horizon as eh

    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(eh, "build_paths", boom)
    for path in ("/api/execution", "/api/exit-horizon", "/api/regimes"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "error" in r.json(), path
