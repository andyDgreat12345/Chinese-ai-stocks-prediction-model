"""US↔China paired series — seeing the lead/lag relationship, not just its number.

The correlation leaderboard reports a coefficient and a best-fit lag per US↔China
symbol pair. This turns each pair into something you can *look at*: the two price
series on one normalized axis, with the US series shifted forward by the measured
lag so a real lead shows up as the lines moving together.

**The timing point this module exists to make.** China A-shares close 15:00 CST
(07:00 UTC); the US closes 16:00 ET (21:00 UTC) the same calendar date — about 14
hours *later*. So a ``lag = 0`` correlation pairs a China close with a US close
that had not happened yet. It is real (both markets respond to the same global
risk mood) but it is **not tradeable**: you cannot act on tomorrow's information
today. Only ``lag >= 1`` — US close on day *d* against the China session on day
*d+1* — is a signal you could actually use.

That distinction is why the leaderboard's headline numbers (r≈0.6 at lag 0) are so
much larger than the model's measured predictive edge (r≈0.1): the big correlation
is the untradeable one. Every payload here is labeled ``predictive`` accordingly,
so the dashboard can't accidentally present a mirage as an edge.

Pure functions over date→value maps. No DB, no network.
"""
from __future__ import annotations

# Market close times, UTC, for the timing explanation.
CHINA_CLOSE_UTC = "07:00"
US_CLOSE_UTC = "21:00"
HOURS_US_AFTER_CHINA = 14


def is_predictive(lag: int) -> bool:
    """Only a lag of at least one session can inform a *future* China move."""
    return lag >= 1


def lag_note(lag: int) -> str:
    """Plain-language reading of what a given lag means for tradeability."""
    if lag >= 1:
        return (f"US close leads the China session by {lag} day(s) — the US bar "
                f"is already known when China opens, so this one is usable.")
    return (f"Same-day: China closes {CHINA_CLOSE_UTC} UTC, the US closes "
            f"{US_CLOSE_UTC} UTC — about {HOURS_US_AFTER_CHINA}h LATER. Both react "
            "to the same global mood, but the US bar does not exist yet when China "
            "closes, so this correlation is NOT tradeable.")


def rebase(series: list[tuple[str, float]]) -> list[dict]:
    """Normalize a (date, close) series to % change from its first point, so two
    instruments at different price scales share one axis. Pure."""
    vals = [(d, v) for d, v in series if v is not None]
    if not vals:
        return []
    base = vals[0][1]
    if not base:
        return []
    return [{"d": d, "v": round((v / base - 1.0) * 100.0, 4)} for d, v in vals]


def align_with_lag(us: list[tuple[str, float]], china: list[tuple[str, float]],
                   lag: int) -> dict:
    """Overlay the two series on the China trading calendar, shifting the US
    series FORWARD by ``lag`` sessions so a genuine lead lines up visually.

    Returns {dates, us, china} as rebased % series over the common sessions."""
    china_map = {d: v for d, v in china if v is not None}
    us_pairs = [(d, v) for d, v in us if v is not None]
    common = sorted(set(china_map) & {d for d, _ in us_pairs})
    if len(common) <= lag:
        return {"dates": [], "us": [], "china": []}
    us_map = dict(us_pairs)
    # China session at index i is paired with the US close `lag` sessions earlier.
    dates = common[lag:]
    us_shifted = [(d, us_map[common[i]]) for i, d in enumerate(dates)]
    china_on = [(d, china_map[d]) for d in dates]
    return {
        "dates": dates,
        "us": rebase(us_shifted),
        "china": rebase(china_on),
    }


def build_pair(row: dict, us_series: list[tuple[str, float]],
               china_series: list[tuple[str, float]], limit: int = 90) -> dict:
    """One leaderboard row + its two aligned, rebased series, ready to chart."""
    lag = int(row.get("best_lag") or 0)
    aligned = align_with_lag(us_series[-(limit + lag):], china_series[-limit:], lag)
    return {
        "us_symbol": row.get("us_symbol"),
        "china_symbol": row.get("china_symbol"),
        "correlation": row.get("correlation"),
        "best_lag": lag,
        "sample_size": row.get("sample_size"),
        "window_days": row.get("window_days"),
        "predictive": is_predictive(lag),
        "lag_note": lag_note(lag),
        **aligned,
    }


def rank_pairs(rows: list[dict], predictive_first: bool = True) -> list[dict]:
    """Order leaderboard rows for display: tradeable (lag>=1) relationships first,
    each group by |correlation| descending. Showing the untradeable lag-0 giants at
    the top would be the single most misleading thing this panel could do."""
    def key(r):
        c = abs(r.get("correlation") or 0.0)
        pred = is_predictive(int(r.get("best_lag") or 0))
        return (0 if (pred and predictive_first) else 1, -c)
    return sorted(rows, key=key)
