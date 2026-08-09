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
    `sector_tags` maps each symbol to its logical China sector.

    ``trade_date`` is only a FALLBACK. When the payload carries the source's own
    ``bar_date`` that wins, because the fetch clock is not the market's clock: the
    job runs at 15:15 CST and stamped whatever the UTC date happened to be, so a
    Saturday run re-filed Friday's bar as a Saturday session, and a run before the
    source refreshed duplicated the previous day. Both appeared in live data.
    """
    rows = []
    for symbol, series in latest_by_symbol.items():
        # A value may be a bare close list (legacy) or {"closes": [...],
        # "ohlc": {...}, "bar_date": ...} when the source dated its own bar.
        bar, row_date = {}, trade_date
        if isinstance(series, dict):
            bar = series.get("ohlc") or {}
            row_date = series.get("bar_date") or trade_date
            series = series.get("closes") or []
        vals = [v for v in series if v is not None]
        if not vals:
            continue
        last = vals[-1]
        pct = None
        if len(vals) >= 2 and vals[-2]:
            pct = round((last / vals[-2] - 1.0) * 100.0, 4)
        rows.append({
            "trade_date": row_date,
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

    # "qfq" = forward-adjusted. Unadjusted prices make a fund's share conversion
    # (份额折算) look like a -75% market move: 159928 shows -74.47% on 2021-06-25,
    # 512170 -68.10% on 2021-02-25. Those are accounting events, not price action,
    # and the backtest scored them as real outcomes while the simulator took the
    # loss. 28 stored bars exceeded the +/-10% ETF daily limit this way.
    return ak.fund_etf_hist_em(symbol=code, period="daily", adjust="qfq")


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
                            "ohlc": bars[-1][1] if bars else {},
                            # The source's OWN date for that bar. Keeping it is
                            # the whole fix: stamping rows with the fetch clock
                            # invented Saturday sessions and duplicated Friday's
                            # bar under the next calendar day.
                            "bar_date": bars[-1][0] if bars else None}
        except Exception as e:  # noqa: BLE001
            print(f"fetch_china_close: {code} failed: {e!r}")
            latest[code] = {"closes": [], "ohlc": {}, "bar_date": None}
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


# ── code verification ─────────────────────────────────────────────────────
def verify_sector_etfs(etfs: dict[str, str] | None = None) -> dict:
    """Check that every configured ETF code actually resolves to price history.

    This exists because the ingestion job fails soft *per symbol*: a wrong or
    renamed code does not raise, it just leaves that sector permanently unscored
    with a line in a log nobody reads. The same silent-inertness cost us the
    entire news layer, where every configured feed was dead for months while the
    job reported success. A code is only trusted once it returns rows.

    Returns {sector: {"code", "ok", "bars", "error"}}. Never raises.
    """
    etfs = etfs or config.CHINA_SECTOR_ETFS
    out: dict[str, dict] = {}
    for sector, code in etfs.items():
        entry = {"code": code, "ok": False, "bars": 0, "error": None}
        try:
            df = download_etf_history(code)
            rows = _to_records(df)
            closes = pick_close_series(rows)
            entry["bars"] = len(closes)
            entry["ok"] = len(closes) > 0
            if not entry["ok"]:
                entry["error"] = "resolved but returned no usable closes"
        except Exception as e:  # noqa: BLE001
            entry["error"] = f"{type(e).__name__}: {e}"
        out[sector] = entry
    return out


def format_verification(result: dict) -> str:
    ok = [s for s, r in result.items() if r["ok"]]
    bad = [s for s, r in result.items() if not r["ok"]]
    lines = ["China sector-ETF code verification", ""]
    for sector, r in sorted(result.items()):
        mark = "ok  " if r["ok"] else "DEAD"
        detail = f"{r['bars']} bars" if r["ok"] else (r["error"] or "no data")
        lines.append(f"  [{mark}] {sector:12} {r['code']:8} {detail}")
    lines += ["", f"  {len(ok)}/{len(result)} codes resolved."]
    if bad:
        lines.append(f"  UNRESOLVED: {', '.join(sorted(bad))} — these sectors will be "
                     "silently unscored until the codes are corrected.")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if "--verify" in argv:
        result = verify_sector_etfs()
        print(format_verification(result))
        return 0 if all(r["ok"] for r in result.values()) else 1
    print(f"fetch_china_close: wrote {fetch_china_close()} rows")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))


# ── data quality ──────────────────────────────────────────────────────────
# A-share price limits are per-board, not universal. Main board is ±10%; ChiNext
# (创业板) and STAR are ±20%. Using one 10% rule flagged the genuine September-
# October 2024 stimulus rally as corrupt — 159915 printed +20.00% and sz399006
# +17.25%, both real limit-moves — while the actual corporate actions sit far
# beyond either bound. A margin is added so a limit-up bar is not borderline.
DEFAULT_DAILY_LIMIT_PCT = 11.0
WIDE_LIMIT_SYMBOLS = {"159915", "sz399006"}   # ChiNext ETF + ChiNext index
WIDE_DAILY_LIMIT_PCT = 21.0


def daily_limit_for(symbol: str) -> float:
    """The daily price limit that applies to this instrument. Pure."""
    return (WIDE_DAILY_LIMIT_PCT if symbol in WIDE_LIMIT_SYMBOLS
            else DEFAULT_DAILY_LIMIT_PCT)


def implausible_moves(db_path=None, limit_pct: float | None = None,
                      table: str = "china_close") -> list[dict]:
    """Stored bars whose daily move exceeds what the market mechanically allows.

    These are silent poison: 0.3% of rows, but a single -75% "move" dominates any
    return statistic it lands in, and both the backtest and the simulator treat
    it as a real outcome. Detection is deliberately independent of the fetch path,
    because the Sina fallback cannot serve adjusted prices at all — fixing the
    Eastmoney request is not sufficient on its own.
    """
    from ..db import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            f"""SELECT trade_date, symbol, sector, close, pct_change FROM {table}
                WHERE pct_change IS NOT NULL
                ORDER BY ABS(pct_change) DESC""").fetchall()
        out = []
        for r in rows:
            cap = limit_pct if limit_pct is not None else daily_limit_for(r["symbol"])
            if abs(r["pct_change"]) > cap:
                out.append({**dict(r), "limit_pct": cap})
        return out
    finally:
        conn.close()


def format_quality_report(rows: list[dict], limit_pct: float | None = None) -> str:
    L = ["China bar quality — moves beyond each instrument's daily price limit",
         "  (main board ±10%, ChiNext/STAR ±20%)", ""]
    if not rows:
        L.append("  none. Every stored bar is within what the market allows.")
    else:
        L.append(f"  {len(rows)} implausible bar(s) — these are corporate actions or bad")
        L.append("  prints, and both the backtest and the simulator score them as real:")
        L.append("")
        for r in rows[:20]:
            L.append(f"    {r['trade_date']}  {r['symbol']:8} {str(r['sector']):11} "
                     f"{r['pct_change']:+8.2f}%  (limit ±{r.get('limit_pct', 0):.0f}%)")
        if len(rows) > 20:
            L.append(f"    ... and {len(rows) - 20} more")
    return "\n".join(L)


def neutralize_corporate_actions(db_path=None) -> int:
    """NULL the pct_change on bars that exceed their instrument's price limit.

    Fetching with adjust="qfq" only helps when Eastmoney answers. When it is
    unreachable the Sina fallback serves unadjusted prices and cannot do
    otherwise, so five share-conversion bars survived a re-backfill unchanged.
    A repair that depends on the source being up is not a repair.

    A move beyond the daily limit is mechanically impossible, so the bar's
    pct_change is not a return — it is an artifact. We do not guess the true
    return: we mark it unknown, which stops the backtest scoring a -74% "loss"
    and the simulator taking it. The close is left alone so charts still render.

    Idempotent. Returns rows changed.
    """
    from ..db import connect

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT trade_date, symbol, pct_change FROM china_close "
            "WHERE pct_change IS NOT NULL").fetchall()
        bad = [(r["trade_date"], r["symbol"]) for r in rows
               if abs(r["pct_change"]) > daily_limit_for(r["symbol"])]
        for d, sym in bad:
            conn.execute("UPDATE china_close SET pct_change = NULL "
                         "WHERE trade_date = ? AND symbol = ?", (d, sym))
        conn.commit()
        print(f"neutralize_corporate_actions: marked {len(bad)} artifact bar(s) unknown")
        return len(bad)
    finally:
        conn.close()
