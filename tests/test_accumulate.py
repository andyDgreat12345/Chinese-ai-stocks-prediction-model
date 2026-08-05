"""Tests for accumulated correlation — strength × persistence × maturity."""
import tempfile

from oracle import db
from oracle.reflection import accumulate as ac


def _obs(vals, start=1, n=30):
    return [{"observed_on": f"2026-06-{start + i:02d}", "correlation": v,
             "sample_size": n} for i, v in enumerate(vals)]


# ── persistence: has the relationship actually held its direction? ────────
def test_persistence_is_one_when_sign_never_flips():
    assert ac.persistence([0.4, 0.5, 0.3, 0.45]) == 1.0


def test_persistence_drops_when_it_flickers():
    p = ac.persistence([0.5, -0.4, 0.6, -0.3])
    assert 0.0 < p < 0.75          # a coin-flip relationship, not a real one


def test_persistence_none_on_empty():
    assert ac.persistence([]) is None


def test_maturity_scales_then_caps():
    assert ac.maturity(0, 30) == 0.0
    assert ac.maturity(15, 30) == 0.5
    assert ac.maturity(60, 30) == 1.0        # capped, never rewards age alone


# ── the combined judgement ────────────────────────────────────────────────
def test_summarize_rewards_strong_consistent_and_mature():
    steady = ac.summarize(_obs([0.4] * 30))
    assert steady["mean_correlation"] == 0.4
    assert steady["persistence"] == 1.0 and steady["maturity"] == 1.0
    assert steady["reliability"] == 0.4       # |r| * 1.0 * 1.0


def test_flickering_pair_scores_below_a_weaker_steady_one():
    steady = ac.summarize(_obs([0.30] * 30))            # weaker but rock-steady
    flaky = ac.summarize(_obs([0.9, -0.8] * 15))        # bigger but flips constantly
    assert steady["reliability"] > flaky["reliability"]


def test_immature_pair_is_discounted_not_hidden():
    young = ac.summarize(_obs([0.8, 0.8, 0.8]))          # strong but only 3 readings
    mature = ac.summarize(_obs([0.5] * 30))
    assert young["reliability"] < mature["reliability"]
    assert young["n_observations"] == 3                  # still reported honestly


def test_summarize_reports_window_and_empty_case():
    s = ac.summarize(_obs([0.2, 0.4, 0.6]))
    assert s["first_observed"] == "2026-06-01" and s["last_observed"] == "2026-06-03"
    assert s["latest_correlation"] == 0.6
    assert ac.summarize([])["n_observations"] == 0


# ── ranking ───────────────────────────────────────────────────────────────
def test_rank_excludes_same_day_pairs_by_default():
    grouped = {
        ("SOXX", "510300", 0): _obs([0.64] * 30),        # strong but untradeable
        ("XLE", "159930", 1): _obs([0.41] * 30),         # weaker, tradeable
    }
    ranked = ac.rank_accumulated(grouped)
    assert [r["us_symbol"] for r in ranked] == ["XLE"]
    # ...but they can be inspected explicitly
    both = ac.rank_accumulated(grouped, predictive_only=False)
    assert len(both) == 2


def test_rank_drops_pairs_with_too_few_observations():
    grouped = {("XLE", "159930", 1): _obs([0.4, 0.5])}
    assert ac.rank_accumulated(grouped, min_observations=3) == []


def test_format_accumulated_handles_empty_and_rows():
    assert "nothing accumulated yet" in ac.format_accumulated([])
    rows = ac.rank_accumulated({("XLE", "159930", 1): _obs([0.4] * 30)})
    text = ac.format_accumulated(rows)
    assert "XLE → 159930" in text and "reliability" in text


# ── persistence through the DB (the append-only accumulation) ────────────
def test_observations_accumulate_and_are_idempotent_per_day():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    for d, r in (("2026-06-01", 0.40), ("2026-06-02", 0.45)):
        db.record_correlation_observation({
            "observed_on": d, "us_symbol": "XLE", "china_symbol": "159930",
            "lag": 1, "window_days": 0, "correlation": r, "sample_size": 30},
            db_path=tmp)
    # re-recording the same day updates rather than duplicating
    db.record_correlation_observation({
        "observed_on": "2026-06-02", "us_symbol": "XLE", "china_symbol": "159930",
        "lag": 1, "window_days": 0, "correlation": 0.50, "sample_size": 31},
        db_path=tmp)
    obs = db.correlation_observations("XLE", "159930", 1, db_path=tmp)
    assert len(obs) == 2 and obs[-1]["correlation"] == 0.50

    grouped = db.correlation_history_grouped(0, db_path=tmp)
    assert ("XLE", "159930", 1) in grouped
    s = ac.summarize(grouped[("XLE", "159930", 1)])
    assert s["n_observations"] == 2 and s["persistence"] == 1.0
