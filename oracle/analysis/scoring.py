"""Weighted signal scoring (spec §4).

A deliberately transparent, explainable scoring function — no black box. It is
a *pure function* of its inputs so it can be tested against fixture data before
being wired to live feeds (spec §6, Phase 3).

    signals -> composite score -> {bullish, neutral, bearish} + confidence
"""
from __future__ import annotations

from dataclasses import dataclass

# Default weights. In production these come from the `weights` table so the
# reflection loop can propose adjustments (spec §4b-iii); passed in explicitly
# here to keep the function pure and testable.
DEFAULT_WEIGHTS = {"us_spillover": 0.45, "sentiment": 0.35, "macro": 0.20}

# Score thresholds for bucketing the composite (range roughly -1..1).
BULLISH_THRESHOLD = 0.15
BEARISH_THRESHOLD = -0.15


# Every component the composite can weight. Weight dicts may carry any subset;
# absent keys contribute nothing, so old 3-signal weights behave exactly as before.
SIGNAL_KEYS = ("us_spillover", "sentiment", "macro",
               "rsi_signal", "momentum_signal", "trend_signal")


@dataclass(frozen=True)
class SectorSignals:
    """Component signals for one China sector on one day.

    All scores are normalized to roughly -1 (bearish) .. +1 (bullish).
    `macro_flag` is a manual dominance signal for scheduled events.

    The three technical fields come from the sector's OWN price history
    (`analysis/technicals.py`): mean-reversion from RSI, trend-following from
    momentum, and position vs. the moving averages. They default to 0.0 so a
    caller that doesn't supply them scores exactly as before.
    """
    us_spillover: float
    sentiment: float
    macro: float = 0.0
    macro_flag: bool = False
    rsi_signal: float = 0.0
    momentum_signal: float = 0.0
    trend_signal: float = 0.0


@dataclass(frozen=True)
class Prediction:
    direction: str      # bullish / neutral / bearish
    confidence: str     # low / med / high
    composite: float
    rationale: str


def _bucket(score: float, threshold: float = BULLISH_THRESHOLD) -> str:
    if score >= threshold:
        return "bullish"
    if score <= -threshold:
        return "bearish"
    return "neutral"


def _confidence(signals: SectorSignals, composite: float,
                threshold: float = BULLISH_THRESHOLD,
                weights: dict[str, float] | None = None) -> str:
    """Confidence rises with signal agreement, falls when signals disagree.

    Per spec §4.4: if US spillover and news sentiment point opposite ways,
    confidence drops. A live macro event (macro_flag) can override upward.

    **Sentiment only counts here if it has earned a weight.** This function used
    to read ``signals.sentiment`` directly, bypassing the weights entirely — so
    when the news layer came alive, sentiment began gating live predictions
    (37% of calls forced to "low" by disagreement) while its fitted weight was
    still pinned at 0.00 for want of coverage. That is two gates where there
    should be one: the learner refuses to trust a signal until it clears the
    coverage bar, and confidence must refuse it on exactly the same terms.
    Passing no weights preserves the old behaviour for callers that have none.
    """
    us_dir = _sign(signals.us_spillover)
    sent_dir = _sign(signals.sentiment)
    if weights is not None and float(weights.get("sentiment", 0.0)) == 0.0:
        sent_dir = 0        # unearned -> carries no agreement information

    agree = us_dir != 0 and us_dir == sent_dir
    disagree = us_dir != 0 and sent_dir != 0 and us_dir != sent_dir

    if disagree:
        return "low"
    if signals.macro_flag and abs(composite) >= threshold:
        return "high"
    if agree and abs(composite) >= threshold:
        return "high"
    if abs(composite) >= threshold:
        return "med"
    return "low"


# ── graded conviction ────────────────────────────────────────────────────
# Five tiers, most bullish to most bearish. Adapted from the rating scale in
# TauricResearch/TradingAgents (Phase 02 of that graft).
#
# This exists because the incumbent `confidence` field was unusable: it depended
# on US spillover AGREEING with news sentiment, and while sentiment carries no
# weight that comparison is always a tie — so every one of 8,253 directional
# calls came out "med" and the simulator's `conviction >= med` filter filtered
# nothing. Selectivity was impossible.
#
# The rating is derived from |composite| relative to the abstain threshold
# instead, which is the quantity that is actually fitted and that varies on every
# session. A call at twice the threshold is a genuinely stronger statement than
# one that just cleared it, and that distinction was previously discarded.
RATINGS_5_TIER = ("Buy", "Overweight", "Hold", "Underweight", "Sell")

# Multiple of the threshold above which a call counts as strong. 2.0 keeps the
# top tier meaningfully rare rather than relabelling half the book "Buy".
STRONG_MULTIPLE = 2.0


def rating(composite: float, threshold: float,
           strong_multiple: float = STRONG_MULTIPLE) -> str:
    """Five-tier rating from the composite. Pure.

    Thresholds are inclusive at the strong boundary so a call sitting exactly on
    it reads as the stronger tier, matching `_bucket`'s treatment of `threshold`.
    """
    thr = abs(float(threshold))
    strong = thr * strong_multiple
    if composite >= strong:
        return "Buy"
    if composite >= thr:
        return "Overweight"
    if composite <= -strong:
        return "Sell"
    if composite <= -thr:
        return "Underweight"
    return "Hold"


def conviction_from_rating(rating_label: str) -> str:
    """Map the 5-tier rating onto the low/med/high vocabulary the simulator and
    the daily report already speak, so the wider scale is usable without
    rewriting every consumer. Pure."""
    if rating_label in ("Buy", "Sell"):
        return "high"
    if rating_label in ("Overweight", "Underweight"):
        return "med"
    return "low"


def _sign(x: float) -> int:
    if x > 1e-9:
        return 1
    if x < -1e-9:
        return -1
    return 0


def score_sector(
    signals: SectorSignals,
    weights: dict[str, float] | None = None,
    threshold: float | None = None,
) -> Prediction:
    """Combine component signals into a bucketed directional prediction.

    ``threshold`` is the |composite| above which a directional call is made
    (below it the model abstains as neutral). It defaults to the module constant
    but is *learnable* — the walk-forward tuner fits it per sector alongside the
    weights (see ``oracle/learning/``)."""
    w = weights or DEFAULT_WEIGHTS
    thr = BULLISH_THRESHOLD if threshold is None else float(threshold)
    composite = sum(float(w.get(k, 0.0)) * float(getattr(signals, k, 0.0))
                    for k in SIGNAL_KEYS)
    composite = max(-1.0, min(1.0, composite))

    direction = _bucket(composite, thr)
    tier = rating(composite, thr)
    # Graded conviction from |composite| replaces the agreement-based confidence,
    # which was constant in practice (see RATINGS_5_TIER). The old signal-
    # agreement read is kept only to DOWNGRADE: genuine disagreement between two
    # weighted signals should still cost conviction, but agreement can no longer
    # be the only route to a high one.
    agreement = _confidence(signals, composite, thr, w)
    confidence = conviction_from_rating(tier)
    # A live macro event still lifts conviction (spec §4.4) — deriving the tier
    # purely from |composite| would otherwise have silently dropped that.
    if signals.macro_flag and direction != "neutral" and confidence == "med":
        confidence = "high"
    # ...but genuine disagreement between two WEIGHTED signals still costs a
    # full step (spec §4.4). Agreement can no longer be the only route to high
    # conviction; disagreement can still take conviction away. The composite
    # already shrinks when signals oppose — this is the separate statement that
    # a call assembled from sources that disagree is less trustworthy than one
    # of the same magnitude whose sources concur.
    elif agreement == "low":
        confidence = {"high": "med", "med": "low"}.get(confidence, confidence)

    parts = [
        f"US spillover {signals.us_spillover:+.2f}",
        f"sentiment {signals.sentiment:+.2f}",
    ]
    if signals.macro_flag:
        parts.append(f"macro event ({signals.macro:+.2f})")
    rationale = f"{direction} ({confidence}): " + ", ".join(parts)

    return Prediction(
        direction=direction,
        confidence=confidence,
        composite=round(composite, 4),
        rationale=rationale,
    )
