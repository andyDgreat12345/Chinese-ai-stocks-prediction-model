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


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {config.DB_PATH}")
