"""The research universe — what we sweep, and *why* each symbol is in it.

A wide correlation sweep is only as good as the hypotheses behind its inputs.
Throwing in hundreds of tickers and keeping whatever survives is how you
manufacture false discoveries; every symbol here is included because there is a
stated economic reason it might lead Chinese equities, and that reason is written
down so a surviving pair can be judged against a mechanism rather than admired as
a number.

The groups, and the transmission channel each is meant to test:

  * **US-listed China proxies** (FXI, KWEB, ASHR, ADRs) — the strongest a-priori
    candidates. They trade *in US hours*, closing ~21:00 UTC, so their close is
    already known when the mainland opens the next morning. If any lead exists,
    it should be loudest here.
  * **Broad US risk** (S&P, Nasdaq, Dow, Russell, VIX) — global risk appetite.
  * **US sectors** (XLE, XLF, XLK, …) — sector-to-sector spillover.
  * **Commodities** (copper, oil, gold, silver) — copper especially is a direct
    read on Chinese industrial demand; the causality may run *from* China.
  * **Rates & FX** (10Y yield, dollar index, USDCNY) — the financial-conditions
    channel; a stronger dollar/weaker yuan usually tightens Chinese equities.
  * **Global EM** (EEM, EFA) — is any "China" signal just generic EM beta?

That last one matters: several groups exist as *controls*. If a China-sector link
is no stronger than the EEM link, we have found beta, not insight.
"""
from __future__ import annotations

# symbol -> (group, why it could plausibly lead Chinese equities)
US_RESEARCH_UNIVERSE: dict[str, tuple[str, str]] = {
    # ── US-listed China proxies: closed before the mainland opens ──────────
    "FXI":   ("china_proxy", "China large-cap ETF, US hours — prices China news before the A-share open"),
    "MCHI":  ("china_proxy", "Broad MSCI China ETF, US hours"),
    "KWEB":  ("china_proxy", "China internet ETF — the growth/tech read"),
    "ASHR":  ("china_proxy", "Direct CSI 300 A-share ETF listed in the US"),
    "CQQQ":  ("china_proxy", "China technology ETF"),
    "BABA":  ("china_adr", "Alibaba ADR — bellwether for China internet sentiment"),
    "PDD":   ("china_adr", "PDD ADR — consumer/e-commerce"),
    "JD":    ("china_adr", "JD ADR — consumer/logistics"),
    "BIDU":  ("china_adr", "Baidu ADR — China AI/search"),
    "NIO":   ("china_adr", "NIO ADR — China EV cycle"),
    # ── broad US risk appetite ─────────────────────────────────────────────
    "^GSPC": ("us_broad", "S&P 500 — global risk benchmark"),
    "^IXIC": ("us_broad", "Nasdaq — global tech risk appetite"),
    "^DJI":  ("us_broad", "Dow — old-economy US"),
    "^RUT":  ("us_broad", "Russell 2000 — US small-cap risk appetite"),
    "^VIX":  ("us_vol", "Volatility index — fear gauge; expected NEGATIVE lead"),
    # ── US sectors (spillover into the matching China sector) ──────────────
    "SOXX":  ("us_sector", "Semiconductors — the clearest sector analogue for China semis"),
    "SMH":   ("us_sector", "Semiconductor ETF (second read on the same channel)"),
    "XLK":   ("us_sector", "US technology"),
    "XLE":   ("us_sector", "US energy"),
    "XLF":   ("us_sector", "US financials"),
    "XLI":   ("us_sector", "US industrials — global manufacturing cycle"),
    "XLB":   ("us_sector", "US materials — commodity demand"),
    "XLY":   ("us_sector", "US consumer discretionary"),
    "XLP":   ("us_sector", "US consumer staples — defensive rotation signal"),
    "XLU":   ("us_sector", "US utilities — defensive rotation signal"),
    "XLV":   ("us_sector", "US healthcare"),
    # ── commodities: real-economy demand, often China-driven ──────────────
    "HG=F":  ("commodity", "Copper — the classic direct read on Chinese industrial demand"),
    "CL=F":  ("commodity", "Crude oil — global growth + China import demand"),
    "GC=F":  ("commodity", "Gold — safe haven / real rates"),
    "SI=F":  ("commodity", "Silver — hybrid industrial/precious"),
    # ── financial conditions ──────────────────────────────────────────────
    "^TNX":  ("rates", "US 10-year yield — global discount rate"),
    "DX-Y.NYB": ("fx", "Dollar index — a stronger USD usually tightens EM equities"),
    "CNY=X": ("fx", "USDCNY — a weaker yuan usually pressures Chinese equities"),
    # ── controls: is the signal China-specific, or just EM beta? ───────────
    "EEM":   ("control_em", "Emerging markets ETF — CONTROL: generic EM beta"),
    "EFA":   ("control_dm", "Developed ex-US ETF — CONTROL: generic global beta"),
}

# Groups that exist purely as controls. A China-sector "discovery" that is no
# stronger than these is beta, not insight — the report says so explicitly.
CONTROL_GROUPS = {"control_em", "control_dm"}


def us_symbols() -> list[str]:
    return list(US_RESEARCH_UNIVERSE)


def group_of(symbol: str) -> str:
    return US_RESEARCH_UNIVERSE.get(symbol, ("unknown", ""))[0]


def rationale_of(symbol: str) -> str:
    return US_RESEARCH_UNIVERSE.get(symbol, ("", "no stated hypothesis"))[1]


def is_control(symbol: str) -> bool:
    return group_of(symbol) in CONTROL_GROUPS
