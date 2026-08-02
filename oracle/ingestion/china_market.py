"""China A-share close ingestion via akshare (spec §3).

Pulls:
  * broad-market indices (SSE Composite, SZSE Component, ChiNext), and
  * per-sector ETFs (semis/energy/financials/…) so the accuracy scorecard can
    score every predicted sector, not just the broad indices.

Network fetch is isolated from the pure parse/normalize steps for testability,
same pattern as us_market.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import config
from ..db import upsert_market_close
from ._retry import with_retries

# Broad-index code -> logical sector tag.
INDEX_SECTOR_TAGS = {"sh000001": "broad", "sz399001": "broad", "sz399006": "growth"}

# ETF code -> China sector (inverse of config.CHINA_SECTOR_ETFS).
ETF_SECTOR_TAGS = {code: sector for sector, code in config.CHINA_SECTOR_ETFS.items()}

# Candidate close-column names across akshare endpoints (index vs ETF, EN vs CN).
_CLOSE_KEYS = ("close", "收盘", "Close", "收盘价")


def pick_close_series(records: list[dict], close_keys=_CLOSE_KEYS) -> list[float]:
    """Extract the ordered close series from a list of row dicts, trying each
    candidate column name. Pure — the core of what we test. Rows missing a
    usable close are skipped."""
    key = next((k for k in close_keys if records and k in records[0]), None)
    if key is None:
        return []
    out = []
    for r in records:
        v = r.get(key)
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def normalize(latest_by_symbol: dict[str, list[float]], fetched_at: str,
              trade_date: str, sector_tags: dict[str, str]) -> list[dict]:
    """{symbol: [.., prev_close, last_close]} -> DB rows. Pure/testable.
    `sector_tags` maps each symbol to its logical China sector."""
    rows = []
    for symbol, series in latest_by_symbol.items():
        vals = [v for v in series if v is not None]
        if not vals:
            continue
        last = vals[-1]
        pct = None
        if len(vals) >= 2 and vals[-2]:
            pct = round((last / vals[-2] - 1.0) * 100.0, 4)
        rows.append({
            "trade_date": trade_date,
            "symbol": symbol,
            "sector": sector_tags.get(symbol),
            "close": round(last, 4),
            "pct_change": pct,
            "fetched_at": fetched_at,
        })
    return rows


@with_retries(attempts=4, base_delay=2.0)
def _download_index(index_code: str):
    """Recent daily rows for one broad index. akshare imported lazily."""
    import akshare as ak

    return ak.stock_zh_index_daily(symbol=index_code)


@with_retries(attempts=4, base_delay=2.0)
def _download_etf(etf_code: str):
    """Recent daily rows for one sector ETF. akshare imported lazily."""
    import akshare as ak

    return ak.fund_etf_hist_em(symbol=etf_code, period="daily", adjust="")


def _to_records(df) -> list[dict]:
    """pandas DataFrame -> list of row dicts, tolerant of either being absent."""
    try:
        return df.to_dict("records")
    except AttributeError:
        return list(df) if df else []


def _collect(codes: list[str], downloader) -> dict[str, list[float]]:
    """Download each code, extract its last two closes, fail soft per symbol."""
    latest: dict[str, list[float]] = {}
    for code in codes:
        try:
            series = pick_close_series(_to_records(downloader(code)))
            latest[code] = series[-2:]
        except Exception as e:  # noqa: BLE001
            print(f"fetch_china_close: {code} failed: {e!r}")
            latest[code] = []
    return latest


def fetch_china_close(
    index_codes: dict[str, str] | None = None,
    sector_etfs: dict[str, str] | None = None,
) -> int:
    """Job entrypoint: pull broad indices + sector ETFs, normalize, persist.
    Returns total rows written. Never raises."""
    codes = list((index_codes or config.CHINA_INDICES).values())
    etfs = sector_etfs or config.CHINA_SECTOR_ETFS
    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat()
    trade_date = now.date().isoformat()

    try:
        index_latest = _collect(codes, _download_index)
        etf_latest = _collect(list(etfs.values()), _download_etf)

        sector_tags = {**INDEX_SECTOR_TAGS, **{c: s for s, c in etfs.items()}}
        rows = normalize({**index_latest, **etf_latest}, fetched_at,
                         trade_date, sector_tags)
        n = upsert_market_close("china_close", rows)
        print(f"fetch_china_close: wrote {n} rows for {trade_date} "
              f"({len(index_latest)} indices, {len(etf_latest)} sector ETFs)")
        return n
    except Exception as e:  # noqa: BLE001
        print(f"fetch_china_close FAILED: {e!r}")
        return 0
