"""Calibrated magnitude forecast — the honest answer to "how much will it move?"

The model predicts *direction*. Turning that into a number people can act on is
where forecasting usually starts lying: a confident-looking point estimate
("+1.4% tomorrow") implies a precision a ~55%-accuracy directional signal simply
does not have.

So instead of inventing a point, this reports the **empirical distribution of
what actually happened** on the historical days the model made this same call for
this same sector: the median move, a p10–p90 range, and the hit-rate behind it.
Every number is a count of real outcomes, not a model extrapolation — if the
range is wide, that is the truth about the edge, displayed rather than hidden.

This is what the dashboard's forecast cone draws. A cone whose width comes from
measured error is a legitimate forecast; a drawn "predicted candlestick" with an
invented open/high/low is not, which is why we render the former and not the
latter (we have no intraday data at all — only one close per day).

Pure functions over the backtest's replayed records. No DB, no network.

**Not investment advice.** A distribution of past outcomes under a similar signal
is not a promise about the next one.
"""
from __future__ import annotations

MIN_SAMPLE = 12          # below this, report "insufficient history" not a number


def percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of an ascending list. q in 0..1. Pure."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def outcome_distribution(records: list[dict], sector: str, direction: str,
                         min_sample: int = MIN_SAMPLE) -> dict:
    """What actually happened, historically, when the model called `direction`
    for `sector`. Returns median/p10/p90 of the realized move plus the hit-rate.

    For a bearish call the *signed* return is reported from the position's point
    of view (a −2% move on a bearish call is a +2% gain), while ``median_move_pct``
    stays in raw market terms so the cone is drawn on the price axis correctly."""
    moves = [r["actual_move"] for r in records
             if r.get("sector") == sector and r.get("model_dir") == direction
             and r.get("actual_move") is not None]
    n = len(moves)
    if n < min_sample:
        return {"n": n, "enough": False, "direction": direction}
    moves.sort()
    hits = sum(1 for m in moves
               if (m > 0 and direction == "bullish") or (m < 0 and direction == "bearish"))
    med = percentile(moves, 0.5)
    return {
        "n": n, "enough": True, "direction": direction,
        "median_move_pct": round(med, 3),
        "p10_move_pct": round(percentile(moves, 0.10), 3),
        "p90_move_pct": round(percentile(moves, 0.90), 3),
        "hit_rate": round(hits / n, 4) if direction != "neutral" else None,
        # Expected gain to a position taking the call (sign-adjusted).
        "median_signed_return_pct": round(med if direction == "bullish" else -med, 3),
    }


def sector_forecasts(records: list[dict], current_calls: dict[str, str],
                     min_sample: int = MIN_SAMPLE) -> dict[str, dict]:
    """Per-sector forecast for today's calls: {sector: distribution}. Pure.

    ``current_calls`` maps sector -> the direction the model is calling now."""
    return {sector: outcome_distribution(records, sector, direction, min_sample)
            for sector, direction in current_calls.items()}


def format_range(dist: dict) -> str:
    """One honest human line, e.g. 'median +0.4%, 10-90% range -1.8% to +2.4%
    (n=63, 57% right)'."""
    if not dist.get("enough"):
        return f"insufficient history (n={dist.get('n', 0)})"
    parts = [f"median {dist['median_move_pct']:+.2f}%",
             f"10–90% range {dist['p10_move_pct']:+.2f}% to {dist['p90_move_pct']:+.2f}%"]
    tail = f"n={dist['n']}"
    if dist.get("hit_rate") is not None:
        tail += f", {dist['hit_rate'] * 100:.0f}% right"
    return f"{', '.join(parts)} ({tail})"
