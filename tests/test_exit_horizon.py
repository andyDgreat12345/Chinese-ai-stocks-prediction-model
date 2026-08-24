"""Tests for the exit-horizon study.

The traps worth guarding are not "does the arithmetic work" but the three ways
this particular measurement can quietly lie: charging friction per day instead
of per round trip (which would rig the test toward the short exit), letting a
corporate action masquerade as a multi-day return, and letting the best of four
horizons look like a discovery.
"""
from oracle.research import exit_horizon as eh


def _bars(prices):
    """Bars from (open, close) pairs on consecutive dates."""
    return [{"trade_date": f"2024-01-{i + 1:02d}", "open": o, "close": c,
             "high": max(o, c), "low": min(o, c)}
            for i, (o, c) in enumerate(prices)]


def test_path_returns_measures_every_horizon_from_the_entry_open():
    # entry open 100. close_d0 110 (+10%), next open 121 (+21%), next close 132,
    # third bar close 143.
    bars = _bars([(90, 100), (100, 110), (121, 132), (140, 143)])
    p = eh.path_returns(bars, 1, limit=100.0)
    assert p["close_d0"] == 10.0
    assert p["open_d1"] == 21.0
    assert p["close_d1"] == 32.0
    assert p["close_d2"] == 43.0


def test_horizons_degrade_independently_at_the_end_of_the_data():
    """A setup near the last session still contributes its shorter horizons."""
    bars = _bars([(90, 100), (100, 110), (110, 120)])
    p = eh.path_returns(bars, 1, limit=100.0)
    assert p["close_d0"] == 10.0 and p["open_d1"] is not None
    assert p["close_d2"] is None          # runs off the end
    assert p is not None


def test_corporate_action_truncates_the_path_rather_than_booking_profit():
    """A 50% overnight jump is a share conversion, not a return."""
    bars = _bars([(90, 100), (100, 110), (165, 170), (170, 175)])
    p = eh.path_returns(bars, 1, limit=10.0)
    assert p["close_d0"] == 10.0          # same-session leg is untouched
    assert p["open_d1"] is None           # everything past the artifact is dropped
    assert p["close_d1"] is None
    assert p["close_d2"] is None


def test_one_round_trip_is_charged_at_every_horizon():
    """Holding longer costs no extra friction — you buy once and sell once.

    Charging per-day would bias the comparison toward the validated exit and
    make the study incapable of ever overturning it.
    """
    rows = [{"sector": "s", "date": f"2024-01-{i:02d}", "prior_body": -2.0,
             "gap": -1.0, "close_d0": 1.0, "open_d1": 1.0, "close_d1": 1.0,
             "close_d2": 1.0} for i in range(1, 41)]
    res = eh.simulate(rows, holdout_frac=0.5)
    nets = {h: res["holdout"][h]["rule"]["net"] for h in eh.HORIZONS}
    # identical gross at every horizon -> identical net, i.e. cost did not scale
    assert len(set(nets.values())) == 1
    assert abs(nets["close_d2"] - (1.0 - eh.COST_PCT)) < 1e-9


def test_rule_is_scored_against_the_same_horizon_baseline():
    """Excess is rule minus everybody, not rule minus zero."""
    rows = []
    for i in range(1, 61):
        setup = i % 2 == 0
        rows.append({"sector": "s", "date": f"2024-02-{i:02d}",
                     "prior_body": -2.0 if setup else 0.5,
                     "gap": -1.0 if setup else 0.5,
                     "close_d0": 2.0 if setup else 1.0,
                     "open_d1": None, "close_d1": None, "close_d2": None})
    res = eh.simulate(rows, holdout_frac=0.5)
    b = res["holdout"]["close_d0"]
    assert b["rule"]["gross"] == 2.0
    assert b["baseline"]["gross"] == 1.5       # mixed pool, not the rule's 2.0
    assert b["excess"] == 0.5


def test_verdict_keeps_the_validated_exit_when_nothing_beats_it():
    rows = [{"sector": "s", "date": f"2024-03-{i:02d}", "prior_body": -2.0,
             "gap": -1.0, "close_d0": 2.0, "open_d1": 0.1, "close_d1": 0.1,
             "close_d2": 0.1} for i in range(1, 41)]
    v = eh.verdict(eh.simulate(rows, holdout_frac=0.5))
    assert v["best"] == "close_d0"
    assert v["keep_current_exit"] is True


def test_a_longer_horizon_must_survive_fdr_to_be_recommended():
    """The best of four horizons is not a finding by itself.

    Here a longer hold has the higher raw net, but its edge over the baseline is
    indistinguishable from the market's own drift, so the verdict must not
    recommend switching to it.
    """
    rows = []
    for i in range(1, 81):
        setup = i % 2 == 0
        rows.append({"sector": "s", "date": f"2024-04-{i:02d}",
                     "prior_body": -2.0 if setup else 0.5,
                     "gap": -1.0 if setup else 0.5,
                     # every session earns the same at the long horizon, so the
                     # rule's apparent advantage there is pure drift
                     "close_d0": 1.0 if setup else 0.9,
                     "open_d1": 5.0, "close_d1": 5.0, "close_d2": 5.0})
    res = eh.simulate(rows, holdout_frac=0.5)
    v = eh.verdict(res)
    assert res["holdout"]["close_d2"]["rule"]["net"] > res["holdout"]["close_d0"]["rule"]["net"]
    assert res["holdout"]["close_d2"]["excess"] == 0.0   # nothing over the market
    assert v["keep_current_exit"] is True


def test_drift_decomposition_reports_agreement_counts():
    """The counts are what separate a market fact from a one-instrument artifact."""
    rows = []
    for sector in ("a", "b"):
        for year in ("2024", "2025"):
            for i in range(1, 21):
                rows.append({"sector": sector, "date": f"{year}-05-{i:02d}",
                             "gap": -0.5, "close_d0": 1.0})
    d = eh.drift_decomposition(rows)
    assert d["overnight"]["mean"] == -0.5
    assert d["intraday"]["mean"] == 1.0
    assert (d["sectors_negative_overnight"], d["sectors"]) == (2, 2)
    assert (d["years_negative_overnight"], d["years"]) == (2, 2)


def test_simulate_handles_no_data_without_raising():
    assert eh.simulate([])["status"] == "no_data"
    assert eh.verdict({"status": "no_data"})["status"] == "no_data"


def test_report_renders_from_empty_and_measured_states():
    assert "no_data" in eh.format_report({"status": "no_data"}, {})
    rows = [{"sector": "s", "date": f"2024-06-{i:02d}", "prior_body": -2.0,
             "gap": -1.0, "close_d0": 1.0, "open_d1": 1.0, "close_d1": 1.0,
             "close_d2": 1.0} for i in range(1, 41)]
    res = eh.simulate(rows, holdout_frac=0.5)
    text = eh.format_report(res, eh.verdict(res))
    assert "Exit horizon" in text and "Not investment advice" in text
