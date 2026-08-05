"""Accumulated correlation — which US↔China links have *held up over time*.

`correlations` is a snapshot: it is overwritten every day, so a pair that reads
+0.6 today and −0.2 next week looks identical to one that has read +0.4 steadily
for months. That is the difference between a relationship and a coincidence, and
a snapshot cannot express it.

This module reads the append-only ``correlation_history`` and turns each pair's
accumulated readings into a judgement with three parts:

  * **strength** — the expanding-window correlation (computed over ALL history to
    date, so its sample grows and its estimate steadies as time passes);
  * **persistence** — what fraction of past readings carried the same sign, i.e.
    has the relationship actually been *stable*, or has it flickered;
  * **maturity** — how many separate days we have observed it at all.

``reliability`` multiplies the three, so a pair only ranks highly by being strong
*and* consistent *and* observed long enough. That is the list to pick "the several
most correlated ones" from — not today's biggest number, which is usually noise
that will not be there next month.

Tradeability is enforced on top: ``lag = 0`` pairs are excluded from the default
ranking because China closes ~14h before the US on the same date, so a same-day
correlation is not something anyone can act on (see ``analysis/pairs.py``).

Pure functions over observation dicts — no DB, no clock.
"""
from __future__ import annotations

# Observations needed before a pair counts as fully "mature". Below this the
# reliability score is scaled down rather than the pair being hidden, so a
# promising new link is visible but honestly discounted.
MATURITY_TARGET = 30


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def stdev(vals: list[float]) -> float | None:
    n = len(vals)
    if n < 2:
        return None
    m = sum(vals) / n
    return (sum((v - m) ** 2 for v in vals) / (n - 1)) ** 0.5


def persistence(values: list[float]) -> float | None:
    """Fraction of readings sharing the sign of the mean reading. 1.0 = the
    relationship never flipped direction; ~0.5 = it is a coin flip."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    m = mean(vals)
    ref = _sign(m)
    if ref == 0:
        return 0.0
    return round(sum(1 for v in vals if _sign(v) == ref) / len(vals), 4)


def maturity(n_observations: int, target: int = MATURITY_TARGET) -> float:
    return round(min(1.0, n_observations / target), 4) if target else 1.0


def summarize(observations: list[dict], target: int = MATURITY_TARGET) -> dict:
    """Accumulated view of one pair/lag from its history of readings. Pure."""
    vals = [o["correlation"] for o in observations if o.get("correlation") is not None]
    n_obs = len(vals)
    if not n_obs:
        return {"n_observations": 0, "reliability": 0.0}
    m = mean(vals)
    p = persistence(vals) or 0.0
    mat = maturity(n_obs, target)
    latest = observations[-1]
    return {
        "n_observations": n_obs,
        "mean_correlation": round(m, 4),
        "latest_correlation": round(latest["correlation"], 4)
        if latest.get("correlation") is not None else None,
        "stdev": round(stdev(vals), 4) if stdev(vals) is not None else None,
        "persistence": p,
        "maturity": mat,
        "sample_size": latest.get("sample_size"),
        "first_observed": observations[0].get("observed_on"),
        "last_observed": latest.get("observed_on"),
        # Strong AND consistent AND observed long enough — all three, or it doesn't rank.
        "reliability": round(abs(m) * p * mat, 4),
    }


def rank_accumulated(grouped: dict, predictive_only: bool = True,
                     min_observations: int = 3, target: int = MATURITY_TARGET) -> list[dict]:
    """Rank every accumulated pair by reliability, most trustworthy first.

    ``grouped`` maps (us_symbol, china_symbol, lag) -> [observations]. By default
    same-day (lag 0) pairs are excluded: they cannot be traded, so ranking them
    would put the least usable relationships at the top of the list."""
    out = []
    for (us_sym, cn_sym, lag), obs in grouped.items():
        if predictive_only and lag < 1:
            continue
        s = summarize(obs, target)
        if s["n_observations"] < min_observations:
            continue
        out.append({"us_symbol": us_sym, "china_symbol": cn_sym, "lag": lag,
                    "predictive": lag >= 1, **s})
    out.sort(key=lambda r: -r["reliability"])
    return out


def format_accumulated(rows: list[dict], limit: int = 10) -> str:
    """Human-readable accumulated leaderboard for the digest / CLI."""
    lines = ["accumulated US→China correlations (built up over time, "
             "tradeable lags only):"]
    if not rows:
        lines.append("  (nothing accumulated yet — readings are recorded daily by "
                     "the reflection pass, so this fills in as days pass)")
        return "\n".join(lines)
    lines.append(f"  {'pair':<26}{'lag':>4}{'mean r':>9}{'persist':>9}"
                 f"{'obs':>6}{'reliab':>9}")
    for r in rows[:limit]:
        pair = f"{r['us_symbol']} → {r['china_symbol']}"
        lines.append(f"  {pair:<26}{r['lag']:>4}{r['mean_correlation']:>9.3f}"
                     f"{r['persistence'] * 100:>8.0f}%{r['n_observations']:>6}"
                     f"{r['reliability']:>9.3f}")
    lines.append("  reliability = |mean r| × sign-persistence × maturity — strong, "
                 "consistent AND observed long enough.")
    return "\n".join(lines)
