"""Tests for the learning-objective diagnostic.

The claim this module makes is strong — that the learner has been rewarding
signals for predicting a segment no trade can reach — so the tests pin both
that it detects a divergence and that it refuses to overclaim when the
tradeable segment is simply unpredictable.
"""
from types import SimpleNamespace

from oracle.learning import objective as ob


def _seg(ctc, body):
    return SimpleNamespace(close_to_close=ctc, body=body, gap=None,
                           upper_wick=None, lower_wick=None, range_pct=None)


def _fixture(n=200, sig_ctc_right=True, sig_body_right=False, name="us_spillover"):
    """Records where one signal calls ctc well and the body badly (or not)."""
    records, segs = [], {"s": {}}
    for i in range(n):
        d = f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}-{i}"
        records.append({"sector": "s", "date": d, name: 1.0})
        ctc = 1.0 if sig_ctc_right else -1.0
        body = 1.0 if sig_body_right else -1.0
        segs["s"][d] = _seg(ctc, body)
    return records, segs


def test_signal_measured_against_both_segments():
    recs, segs = _fixture()
    e = ob.signal_edges(recs, segs, signals=("us_spillover",))[0]
    assert e["signal"] == "us_spillover"
    assert e["scored"]["hit"] == 1.0        # always right on close-to-close
    assert e["tradeable"]["hit"] == 0.0     # always wrong on the body
    assert e["divergence"] > 0


def test_abstentions_are_excluded_not_scored_as_coin_flips():
    """A signal silent most of the time is measured where it speaks."""
    recs, segs = _fixture(n=100)
    for r in recs[:60]:
        r["us_spillover"] = 0.0             # abstain
    e = ob.signal_edges(recs, segs, signals=("us_spillover",), min_n=5)[0]
    assert e["scored"]["n"] == 40


def test_signal_below_the_sample_floor_is_dropped():
    recs, segs = _fixture(n=10)
    assert ob.signal_edges(recs, segs, signals=("us_spillover",)) == []


def test_alignment_flags_a_disagreement_about_the_best_signal():
    recs_a, segs = _fixture(n=200, name="us_spillover")
    # a second signal that is mediocre on ctc but the best on the body
    for i, r in enumerate(recs_a):
        r["rsi_signal"] = 1.0
        seg = segs["s"][r["date"]]
        segs["s"][r["date"]] = _seg(seg.close_to_close, 1.0 if i % 3 else -1.0)
    # us_spillover now wrong on body, rsi right 2/3 of the time
    e = ob.signal_edges(recs_a, segs, signals=("us_spillover", "rsi_signal"))
    # both read the same segments, so add a third to clear the floor of 3
    for r in recs_a:
        r["trend_signal"] = -1.0
    e = ob.signal_edges(recs_a, segs,
                        signals=("us_spillover", "rsi_signal", "trend_signal"))
    a = ob.alignment(e)
    assert a["status"] == "measured"
    assert a["rank_correlation"] is not None


def test_alignment_reports_when_nothing_predicts_the_tradeable_segment():
    """The honest reading: a wrong objective is not proof a better one exists."""
    recs, segs = [], {"s": {}}
    for i in range(300):
        d = f"d{i}"
        recs.append({"sector": "s", "date": d, "us_spillover": 1.0,
                     "rsi_signal": 1.0, "trend_signal": 1.0})
        segs["s"][d] = _seg(1.0, 1.0 if i % 2 else -1.0)   # body is a coin flip
    a = ob.alignment(ob.signal_edges(recs, segs))
    assert a["nothing_predicts_the_tradeable_segment"] is True
    assert a["signals_with_tradeable_edge"] == []
    text = ob.format_report(ob.signal_edges(recs, segs), a)
    assert "BEFORE CONCLUDING THE FIX IS TO REWEIGHT" in text


def test_report_keeps_the_gap_edge_worth_having():
    """The diagnostic must not read as 'throw the model away'."""
    recs, segs = _fixture()
    for r in recs:
        r["rsi_signal"] = 1.0
        r["trend_signal"] = -1.0
    e = ob.signal_edges(recs, segs)
    text = ob.format_report(e, ob.alignment(e))
    assert "the one thing this model does well" in text
    assert "Not investment advice" in text


def test_spearman_matches_known_values():
    assert ob._spearman([1, 2, 3], [1, 2, 3]) == 1.0
    assert ob._spearman([1, 2, 3], [3, 2, 1]) == -1.0
    assert ob._spearman([1, 1, 1], [1, 2, 3]) is None    # no variance
    assert ob._spearman([1], [1]) is None


def test_alignment_degrades_with_too_few_signals():
    recs, segs = _fixture()
    a = ob.alignment(ob.signal_edges(recs, segs, signals=("us_spillover",)))
    assert a["status"] == "too few signals"
    assert "too few signals" in ob.format_report([], a)


def test_learning_report_says_its_hit_rates_are_not_tradeable():
    """The caveat has to travel with the number it qualifies.

    A scored accuracy quoted without it reads as tradeable accuracy, which is
    the conflation this whole module exists to prevent.
    """
    from oracle.learning.autotune import format_learning_report

    text = format_learning_report([], {})
    assert "close-to-close" in text
    assert "NOT the accuracy of anything a trade could have captured" in text
