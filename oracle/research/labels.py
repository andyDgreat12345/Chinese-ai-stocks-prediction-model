"""Human-readable labels for every symbol we chart or report.

A ticker like ``512480`` or ``^RUT`` means nothing at a glance, and a correlation
table full of bare codes is unreadable. Every symbol the system displays resolves
here to four things:

  * ``name``    — what it is in words ("Semiconductor ETF", "Alibaba")
  * ``sector``  — the sector bucket it belongs to ("semis", "energy", "broad")
  * ``company`` — the specific company, when the instrument IS one (ADRs and
                  single stocks); empty for indices and baskets
  * ``kind``    — index / sector-etf / broad-etf / adr / stock / commodity /
                  rates / fx / volatility

Unknown symbols degrade to the ticker itself rather than raising, so a newly
ingested instrument shows up unlabeled instead of breaking a panel.
"""
from __future__ import annotations

from .. import config


def _e(name, sector, kind, company=""):
    return {"name": name, "sector": sector, "kind": kind, "company": company}


# ── China instruments ─────────────────────────────────────────────────────
CHINA_LABELS: dict[str, dict] = {
    "sh000001": _e("SSE Composite Index", "broad", "index"),
    "sz399001": _e("SZSE Component Index", "broad", "index"),
    "sz399006": _e("ChiNext Index", "growth", "index"),
    "510300": _e("CSI 300 ETF", "broad", "sector-etf"),
    "159915": _e("ChiNext ETF", "growth", "sector-etf"),
    "512480": _e("Semiconductor ETF", "semis", "sector-etf"),
    "159930": _e("Energy ETF", "energy", "sector-etf"),
    "512800": _e("Bank / Financials ETF", "financials", "sector-etf"),
}

# ── US instruments (mirrors research/universe.py groups) ──────────────────
US_LABELS: dict[str, dict] = {
    "^GSPC": _e("S&P 500", "broad", "index"),
    "^IXIC": _e("Nasdaq Composite", "tech", "index"),
    "^DJI": _e("Dow Jones Industrial", "broad", "index"),
    "^RUT": _e("Russell 2000 (small-cap)", "broad", "index"),
    "^VIX": _e("VIX volatility index", "volatility", "volatility"),
    "^TNX": _e("US 10-year Treasury yield", "rates", "rates"),
    "DX-Y.NYB": _e("US Dollar Index", "fx", "fx"),
    "CNY=X": _e("USD/CNY exchange rate", "fx", "fx"),
    "SOXX": _e("Semiconductor ETF (iShares)", "semis", "sector-etf"),
    "SMH": _e("Semiconductor ETF (VanEck)", "semis", "sector-etf"),
    "XLK": _e("Technology sector", "tech", "sector-etf"),
    "XLE": _e("Energy sector", "energy", "sector-etf"),
    "XLF": _e("Financials sector", "financials", "sector-etf"),
    "XLI": _e("Industrials sector", "industrials", "sector-etf"),
    "XLB": _e("Materials sector", "materials", "sector-etf"),
    "XLY": _e("Consumer discretionary", "consumer", "sector-etf"),
    "XLP": _e("Consumer staples", "consumer", "sector-etf"),
    "XLU": _e("Utilities sector", "utilities", "sector-etf"),
    "XLV": _e("Healthcare sector", "healthcare", "sector-etf"),
    "HG=F": _e("Copper futures", "commodity", "commodity"),
    "CL=F": _e("Crude oil futures", "commodity", "commodity"),
    "GC=F": _e("Gold futures", "commodity", "commodity"),
    "SI=F": _e("Silver futures", "commodity", "commodity"),
    "FXI": _e("China large-cap ETF", "broad", "broad-etf"),
    "MCHI": _e("MSCI China ETF", "broad", "broad-etf"),
    "KWEB": _e("China internet ETF", "growth", "sector-etf"),
    "ASHR": _e("CSI 300 A-share ETF (US-listed)", "broad", "broad-etf"),
    "CQQQ": _e("China technology ETF", "tech", "sector-etf"),
    "EEM": _e("Emerging markets ETF", "global", "broad-etf"),
    "EFA": _e("Developed ex-US ETF", "global", "broad-etf"),
    "BABA": _e("Alibaba", "growth", "adr", "Alibaba Group"),
    "PDD": _e("PDD Holdings", "growth", "adr", "PDD Holdings"),
    "JD": _e("JD.com", "growth", "adr", "JD.com"),
    "BIDU": _e("Baidu", "growth", "adr", "Baidu"),
    "NIO": _e("NIO", "consumer", "adr", "NIO Inc."),
}


def _sector_stock_labels() -> dict[str, dict]:
    """Single names from the watchlist (config.SECTOR_STOCKS) are company-specific."""
    out: dict[str, dict] = {}
    for sector, names in config.SECTOR_STOCKS.items():
        for s in names:
            out.setdefault(s["ticker"], _e(s["name"], sector, "stock", s["name"]))
    return out


def label(symbol: str) -> dict:
    """Resolve any symbol to {symbol, name, sector, kind, company, display}.
    Unknown symbols fall back to the bare ticker rather than raising."""
    sym = (symbol or "").strip()
    entry = (CHINA_LABELS.get(sym) or US_LABELS.get(sym)
             or _sector_stock_labels().get(sym))
    if entry is None:
        entry = _e(sym, "unknown", "unknown")
    out = {"symbol": sym, **entry}
    out["display"] = short_label(out)
    return out


def short_label(entry: dict) -> str:
    """One compact string for a chart legend: 'Semiconductor ETF · semis'."""
    name, sector = entry.get("name") or entry.get("symbol", ""), entry.get("sector")
    return f"{name} · {sector}" if sector and sector != "unknown" else name


def sector_of(symbol: str) -> str:
    return label(symbol)["sector"]


def company_of(symbol: str) -> str:
    """The specific company, or '' when the instrument is an index/basket."""
    return label(symbol)["company"]
