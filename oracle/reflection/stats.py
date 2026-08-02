"""Pure statistics for the reflection loop (spec §4b). No I/O, no deps."""
from __future__ import annotations


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation of two equal-length series. None if undefined
    (fewer than 2 points or a zero-variance series)."""
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / ((vx ** 0.5) * (vy ** 0.5))


def lagged_pairs(
    us_by_date: dict[str, float],
    china_by_date: dict[str, float],
    lag: int,
) -> tuple[list[float], list[float]]:
    """Pair China move on day t with US move on day t-`lag`, over the two
    series' common trading dates (their intersection is treated as the trading
    sequence, so mismatched holiday calendars don't misalign the pairs)."""
    common = sorted(set(us_by_date) & set(china_by_date))
    xs, ys = [], []
    for i in range(lag, len(common)):
        xs.append(us_by_date[common[i - lag]])   # US, lagged
        ys.append(china_by_date[common[i]])      # China, current day
    return xs, ys


def best_lag_correlation(
    us_by_date: dict[str, float],
    china_by_date: dict[str, float],
    max_lag: int = 1,
    window: int | None = None,
) -> tuple[float | None, int, int]:
    """Return (correlation, lag, sample_size) for the lag in 0..max_lag with the
    strongest |correlation|. `window` limits to the most recent N pairs.
    (None, 0, 0) if nothing is computable."""
    best: tuple[float | None, int, int] = (None, 0, 0)
    for lag in range(0, max_lag + 1):
        xs, ys = lagged_pairs(us_by_date, china_by_date, lag)
        if window is not None and len(xs) > window:
            xs, ys = xs[-window:], ys[-window:]
        c = pearson(xs, ys)
        if c is None:
            continue
        if best[0] is None or abs(c) > abs(best[0]):
            best = (round(c, 4), lag, len(xs))
    return best


# ── Directional bucketing of an actual move (spec §4b-i) ──────────────────
NEUTRAL_BAND_PCT = 0.15  # |move| below this is "neutral" rather than up/down


def direction_from_move(pct: float, band: float = NEUTRAL_BAND_PCT) -> str:
    if pct > band:
        return "bullish"
    if pct < -band:
        return "bearish"
    return "neutral"


# Probability a confidence bucket implies it is correct — for calibration/Brier.
CONFIDENCE_P = {"low": 0.50, "med": 0.65, "high": 0.80}


def brier(confidence: str, correct: bool) -> float:
    """(implied probability - outcome)^2. Lower is better-calibrated (§4b-i)."""
    p = CONFIDENCE_P.get(confidence, 0.5)
    return round((p - (1.0 if correct else 0.0)) ** 2, 4)


def variance(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return sum((v - m) ** 2 for v in values) / (n - 1)
