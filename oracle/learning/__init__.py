"""The learning subsystem — how the model actually gets better over time.

`walkforward` fits signal weights + the abstain threshold on past data and scores
them on windows the search never saw; `autotune` is the guarded job that adopts a
change only when it beats the incumbent out-of-sample, and writes every attempt
(adopted or refused) to the learning ledger.
"""
