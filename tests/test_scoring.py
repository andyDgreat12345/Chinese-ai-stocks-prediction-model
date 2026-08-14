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


# ── graded conviction (Phase 02) ──────────────────────────────────────────
def test_rating_grades_by_composite_strength():
    """Regression: confidence depended on US spillover AGREEING with sentiment,
    and while sentiment carries no weight that comparison is always a tie — so
    all 8,253 directional calls came out 'med' and the simulator's
    `conviction >= med` filter filtered nothing."""
    from oracle.analysis.scoring import rating

    assert rating(0.40, 0.15) == "Buy"           # >= 2x threshold
    assert rating(0.20, 0.15) == "Overweight"    # cleared, not strongly
    assert rating(0.00, 0.15) == "Hold"
    assert rating(-0.20, 0.15) == "Underweight"
    assert rating(-0.40, 0.15) == "Sell"


def test_rating_boundary_reads_as_the_stronger_tier():
    from oracle.analysis.scoring import rating

    assert rating(0.30, 0.15) == "Buy"           # exactly 2x
    assert rating(0.15, 0.15) == "Overweight"    # exactly 1x


def test_conviction_actually_varies_now():
    """The whole point: a filter on conviction must be able to filter."""
    from oracle.analysis.scoring import SectorSignals, score_sector

    w = {"us_spillover": 1.0, "threshold": 0.15}
    strong = score_sector(SectorSignals(us_spillover=0.9, sentiment=0.0), w, 0.15)
    weak = score_sector(SectorSignals(us_spillover=0.2, sentiment=0.0), w, 0.15)
    assert strong.confidence == "high"
    assert weak.confidence == "med"
    assert strong.confidence != weak.confidence


def test_disagreement_costs_a_full_step():
    """A call assembled from sources that disagree is less trustworthy than one
    of the same magnitude whose sources concur (spec §4.4)."""
    from oracle.analysis.scoring import SectorSignals, score_sector

    w = {"us_spillover": 0.6, "sentiment": 0.4, "threshold": 0.15}
    concur = score_sector(SectorSignals(us_spillover=0.9, sentiment=0.9), w, 0.15)
    oppose = score_sector(SectorSignals(us_spillover=0.9, sentiment=-0.2), w, 0.15)
    assert concur.confidence == "high"
    assert oppose.confidence in ("med", "low")
