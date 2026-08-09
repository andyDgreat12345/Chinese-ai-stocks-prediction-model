"""Tests for the marginal (conditional) analysis and its rule simulation."""
from oracle.research import marginal as mg


def _row(date, body, gap=0.0, prior=0.0, sector="a", weekday=0):
    return {"date": date, "body": body, "gap": gap, "prior_body": prior,
            "sector": sector, "weekday": weekday, "volatility": 1.0,
            "signal_strength": 0.5}


# ── outlier exclusion ────────────────────────────────────────────────────
def test_corporate_actions_are_excluded():
    """One unadjusted share conversion at -75% dominates any statistic it lands
    in: 159928 shows -74.47% on 2021-06-25 purely as an accounting event."""
    rows = [_row(f"2020-01-{i:02}", 0.5) for i in range(1, 20)] + [
        _row("2020-02-01", -75.0), _row("2020-02-02", 0.5, gap=-60.0)]
    res = mg.analyse(rows, min_bucket=1)
    assert res["n_sessions"] == 19, "both the -75% body and the -60% gap must go"


# ── multiplicity control ─────────────────────────────────────────────────
def test_q_values_account_for_every_bucket_tested():
    """Slicing 21,000 sessions enough ways always yields a bucket at 60%."""
    rows = [_row(f"2020-{1 + i // 28:02}-{1 + i % 28:02}", 1.0 if i % 2 else -1.0,
                 gap=i % 7 - 3, prior=i % 5 - 2, sector=f"s{i % 3}",
                 weekday=i % 5) for i in range(600)]
    res = mg.analyse(rows, min_bucket=10)
    assert res["tests"] > 1
    for r in res["rows"]:
        assert r["q_value"] >= r["p_value"] - 1e-9, "q must never beat raw p"


def test_a_bucket_that_flips_between_halves_does_not_survive():
    """Works in one half, reverses in the other: noise that averaged out."""
    early = [_row(f"2020-01-{i:02}", 1.0, sector="x") for i in range(1, 29)]
    late = [_row(f"2024-01-{i:02}", -1.0, sector="x") for i in range(1, 29)]
    res = mg.analyse(early + late, min_bucket=10)
    x = [r for r in res["rows"] if r["bucket"] == "sector=x"]
    assert x and not x[0]["survives"]


def test_report_names_the_rejection_when_nothing_survives():
    rows = [_row(f"2020-01-{i:02}", 1.0 if i % 2 else -1.0) for i in range(1, 29)]
    text = mg.format_report(mg.analyse(rows, min_bucket=5))
    assert "Nothing survived" in text or "survived" in text


# ── the simulation test ──────────────────────────────────────────────────
def test_rule_is_priced_against_baseline_and_costs_not_against_zero():
    """The body carries a positive baseline drift, so a rule must beat
    buy-every-session; and a 15bps round trip exceeds most of the lift on offer."""
    rows = [_row(f"2020-{1 + i // 28:02}-{1 + i % 28:02}",
                 body=0.30 if i % 2 else 0.05, prior=-2.0 if i % 2 else 2.0)
            for i in range(400)]
    res = mg.simulate_rule(rows, lambda r: r["prior_body"] <= -1.0, cost_bps=15.0)
    assert res["status"] == "measured"
    h = res["holdout"]
    assert h["rule"]["gross"] > h["baseline_all_sessions"]["gross"]
    # net must be gross minus the round trip, not gross
    assert abs(h["rule"]["net"] - (h["rule"]["gross"] - 0.15)) < 1e-9


def test_holdout_is_chronological_and_disjoint():
    rows = [_row(f"2020-01-{i:02}", 1.0) for i in range(1, 29)]
    res = mg.simulate_rule(rows, lambda r: True, holdout_frac=0.25)
    assert res["train"]["rule"]["n"] + res["holdout"]["rule"]["n"] == len(rows)
    assert res["holdout"]["rule"]["n"] == 7


def test_simulate_rule_handles_no_matching_sessions():
    rows = [_row(f"2020-01-{i:02}", 1.0) for i in range(1, 29)]
    res = mg.simulate_rule(rows, lambda r: False)
    assert res["holdout"]["rule"]["n"] == 0
    assert res["holdout"]["rule"]["net"] is None
    assert "n/a" in mg.format_rule_test("none", res)


def test_conditions_are_all_observable_before_the_open():
    """A condition read from the session it labels would be lookahead."""
    assert set(k for k, _ in mg.CONDITIONS) == {
        "gap", "signal_strength", "prior_body", "volatility", "sector", "weekday"}
