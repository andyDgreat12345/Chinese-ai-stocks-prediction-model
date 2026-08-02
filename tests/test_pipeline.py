"""Tests for the analysis pipeline (spec §4, Phase 3)."""
import tempfile

from oracle import db
from oracle.analysis import pipeline
from oracle.analysis.scoring import score_sector


def _us(symbol, sector, pct):
    return {"symbol": symbol, "sector": sector, "pct_change": pct,
            "trade_date": "2026-08-02", "close": 100.0, "fetched_at": "t"}


def _news(cat, sent):
    return {"category": cat, "sentiment": sent, "trade_date": "2026-08-02",
            "source": "reuters", "headline": "h", "summary": "", "fetched_at": "t"}


def test_semis_bullish_when_us_semis_up_and_chip_news_positive():
    sig = pipeline.build_signals(
        us_rows=[_us("SOXX", "semis", 2.0), _us("^IXIC", "tech", 1.5)],
        news_rows=[_news("chip_export", 0.6)],
        macro_events=[],
    )["semis"]
    assert sig.us_spillover > 0
    assert sig.sentiment > 0
    assert score_sector(sig).direction == "bullish"


def test_spillover_scaled_and_clamped():
    # A +6% move is way past the ±2% full-strength scale -> clamps at 1.0.
    sig = pipeline.build_signals([_us("XLE", "energy", 6.0)], [], [])["energy"]
    assert sig.us_spillover == 1.0


def test_macro_event_sets_flag_on_all_sectors():
    sigs = pipeline.build_signals([], [], [{"event_date": "2026-08-02"}])
    assert all(s.macro_flag for s in sigs.values())


def test_missing_data_yields_neutral_signals():
    sigs = pipeline.build_signals([], [], [])
    assert sigs["broad"].us_spillover == 0.0
    assert sigs["broad"].sentiment == 0.0


def test_all_china_sectors_present():
    sigs = pipeline.build_signals([], [], [])
    assert set(sigs) == set(pipeline.CHINA_SECTORS)


def test_run_analysis_writes_predictions_end_to_end():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    db.upsert_market_close("us_close", [
        {"trade_date": "2026-08-02", "symbol": "SOXX", "sector": "semis",
         "close": 100.0, "pct_change": 2.0, "fetched_at": "t"},
    ], db_path=tmp)
    db.insert_news([
        {"trade_date": "2026-08-02", "source": "r", "category": "chip_export",
         "headline": "chips rally", "summary": "", "sentiment": 0.7, "fetched_at": "t"},
    ], db_path=tmp)

    # point the pipeline's db reads/writes at the temp db
    import oracle.analysis.pipeline as pl
    orig = pl.db.config.DB_PATH
    pl.db.config.DB_PATH = tmp
    try:
        n = pipeline.run_analysis("2026-08-02")
    finally:
        pl.db.config.DB_PATH = orig

    assert n == len(pipeline.CHINA_SECTORS)
    preds = db.latest_predictions(db_path=tmp)
    semis = next(p for p in preds if p["sector"] == "semis")
    assert semis["direction"] == "bullish"
    # component signals are stored, not just the verdict (spec §4b-i)
    assert semis["us_spillover"] is not None
    assert semis["sentiment_score"] is not None
