"""Tests for the self-improvement / reflection loop (spec §4b)."""
import pytest
import json
import tempfile

from oracle import config, db
from oracle.reflection import stats
from oracle.reflection.correlation import compute_news_impact
from oracle.reflection.reflect import build_context, rule_based_reflection


# ── (stats) ────────────────────────────────────────────────────────────────
def test_pearson_perfect_positive():
    assert stats.pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)


def test_pearson_none_on_zero_variance():
    assert stats.pearson([1, 1, 1], [1, 2, 3]) is None


def test_best_lag_prefers_lagged_relationship():
    # China tracks US with a one-day lag; the same-day (lag 0) alignment is
    # spoiled by an unrelated first value, so lag 1 must win.
    us = {"d1": 3.0, "d2": 1.0, "d3": 4.0, "d4": 1.0, "d5": 5.0, "d6": 9.0}
    china = {"d1": 100.0, "d2": 3.0, "d3": 1.0, "d4": 4.0, "d5": 1.0, "d6": 5.0}
    corr, lag, n = stats.best_lag_correlation(us, china, max_lag=1)
    assert lag == 1
    assert corr == pytest.approx(1.0)
    assert n == 5


def test_direction_bucketing_uses_neutral_band():
    assert stats.direction_from_move(1.0) == "bullish"
    assert stats.direction_from_move(-1.0) == "bearish"
    assert stats.direction_from_move(0.05) == "neutral"


def test_brier_rewards_calibration():
    # A confident correct call scores better (lower) than a confident wrong one.
    assert stats.brier("high", True) < stats.brier("high", False)


# ── (i) scoring end-to-end ─────────────────────────────────────────────────
def _seed(tmp):
    db.init_db(tmp)
    db.upsert_market_close("china_close", [
        {"trade_date": "2026-08-02", "symbol": "sh000001", "sector": "broad",
         "close": 100.0, "pct_change": 1.5, "fetched_at": "t"},
    ], db_path=tmp)
    db.upsert_prediction({
        "trade_date": "2026-08-02", "sector": "broad", "direction": "bullish",
        "confidence": "high", "composite_score": 0.5, "us_spillover": 0.6,
        "sentiment_score": 0.3, "macro_flag": 0, "rationale": "r", "created_at": "t",
    }, db_path=tmp)


def test_score_predictions_marks_correct(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed(tmp)
    from oracle.reflection.scoring import score_predictions
    assert score_predictions("2026-08-02") == 1
    hist = db.prediction_history(db_path=tmp)
    broad = next(h for h in hist if h["sector"] == "broad")
    assert broad["correct"] == 1
    assert broad["actual_direction"] == "bullish"


# ── (ii) news impact ───────────────────────────────────────────────────────
def test_compute_news_impact_aggregates_by_category_sector():
    news_by_date = {"d1": {"chip_export"}, "d2": {"chip_export"}}
    china_moves = {"d1": {"semis": 2.0}, "d2": {"semis": 1.0}, "d3": {"semis": 3.0}}
    rows = compute_news_impact(news_by_date, china_moves)
    semis = next(r for r in rows if r["china_sector"] == "semis")
    assert semis["category"] == "chip_export"
    # d1 news -> d2 move (1.0); d2 news -> d3 move (3.0). Never the same day.
    assert semis["avg_move"] == 2.0
    assert semis["sample_size"] == 2


def test_news_impact_never_reads_the_same_session():
    """Regression: this was a same-day join. The morning job fires at 21:00 UTC
    (05:00 CST the NEXT day) and stamps trade_date with the UTC date, so news
    stamped D is gathered ~14h after china_close[D] has already printed —
    joining them same-day scored news against a move that was already history."""
    rows = compute_news_impact({"d1": {"chip_export"}}, {"d1": {"semis": 9.9}})
    # The only session available is the same day, so there is nothing to learn.
    assert rows == []


def test_news_impact_skips_to_the_next_trading_session_over_a_gap():
    """Weekends/holidays: the next session may be several calendar days later."""
    rows = compute_news_impact({"2026-08-07": {"chip_export"}},
                               {"2026-08-07": {"semis": 1.0},
                                "2026-08-10": {"semis": 4.0}})
    semis = next(r for r in rows if r["china_sector"] == "semis")
    assert semis["avg_move"] == 4.0      # Friday news -> Monday session


# ── (iii) reflection ───────────────────────────────────────────────────────
def test_rule_based_reflection_flags_missed_signal(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    # Actual move is DOWN, but both signals pointed UP -> both should be "missed".
    db.upsert_market_close("china_close", [
        {"trade_date": "2026-08-02", "symbol": "sh000001", "sector": "broad",
         "close": 100.0, "pct_change": -2.0, "fetched_at": "t"},
    ], db_path=tmp)
    db.upsert_prediction({
        "trade_date": "2026-08-02", "sector": "broad", "direction": "bullish",
        "confidence": "high", "composite_score": 0.5, "us_spillover": 0.8,
        "sentiment_score": 0.4, "macro_flag": 0, "rationale": "r", "created_at": "t",
    }, db_path=tmp)

    ctx = build_context("2026-08-02")
    refl = rule_based_reflection(ctx)
    assert set(refl["signals_that_missed"]) == {"us_spillover", "sentiment"}
    assert refl["signals_that_worked"] == []
    assert refl["suggested_weight_adjustment"]["direction"] == "decrease"
    # schema shape matches the spec's reflection JSON
    assert set(refl) >= {"date", "predicted", "actual", "signals_that_worked",
                         "signals_that_missed", "likely_reason_for_miss",
                         "suggested_weight_adjustment", "confidence_in_this_reflection"}


def test_min_sample_guard_flags_unestablished(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    # 3 aligned days is well below MIN_CORRELATION_SAMPLE (30) -> not established.
    for i, (u, c) in enumerate([(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]):
        d = f"2026-08-0{i+1}"
        db.upsert_market_close("us_close", [
            {"trade_date": d, "symbol": "^IXIC", "sector": "tech",
             "close": 100.0, "pct_change": u, "fetched_at": "t"}], db_path=tmp)
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "sz399006", "sector": "growth",
             "close": 100.0, "pct_change": c, "fetched_at": "t"}], db_path=tmp)
    from oracle.reflection.correlation import update_correlations
    update_correlations()
    lb = db.leaderboard(db_path=tmp)
    assert lb, "expected at least one correlation row"
    assert all(r["established"] == 0 for r in lb)  # noise, correctly flagged
