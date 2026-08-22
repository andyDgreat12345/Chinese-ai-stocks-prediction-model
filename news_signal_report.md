## Chinese news sentiment — predictive value

**Not measured yet** — 1 day(s) of news history, 40 needed. 39 to go.

News history only accumulates forward: RSS serves the current window,
so there is no backfill for this the way there is for prices. The
figure below is what the wait buys.

Buckets ready: 11 × 8 China symbols × 3 lags = 264 planned tests.

### Detectability

Smallest |r| a test could resolve, by history length. 'corrected' is the worst-case Benjamini–Hochberg bar for this sweep's width.

| days | min |r| raw | min |r| FDR-corrected |
|---|---|---|
| 20 | 0.443 | 0.697 |
| 40 | 0.312 | 0.526 |
| 60 | 0.254 | 0.439 |
| 90 | 0.207 | 0.364 |
| 120 | 0.179 | 0.317 |
| 250 | 0.124 | 0.222 |

_Not investment advice. A surviving bucket is a hypothesis, not a trade._
