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


def test_settlement_warning_prints_even_with_no_trades_to_price():
    """The caveat must not depend on the numbers computing.

    A warning that appears only when there is data is missing on exactly the
    run where someone reads the report and decides to fund something.
    """
    text = ex.format_report(ex.slippage_curve([]), ex.breakeven([]),
                            ex.settlement_comparison([]))
    assert "VERIFY THIS WITH THE BROKER" in text
    assert "T+1" in text


def _grid_rows(n=800, t0=1.0, t1=1.0):
    """Rows spanning the threshold grid so every cell clears its sample floor."""
    rows = []
    for i in range(n):
        rows.append({"sector": "s", "date": f"2024-{i:04d}",
                     # spread across the grid so all 36 cells populate
                     "prior_body": -0.6 - (i % 12) * 0.1,
                     "gap": -0.2 - (i % 9) * 0.1,
                     "close_d0": t0, "open_d1": t1,
                     "close_d1": t1, "close_d2": t1})
    return rows


def test_surface_judges_both_legs_by_one_plateau_definition():
    from oracle.research.marginal import MAX_PLATEAU_RATIO

    sur = ex.settlement_surface(_grid_rows())
    assert set(sur) == {"t0", "t1"}
    for leg in ("t0", "t1"):
        v = sur[leg]["verdict"]
        assert v["cells"] > 0
        assert v["plateau"] is True
        assert v["spread_ratio"] <= MAX_PLATEAU_RATIO


def test_surface_fails_the_leg_that_does_not_hold_up():
    """A plateau under T+0 says nothing about an unexecutable T+1 exit."""
    sur = ex.settlement_surface(_grid_rows(t0=1.0, t1=-1.0))
    assert sur["t0"]["verdict"]["plateau"] is True
    assert sur["t1"]["verdict"]["plateau"] is False
    assert sur["t1"]["verdict"]["positive"] == 0


def test_surface_grid_is_fixed_not_searched():
    """The cells are pre-registered around the validated thresholds.

    Selecting the best cell would be dredging the same ten years twice, so the
    grid must be a constant and the report must not name a winner.
    """
    assert -0.96 in ex.PRIOR_GRID and -0.40 in ex.GAP_GRID
    text = ex.format_report(ex.slippage_curve(_grid_rows()),
                            ex.breakeven(_grid_rows()),
                            ex.settlement_comparison(_grid_rows(), holdout_frac=0.5),
                            ex.settlement_surface(_grid_rows()))
    assert "nothing is" in text and "selected from it" in text
    assert "dredging" in text


def test_surface_absent_does_not_break_the_report():
    text = ex.format_report(ex.slippage_curve([]), ex.breakeven([]),
                            ex.settlement_comparison([]))
    assert "Not investment advice" in text
