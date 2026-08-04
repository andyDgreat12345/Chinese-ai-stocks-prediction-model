"""Technical-indicator engine — the "specific indicators, technical words" layer.

The analyst was reasoning over the day's *headlines and one-day US moves* — broad,
soft signals. This adds the hard technical state of each China sector's own price
series (the sector ETF we already store day over day): trend, RSI, MACD, and
momentum. Feeding these into the analyst makes its calls concrete and technical
("KWEB oversold, RSI 28, below the 20-day, MACD turning up") instead of vague.

All pure functions over a chronological (oldest→newest) list of closes, so they
unit-test without a DB or network. Every function degrades gracefully to ``None``
when the series is too short rather than raising, because early in the data's life
there simply isn't enough history for, say, a 50-day average.

**Not investment advice.** Indicators describe price behavior; they are one input
to a probabilistic lean, never a buy/sell trigger on their own.
"""
from __future__ import annotations


def sma(vals: list[float], n: int) -> float | None:
    return round(sum(vals[-n:]) / n, 4) if len(vals) >= n else None


def _ema_series(vals: list[float], n: int) -> list[float]:
    """EMA at each step from the first full window onward (seeded with the SMA)."""
    if len(vals) < n:
        return []
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out = [e]
    for v in vals[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def ema(vals: list[float], n: int) -> float | None:
    s = _ema_series(vals, n)
    return round(s[-1], 4) if s else None


def rsi(vals: list[float], n: int = 14) -> float | None:
    """Wilder's RSI over the whole series (0–100). >70 overbought, <30 oversold."""
    if len(vals) <= n:
        return None
    deltas = [vals[i] - vals[i - 1] for i in range(1, len(vals))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain, avg_loss = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(deltas)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 2)


def macd(vals: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD line, signal line, and histogram (last values). Hist > 0 = bullish
    momentum crossover, < 0 = bearish."""
    ef, es = _ema_series(vals, fast), _ema_series(vals, slow)
    if not ef or not es:
        return None
    m = min(len(ef), len(es))
    macd_line = [a - b for a, b in zip(ef[-m:], es[-m:])]
    sig = _ema_series(macd_line, signal)
    line = round(macd_line[-1], 4)
    if not sig:
        return {"macd": line, "signal": None, "hist": None}
    return {"macd": line, "signal": round(sig[-1], 4),
            "hist": round(macd_line[-1] - sig[-1], 4)}


def momentum(vals: list[float], n: int = 10) -> float | None:
    """N-day price momentum as a percent return."""
    if len(vals) <= n or vals[-n - 1] == 0:
        return None
    return round((vals[-1] / vals[-n - 1] - 1) * 100, 2)


def technical_signals(ind: dict) -> dict:
    """Turn an indicator snapshot into normalized −1..+1 model signals the scorer
    can weight. Pure.

    Deliberately encodes three *different* hypotheses so the learner can discover
    which (if any) pays in this market, rather than us assuming:
      * ``rsi_signal`` — MEAN REVERSION: oversold (low RSI) reads bullish.
      * ``momentum_signal`` — TREND FOLLOWING: recent gains read bullish. This is
        the opposite bet to the RSI term, on purpose.
      * ``trend_signal`` — position relative to the moving averages.
    A signal is 0.0 when its indicator isn't computable yet, so short history
    contributes nothing rather than a fabricated reading."""
    out = {"rsi_signal": 0.0, "momentum_signal": 0.0, "trend_signal": 0.0}
    rsi_v = ind.get("rsi")
    if rsi_v is not None:
        out["rsi_signal"] = max(-1.0, min(1.0, (50.0 - rsi_v) / 30.0))
    mom = ind.get("momentum_10d")
    if mom is not None:
        out["momentum_signal"] = max(-1.0, min(1.0, mom / 8.0))
    trend = ind.get("trend")
    out["trend_signal"] = {"uptrend": 1.0, "above 20d": 0.5,
                           "below 20d": -0.5, "downtrend": -1.0}.get(trend, 0.0)
    return {k: round(v, 4) for k, v in out.items()}


def signals_from_closes(closes: list[float]) -> dict:
    """Convenience: indicator snapshot → model signals, in one step."""
    return technical_signals(compute_indicators(closes))


def compute_indicators(closes: list[float]) -> dict:
    """Full technical snapshot + human-readable state for one instrument."""
    n = len(closes)
    if n < 2:
        return {"n": n, "technical_note": "insufficient history"}
    last = closes[-1]
    s20, s50 = sma(closes, 20), sma(closes, 50)
    r = rsi(closes)
    mo = momentum(closes, 10)
    mac = macd(closes)

    if s20 and s50 and last > s20 > s50:
        trend = "uptrend"
    elif s20 and s50 and last < s20 < s50:
        trend = "downtrend"
    elif s20:
        trend = "above 20d" if last >= s20 else "below 20d"
    else:
        trend = "unknown"
    rsi_state = ("overbought" if r is not None and r >= 70
                 else "oversold" if r is not None and r <= 30
                 else "neutral" if r is not None else "n/a")
    macd_state = ("n/a" if not mac or mac["hist"] is None
                  else "bullish" if mac["hist"] > 0 else "bearish")

    parts = [trend]
    if r is not None:
        parts.append(f"RSI {r} ({rsi_state})")
    if mo is not None:
        parts.append(f"10d mom {mo:+.1f}%")
    if mac and mac["hist"] is not None:
        parts.append(f"MACD {macd_state}")
    return {
        "n": n, "last": round(last, 4),
        "sma20": s20, "sma50": s50, "rsi": r, "rsi_state": rsi_state,
        "macd": mac, "macd_state": macd_state, "momentum_10d": mo,
        "trend": trend, "technical_note": "; ".join(parts),
    }
