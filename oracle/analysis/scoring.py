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


@dataclass(frozen=True)
class SectorSignals:
    """Component signals for one China sector on one day.

    All scores are normalized to roughly -1 (bearish) .. +1 (bullish).
    `macro_flag` is a manual dominance signal for scheduled events.
    """
    us_spillover: float
    sentiment: float
    macro: float = 0.0
    macro_flag: bool = False


@dataclass(frozen=True)
class Prediction:
    direction: str      # bullish / neutral / bearish
    confidence: str     # low / med / high
    composite: float
    rationale: str


def _bucket(score: float) -> str:
    if score >= BULLISH_THRESHOLD:
        return "bullish"
    if score <= BEARISH_THRESHOLD:
        return "bearish"
    return "neutral"


def _confidence(signals: SectorSignals, composite: float) -> str:
    """Confidence rises with signal agreement, falls when signals disagree.

    Per spec §4.4: if US spillover and news sentiment point opposite ways,
    confidence drops. A live macro event (macro_flag) can override upward.
    """
    us_dir = _sign(signals.us_spillover)
    sent_dir = _sign(signals.sentiment)

    agree = us_dir != 0 and us_dir == sent_dir
    disagree = us_dir != 0 and sent_dir != 0 and us_dir != sent_dir

    if disagree:
        return "low"
    if signals.macro_flag and abs(composite) >= BULLISH_THRESHOLD:
        return "high"
    if agree and abs(composite) >= BULLISH_THRESHOLD:
        return "high"
    if abs(composite) >= BULLISH_THRESHOLD:
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
) -> Prediction:
    """Combine component signals into a bucketed directional prediction."""
    w = weights or DEFAULT_WEIGHTS
    composite = (
        w["us_spillover"] * signals.us_spillover
        + w["sentiment"] * signals.sentiment
        + w["macro"] * signals.macro
    )
    composite = max(-1.0, min(1.0, composite))

    direction = _bucket(composite)
    confidence = _confidence(signals, composite)

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
