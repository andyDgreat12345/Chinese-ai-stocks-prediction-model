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
              trade_date: str, bar_dates: dict[str, str] | None = None) -> list[dict]:
    """Turn {symbol: [.., prev_close, last_close]} into DB rows.

    Pure function — the core of what we test. % change is (last/prev - 1)*100;
    symbols with fewer than 2 valid closes get a null pct_change.

    ``trade_date`` is a FALLBACK only. When ``bar_dates`` gives the source's own
    date for a symbol's last bar, that wins — the fetch clock is not the market's
    clock, and stamping by it filed Friday's S&P close as a Saturday session in
    live data, an exact duplicate row for a day the market never opened.
    """
    bar_dates = bar_dates or {}
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
            "trade_date": bar_dates.get(symbol) or trade_date,
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


def extract_bar_dates(df, symbols: list[str],
                      closes: dict[str, list[float]]) -> dict[str, str]:
    """{symbol: ISO date of that symbol's last non-null close}, from the frame's
    own DatetimeIndex.

    Per symbol rather than one date for the frame: yfinance pads every symbol to
    a shared index, so a symbol that did not print today carries a trailing NaN
    and its real last bar is earlier. Taking the frame's final index date for
    everything would re-date those stale bars to today — the same class of error
    this whole change removes. Returns {} if the frame has no usable index.
    """
    try:
        raw = df.index
        # pandas indexes expose .tolist(); a plain sequence is already iterable.
        raw = raw.tolist() if hasattr(raw, "tolist") else list(raw)
        index = [str(d)[:10] for d in raw]
    except (AttributeError, TypeError):
        return {}
    if not index:
        return {}
    out: dict[str, str] = {}
    for sym in symbols:
        series = closes.get(sym) or []
        # walk back to the last non-null close and take that row's date
        for i in range(min(len(series), len(index)) - 1, -1, -1):
            if series[i] is not None:
                out[sym] = index[i]
                break
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
        rows = normalize(closes, fetched_at, trade_date,
                         extract_bar_dates(df, symbols, closes))
        n = upsert_market_close("us_close", rows)
        print(f"fetch_us_close: wrote {n} rows for {trade_date}")
        return n
    except Exception as e:  # noqa: BLE001 — job must not crash scheduler
        print(f"fetch_us_close FAILED: {e!r}")
        return 0
