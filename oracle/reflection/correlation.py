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

from bisect import bisect_right
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


def refresh_proven_pairs(db_path=None) -> int:
    """Re-measure every sweep-proven pair and update its correlation factor.

    Runs as part of the reflection round (§4b-ii). A pair proved once is not
    trusted forever: each round recomputes its correlation over all history to
    date, writes the fresh factor to `proven_pairs`, and appends the reading to
    `correlation_history` so the accumulated view keeps building. A link that
    decays shows up as a falling `current_r` instead of silently going stale."""
    now = datetime.now(timezone.utc).isoformat()
    today = now[:10]
    refreshed = 0
    try:
        pairs = db.proven_pairs(db_path)
        if not pairs:
            return 0
        us_cache: dict[str, dict] = {}
        cn_cache: dict[str, dict] = {}
        for p in pairs:
            us_sym, cn_sym, lag = p["us_symbol"], p["china_symbol"], p["lag"]
            if us_sym not in us_cache:
                us_cache[us_sym] = db.series_for_symbol("us_close", us_sym, db_path)
            if cn_sym not in cn_cache:
                cn_cache[cn_sym] = db.series_for_symbol("china_close", cn_sym, db_path)
            xs, ys = lagged_pairs(us_cache[us_sym], cn_cache[cn_sym], lag)
            r = pearson(xs, ys)
            if r is None:
                continue
            db.refresh_proven_pair(us_sym, cn_sym, lag, round(r, 6), len(xs),
                                   today, db_path)
            db.record_correlation_observation({
                "observed_on": today, "us_symbol": us_sym, "china_symbol": cn_sym,
                "lag": lag, "window_days": 0, "correlation": round(r, 6),
                "sample_size": len(xs)}, db_path=db_path)
            refreshed += 1
        print(f"refresh_proven_pairs: updated {refreshed} proven pair(s) for {today}")
        return refreshed
    except Exception as e:  # noqa: BLE001 — never break the reflection round
        print(f"refresh_proven_pairs FAILED: {e!r}")
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


def _next_session(sessions: list[str], after: str) -> str | None:
    """First China trading session strictly after `after`, or None."""
    i = bisect_right(sessions, after)
    return sessions[i] if i < len(sessions) else None


def compute_news_impact(
    news_by_date: dict[str, set[str]],
    china_moves_by_date: dict[str, dict[str, float]],
) -> list[dict]:
    """Pure core: given {date: {categories}} and {date: {sector: move}}, return
    per (category, china_sector) rows of avg move, variance, and sample size.

    **News on date D is mapped to the NEXT China session, not the same one.**
    This used to be a same-day join, justified by a comment saying news is
    "fetched ~04:30" and so belongs to that morning's session. The cron says
    otherwise: the morning job fires at 21:00 UTC, which is 05:00 CST the
    *following* day, and `fetch_world_news` stamps trade_date with the UTC date
    at fetch time. So headlines stamped D are gathered ~14h AFTER china_close[D]
    has already printed. Joining them same-day measured news against a move that
    was already history — the predictor postdated the outcome.

    Mapping D to the next session is both honest and the only tradeable reading:
    it leaves ~4.5h before the 09:30 CST open.
    """
    sessions = sorted(china_moves_by_date)
    buckets: dict[tuple[str, str], list[float]] = {}
    for date, categories in news_by_date.items():
        nxt = _next_session(sessions, date)
        if nxt is None:
            continue          # no session has traded since this news yet
        moves = china_moves_by_date.get(nxt, {})
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
