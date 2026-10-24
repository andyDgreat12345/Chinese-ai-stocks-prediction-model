# US → China correlation sweep — research report

_Generated 2026-08-05. Not investment advice._

## Method

- **1,120 correlation tests** run: 35 US symbols × 8 China symbols × lags [0, 1, 2, 3].
- Minimum 40 paired sessions per test.
- Two-sided p-value via the Fisher z-transform.
- **Benjamini–Hochberg false-discovery control at q ≤ 0.1**, applied across the whole sweep.
- **Split-half stability**: history halved, both halves must agree in sign.
- **Tradeability**: lag 0 is reported but never counted as a finding — China closes ~14h before the US on the same date, so a same-day correlation cannot be acted on.

### Why the correction matters

At an uncorrected p < 0.05, **274 of 1,120 tests look significant** — but on pure noise a sweep this wide would produce roughly 56 by chance alone. After FDR control, sign-stability and the tradeability filter, **98 survive**. That gap is the difference between a screen and a finding.

## Findings — pairs that survived every filter

| US | → China | lag | r | n | q-value | 1st half | 2nd half | channel |
|---|---|---:|---:|---:|---:|---:|---:|---|
| SOXX | broad (510300) | 1 | +0.300 | 354 | 0.0000 | +0.32 | +0.29 | us_sector |
| SOXX | growth (159915) | 1 | +0.299 | 354 | 0.0000 | +0.28 | +0.31 | us_sector |
| ^RUT | broad (510300) | 1 | +0.299 | 353 | 0.0000 | +0.33 | +0.28 | us_broad |
| XLK | broad (510300) | 1 | +0.296 | 353 | 0.0000 | +0.29 | +0.30 | us_sector |
| SMH | broad (510300) | 1 | +0.294 | 353 | 0.0000 | +0.30 | +0.29 | us_sector |
| XLE | energy (159930) | 1 | +0.293 | 354 | 0.0000 | +0.31 | +0.29 | us_sector |
| ^VIX | broad (510300) | 1 | -0.292 | 355 | 0.0000 | -0.33 | -0.26 | us_vol |
| SOXX | SZSE Component (sz399001) | 1 | +0.292 | 354 | 0.0000 | +0.26 | +0.31 | us_sector |
| ^RUT | SSE Composite (sh000001) | 1 | +0.291 | 353 | 0.0000 | +0.30 | +0.29 | us_broad |
| XLK | growth (159915) | 1 | +0.287 | 353 | 0.0000 | +0.27 | +0.31 | us_sector |
| SOXX | ChiNext (sz399006) | 1 | +0.287 | 354 | 0.0000 | +0.26 | +0.31 | us_sector |
| XLK | SZSE Component (sz399001) | 1 | +0.286 | 353 | 0.0000 | +0.25 | +0.31 | us_sector |
| ^VIX | SSE Composite (sh000001) | 1 | -0.284 | 355 | 0.0000 | -0.30 | -0.27 | us_vol |
| ^VIX | SZSE Component (sz399001) | 1 | -0.284 | 355 | 0.0000 | -0.30 | -0.28 | us_vol |
| ^GSPC | SZSE Component (sz399001) | 1 | +0.284 | 354 | 0.0000 | +0.29 | +0.30 | us_broad |
| ^RUT | SZSE Component (sz399001) | 1 | +0.283 | 353 | 0.0000 | +0.32 | +0.26 | us_broad |
| SMH | growth (159915) | 1 | +0.282 | 353 | 0.0000 | +0.26 | +0.30 | us_sector |
| ^GSPC | broad (510300) | 1 | +0.280 | 354 | 0.0000 | +0.29 | +0.28 | us_broad |
| ^GSPC | SSE Composite (sh000001) | 1 | +0.280 | 354 | 0.0000 | +0.28 | +0.30 | us_broad |
| ^IXIC | SZSE Component (sz399001) | 1 | +0.279 | 354 | 0.0000 | +0.25 | +0.33 | us_broad |
| ^IXIC | broad (510300) | 1 | +0.278 | 354 | 0.0000 | +0.26 | +0.31 | us_broad |
| XLK | ChiNext (sz399006) | 1 | +0.275 | 353 | 0.0000 | +0.24 | +0.30 | us_sector |
| XLK | SSE Composite (sh000001) | 1 | +0.274 | 353 | 0.0000 | +0.25 | +0.29 | us_sector |
| ^IXIC | growth (159915) | 1 | +0.273 | 354 | 0.0000 | +0.25 | +0.31 | us_broad |
| SMH | SZSE Component (sz399001) | 1 | +0.273 | 353 | 0.0000 | +0.23 | +0.31 | us_sector |

## Hypothesis groups — is it China, or just beta?

| group | tests | survivors | rate | best r |
|---|---:|---:|---:|---:|
| us_vol | 32 | 6 | 18.8% | -0.292 |
| control_em *(control)* | 32 | 6 | 18.8% | +0.233 |
| control_dm *(control)* | 32 | 6 | 18.8% | +0.243 |
| us_broad | 128 | 22 | 17.2% | +0.299 |
| us_sector | 352 | 50 | 14.2% | +0.300 |
| rates | 32 | 1 | 3.1% | +0.150 |
| fx | 64 | 2 | 3.1% | -0.152 |
| commodity | 128 | 3 | 2.3% | +0.201 |
| china_proxy | 160 | 1 | 0.6% | +0.131 |
| china_adr | 160 | 1 | 0.6% | +0.127 |

Read the control rows first: if `control_em` (EEM) survives at a similar rate to the China-specific groups, the 'China signal' is generic emerging-market beta wearing a China label.

## Strongest same-day co-movements (NOT tradeable)

| US | → China | r | n |
|---|---|---:|---:|
| ASHR | broad | +0.800 | 354 |
| ASHR | SZSE Component | +0.758 | 354 |
| ASHR | SSE Composite | +0.745 | 354 |
| ASHR | ChiNext | +0.731 | 354 |
| ASHR | growth | +0.730 | 354 |
| CQQQ | semis | +0.662 | 354 |
| CQQQ | SZSE Component | +0.634 | 354 |
| ASHR | semis | +0.634 | 354 |

These are the biggest numbers in the sweep and the least useful: the US session they describe had not happened yet when China closed. They are listed to be explicit about what the headline correlations actually are.

## Universe and stated hypotheses

| symbol | group | why it might lead China |
|---|---|---|
| ASHR | china_proxy | Direct CSI 300 A-share ETF listed in the US |
| BABA | china_adr | Alibaba ADR — bellwether for China internet sentiment |
| BIDU | china_adr | Baidu ADR — China AI/search |
| CL=F | commodity | Crude oil — global growth + China import demand |
| CNY=X | fx | USDCNY — a weaker yuan usually pressures Chinese equities |
| CQQQ | china_proxy | China technology ETF |
| DX-Y.NYB | fx | Dollar index — a stronger USD usually tightens EM equities |
| EEM | control_em | Emerging markets ETF — CONTROL: generic EM beta |
| EFA | control_dm | Developed ex-US ETF — CONTROL: generic global beta |
| FXI | china_proxy | China large-cap ETF, US hours — prices China news before the A-share open |
| GC=F | commodity | Gold — safe haven / real rates |
| HG=F | commodity | Copper — the classic direct read on Chinese industrial demand |
| JD | china_adr | JD ADR — consumer/logistics |
| KWEB | china_proxy | China internet ETF — the growth/tech read |
| MCHI | china_proxy | Broad MSCI China ETF, US hours |
| NIO | china_adr | NIO ADR — China EV cycle |
| PDD | china_adr | PDD ADR — consumer/e-commerce |
| SI=F | commodity | Silver — hybrid industrial/precious |
| SMH | us_sector | Semiconductor ETF (second read on the same channel) |
| SOXX | us_sector | Semiconductors — the clearest sector analogue for China semis |
| XLB | us_sector | US materials — commodity demand |
| XLE | us_sector | US energy |
| XLF | us_sector | US financials |
| XLI | us_sector | US industrials — global manufacturing cycle |
| XLK | us_sector | US technology |
| XLP | us_sector | US consumer staples — defensive rotation signal |
| XLU | us_sector | US utilities — defensive rotation signal |
| XLV | us_sector | US healthcare |
| XLY | us_sector | US consumer discretionary |
| ^DJI | us_broad | Dow — old-economy US |
| ^GSPC | us_broad | S&P 500 — global risk benchmark |
| ^IXIC | us_broad | Nasdaq — global tech risk appetite |
| ^RUT | us_broad | Russell 2000 — US small-cap risk appetite |
| ^TNX | rates | US 10-year yield — global discount rate |
| ^VIX | us_vol | Volatility index — fear gauge; expected NEGATIVE lead |

---

_Not investment advice. Output is a probabilistic signal for you to weigh yourself — not a buy/sell instruction and not a guarantee of returns._