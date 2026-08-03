"""SQLite access layer. One place that owns the connection + schema init."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    # Resolve at call time (not import time) so config.DB_PATH can be patched,
    # e.g. by tests pointing at a temp DB.
    conn = sqlite3.connect(db_path if db_path is not None else config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    """Create all tables from schema.sql (idempotent)."""
    schema = (Path(__file__).resolve().parent / "schema.sql").read_text()
    conn = connect(db_path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


def upsert_market_close(table: str, rows: list[dict], db_path=None) -> int:
    """Insert-or-replace close rows into `us_close` or `china_close`.

    Each row: {trade_date, symbol, sector, close, pct_change, fetched_at}.
    Returns the number of rows written.
    """
    if table not in ("us_close", "china_close"):
        raise ValueError(f"unexpected table: {table}")
    if not rows:
        return 0
    conn = connect(db_path)
    try:
        conn.executemany(
            f"""INSERT INTO {table}
                    (trade_date, symbol, sector, close, pct_change, fetched_at)
                VALUES
                    (:trade_date, :symbol, :sector, :close, :pct_change, :fetched_at)
                ON CONFLICT(trade_date, symbol) DO UPDATE SET
                    sector=excluded.sector, close=excluded.close,
                    pct_change=excluded.pct_change, fetched_at=excluded.fetched_at""",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def insert_news(rows: list[dict], db_path=None) -> int:
    """Insert news rows: {trade_date, source, category, headline, summary,
    sentiment, fetched_at}. Skips exact-duplicate headlines for the same date."""
    if not rows:
        return 0
    conn = connect(db_path)
    written = 0
    try:
        for r in rows:
            dup = conn.execute(
                "SELECT 1 FROM news WHERE trade_date=? AND headline=? LIMIT 1",
                (r["trade_date"], r["headline"]),
            ).fetchone()
            if dup:
                continue
            conn.execute(
                """INSERT INTO news
                       (trade_date, source, category, headline, summary,
                        sentiment, fetched_at)
                   VALUES (:trade_date, :source, :category, :headline,
                           :summary, :sentiment, :fetched_at)""",
                r,
            )
            written += 1
        conn.commit()
        return written
    finally:
        conn.close()


def get_weights(db_path=None) -> dict[str, float]:
    """Return {signal: current_weight} for the scoring function."""
    conn = connect(db_path)
    try:
        return {r["signal"]: r["current_weight"]
                for r in conn.execute("SELECT signal, current_weight FROM weights")}
    finally:
        conn.close()


def get_rows_for_date(table: str, trade_date: str, db_path=None) -> list[dict]:
    """Fetch all rows of `us_close` / `china_close` / `news` for one date."""
    if table not in ("us_close", "china_close", "news"):
        raise ValueError(f"unexpected table: {table}")
    conn = connect(db_path)
    try:
        cur = conn.execute(f"SELECT * FROM {table} WHERE trade_date = ?", (trade_date,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def insert_macro_events(rows: list[dict], db_path=None) -> int:
    """Insert macro calendar rows, skipping exact (date, description) dupes.
    Row: {event_date, category, description, weight, notes}. Returns count."""
    if not rows:
        return 0
    conn = connect(db_path)
    written = 0
    try:
        for r in rows:
            dup = conn.execute(
                "SELECT 1 FROM macro_events WHERE event_date=? AND description=? LIMIT 1",
                (r["event_date"], r["description"]),
            ).fetchone()
            if dup:
                continue
            conn.execute(
                """INSERT INTO macro_events (event_date, category, description, weight, notes)
                   VALUES (:event_date, :category, :description, :weight, :notes)""",
                r,
            )
            written += 1
        conn.commit()
        return written
    finally:
        conn.close()


def macro_event_dates(trade_date: str, db_path=None) -> list[dict]:
    """Scheduled macro events on a given date (spec §4.3)."""
    conn = connect(db_path)
    try:
        cur = conn.execute("SELECT * FROM macro_events WHERE event_date = ?", (trade_date,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def upsert_prediction(row: dict, db_path=None) -> None:
    """Insert-or-replace one prediction, storing its component signals (§4b-i)."""
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO predictions
                   (trade_date, sector, direction, confidence, composite_score,
                    us_spillover, sentiment_score, macro_flag, rationale, created_at)
               VALUES
                   (:trade_date, :sector, :direction, :confidence, :composite_score,
                    :us_spillover, :sentiment_score, :macro_flag, :rationale, :created_at)
               ON CONFLICT(trade_date, sector) DO UPDATE SET
                    direction=excluded.direction, confidence=excluded.confidence,
                    composite_score=excluded.composite_score,
                    us_spillover=excluded.us_spillover,
                    sentiment_score=excluded.sentiment_score,
                    macro_flag=excluded.macro_flag, rationale=excluded.rationale,
                    created_at=excluded.created_at""",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def latest_predictions(db_path=None) -> list[dict]:
    """All predictions for the most recent predicted date."""
    conn = connect(db_path)
    try:
        latest = conn.execute("SELECT MAX(trade_date) AS d FROM predictions").fetchone()
        if not latest or latest["d"] is None:
            return []
        cur = conn.execute(
            "SELECT * FROM predictions WHERE trade_date = ? ORDER BY sector", (latest["d"],)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def prediction_history(limit: int = 200, db_path=None) -> list[dict]:
    """Recent predictions joined with their scores, for the history/accuracy view."""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            """SELECT p.*, s.actual_direction, s.actual_pct_change, s.correct
               FROM predictions p
               LEFT JOIN prediction_scores s ON s.prediction_id = p.id
               ORDER BY p.trade_date DESC, p.sector
               LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def latest_market_rows(table: str, db_path=None) -> list[dict]:
    """All rows of `us_close`/`china_close` for the most recent trade_date."""
    if table not in ("us_close", "china_close"):
        raise ValueError(f"unexpected table: {table}")
    conn = connect(db_path)
    try:
        latest = conn.execute(f"SELECT MAX(trade_date) AS d FROM {table}").fetchone()
        if not latest or latest["d"] is None:
            return []
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE trade_date = ? ORDER BY symbol", (latest["d"],)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def predictions_for_date(trade_date: str, db_path=None) -> list[dict]:
    """All predictions for a specific predicted date (with their id + signals)."""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM predictions WHERE trade_date = ? ORDER BY sector", (trade_date,)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def insert_prediction_score(row: dict, db_path=None) -> None:
    """Write/overwrite a prediction's directional + calibration score (§4b-i)."""
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO prediction_scores
                   (prediction_id, actual_direction, actual_pct_change,
                    correct, confidence_p, brier, scored_at)
               VALUES
                   (:prediction_id, :actual_direction, :actual_pct_change,
                    :correct, :confidence_p, :brier, :scored_at)
               ON CONFLICT(prediction_id) DO UPDATE SET
                    actual_direction=excluded.actual_direction,
                    actual_pct_change=excluded.actual_pct_change,
                    correct=excluded.correct, confidence_p=excluded.confidence_p,
                    brier=excluded.brier, scored_at=excluded.scored_at""",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def distinct_symbols(table: str, db_path=None) -> list[str]:
    if table not in ("us_close", "china_close"):
        raise ValueError(f"unexpected table: {table}")
    conn = connect(db_path)
    try:
        cur = conn.execute(f"SELECT DISTINCT symbol FROM {table} ORDER BY symbol")
        return [r["symbol"] for r in cur.fetchall()]
    finally:
        conn.close()


def series_for_symbol(table: str, symbol: str, db_path=None) -> dict[str, float]:
    """{trade_date: pct_change} for one symbol (nulls excluded), for correlation."""
    if table not in ("us_close", "china_close"):
        raise ValueError(f"unexpected table: {table}")
    conn = connect(db_path)
    try:
        cur = conn.execute(
            f"""SELECT trade_date, pct_change FROM {table}
                WHERE symbol = ? AND pct_change IS NOT NULL
                ORDER BY trade_date""",
            (symbol,),
        )
        return {r["trade_date"]: r["pct_change"] for r in cur.fetchall()}
    finally:
        conn.close()


def upsert_correlation(row: dict, db_path=None) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO correlations
                   (us_symbol, china_symbol, window_days, correlation, best_lag,
                    sample_size, established, computed_at)
               VALUES
                   (:us_symbol, :china_symbol, :window_days, :correlation, :best_lag,
                    :sample_size, :established, :computed_at)
               ON CONFLICT(us_symbol, china_symbol, window_days) DO UPDATE SET
                    correlation=excluded.correlation, best_lag=excluded.best_lag,
                    sample_size=excluded.sample_size, established=excluded.established,
                    computed_at=excluded.computed_at""",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def leaderboard(established_only: bool = False, db_path=None) -> list[dict]:
    """Correlations ranked by strength (|correlation|), for the §4b-ii display."""
    conn = connect(db_path)
    try:
        where = "WHERE established = 1" if established_only else ""
        cur = conn.execute(
            f"""SELECT * FROM correlations {where}
                ORDER BY ABS(correlation) DESC"""
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def upsert_news_impact(row: dict, db_path=None) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO news_impact
                   (category, china_sector, avg_move, variance, sample_size, updated_at)
               VALUES
                   (:category, :china_sector, :avg_move, :variance, :sample_size, :updated_at)
               ON CONFLICT(category, china_sector) DO UPDATE SET
                    avg_move=excluded.avg_move, variance=excluded.variance,
                    sample_size=excluded.sample_size, updated_at=excluded.updated_at""",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def news_impact_table(db_path=None) -> list[dict]:
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM news_impact ORDER BY category, ABS(avg_move) DESC"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def insert_reflection(row: dict, db_path=None) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            """INSERT INTO reflection_log
                   (trade_date, predicted_json, actual_json, signals_that_worked,
                    signals_that_missed, likely_reason_for_miss, suggested_adjustment,
                    reflection_confidence, created_at)
               VALUES
                   (:trade_date, :predicted_json, :actual_json, :signals_that_worked,
                    :signals_that_missed, :likely_reason_for_miss, :suggested_adjustment,
                    :reflection_confidence, :created_at)
               ON CONFLICT(trade_date) DO UPDATE SET
                    predicted_json=excluded.predicted_json, actual_json=excluded.actual_json,
                    signals_that_worked=excluded.signals_that_worked,
                    signals_that_missed=excluded.signals_that_missed,
                    likely_reason_for_miss=excluded.likely_reason_for_miss,
                    suggested_adjustment=excluded.suggested_adjustment,
                    reflection_confidence=excluded.reflection_confidence,
                    created_at=excluded.created_at""",
            row,
        )
        conn.commit()
    finally:
        conn.close()


def recent_reflections(limit: int = 5, db_path=None) -> list[dict]:
    """Most recent reflection-log entries — retrieved by run_analysis (§4b-iii)."""
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "SELECT * FROM reflection_log ORDER BY trade_date DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {config.DB_PATH}")
