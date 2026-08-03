"""Tests for the cost-aware paper-trading simulator."""
import tempfile

from oracle import config, db
from oracle import costsim as cs
from oracle import backtest as bt


# ── pure helpers ──────────────────────────────────────────────────────────
def test_equity_curve_compounds():
    curve = cs.equity_curve([10.0, -10.0])   # +10% then -10%
    assert curve[0] == 1.1
    assert round(curve[1], 4) == 0.99        # 1.1 * 0.9


def test_max_drawdown_finds_worst_trough():
    curve = cs.equity_curve([20.0, -50.0, 10.0])  # peak 1.2, trough 0.6
    dd = cs.max_drawdown_pct(curve)
    assert round(dd, 1) == -50.0
    assert cs.max_drawdown_pct([]) == 0.0


def test_round_trip_cost_from_bps():
    costs = cs.TradingCosts(commission_bps=2.5, slippage_bps=5.0)
    # 2*(2.5+5) = 15 bps = 0.15%
    assert round(costs.round_trip_pct, 4) == 0.15


def test_costs_make_net_below_gross_and_drag_positive():
    records = [
        {"date": "d1", "actual_move": 1.0},
        {"date": "d2", "actual_move": 1.0},
    ]
    dir_fn = lambda r: "bullish"           # always long, always right here
    m = cs.simulate(records, dir_fn, costs=cs.TradingCosts(5.0, 5.0))
    assert m["n_trades"] == 2
    assert m["net_total_return_pct"] < m["gross_total_return_pct"]
    assert m["cost_drag_pct"] > 0


def test_neutral_and_none_take_no_position():
    records = [{"date": "d1", "actual_move": 2.0}]
    assert cs.simulate(records, lambda r: "neutral")["n_trades"] == 0
    assert cs.simulate(records, lambda r: None)["n_trades"] == 0


def test_gross_exposure_scales_returns():
    records = [{"date": "d1", "actual_move": 2.0}]
    full = cs.simulate(records, lambda r: "bullish",
                       costs=cs.TradingCosts(0, 0), sizing=cs.Sizing(1.0))
    half = cs.simulate(records, lambda r: "bullish",
                       costs=cs.TradingCosts(0, 0), sizing=cs.Sizing(0.5))
    assert round(half["net_total_return_pct"] * 2, 4) == round(
        full["net_total_return_pct"], 4)


def test_breakeven_zero_when_unprofitable_even_free():
    # always wrong -> loses money at any cost, breakeven is 0
    by_date = {"d1": [-1.0], "d2": [-1.0]}
    assert cs.breakeven_roundtrip_bps(by_date, 1.0) == 0.0


def test_breakeven_positive_for_a_real_gross_edge():
    # +1% per bet gross -> profitable until friction eats the 1%
    by_date = {"d1": [1.0], "d2": [1.0], "d3": [1.0]}
    be = cs.breakeven_roundtrip_bps(by_date, 1.0)
    assert be is not None and be > 0
    # ~100 bps round trip (1%) is where a flat +1%/bet edge breaks even
    assert 90 < be < 110


# ── end-to-end over a seeded DB (reuses the backtest seed) ────────────────
def _seed_days(tmp):
    db.init_db(tmp)
    days = [("2026-08-01", 2.0, 2.4), ("2026-08-02", -1.5, -1.8), ("2026-08-03", 1.8, 2.1)]
    for d, us_pct, cn_pct in days:
        db.upsert_market_close("us_close", [
            {"trade_date": d, "symbol": "SOXX", "sector": "semis",
             "close": 100.0, "pct_change": us_pct, "fetched_at": "t"}], db_path=tmp)
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "512480", "sector": "semis",
             "close": 1.0, "pct_change": cn_pct, "fetched_at": "t"}], db_path=tmp)


def test_run_cost_backtest_end_to_end(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed_days(tmp)

    report = cs.run_cost_backtest(db_path=tmp)
    assert "model" in report["strategies"]
    assert "baseline: US-direction" in report["strategies"]
    assert report["costs"]["round_trip_bps"] == 15.0
    # renders without error
    assert "cost-aware" in cs.format_cost_report(report)


def test_backtest_report_embeds_cost_aware_section(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    _seed_days(tmp)

    report = bt.run_backtest(db_path=tmp)
    assert "cost_aware" in report and "error" not in report["cost_aware"]
    assert "cost-aware paper trading" in bt.format_report(report)
