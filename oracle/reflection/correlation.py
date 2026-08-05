"""(ii) US→China influence measurement — mathematical (spec §4b-ii).

Answers "how much does the US market actually move Chinese markets, and where."
Two products, both recomputed daily as new data lands:

  * rolling correlation + best-fit lag for every US↔China symbol pair, gated by
    a minimum sample-size guard so a handful of points can't masquerade as a
    "strong correlation";
  * a news-category → typical-subsequent-China-move table with sample size and
    variance, turning ad-hoc sentiment into an empirically grounded mapping.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import config, db
from ..analysis.pipeline import SECTOR_NEWS_CATEGORIES
from .stats import best_lag_correlation, lagged_pairs, pearson, variance


def update_correlations() -> int:
    """Recompute rolling correlations for all US↔China symbol pairs over each
    configured window. Returns the number of (pair, window) rows written."""
    now = datetime.now(timezone.utc).isoformat()
    today = now[:10]
    observed = 0
    try:
        us_symbols = db.distinct_symbols("us_close")
        china_symbols = db.distinct_symbols("china_close")
        us_series = {s: db.series_for_symbol("us_close", s) for s in us_symbols}
        china_series = {s: db.series_for_symbol("china_close", s) for s in china_symbols}

        written = 0
        for us_sym in us_symbols:
            for ch_sym in china_symbols:
                for window in config.CORRELATION_WINDOWS:
                    corr, lag, n = best_lag_correlation(
                        us_series[us_sym], china_series[ch_sym],
                        max_lag=1, window=window,
                    )
                    if corr is None:
                        continue
                    db.upsert_correlation({
                        "us_symbol": us_sym,
                        "china_symbol": ch_sym,
                        "window_days": window,
                        "correlation": corr,
                        "best_lag": lag,
                        "sample_size": n,
                        # Below the guard, treat as noise — flagged distinctly (§4b-ii).
                        "established": 1 if n >= config.MIN_CORRELATION_SAMPLE else 0,
                        "computed_at": now,
                    })
                    written += 1

                # ── accumulation (§4b-ii): record today's reading per lag on the
                # EXPANDING window (all history to date), so the estimate steadies
                # as data grows and a stable relationship becomes distinguishable
                # from a lucky snapshot. The snapshot rows above are overwritten
                # daily; these are append-only.
                for lag_k in (0, 1):
                    xs, ys = lagged_pairs(us_series[us_sym], china_series[ch_sym], lag_k)
                    r_all = pearson(xs, ys)
                    if r_all is None:
                        continue
                    db.record_correlation_observation({
                        "observed_on": today, "us_symbol": us_sym,
                        "china_symbol": ch_sym, "lag": lag_k, "window_days": 0,
                        "correlation": round(r_all, 6), "sample_size": len(xs),
                    })
                    observed += 1

        print(f"update_correlations: wrote {written} snapshot rows, "
              f"recorded {observed} accumulated observations for {today}")
        return written
    except Exception as e:  # noqa: BLE001
        print(f"update_correlations FAILED: {e!r}")
        return 0


def accumulated_leaderboard(predictive_only: bool = True, db_path=None) -> list[dict]:
    """The accumulated (built-up-over-time) ranking — the list to pick the most
    reliably correlated pairs from. Defaults to tradeable lags only."""
    from .accumulate import rank_accumulated

    return rank_accumulated(db.correlation_history_grouped(0, db_path),
                            predictive_only=predictive_only)


# Which China sectors a news category is expected to move (inverse of the
# pipeline's sector→categories map), so impact is attributed to the right sector.
_CATEGORY_TO_SECTORS: dict[str, list[str]] = {}
for _sector, _cats in SECTOR_NEWS_CATEGORIES.items():
    for _c in _cats:
        _CATEGORY_TO_SECTORS.setdefault(_c, []).append(_sector)


def compute_news_impact(
    news_by_date: dict[str, set[str]],
    china_moves_by_date: dict[str, dict[str, float]],
) -> list[dict]:
    """Pure core: given {date: {categories}} and {date: {sector: move}}, return
    per (category, china_sector) rows of avg move, variance, and sample size.

    Same-day association: overnight news (fetched ~04:30) is mapped to that
    session's China close move for the sectors the category bears on."""
    buckets: dict[tuple[str, str], list[float]] = {}
    for date, categories in news_by_date.items():
        moves = china_moves_by_date.get(date, {})
        for category in categories:
            for sector in _CATEGORY_TO_SECTORS.get(category, []):
                if sector in moves:
                    buckets.setdefault((category, sector), []).append(moves[sector])

    rows = []
    for (category, sector), vals in buckets.items():
        rows.append({
            "category": category,
            "china_sector": sector,
            "avg_move": round(sum(vals) / len(vals), 4),
            "variance": round(variance(vals), 4),
            "sample_size": len(vals),
        })
    return rows


def update_news_impact() -> int:
    """Recompute the news-category → China-sector impact table. Returns rows."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn = db.connect()
        try:
            news = conn.execute(
                "SELECT trade_date, category FROM news WHERE category IS NOT NULL"
            ).fetchall()
            china = conn.execute(
                """SELECT trade_date, sector, AVG(pct_change) AS mv FROM china_close
                   WHERE pct_change IS NOT NULL AND sector IS NOT NULL
                   GROUP BY trade_date, sector"""
            ).fetchall()
        finally:
            conn.close()

        news_by_date: dict[str, set[str]] = {}
        for r in news:
            news_by_date.setdefault(r["trade_date"], set()).add(r["category"])
        china_moves: dict[str, dict[str, float]] = {}
        for r in china:
            china_moves.setdefault(r["trade_date"], {})[r["sector"]] = r["mv"]

        rows = compute_news_impact(news_by_date, china_moves)
        for row in rows:
            db.upsert_news_impact({**row, "updated_at": now})
        print(f"update_news_impact: wrote {len(rows)} category/sector rows")
        return len(rows)
    except Exception as e:  # noqa: BLE001
        print(f"update_news_impact FAILED: {e!r}")
        return 0
