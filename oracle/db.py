"""SQLite access layer. One place that owns the connection + schema init."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config


def connect(db_path: Path | str = config.DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str = config.DB_PATH) -> None:
    """Create all tables from schema.sql (idempotent)."""
    schema = (Path(__file__).resolve().parent / "schema.sql").read_text()
    conn = connect(db_path)
    try:
        conn.executescript(schema)
        conn.commit()
    finally:
        conn.close()


def upsert_market_close(table: str, rows: list[dict], db_path=config.DB_PATH) -> int:
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


def insert_news(rows: list[dict], db_path=config.DB_PATH) -> int:
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


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {config.DB_PATH}")
