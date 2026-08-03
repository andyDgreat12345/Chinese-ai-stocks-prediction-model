"""Tests for macro-calendar ingestion + pre-open confidence refresh (v1 finish)."""
import json
import tempfile

from oracle import config, db
from oracle.ingestion import macro
from oracle.analysis.pipeline import run_analysis, pre_open_refresh


# ── macro ingestion ────────────────────────────────────────────────────────
def test_normalize_macro_tolerates_aliases_and_bad_weight():
    rows = macro.normalize_macro([
        {"date": "2026-08-13", "event": "FOMC", "weight": "oops"},  # alias + bad weight
        {"category": "x"},                                          # no date -> dropped
    ])
    assert len(rows) == 1
    assert rows[0]["event_date"] == "2026-08-13"
    assert rows[0]["description"] == "FOMC"
    assert rows[0]["weight"] == 1.0  # bad weight falls back to default


def test_load_from_file_and_dedupe(monkeypatch, tmp_path):
    cal = tmp_path / "macro.json"
    cal.write_text(json.dumps([
        {"event_date": "2026-08-13", "category": "fed_policy", "description": "FOMC"},
    ]))
    monkeypatch.setattr(config, "MACRO_CALENDAR_FILE", cal)
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db()

    assert macro.fetch_macro_calendar() == 1
    assert macro.fetch_macro_calendar() == 0  # dedupe on second load
    assert db.macro_event_dates("2026-08-13")[0]["category"] == "fed_policy"


def test_macro_flag_flows_into_prediction(monkeypatch, tmp_path):
    cal = tmp_path / "macro.json"
    cal.write_text(json.dumps([{"event_date": "2026-08-02", "category": "fed_policy",
                                "description": "FOMC"}]))
    monkeypatch.setattr(config, "MACRO_CALENDAR_FILE", cal)
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db()
    db.upsert_market_close("us_close", [
        {"trade_date": "2026-08-02", "symbol": "^GSPC", "sector": "broad",
         "close": 100.0, "pct_change": 1.5, "fetched_at": "t"}])
    macro.fetch_macro_calendar()
    run_analysis("2026-08-02")
    broad = next(p for p in db.predictions_for_date("2026-08-02") if p["sector"] == "broad")
    assert broad["macro_flag"] == 1


# ── pre-open confidence refresh ─────────────────────────────────────────────
def _seed_bullish(tmp):
    db.init_db(tmp)
    db.upsert_market_close("us_close", [
        {"trade_date": "2026-08-02", "symbol": "^GSPC", "sector": "broad",
         "close": 100.0, "pct_change": 1.5, "fetched_at": "t"}], db_path=tmp)
    run_analysis("2026-08-02")


def test_refresh_downgrades_to_low_when_breaking_news_contradicts(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed_bullish(tmp)
    before = next(p for p in db.predictions_for_date("2026-08-02") if p["sector"] == "broad")
    assert before["direction"] == "bullish"

    # breaking: strongly negative Fed-policy headline lands after the morning call
    db.insert_news([{"trade_date": "2026-08-02", "source": "r", "category": "fed_policy",
                     "headline": "Fed shock: emergency hike, markets plunge on recession fears",
                     "summary": "", "sentiment": -0.9, "fetched_at": "t"}])
    n = pre_open_refresh("2026-08-02")

    after = next(p for p in db.predictions_for_date("2026-08-02") if p["sector"] == "broad")
    assert n >= 1
    assert after["direction"] == before["direction"]   # direction preserved
    assert after["confidence"] == "low"                 # confidence dropped
    assert "pre-open" in after["rationale"]
    # morning component signals are untouched (audit trail intact, §4b-i)
    assert after["us_spillover"] == before["us_spillover"]


def test_refresh_noop_when_news_unchanged(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed_bullish(tmp)
    assert pre_open_refresh("2026-08-02") == 0  # nothing new -> no adjustments
