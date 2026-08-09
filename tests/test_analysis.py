

# ── confidence must not bypass the weights ────────────────────────────────
def test_sentiment_cannot_gate_confidence_until_it_earns_a_weight():
    """Live bug: _confidence read signals.sentiment directly, so when the news
    layer came alive it forced 37% of calls to 'low' by disagreement while its
    fitted weight was still pinned at 0.00 for want of coverage."""
    from oracle.analysis.scoring import SectorSignals, score_sector

    # US bullish, news bearish — a disagreement.
    sig = SectorSignals(us_spillover=0.8, sentiment=-0.8, macro=0.0)
    unearned = {"us_spillover": 1.0, "sentiment": 0.0, "threshold": 0.15}
    earned = {"us_spillover": 0.6, "sentiment": 0.4, "threshold": 0.15}

    assert score_sector(sig, unearned, 0.15).confidence != "low", \
        "a zero-weight signal must not drag confidence down"
    assert score_sector(sig, earned, 0.15).confidence == "low", \
        "once weighted, disagreement should still lower confidence"


def test_weighted_agreement_still_raises_confidence():
    from oracle.analysis.scoring import SectorSignals, score_sector

    sig = SectorSignals(us_spillover=0.8, sentiment=0.8, macro=0.0)
    earned = {"us_spillover": 0.6, "sentiment": 0.4, "threshold": 0.15}
    assert score_sector(sig, earned, 0.15).confidence == "high"
