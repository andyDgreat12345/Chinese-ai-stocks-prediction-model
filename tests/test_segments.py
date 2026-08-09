"""Tests for K-line segment decomposition and scoring."""
from oracle.analysis import segments as sg


def test_decompose_splits_a_bar_into_its_parts():
    s = sg.decompose(100.0, {"open": 102.0, "high": 105.0, "low": 101.0, "close": 104.0})
    assert s.gap == 2.0                     # 100 -> 102
    assert s.body == round((104 / 102 - 1) * 100, 4)
    assert s.close_to_close == 4.0          # 100 -> 104
    assert s.upper_wick == round((105 / 104 - 1) * 100, 4)
    assert s.lower_wick == round((101 / 102 - 1) * 100, 4)


def test_gap_plus_body_compose_to_close_to_close():
    s = sg.decompose(100.0, {"open": 102.0, "close": 104.0})
    combined = (1 + s.gap / 100) * (1 + s.body / 100) - 1
    # 1e-3, not 1e-6: each segment is rounded to 4dp, so recomposing them
    # carries ~1e-5 of rounding error. Tight enough to catch a real mistake.
    assert abs(combined * 100 - s.close_to_close) < 1e-3


def test_segments_degrade_independently():
    """A source publishing only closes must still yield close_to_close."""
    s = sg.decompose(100.0, {"close": 104.0})
    assert s.close_to_close == 4.0
    assert s.gap is None and s.body is None
    empty = sg.decompose(None, None)
    assert all(v is None for v in (empty.gap, empty.body, empty.close_to_close))


def test_zero_reference_price_does_not_explode():
    assert sg.decompose(0.0, {"open": 1.0, "close": 1.0}).gap is None


def test_direction_deadband_rejects_noise():
    """Gaps cluster near zero; scoring a 0.01% gap as a hit would inflate every
    accuracy number with coin flips on noise."""
    assert sg.direction(0.5) == "bullish"
    assert sg.direction(-0.5) == "bearish"
    assert sg.direction(0.05, deadband=0.1) is None
    assert sg.direction(0.0) is None
    assert sg.direction(None) is None


def test_tradeability_is_recorded_and_gap_is_not_tradeable():
    """The load-bearing fact: the gap runs from the previous close to this open,
    and the US session driving it trades inside that window — owning it means
    entering before the US session the signal comes from."""
    assert sg.TRADEABLE_FROM_OPEN["gap"] is False
    assert sg.TRADEABLE_FROM_OPEN["close_to_close"] is False
    assert sg.TRADEABLE_FROM_OPEN["body"] is True


def test_score_segments_counts_only_scoreable_calls():
    recs = [
        {"sector": "a", "date": "d1", "model_dir": "bullish"},
        {"sector": "a", "date": "d2", "model_dir": "bearish"},
        {"sector": "a", "date": "d3", "model_dir": "neutral"},   # abstention
        {"sector": "b", "date": "d1", "model_dir": "bullish"},    # no bars
    ]
    segs = {"a": {
        "d1": sg.decompose(100.0, {"open": 101.0, "close": 102.0}),   # both up
        "d2": sg.decompose(100.0, {"open": 99.0, "close": 98.0}),     # both down
        "d3": sg.decompose(100.0, {"open": 101.0, "close": 102.0}),
    }}
    rows = {r["segment"]: r for r in sg.score_segments(recs, segs)}
    assert rows["gap"]["n"] == 2 and rows["gap"]["hit_rate"] == 1.0
    assert rows["body"]["n"] == 2 and rows["body"]["hit_rate"] == 1.0
    assert rows["gap"]["tradeable_from_open"] is False


def test_report_leads_with_tradeability():
    rows = sg.score_segments([], {})
    text = sg.format_segment_report(rows)
    assert "capturable entering at the open" in text
    assert "arrives too" in text
