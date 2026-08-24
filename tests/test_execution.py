"""Tests for the execution-realism study.

The load-bearing behaviours are that slippage actually bites, that the T+1
comparison reads the horizon it claims to, and that the settlement verdict is
capable of returning "no" — a feasibility check that always passes is not one.
"""
from oracle.research import execution as ex


def _rows(n=80, body=1.0, nextopen=0.9, setup_every=1):
    out = []
    for i in range(1, n + 1):
        setup = (i % setup_every == 0)
        out.append({"sector": "s", "date": f"2024-01-{i:03d}",
                    "prior_body": -2.0 if setup else 0.5,
                    "gap": -1.0 if setup else 0.5,
                    "close_d0": body if setup else 0.0,
                    "open_d1": nextopen if setup else 0.0,
                    "close_d1": 0.5, "close_d2": 0.5})
    return out


def test_slippage_reduces_net_monotonically():
    curve = ex.slippage_curve(_rows())
    nets = [r["net"] for r in curve]
    assert nets == sorted(nets, reverse=True)
    assert curve[0]["slippage"] == 0.0


def test_slippage_is_charged_on_top_of_the_assumed_cost():
    curve = ex.slippage_curve(_rows(body=1.0), cost_pct=0.15,
                              steps=(0.0, 0.10))
    assert abs(curve[0]["net"] - (1.0 - 0.15)) < 1e-9
    assert abs(curve[1]["net"] - (1.0 - 0.25)) < 1e-9


def test_hit_rate_counts_trades_that_clear_friction_not_zero():
    """A trade that gains less than it costs is not a win."""
    rows = [{"sector": "s", "date": f"2024-02-{i:02d}", "prior_body": -2.0,
             "gap": -1.0, "close_d0": 0.10, "open_d1": 0.1,
             "close_d1": 0.1, "close_d2": 0.1} for i in range(1, 21)]
    curve = ex.slippage_curve(rows, cost_pct=0.15, steps=(0.0,))
    assert curve[0]["hit"] == 0.0        # +0.10% gross never clears 0.15% cost
    assert curve[0]["net"] < 0


def test_breakeven_is_expressed_against_the_daily_move():
    rows = _rows(body=1.0)
    be = ex.breakeven(rows, cost_pct=0.15)
    assert be["status"] == "measured"
    assert abs(be["breakeven_extra_pct"] - 0.85) < 1e-9
    # daily move here is |gap| + |body| = 1.0 + 1.0 = 2.0
    assert abs(be["mean_daily_move"] - 2.0) < 1e-9
    assert abs(be["as_share_of_daily_move"] - 0.425) < 1e-3
    assert abs(be["as_multiple_of_assumed_cost"] - (0.85 / 0.15)) < 1e-2


def test_settlement_comparison_prices_both_exits():
    rows = _rows(body=1.0, nextopen=0.5)
    s = ex.settlement_comparison(rows, holdout_frac=0.5)
    assert s["status"] == "measured"
    assert s["t0_exit"] == "close_d0" and s["t1_exit"] == "open_d1"
    assert s["t0"]["gross"] == 1.0 and s["t1"]["gross"] == 0.5
    # the constraint costs the difference between the two exits
    assert abs(s["cost_of_constraint"] - (-0.5)) < 1e-9


def test_settlement_verdict_can_fail():
    """A feasibility check that always passes is not a check.

    Here the T+1 exit loses money, so the rule must be reported as not
    surviving the constraint.
    """
    rows = _rows(body=1.0, nextopen=-1.0)
    s = ex.settlement_comparison(rows, holdout_frac=0.5)
    assert s["t1"]["net"] < 0
    assert s["survives_t1"] is False


def test_settlement_verdict_requires_significance_not_just_profit():
    """A barely-positive, noisy T+1 exit is not a pass."""
    rows = []
    for i in range(1, 61):
        rows.append({"sector": "s", "date": f"2024-03-{i:03d}",
                     "prior_body": -2.0, "gap": -1.0, "close_d0": 1.0,
                     # alternating large swings: positive mean, huge spread
                     "open_d1": 10.0 if i % 2 else -9.6,
                     "close_d1": 0.1, "close_d2": 0.1})
    s = ex.settlement_comparison(rows, holdout_frac=0.5)
    assert s["t1"]["net"] > 0          # profitable on average
    assert s["survives_t1"] is False   # but nowhere near significant


def test_no_data_paths_do_not_raise():
    assert ex.breakeven([])["status"] == "no_data"
    assert ex.settlement_comparison([])["status"] == "no_data"
    assert ex.slippage_curve([]) [0]["n"] == 0


def test_report_renders_and_carries_the_settlement_warning():
    rows = _rows()
    text = ex.format_report(ex.slippage_curve(rows), ex.breakeven(rows),
                            ex.settlement_comparison(rows, holdout_frac=0.5))
    assert "VERIFY THIS WITH THE BROKER" in text
    assert "T+1" in text and "Not investment advice" in text
