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
    confidence = _confidence(signals, composite, thr, w)

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
