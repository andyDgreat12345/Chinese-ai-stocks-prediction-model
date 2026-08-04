"""Near-real-time quote snapshots — the "latest price at the open" layer.

The analyst names a single stock to watch; this fetches that name's latest quote
(price, day change) so the daily report shows where it actually sits, not just the
sector ETF. Sina's lightweight quote endpoint (``hq.sinajs.cn``) covers A-shares,
HK lines, and US ADRs in one call.

Reality check: the morning job runs pre-open (≈05:00 CST, China opens 09:30), so
the "quote" is the latest available print (prior close / any pre-market), not a
live intraday tick — honest, and still useful for siting the pick. It is
**fail-soft**: the exchange endpoints block datacenter IPs intermittently, so any
error (or a disabled provider) yields no quote and the pipeline runs unchanged.

Parsing + symbol mapping are pure (unit-tested on sample lines); only ``fetch_*``
touches the network. Off unless ``ORACLE_QUOTES_PROVIDER`` is set (default off so
the pipeline is unchanged until opted in).

**Not investment advice.** A price is context for a name to research, nothing more.
"""
from __future__ import annotations

import os
import sys

# Sina requires a finance.sina Referer or it 403s.
_SINA_URL = "https://hq.sinajs.cn/list="
_SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}


def _f(x) -> float | None:
    try:
        v = float(x)
        return v
    except (TypeError, ValueError):
        return None


def sina_symbol(ticker: str) -> str | None:
    """Map a SECTOR_STOCKS ticker to a Sina quote symbol.
    600519.SS→sh600519, 002371.SZ→sz002371, 0700.HK→hk00700, BABA→gb_baba."""
    t = (ticker or "").strip().upper()
    if t.endswith(".SS"):
        return "sh" + t[:-3]
    if t.endswith(".SZ"):
        return "sz" + t[:-3]
    if t.endswith(".HK"):
        return "hk" + t[:-3].zfill(5)
    if t and t.replace(".", "").isalpha():          # US ADR ticker (BABA, PDD, …)
        return "gb_" + t.lower()
    return None


def _finalize(q: dict) -> dict:
    price, prev = q.get("price"), q.get("prev_close")
    q["pct_change"] = round((price / prev - 1) * 100, 2) if price and prev else None
    return q


def parse_sina_line(line: str) -> dict | None:
    """Parse one `var hq_str_KEY="a,b,c,...";` line into a quote dict, per the
    market-specific field layout (A-share / HK / US). Pure."""
    if "hq_str_" not in line or '="' not in line:
        return None
    key = line.split("hq_str_", 1)[1].split("=", 1)[0].strip()
    payload = line.split('="', 1)[1].rsplit('"', 1)[0]
    if not payload:
        return None                                  # empty = unknown/suspended symbol
    f = payload.split(",")
    try:
        if key.startswith(("sh", "sz")):             # A-share: name,open,prevclose,now,high,low,...
            q = {"symbol": key, "name": f[0], "open": _f(f[1]),
                 "prev_close": _f(f[2]), "price": _f(f[3]),
                 "high": _f(f[4]), "low": _f(f[5])}
        elif key.startswith("hk"):                    # HK: eng,cn,open,prevclose,high,low,now,...
            q = {"symbol": key, "name": (f[1] or f[0]), "open": _f(f[2]),
                 "prev_close": _f(f[3]), "high": _f(f[4]), "low": _f(f[5]),
                 "price": _f(f[6])}
        elif key.startswith("gb_"):                   # US: name,now,pct,time,chg,open,high,low,...
            q = {"symbol": key, "name": f[0], "price": _f(f[1]),
                 "open": _f(f[5]) if len(f) > 5 else None,
                 "prev_close": _f(f[26]) if len(f) > 26 else None}
        else:
            return None
    except IndexError:
        return None
    return _finalize(q)


def fetch_quotes(tickers: list[str], provider: str | None = None) -> dict[str, dict]:
    """Latest quote per ticker, keyed by the ORIGINAL ticker. Fail-soft: returns
    only what resolves; {} on any error or when disabled."""
    provider = (provider or os.environ.get("ORACLE_QUOTES_PROVIDER") or "").strip().lower()
    if provider in ("", "off", "none"):
        return {}
    if provider == "sina":
        return _fetch_sina(tickers)
    return {}


def _fetch_sina(tickers: list[str]) -> dict[str, dict]:
    import urllib.request

    sym_map = {t: sina_symbol(t) for t in tickers}
    syms = sorted({s for s in sym_map.values() if s})
    if not syms:
        return {}
    try:
        req = urllib.request.Request(_SINA_URL + ",".join(syms), headers=_SINA_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 — fixed host
            text = r.read().decode("gb18030", "ignore")
    except Exception as e:  # noqa: BLE001 — network is best-effort
        # stderr, so a blocked endpoint never leaks into report.md / the email.
        print(f"quotes: sina fetch failed ({e!r})", file=sys.stderr)
        return {}
    by_sym = {}
    for line in text.splitlines():
        q = parse_sina_line(line)
        if q and q.get("price") is not None:
            by_sym[q["symbol"]] = q
    return {t: by_sym[s] for t, s in sym_map.items() if s in by_sym}
