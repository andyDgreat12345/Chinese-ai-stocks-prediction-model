"""Fixture-driven tests for the scoring function (spec §6, Phase 3)."""
from oracle.analysis.scoring import SectorSignals, score_sector


def test_strong_agreement_is_bullish_high():
    p = score_sector(SectorSignals(us_spillover=0.8, sentiment=0.6))
    assert p.direction == "bullish"
    assert p.confidence == "high"


def test_strong_agreement_bearish_high():
    p = score_sector(SectorSignals(us_spillover=-0.7, sentiment=-0.5))
    assert p.direction == "bearish"
    assert p.confidence == "high"


def test_disagreement_drops_confidence():
    # US strongly up, news strongly down -> low confidence (spec §4.4).
    p = score_sector(SectorSignals(us_spillover=0.9, sentiment=-0.9))
    assert p.confidence == "low"


def test_flat_signals_are_neutral():
    p = score_sector(SectorSignals(us_spillover=0.0, sentiment=0.0))
    assert p.direction == "neutral"


def test_macro_flag_can_lift_confidence():
    weak = score_sector(SectorSignals(us_spillover=0.2, sentiment=0.0))
    strong = score_sector(
        SectorSignals(us_spillover=0.2, sentiment=0.0, macro=0.8, macro_flag=True)
    )
    assert strong.composite > weak.composite
    assert strong.confidence == "high"


def test_composite_is_clamped():
    p = score_sector(SectorSignals(us_spillover=1.0, sentiment=1.0, macro=1.0))
    assert -1.0 <= p.composite <= 1.0
