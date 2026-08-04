"""Tests for the backtest / evaluation engine."""
import tempfile

from oracle import config, db
from oracle import backtest as bt


# ── pure metric helpers ──────────────────────────────────────────────────
def test_binom_p_value_strong_edge_is_significant():
    # 18/20 correct is very unlikely under a fair coin
    assert bt.binom_p_value(18, 20) < 0.01


def test_binom_p_value_coinflip_is_not_significant():
    assert bt.binom_p_value(11, 20) > 0.2


def test_annualized_sharpe_positive_for_consistent_gains():
    s = bt.annualized_sharpe([0.5, 0.4, 0.6, 0.5])
    assert s is not None and s > 0


def test_annualized_sharpe_none_when_no_variance():
    assert bt.annualized_sharpe([0.5, 0.5, 0.5]) is None


def test_evaluate_perfect_strategy():
    records = [
        {"date": "d1", "actual_dir": "bullish", "actual_move": 1.0},
        {"date": "d2", "actual_dir": "bearish", "actual_move": -1.0},
    ]
    m = bt.evaluate(records, lambda r: r["actual_dir"])  # oracle strategy
    assert m["accuracy"] == 1.0
    assert m["bet_accuracy"] == 1.0
    assert m["total_return_pct"] == 2.0   # +1.0 on the bullish, +1.0 shorting the bearish


def test_evaluate_neutral_takes_no_position():
    records = [{"date": "d1", "actual_dir": "bullish", "actual_move": 2.0}]
    m = bt.evaluate(records, lambda r: "neutral")
    assert m["bets"] == 0
    assert m["total_return_pct"] == 0.0


def test_calibration_groups_by_confidence():
    records = [
        {"model_conf": "high", "model_dir": "bullish", "actual_dir": "bullish"},
        {"model_conf": "high", "model_dir": "bullish", "actual_dir": "bearish"},
        {"model_conf": "low", "model_dir": "neutral", "actual_dir": "neutral"},
    ]
    cal = bt.calibration(records)
    assert cal["high"]["n"] == 2
    assert cal["high"]["accuracy"] == 0.5
    assert cal["low"]["accuracy"] == 1.0


# ── end-to-end over a seeded multi-day DB ────────────────────────────────
def _seed_days(tmp):
    db.init_db(tmp)
    # Three days where US semis lead China semis in the same direction.
    days = [("2026-08-01", 2.0, 2.4), ("2026-08-02", -1.5, -1.8), ("2026-08-03", 1.8, 2.1)]
    for d, us_pct, cn_pct in days:
        db.upsert_market_close("us_close", [
            {"trade_date": d, "symbol": "SOXX", "sector": "semis",
             "close": 100.0, "pct_change": us_pct, "fetched_at": "t"}], db_path=tmp)
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "512480", "sector": "semis",
             "close": 1.0, "pct_change": cn_pct, "fetched_at": "t"}], db_path=tmp)


def test_run_backtest_end_to_end(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed_days(tmp)

    report = bt.run_backtest(db_path=tmp)
    assert report["window"]["trading_days"] == 3
    assert report["n_records"] == 3

    model = report["strategies"]["model"]
    # US semis strongly lead China semis here, so the model should score well
    assert model["scored"] == 3
    assert model["bet_accuracy"] == 1.0
    # report renders without error
    assert "backtest" in bt.format_report(report)


def test_llm_strategy_scored_only_on_recorded_calls(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed_days(tmp)
    # Record one AI-analyst call, on 2026-08-02 semis (actual there is bearish).
    db.upsert_llm_call({
        "trade_date": "2026-08-02", "sector": "semis", "provider": "deepseek",
        "model": "deepseek-chat", "direction": "bearish", "conviction": "high",
        "tradeable_etf": "KWEB", "key_drivers": "[]", "rationale": "r",
        "created_at": "t"}, db_path=tmp)

    report = bt.run_backtest(db_path=tmp)
    llm = report["strategies"].get("llm (recorded)")
    assert llm is not None                 # strategy appears because a call exists
    assert llm["bets"] == 1                # scored only on the one recorded call
    assert llm["bet_accuracy"] == 1.0      # bearish call, bearish actual
    # and it shows up in the rendered report + cost-aware section
    text = bt.format_report(report)
    assert "llm (recorded)" in text


def test_no_llm_strategy_without_calls(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed_days(tmp)
    report = bt.run_backtest(db_path=tmp)
    assert "llm (recorded)" not in report["strategies"]   # no noise row when absent


def test_collect_records_skips_sectors_without_actuals(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed_days(tmp)
    recs = bt.collect_records(db_path=tmp)
    # only 'semis' has actuals in the seed -> one record per day
    assert {r["sector"] for r in recs} == {"semis"}
    assert len(recs) == 3
