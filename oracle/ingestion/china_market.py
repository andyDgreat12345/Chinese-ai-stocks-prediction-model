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
        # A value may be a bare close list (legacy) or {"closes": [...],
        # "ohlc": {...}} when the source also gave us the day's bar.
        bar = {}
        if isinstance(series, dict):
            bar = series.get("ohlc") or {}
            series = series.get("closes") or []
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
            "open": bar.get("open"), "high": bar.get("high"), "low": bar.get("low"),
            "pct_change": pct,
            "fetched_at": fetched_at,
        })
    return rows


@with_retries(attempts=4, base_delay=2.0)
def _download_index(index_code: str):
    """Recent daily rows for one broad index. akshare imported lazily."""
    import akshare as ak

    return ak.stock_zh_index_daily(symbol=index_code)


def sina_etf_symbol(code: str) -> str:
    """Prefix a bare ETF code with its Sina exchange segment. Shanghai funds
    start with '5' (510300, 512480…), Shenzhen with '1' (159915…). Pure."""
    code = str(code)
    if code.startswith("1"):
        return f"sz{code}"
    return f"sh{code}"  # '5' and anything else -> Shanghai


@with_retries(attempts=2, base_delay=2.0)
def _etf_hist_em(code: str):
    """Sector-ETF daily history from Eastmoney (richest, but its endpoint resets
    connections from some datacenter IPs — see the Sina fallback)."""
    import akshare as ak

    return ak.fund_etf_hist_em(symbol=code, period="daily", adjust="")


@with_retries(attempts=2, base_delay=2.0)
def _etf_hist_sina(code: str):
    """Sector-ETF daily history from Sina. Reachable from IPs that Eastmoney's
    endpoint resets (the broad indices already load via Sina), so this is what
    lets the sector ETFs come through without a China-routed host."""
    import akshare as ak

    return ak.fund_etf_hist_sina(symbol=sina_etf_symbol(code))


# Ordered ETF data sources: try Eastmoney first (richer), fall back to Sina.
ETF_SOURCES = (("eastmoney", _etf_hist_em), ("sina", _etf_hist_sina))


def download_etf_history(code: str):
    """Full daily history for one sector ETF, trying each source in ETF_SOURCES
    until one returns usable rows. Shared by the daily job and the backfill so
    both get the same fallback behavior. Raises only if every source fails."""
    last_exc: Exception | None = None
    for name, fn in ETF_SOURCES:
        try:
            df = fn(code)
            if _to_records(df):
                return df
        except Exception as e:  # noqa: BLE001 — try the next source
            last_exc = e
            print(f"fetch_china_close: ETF {code} via {name} failed: {e!r}")
    if last_exc is not None:
        raise last_exc
    return None


# Back-compat alias: the collector calls this name.
_download_etf = download_etf_history


def _to_records(df) -> list[dict]:
    """pandas DataFrame -> list of row dicts, tolerant of either being absent."""
    try:
        return df.to_dict("records")
    except AttributeError:
        return list(df) if df else []


def _collect(codes: list[str], downloader) -> dict[str, dict]:
    """Download each code and keep its last two closes plus the latest day's OHLC
    bar (for candlestick rendering). Fail soft per symbol."""
    from ..backfill import extract_dated_ohlc

    latest: dict[str, dict] = {}
    for code in codes:
        try:
            records = _to_records(downloader(code))
            closes = pick_close_series(records)
            bars = extract_dated_ohlc(records)
            latest[code] = {"closes": closes[-2:],
                            "ohlc": bars[-1][1] if bars else {}}
        except Exception as e:  # noqa: BLE001
            print(f"fetch_china_close: {code} failed: {e!r}")
            latest[code] = {"closes": [], "ohlc": {}}
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
