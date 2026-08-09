"""Historical data backfill — load months of US + China history into the DB so
the backtest engine can produce a real verdict (not just a fixture demo).

One-shot loader:

    python -m oracle.backfill [days]      # default 180 calendar days

Pulls daily closes for the configured US symbols (yfinance) and China broad
indices + sector ETFs (akshare), computes each day's % change from the prior
close, and upserts into `us_close` / `china_close` (idempotent — safe to re-run).

As everywhere else, the network fetch is isolated from the pure transform
(`series_to_rows`, `extract_dated_series`), so the transform is unit-tested
offline and each symbol fails soft without aborting the run.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from . import config
from .db import upsert_market_close
from .ingestion._retry import with_retries
from .ingestion.china_market import (
    ETF_SECTOR_TAGS,
    INDEX_SECTOR_TAGS,
    download_etf_history,
)
from .ingestion.us_market import SECTOR_TAGS as US_SECTOR_TAGS

_DATE_KEYS = ("date", "日期", "Date", "trade_date")
_CLOSE_KEYS = ("close", "收盘", "Close", "收盘价")


# ── pure transforms (unit-tested) ────────────────────────────────────────
def extract_dated_series(records: list[dict],
                         date_keys=_DATE_KEYS, close_keys=_CLOSE_KEYS) -> list[tuple[str, float]]:
    """Pull an ascending [(date, close)] series out of row dicts, tolerant of
    English/Chinese column names. Rows missing a usable date or close are
    skipped."""
    if not records:
        return []
    dkey = next((k for k in date_keys if k in records[0]), None)
    ckey = next((k for k in close_keys if k in records[0]), None)
    if dkey is None or ckey is None:
        return []
    out = []
    for r in records:
        d, c = r.get(dkey), r.get(ckey)
        if d is None or c is None:
            continue
        try:
            out.append((str(d)[:10], float(c)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda p: p[0])
    return out


_OPEN_KEYS = ("open", "开盘", "Open", "开盘价")
_HIGH_KEYS = ("high", "最高", "High", "最高价")
_LOW_KEYS = ("low", "最低", "Low", "最低价")


def _first_key(row: dict, keys) -> str | None:
    return next((k for k in keys if k in row), None)


def _num(v):
    try:
        return None if v is None else round(float(v), 4)
    except (TypeError, ValueError):
        return None


def extract_dated_ohlc(records: list[dict]) -> list[tuple[str, dict]]:
    """Ascending [(date, {close, open, high, low})] from row dicts, tolerant of
    English/Chinese column names. Close is required; O/H/L are optional so a
    source that only publishes closes still loads (the chart then draws a line
    instead of candles). Pure."""
    if not records:
        return []
    r0 = records[0]
    dkey = _first_key(r0, _DATE_KEYS)
    ckey = _first_key(r0, _CLOSE_KEYS)
    if dkey is None or ckey is None:
        return []
    okey, hkey, lkey = (_first_key(r0, _OPEN_KEYS), _first_key(r0, _HIGH_KEYS),
                        _first_key(r0, _LOW_KEYS))
    out = []
    for r in records:
        d, c = r.get(dkey), _num(r.get(ckey))
        if d is None or c is None:
            continue
        out.append((str(d)[:10], {
            "close": c,
            "open": _num(r.get(okey)) if okey else None,
            "high": _num(r.get(hkey)) if hkey else None,
            "low": _num(r.get(lkey)) if lkey else None,
        }))
    out.sort(key=lambda p: p[0])
    return out


def ohlc_to_rows(symbol: str, sector: str | None,
                 series: list[tuple[str, dict]], fetched_at: str) -> list[dict]:
    """OHLC series -> DB rows with the daily % change vs the previous close. Pure."""
    rows, prev = [], None
    for d, bar in series:
        c = bar["close"]
        rows.append({
            "trade_date": d, "symbol": symbol, "sector": sector,
            "close": c, "open": bar.get("open"), "high": bar.get("high"),
            "low": bar.get("low"),
            "pct_change": round((c / prev - 1.0) * 100.0, 4) if prev else None,
            "fetched_at": fetched_at,
        })
        prev = c
    return rows


def series_to_rows(symbol: str, sector: str | None,
                   series: list[tuple[str, float]], fetched_at: str) -> list[dict]:
    """Turn an ascending [(date, close)] series into close rows with a daily
    % change computed from the previous close. Pure — the core we test."""
    rows = []
    prev = None
    for d, c in series:
        pct = round((c / prev - 1.0) * 100.0, 4) if prev else None
        rows.append({
            "trade_date": d, "symbol": symbol, "sector": sector,
            "close": round(c, 4), "pct_change": pct, "fetched_at": fetched_at,
        })
        prev = c
    return rows


# Sentinel for "keep every bar the source returned".
ALL_HISTORY = 0


def _trim_days(series: list[tuple[str, float]], days: int) -> list[tuple[str, float]]:
    """Keep the last `days` bars (+1 so the first has a prior close for its %
    change). ``days <= 0`` keeps everything.

    The default used to discard most of what had already been downloaded: the
    ETF endpoints return full history — 1,559 to 3,557 bars per fund — and a
    365-day trim stored 371. That is 4-9x the training data thrown away after
    paying the network cost for it.
    """
    if not days or days <= 0:
        return series
    return series[-(days + 1):] if len(series) > days + 1 else series


# ── network fetch (isolated, fail-soft) ──────────────────────────────────
@with_retries(attempts=3, base_delay=2.0)
def _download_us(symbol: str, days: int):
    import yfinance as yf

    # yfinance caps a "<n>d" period well short of a full listing history, so
    # ask for "max" whenever we want everything. Without this the US side would
    # silently stay short while the China side went long, and every pairing is
    # limited by whichever leg is shorter.
    period = "max" if not days or days <= 0 else f"{days}d"
    return yf.download(symbol, period=period, interval="1d",
                       auto_adjust=False, progress=False)


def _us_records(df) -> list[dict]:
    """yfinance frame -> [{date, close, open, high, low}] records (network-side,
    not unit-tested). O/H/L are best-effort: if a column is missing the bar still
    loads with just its close and the chart falls back to a line."""
    def col(name):
        try:
            c = df[name]
        except (KeyError, TypeError):
            return None
        return c.iloc[:, 0] if hasattr(c, "columns") else c

    close = col("Close")
    if close is None:
        return []
    opens, highs, lows = col("Open"), col("High"), col("Low")

    def at(series, idx):
        if series is None:
            return None
        try:
            v = float(series.get(idx))
            return v if v == v else None          # drop NaN
        except (TypeError, ValueError, AttributeError):
            return None

    try:
        return [{"date": idx.strftime("%Y-%m-%d"), "close": float(v),
                 "open": at(opens, idx), "high": at(highs, idx), "low": at(lows, idx)}
                for idx, v in close.items() if v == v]
    except (TypeError, AttributeError):
        return []


@with_retries(attempts=3, base_delay=2.0)
def _download_china_index(code: str):
    import akshare as ak
    return ak.stock_zh_index_daily(symbol=code)


def _download_china_etf(code: str):
    """Full ETF history via the shared Eastmoney→Sina fallback chain (retries
    live inside each source), so the backfill loads sector ETFs from the same
    IPs that reset Eastmoney."""
    return download_etf_history(code)


def _to_records(df) -> list[dict]:
    try:
        return df.to_dict("records")
    except AttributeError:
        return list(df) if df else []


# ── backfill jobs ────────────────────────────────────────────────────────
def backfill_us(days: int = 180) -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    symbols = config.US_INDICES + config.US_SECTOR_ETFS + config.PRECIOUS_METALS
    total = 0
    for sym in symbols:
        try:
            series = _trim_days(extract_dated_ohlc(_us_records(_download_us(sym, days))), days)
            rows = ohlc_to_rows(sym, US_SECTOR_TAGS.get(sym), series, fetched_at)
            total += upsert_market_close("us_close", rows)
        except Exception as e:  # noqa: BLE001
            print(f"backfill_us: {sym} failed: {e!r}")
    print(f"backfill_us: wrote {total} rows across {len(symbols)} symbols")
    return total


def backfill_china(days: int = 180) -> int:
    fetched_at = datetime.now(timezone.utc).isoformat()
    total = 0
    for code, sector in INDEX_SECTOR_TAGS.items():
        try:
            series = _trim_days(extract_dated_ohlc(_to_records(_download_china_index(code))), days)
            total += upsert_market_close("china_close",
                                         ohlc_to_rows(code, sector, series, fetched_at))
        except Exception as e:  # noqa: BLE001
            print(f"backfill_china: index {code} failed: {e!r}")
    for code, sector in ETF_SECTOR_TAGS.items():
        try:
            series = _trim_days(extract_dated_ohlc(_to_records(_download_china_etf(code))), days)
            total += upsert_market_close("china_close",
                                         ohlc_to_rows(code, sector, series, fetched_at))
        except Exception as e:  # noqa: BLE001
            print(f"backfill_china: ETF {code} failed: {e!r}")
    print(f"backfill_china: wrote {total} rows")
    return total


def backfill(days: int = 180) -> int:
    from .db import init_db
    init_db()
    n = backfill_us(days) + backfill_china(days)
    span = "all available history" if not days or days <= 0 else f"~{days} days"
    print(f"backfill: {n} total rows for {span}. Now run: python -m oracle.backtest")
    return n


def main(argv: list[str]) -> int:
    """``python -m oracle.backfill [days|all|0]`` — 'all' pulls full history."""
    if argv and argv[0].lower() in ("all", "max", "full"):
        days = ALL_HISTORY
    else:
        days = int(argv[0]) if argv else 180
    backfill(days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


# ── repair ────────────────────────────────────────────────────────────────
def purge_phantom_sessions(db_path=None) -> dict:
    """Delete rows stamped on days the market cannot have traded.

    The fetch-clock stamping bug (fixed in the ingestion jobs) filed Friday's bar
    as a Saturday session and re-filed an unchanged bar under the next calendar
    day. Fixing the writer stops new ones; this removes those already stored,
    because every phantom row is a fake "actual" the model gets scored against.

    Only weekend rows are deleted. A consecutive-duplicate bar is NOT removed:
    a genuinely flat session looks identical, and deleting real flat days to
    catch a few phantoms would be a worse trade. Weekends are unambiguous.

    Idempotent. Returns {table: rows_deleted}.
    """
    from datetime import date

    from . import db as _db

    out = {}
    conn = _db.connect(db_path)
    try:
        for table in ("us_close", "china_close"):
            dates = [r[0] for r in conn.execute(
                f"SELECT DISTINCT trade_date FROM {table}").fetchall()]
            weekend = [d for d in dates
                       if date.fromisoformat(d).weekday() >= 5]
            if not weekend:
                out[table] = 0
                continue
            marks = ",".join("?" * len(weekend))
            cur = conn.execute(
                f"DELETE FROM {table} WHERE trade_date IN ({marks})", weekend)
            out[table] = cur.rowcount
        # Predictions made FOR a non-session are equally meaningless.
        pdates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM predictions").fetchall()]
        pweekend = [d for d in pdates if date.fromisoformat(d).weekday() >= 5]
        if pweekend:
            marks = ",".join("?" * len(pweekend))
            cur = conn.execute(
                f"DELETE FROM predictions WHERE trade_date IN ({marks})", pweekend)
            out["predictions"] = cur.rowcount
        else:
            out["predictions"] = 0
        conn.commit()
    finally:
        conn.close()
    print(f"purge_phantom_sessions: {out}")
    return out


def prune_history(before: str | None = None, db_path=None) -> dict:
    """Delete stored market rows older than ``before`` (default: the configured
    ten-year window).

    Capping what the backfill *fetches* does not remove what is already stored —
    upserts only insert and update. Without this the DB keeps the 1990-onward
    rows a previous full-history run wrote, and every report still spans them
    even though the learner is bounded to the recent window. Two different
    answers to "how far back does this system look" is one too many.

    Predictions and their scores are left alone: they are the system's own
    record of what it said, not market data, and rewriting history there would
    destroy the audit trail.

    Idempotent. Returns {table: rows_deleted}.
    """
    from . import config
    from . import db as _db

    cutoff = before or config.LEARNING_TRAIN_START
    out = {}
    if not cutoff:
        return {"skipped": "no cutoff configured"}
    conn = _db.connect(db_path)
    try:
        for table in ("us_close", "china_close"):
            cur = conn.execute(
                f"DELETE FROM {table} WHERE trade_date < ?", (cutoff,))
            out[table] = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    print(f"prune_history: removed rows before {cutoff}: {out}")
    return out
