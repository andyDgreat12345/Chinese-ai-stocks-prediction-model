paper.record: 0 setup(s) for 2026-08-13
Paper strategy — mean_reversion_v1

  rule: enter at the open when prior body <= -0.96% AND gap <= -0.4%; exit at the close
  costs: 0.15% round trip

  record                      n      hit        net       t
  ---------------------------------------------------------
  retrospective holdout     332   59.6%    +0.312%   +2.40
  forward (live)              0        —          —       —

  No settled forward trades yet. The setup fires on about 5% of
  sessions, so across ten sectors expect roughly one every other day.
  Nothing here is evidence until this row fills.

  Every retrospective test reuses the same ten years. This row is the
  only evidence that could not have been fitted, which is why it is the
  one worth waiting for.

  Records what the rule would have done. No order is placed or advised.
  Not investment advice.
