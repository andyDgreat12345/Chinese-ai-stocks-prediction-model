"""Tests for the lexicon sentiment + category classifier (spec §4.2)."""
from oracle.analysis.sentiment import analyze, classify_category, score_sentiment


def test_positive_headline_scores_positive():
    assert score_sentiment("Stocks rally as chip makers surge to record highs") > 0.3


def test_negative_headline_scores_negative():
    assert score_sentiment("Markets plunge on recession fears and heavy selloff") < -0.3


def test_neutral_headline_is_zero():
    assert score_sentiment("Committee meets to discuss quarterly schedule") == 0.0


def test_negation_flips_sign():
    assert score_sentiment("no gains for markets today") < 0


def test_score_stays_in_range():
    s = score_sentiment("surge rally soar boost jump gains record strong bullish")
    assert -1.0 <= s <= 1.0


def test_category_fed():
    assert classify_category("Fed signals rate cut after FOMC meeting") == "fed_policy"


def test_category_chip_export():
    assert classify_category("US tightens semiconductor export controls") == "chip_export"


def test_category_falls_back_to_general():
    assert classify_category("Local weather disrupts weekend travel") == "general"


def test_analyze_returns_both():
    sig = analyze("China unveils stimulus", "PBOC cuts reserve requirement")
    assert sig.category == "china_stimulus"
    assert isinstance(sig.sentiment, float)
