"""Fit the trader's exit rules — the largest untuned surface in the system.

``TraderRules`` defaults (stop 3%, target 6%, hold 5d, risk 2%, 3 slots) were
hand-set guesses, and they are where the returns actually come from. On the
current history the simulator wins 51% of trades — a coin flip, one-sided
p = 0.41 — yet still compounds, because the average win is 1.6x the average
loss. Strip that asymmetry out and the same win rate earns +0.04%/trade instead
of +0.69%. The exits are the edge, and nobody has ever fitted them.

**The objective is expectancy, not win rate.** Win rate is trivially gamed: a 1%
target against a 5% stop wins ~70% of the time and loses money. So candidates are
scored on a t-statistic over per-trade P&L — mean/(sd/√n) — which rewards being
profitable *and* consistent, and cannot be won by one lucky trade. A ``min_trades``
floor rejects degenerate corners that trade twice.

Expectancy also converges far faster than win rate, which matters: confirming a
genuine 51%→55% win-rate improvement needs ~785 trades, about seven years at the
current rate. Fitting what we can measure beats waiting on what we cannot.

The protocol mirrors ``learning/walkforward``, for the same reason:

  1. **Holdout** — the most recent sessions are cut off and never scored during
     the search.
  2. **Walk-forward slices** over the rest; a candidate's score is its *mean*
     score across slices, so a rule set that only works in one regime loses to
     one that works in several.
  3. **Selection** by that mean.
  4. **Final judgement** on the untouched holdout — the only number that decides
     adoption, because step 3 already consumed its slices by choosing.

Note the simulator is path-dependent (compounding equity, finite position slots),
so every slice is run as its own simulation from a fresh starting balance. Scoring
one long run and slicing the trades afterwards would let a fold inherit the
equity and open positions of the fold before it.

**Not investment advice.** Fitted exits are a research artifact.
"""
from __future__ import annotations

from dataclasses import replace
from math import sqrt
from statistics import pstdev

from . import engine
from .trader import TraderRules

# Kept coarse on purpose. A few hundred trades cannot resolve a 0.25% difference
# in stop distance, and a fine grid would fit the noise between them.
STOP_PCTS = (2.0, 3.0, 4.0, 5.0)
TARGET_PCTS = (3.0, 4.5, 6.0, 8.0, 10.0)
HOLD_DAYS = (3, 5, 8, 12)
MAX_POSITIONS = (2, 3, 5)

# `risk_per_trade_pct` is deliberately NOT searched. The objective is a
# t-statistic over `pnl_pct`, and pnl_pct = pnl / entry_val — risk sizing scales
# `shares`, and therefore `pnl` and `entry_val`, by the same factor. The score is
# mathematically invariant to it. Including it in the grid does not fit it; it
# picks arbitrarily among exact ties, and that arbitrary pick then changes real
# returns (halving risk% halved the holdout return for no measured reason).
# A parameter the objective cannot see does not belong in the search — the same
# hazard `live_signals` guards against for dead signals. Sizing is a risk
# decision to be judged on the equity curve, not on per-trade P&L.

MIN_TRADES_PER_SLICE = 8      # below this a slice score is noise
MIN_HOLDOUT_TRADES = 15
MIN_IMPROVEMENT = 0.15        # holdout t must beat the incumbent by this much


def candidate_rules(base: TraderRules | None = None) -> list[TraderRules]:
    """The search grid. Pure and deterministic.

    Only exit/sizing parameters vary. ``allow_short`` is deliberately NOT searched:
    shorting A-shares is not realistically available to a retail investor, so a
    rule set that needs it would be fitting a market we cannot trade.
    """
    base = base or TraderRules()
    out = []
    for stop in STOP_PCTS:
        for target in TARGET_PCTS:
            if target <= stop:
                continue          # a target inside the stop is not a trade plan
            for hold in HOLD_DAYS:
                for slots in MAX_POSITIONS:
                    out.append(replace(
                        base, stop_loss_pct=stop, take_profit_pct=target,
                        max_hold_days=hold, max_positions=slots))
    return out


def expectancy_t(result: dict, min_trades: int = MIN_TRADES_PER_SLICE) -> float:
    """t-statistic over per-trade P&L. -inf when too thin to judge. Pure."""
    pnls = [t["pnl_pct"] for t in result.get("trades", [])]
    n = len(pnls)
    if n < min_trades:
        return float("-inf")
    mean = sum(pnls) / n
    sd = pstdev(pnls)
    if sd <= 0:
        return float("-inf")
    return mean / (sd / sqrt(n))


def slice_dates(dates: list[str], n_slices: int) -> list[list[str]]:
    """Contiguous, equal-ish, chronological slices. Pure."""
    if n_slices < 1 or not dates:
        return []
    size = len(dates) / n_slices
    return [dates[int(i * size):int((i + 1) * size)] for i in range(n_slices)]


def split_holdout(dates: list[str], holdout_sessions: int) -> tuple[list, list]:
    """(search_pool, holdout) — the most recent sessions are reserved. Pure."""
    if holdout_sessions <= 0 or holdout_sessions >= len(dates):
        return dates, []
    return dates[:-holdout_sessions], dates[-holdout_sessions:]


def score_rules(calls: dict, bars: dict, slices: list[list[str]],
                rules: TraderRules, cash: float,
                min_trades: int = MIN_TRADES_PER_SLICE) -> float:
    """Mean expectancy-t across slices, each simulated independently.

    Slices scoring -inf (too few trades) are skipped rather than poisoning the
    mean, but a candidate that is thin *everywhere* scores -inf overall — being
    unmeasurable is not the same as being good.
    """
    scores = []
    for chunk in slices:
        if not chunk:
            continue
        res = engine.simulate(calls, bars, chunk, rules, cash)
        t = expectancy_t(res, min_trades)
        if t != float("-inf"):
            scores.append(t)
    if not scores:
        return float("-inf")
    return sum(scores) / len(scores)


def select_rules(calls: dict, bars: dict, search_dates: list[str],
                 cash: float = 100_000.0, n_slices: int = 4,
                 base: TraderRules | None = None) -> tuple[TraderRules | None, float]:
    """Best rule set by mean score across walk-forward slices. Never sees holdout."""
    slices = slice_dates(search_dates, n_slices)
    if not slices:
        return None, float("-inf")
    best, best_score = None, float("-inf")
    for rules in candidate_rules(base):
        s = score_rules(calls, bars, slices, rules, cash)
        if s > best_score:
            best, best_score = rules, s
    return best, best_score


def tune(db_path=None, cash: float = 100_000.0, holdout_sessions: int = 90,
         n_slices: int = 4) -> dict:
    """Fit → judge on holdout → recommend. Returns the full comparison."""
    from .run import load_inputs

    calls, bars, dates = load_inputs(db_path)
    if not dates:
        return {"status": "no_history"}

    search_dates, holdout = split_holdout(dates, holdout_sessions)
    incumbent = TraderRules()

    if len(holdout) < 20:
        return {"status": "insufficient_history", "sessions": len(dates)}

    candidate, search_score = select_rules(calls, bars, search_dates, cash,
                                           n_slices)
    if candidate is None:
        return {"status": "no_candidate", "sessions": len(dates)}

    before = engine.simulate(calls, bars, holdout, incumbent, cash)
    after = engine.simulate(calls, bars, holdout, candidate, cash)
    t_before = expectancy_t(before, MIN_HOLDOUT_TRADES)
    t_after = expectancy_t(after, MIN_HOLDOUT_TRADES)

    gain = (t_after - t_before) if t_before != float("-inf") else float("-inf")
    adopt = (t_after != float("-inf")
             and t_before != float("-inf")
             and gain >= MIN_IMPROVEMENT)

    return {
        "status": "measured",
        "sessions": len(dates),
        "search_sessions": len(search_dates),
        "holdout_sessions": len(holdout),
        "search_score": search_score,
        "incumbent": incumbent.__dict__,
        "candidate": candidate.__dict__,
        "holdout_before": before,
        "holdout_after": after,
        "t_before": t_before,
        "t_after": t_after,
        "gain": gain,
        "adopt": adopt,
    }


def _fmt(r: dict) -> str:
    wr = r.get("win_rate")
    return (f"trades {r['n_trades']:3}  win {('%.0f%%' % (wr * 100)) if wr else '  n/a'}  "
            f"ret {r['return_pct']:+7.2f}%  PF {r.get('profit_factor') or 0:5.2f}  "
            f"maxDD {r['max_drawdown_pct']:+6.2f}%")


def format_report(res: dict) -> str:
    if res.get("status") != "measured":
        return (f"trader-rule tuning: {res.get('status')} "
                f"({res.get('sessions', 0)} sessions) — need more history")

    inc, cand = res["incumbent"], res["candidate"]
    L = ["China Market Oracle — trader-rule tuning", ""]
    L.append(f"  search {res['search_sessions']} sessions (walk-forward slices), "
             f"holdout {res['holdout_sessions']} sessions never scored during search")
    L.append("")
    L.append("  parameter        incumbent   fitted")
    for key, label in (("stop_loss_pct", "stop %"), ("take_profit_pct", "target %"),
                       ("max_hold_days", "max hold d"), ("max_positions", "slots")):
        L.append(f"  {label:15} {inc[key]:>9}   {cand[key]:>6}")
    L += ["",
          "  holdout (the only number that decides):",
          f"    incumbent  {_fmt(res['holdout_before'])}",
          f"    fitted     {_fmt(res['holdout_after'])}",
          f"    expectancy-t {res['t_before']:+.3f} → {res['t_after']:+.3f} "
          f"({res['gain']:+.3f})",
          ""]
    if res["adopt"]:
        L.append(f"  VERDICT: adopt — beats the incumbent out of sample by "
                 f"{res['gain']:+.3f} t (bar {MIN_IMPROVEMENT}).")
    else:
        L.append(f"  VERDICT: keep incumbent — no out-of-sample gain "
                 f"(gain {res['gain']:+.3f} < {MIN_IMPROVEMENT}).")
    L += ["",
          "  Scored on expectancy, not win rate: a 1% target against a 5% stop wins",
          "  ~70% of the time and loses money. Win rate alone is not an objective.",
          "",
          "  Not investment advice. Fitted exits are a research artifact."]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    print(format_report(tune()))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
