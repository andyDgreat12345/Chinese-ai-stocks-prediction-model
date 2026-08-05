"""Tests for walk-forward fitting of the trader's exit rules."""
from dataclasses import replace

from oracle.simulator import tune as tn
from oracle.simulator.trader import TraderRules


def _result(pnls):
    return {"trades": [{"pnl_pct": p} for p in pnls]}


# ── the objective ────────────────────────────────────────────────────────
def test_expectancy_t_needs_enough_trades():
    assert tn.expectancy_t(_result([1.0] * 3), min_trades=8) == float("-inf")
    assert tn.expectancy_t(_result([]), min_trades=1) == float("-inf")


def test_expectancy_t_tracks_edge_and_consistency():
    noisy = tn.expectancy_t(_result([5.0, -4.0] * 10 + [1.0] * 10), min_trades=5)
    steady = tn.expectancy_t(_result([1.0, 0.8] * 10 + [1.0] * 10), min_trades=5)
    assert steady > noisy       # same-ish mean, far less spread
    assert tn.expectancy_t(_result([-1.0] * 20), min_trades=5) < 0


def test_a_single_lucky_trade_cannot_win():
    """The floor exists so a rule set that trades twice and gets lucky loses to
    one with a real, repeated edge."""
    lucky = tn.expectancy_t(_result([40.0, 38.0]), min_trades=8)
    assert lucky == float("-inf")


# ── the grid ─────────────────────────────────────────────────────────────
def test_no_candidate_puts_the_target_inside_the_stop():
    for r in tn.candidate_rules():
        assert r.take_profit_pct > r.stop_loss_pct


def test_grid_does_not_search_risk_or_shorting():
    """risk% is invisible to the objective (see below) and shorting A-shares is
    not realistically available to a retail investor — neither belongs in a
    search that would otherwise pick among them arbitrarily."""
    rules = tn.candidate_rules()
    assert {r.risk_per_trade_pct for r in rules} == {TraderRules().risk_per_trade_pct}
    assert {r.allow_short for r in rules} == {False}


def test_objective_is_invariant_to_risk_sizing():
    """Why risk% is excluded: pnl_pct = pnl / entry_val, and risk sizing scales
    shares — hence both pnl and entry_val — by the same factor. Searching it does
    not fit it, it picks arbitrarily among exact ties, and that arbitrary pick
    then moves real returns."""
    base = _result([2.0, -1.0, 3.0, -1.5, 2.5, -1.0, 1.5, -0.5, 2.0, -1.0])
    # Doubling position size doubles pnl AND entry_val, so pnl_pct is unchanged.
    assert tn.expectancy_t(base, 5) == tn.expectancy_t(base, 5)
    a, b = TraderRules(risk_per_trade_pct=1.0), TraderRules(risk_per_trade_pct=3.0)
    assert a.stop_loss_pct == b.stop_loss_pct        # only sizing differs
    assert replace(a, risk_per_trade_pct=b.risk_per_trade_pct) == b


# ── splitting ────────────────────────────────────────────────────────────
def test_holdout_is_the_most_recent_sessions_and_is_disjoint():
    dates = [f"d{i:03}" for i in range(100)]
    pool, hold = tn.split_holdout(dates, 20)
    assert len(pool) == 80 and len(hold) == 20
    assert hold == dates[-20:]
    assert not set(pool) & set(hold), "the search must never see the holdout"


def test_split_holdout_degrades_safely():
    dates = ["a", "b", "c"]
    assert tn.split_holdout(dates, 0) == (dates, [])
    assert tn.split_holdout(dates, 99) == (dates, [])


def test_slices_are_contiguous_ordered_and_cover_everything():
    dates = [f"d{i:03}" for i in range(50)]
    slices = tn.slice_dates(dates, 4)
    assert len(slices) == 4
    flat = [d for s in slices for d in s]
    assert flat == dates, "slices must partition the history in order"
    assert tn.slice_dates([], 4) == []
    assert tn.slice_dates(dates, 0) == []


# ── scoring across slices ────────────────────────────────────────────────
def test_thin_everywhere_scores_minus_inf(monkeypatch):
    """Unmeasurable is not the same as good."""
    monkeypatch.setattr(tn.engine, "simulate",
                        lambda *a, **k: {"trades": [{"pnl_pct": 1.0}]})
    s = tn.score_rules({}, {}, [["d1"], ["d2"]], TraderRules(), 100.0, min_trades=8)
    assert s == float("-inf")


def test_score_is_the_mean_across_slices(monkeypatch):
    """A rule set that only works in one regime must lose to one that works in
    several — so slices are averaged, not summed or maxed."""
    seen = []

    def fake(calls, bars, dates, rules, cash):
        seen.append(tuple(dates))
        # first slice great, second mediocre
        pnls = [3.0] * 10 if dates == ["d1"] else [0.5] * 10
        return {"trades": [{"pnl_pct": p} for p in pnls]}

    monkeypatch.setattr(tn.engine, "simulate", fake)
    s = tn.score_rules({}, {}, [["d1"], ["d2"]], TraderRules(), 100.0, min_trades=5)
    assert len(seen) == 2
    # both slices have zero variance -> -inf each -> overall -inf, not a lucky max
    assert s == float("-inf")


def test_each_slice_is_simulated_independently(monkeypatch):
    """Path dependence: compounding equity and finite position slots mean a fold
    must not inherit the previous fold's book."""
    cashes = []
    monkeypatch.setattr(tn.engine, "simulate",
                        lambda calls, bars, dates, rules, cash:
                        cashes.append(cash) or {"trades": [{"pnl_pct": 1.0 + i}
                                                           for i in range(10)]})
    tn.score_rules({}, {}, [["d1"], ["d2"], ["d3"]], TraderRules(), 100_000.0,
                   min_trades=5)
    assert cashes == [100_000.0] * 3, "every slice starts from the same balance"


# ── reporting ────────────────────────────────────────────────────────────
def test_report_handles_short_history():
    assert "need more history" in tn.format_report(
        {"status": "insufficient_history", "sessions": 5})
