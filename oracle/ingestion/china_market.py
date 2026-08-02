"""China A-share close ingestion via akshare (spec §3).

Pulls closing level + daily % change for the SSE Composite, SZSE Component, and
ChiNext indices. Network fetch isolated from normalize for testability, same
pattern as us_market.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import config
from ..db import upsert_market_close
from ._retry import with_retries

# China index code -> logical sector tag (broad indices tagged 'broad').
SECTOR_TAGS = {"sh000001": "broad", "sz399001": "broad", "sz399006": "growth"}


@with_retries(attempts=4, base_delay=2.0)
def _download(index_code: str):
    """Return an akshare DataFrame of recent daily rows for one index.
    Imported lazily so akshare stays an optional dependency."""
    import akshare as ak

    return ak.stock_zh_index_daily(symbol=index_code)


def normalize(latest_by_symbol: dict[str, list[float]], fetched_at: str,
              trade_date: str) -> list[dict]:
    """{index_code: [.., prev_close, last_close]} -> DB rows. Pure/testable."""
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
            "sector": SECTOR_TAGS.get(symbol),
            "close": round(last, 4),
            "pct_change": pct,
            "fetched_at": fetched_at,
        })
    return rows


def fetch_china_close(index_codes: dict[str, str] | None = None) -> int:
    """Job entrypoint: download each index, normalize, persist. Never raises."""
    codes = list((index_codes or config.CHINA_INDICES).values())
    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat()
    trade_date = now.date().isoformat()

    latest: dict[str, list[float]] = {}
    for code in codes:
        try:
            df = _download(code)
            latest[code] = [float(v) for v in df["close"].tail(2).tolist()]
        except Exception as e:  # noqa: BLE001
            print(f"fetch_china_close: {code} failed: {e!r}")
            latest[code] = []
    try:
        rows = normalize(latest, fetched_at, trade_date)
        n = upsert_market_close("china_close", rows)
        print(f"fetch_china_close: wrote {n} rows for {trade_date}")
        return n
    except Exception as e:  # noqa: BLE001
        print(f"fetch_china_close FAILED: {e!r}")
        return 0
