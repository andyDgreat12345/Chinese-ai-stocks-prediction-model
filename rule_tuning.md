China Market Oracle — trader-rule tuning

  search 2339 sessions (walk-forward slices), holdout 90 sessions never scored during search

  parameter        incumbent   fitted
  stop %                3.0      2.0
  target %              6.0      4.5
  max hold d              5        3
  slots                   3        2

  holdout (the only number that decides):
    incumbent  trades  64  win 48%  ret  +20.55%  PF  1.72  maxDD -11.05%
    fitted     trades  88  win 45%  ret  +12.03%  PF  1.47  maxDD  -9.39%
    expectancy-t +1.943 → +1.659 (-0.284)

  VERDICT: keep incumbent — no out-of-sample gain (gain -0.284 < 0.15).

  Scored on expectancy, not win rate: a 1% target against a 5% stop wins
  ~70% of the time and loses money. Win rate alone is not an objective.

  Not investment advice. Fitted exits are a research artifact.
