"""US market close ingestion via yfinance (spec §3).

Pulls closing price + daily % change for US indices, sector ETFs, and precious
metals. The network fetch (`_download`) is isolated from the normalize step
(`normalize`) so the transform is unit-testable on fixture data without yfinance
or a network connection.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import config
from ..db import upsert_market_close
from ._retry import with_retries

# Logical sector tags used to map US moves onto China sectors downstream (§4.1).
SECTOR_TAGS = {
    "^GSPC": "broad", "^IXIC": "tech", "^DJI": "broad",
    "XLE": "energy", "XLF": "financials", "SOXX": "semis",
    "GC=F": "gold", "SI=F": "silver",
    # Spillover sources for the added China sectors.
    "XLV": "healthcare", "XLP": "staples", "XLI": "industrials",
}

SYMBOLS = config.US_INDICES + config.US_SECTOR_ETFS + config.PRECIOUS_METALS


@with_retries(attempts=4, base_delay=2.0)
def _download(symbols: list[str]):
    """Return a yfinance DataFrame of recent daily closes. Isolated for retry
    + mockability. Imports yfinance lazily so the package stays optional."""
    import yfinance as yf

    # 5 days covers weekends/holidays so we always have a prior close to diff.
    return yf.download(
        symbols, period="5d", interval="1d",
        group_by="ticker", progress=False, auto_adjust=False,
    )


def normalize(closes_by_symbol: dict[str, list[float]], fetched_at: str,
              trade_date: str) -> list[dict]:
    """Turn {symbol: [.., prev_close, last_close]} into DB rows.

    Pure function — the core of what we test. % change is (last/prev - 1)*100;
    symbols with fewer than 2 valid closes get a null pct_change.
    """
    rows = []
    for symbol, series in closes_by_symbol.items():
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


def _extract_closes(df, symbols: list[str]) -> dict[str, list[float]]:
    """Pull per-symbol close series out of a yfinance multi-index frame."""
    out: dict[str, list[float]] = {}
    for sym in symbols:
        try:
            col = df[sym]["Close"] if len(symbols) > 1 else df["Close"]
            out[sym] = [None if v != v else float(v) for v in col.tolist()]
        except (KeyError, TypeError):
            out[sym] = []
    return out


def fetch_us_close(symbols: list[str] | None = None) -> int:
    """Job entrypoint: download, normalize, persist. Returns rows written.
    Never raises — logs and returns 0 on failure so the scheduler stays up."""
    symbols = symbols or SYMBOLS
    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat()
    trade_date = now.date().isoformat()
    try:
        df = _download(symbols)
        closes = _extract_closes(df, symbols)
        rows = normalize(closes, fetched_at, trade_date)
        n = upsert_market_close("us_close", rows)
        print(f"fetch_us_close: wrote {n} rows for {trade_date}")
        return n
    except Exception as e:  # noqa: BLE001 — job must not crash scheduler
        print(f"fetch_us_close FAILED: {e!r}")
        return 0
