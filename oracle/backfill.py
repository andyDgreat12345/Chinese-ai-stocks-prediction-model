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


def _trim_days(series: list[tuple[str, float]], days: int) -> list[tuple[str, float]]:
    return series[-(days + 1):] if days and len(series) > days + 1 else series


# ── network fetch (isolated, fail-soft) ──────────────────────────────────
@with_retries(attempts=3, base_delay=2.0)
def _download_us(symbol: str, days: int):
    import yfinance as yf

    return yf.download(symbol, period=f"{days}d", interval="1d",
                       auto_adjust=False, progress=False)


def _us_records(df) -> list[dict]:
    """yfinance frame -> [{date, close}] records (network-side, not unit-tested)."""
    try:
        close = df["Close"]
        # single-symbol download: a DataFrame column or a Series
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        return [{"date": idx.strftime("%Y-%m-%d"), "close": float(v)}
                for idx, v in close.items() if v == v]
    except (KeyError, TypeError, AttributeError):
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
            series = _trim_days(extract_dated_series(_us_records(_download_us(sym, days))), days)
            rows = series_to_rows(sym, US_SECTOR_TAGS.get(sym), series, fetched_at)
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
            series = _trim_days(extract_dated_series(_to_records(_download_china_index(code))), days)
            total += upsert_market_close("china_close",
                                         series_to_rows(code, sector, series, fetched_at))
        except Exception as e:  # noqa: BLE001
            print(f"backfill_china: index {code} failed: {e!r}")
    for code, sector in ETF_SECTOR_TAGS.items():
        try:
            series = _trim_days(extract_dated_series(_to_records(_download_china_etf(code))), days)
            total += upsert_market_close("china_close",
                                         series_to_rows(code, sector, series, fetched_at))
        except Exception as e:  # noqa: BLE001
            print(f"backfill_china: ETF {code} failed: {e!r}")
    print(f"backfill_china: wrote {total} rows")
    return total


def backfill(days: int = 180) -> int:
    from .db import init_db
    init_db()
    n = backfill_us(days) + backfill_china(days)
    print(f"backfill: {n} total rows for ~{days} days. Now run: python -m oracle.backtest")
    return n


def main(argv: list[str]) -> int:
    days = int(argv[0]) if argv else 180
    backfill(days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
