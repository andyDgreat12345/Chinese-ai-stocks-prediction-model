"""Can the validated rule actually be executed, and what does it cost?

Two threats sit between a backtested edge and a real one, and neither is
visible in a hit rate.

**Settlement.** Every instrument in the traded universe is a domestic A-share
equity ETF. Mainland equities settle T+1: shares bought today cannot be sold
until the next session. If that applies to these ETFs, the validated rule —
enter at the open, exit at the same session's close — is not a rule that can be
executed at all, and the earliest legal exit is the following session.

This module cannot resolve that question, because settlement rules are not in
price data. What it can do is refuse to let the question go unasked, and price
the answer both ways: the rule is reported under same-session exit and under the
earliest T+1-compatible exit, so the cost of the constraint is a number rather
than a surprise. Notably it is a small number — the rule degrades rather than
dies — which is the useful thing to know before anyone calls a broker.

There is a suggestive consistency here worth noticing rather than trusting. The
overnight window in these instruments carries a large negative drift, and T+1 is
the most natural explanation for it: forced sellers must wait for the open. If
that is the mechanism, then the anomaly persists *because* the obvious trade
against it is the one settlement forbids. That would make the edge more durable
and less accessible at the same time.

**Slippage.** The rule assumes it transacts at the open. China runs an opening
call auction that clears at a single price, so that fill is achievable in
principle — but only in principle, and every basis point of shortfall comes
straight off a net return already thin relative to its spread. Rather than
guess at a fill quality, this sweeps the assumption and reports how much extra
friction the edge can absorb before it is gone.

**Not investment advice.** No order is placed or recommended.
"""
from __future__ import annotations

from .exit_horizon import T0_EXIT, T1_EXIT, _stats, build_paths, simulate
from ..paper import COST_PCT, qualifies

# Extra round-trip friction to test, in percent, on top of the cost already
# assumed. 0.05 is roughly a one-tick shortfall on a liquid ETF; 0.50 is a bad
# fill on both sides of an illiquid one.
SLIPPAGE_STEPS = (0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50)



def slippage_curve(rows: list[dict], horizon: str = T0_EXIT,
                   predicate=None, cost_pct: float = COST_PCT,
                   steps=SLIPPAGE_STEPS) -> list[dict]:
    """Net return and hit rate at each level of extra friction. Pure.

    Slippage is charged as additional round-trip cost. To first order a fill
    worse by k% on entry and exit reduces the return by k%, which is what
    subtracting it from the gross return does.
    """
    if predicate is None:
        predicate = lambda r: qualifies(r.get("prior_body"), r.get("gap"))  # noqa: E731
    vals = [r[horizon] for r in rows
            if predicate(r) and r.get(horizon) is not None]
    out = []
    for slip in steps:
        s = _stats(vals, cost_pct + slip)
        out.append({"slippage": slip, **s})
    return out


def breakeven(rows: list[dict], horizon: str = T0_EXIT, predicate=None,
              cost_pct: float = COST_PCT) -> dict:
    """How much extra friction the edge can absorb before it is gone. Pure.

    The headline number is just the net return — friction and return are the
    same units, so an edge of +0.30% dies at +0.30% of extra cost. What makes it
    informative is the context it is expressed in: as a share of the average
    daily range (execution shortfall scales with volatility, not with a fixed
    tick) and as a multiple of the friction already assumed.
    """
    if predicate is None:
        predicate = lambda r: qualifies(r.get("prior_body"), r.get("gap"))  # noqa: E731
    picked = [r for r in rows if predicate(r)]
    vals = [r[horizon] for r in picked if r.get(horizon) is not None]
    if not vals:
        return {"status": "no_data", "horizon": horizon}
    ranges = [abs(r["gap"]) + abs(r[T0_EXIT]) for r in picked
              if r.get("gap") is not None and r.get(T0_EXIT) is not None]
    mean_range = (sum(ranges) / len(ranges)) if ranges else None
    s = _stats(vals, cost_pct)
    net = s["net"]
    return {
        "status": "measured", "horizon": horizon, "n": s["n"],
        "net": net, "sd": s["sd"], "t": s["t"],
        "breakeven_extra_pct": None if net is None else round(net, 4),
        "as_share_of_daily_move": (None if not (net and mean_range)
                                   else round(net / mean_range, 4)),
        "as_multiple_of_assumed_cost": (None if net is None or not cost_pct
                                        else round(net / cost_pct, 2)),
        "mean_daily_move": None if mean_range is None else round(mean_range, 4),
    }


def settlement_comparison(rows: list[dict], predicate=None,
                          cost_pct: float = COST_PCT,
                          holdout_frac: float = 0.3) -> dict:
    """The rule priced under T+0 and under the T+1 constraint. Pure.

    Uses the same chronological holdout as the validated result, so the T+0 row
    is directly comparable to the number already published.
    """
    res = simulate(rows, predicate=predicate, cost_pct=cost_pct,
                   holdout_frac=holdout_frac)
    if res.get("status") != "measured":
        return {"status": res.get("status", "no_data")}
    hold = res["holdout"]
    t0, t1 = hold[T0_EXIT]["rule"], hold[T1_EXIT]["rule"]
    cost = (None if t0["net"] is None or t1["net"] is None
            else round(t1["net"] - t0["net"], 4))
    return {
        "status": "measured", "t0_exit": T0_EXIT, "t1_exit": T1_EXIT,
        "t0": t0, "t1": t1, "cost_of_constraint": cost,
        "survives_t1": bool(t1["net"] is not None and t1["net"] > 0
                            and t1["t"] is not None and t1["t"] >= 2.0),
    }


def format_report(curve: list[dict], be: dict, sett: dict) -> str:
    L = ["Execution realism — settlement and slippage", ""]

    # ── settlement ───────────────────────────────────────────────────────
    L += ["  1. Settlement: can this rule be executed at all?", ""]
    measured = sett.get("status") == "measured"
    if not measured:
        L += [f"     Not measurable here ({sett.get('status')}) — but the question",
              "     below does not go away just because there are no trades to price."]
    else:
        t0, t1 = sett["t0"], sett["t1"]
        L += [f"     {'exit':22}{'n':>7}{'hit':>8}{'net':>10}{'t':>8}",
              f"     {'-' * 55}"]
        for label, b in ((f"{sett['t0_exit']} (T+0)", t0),
                         (f"{sett['t1_exit']} (T+1)", t1)):
            if not b["n"]:
                L.append(f"     {label:22}{'—':>7}")
                continue
            t = "n/a" if b["t"] is None else f"{b['t']:+.2f}"
            L.append(f"     {label:22}{b['n']:>7}{b['hit']:>7.1%}"
                     f"{b['net']:>+9.3f}%{t:>8}")
        if sett["cost_of_constraint"] is not None:
            L += ["", f"     Cost of the T+1 constraint: "
                      f"{sett['cost_of_constraint']:+.3f}% per trade."]
    # Unconditional: a caveat that only appears when the numbers happen to
    # compute is a caveat that will be missing on exactly the run where someone
    # reads the report and decides to fund something.
    L += ["",
          "     The rule as validated exits at the same session's close. Every",
          "     instrument here is a domestic A-share equity ETF, and mainland",
          "     equities settle T+1 — shares bought today cannot be sold until",
          "     the next session. If that applies to these ETFs, the validated",
          "     exit is not executable and the T+1 row is the real rule.",
          "",
          "     VERIFY THIS WITH THE BROKER BEFORE FUNDING ANYTHING. It cannot",
          "     be determined from price data, and it decides which row above is",
          "     the honest one."]
    if measured:
        if sett["survives_t1"]:
            L += ["", "     The rule stays profitable and significant under the",
                  "     constraint, so this is a question of degree, not of viability."]
        else:
            L += ["", "     The rule does NOT clear the significance bar under the",
                  "     constraint. If T+1 applies, treat it as unvalidated."]

    # ── slippage ─────────────────────────────────────────────────────────
    L += ["", "  2. Slippage: how much bad fill can the edge absorb?", "",
          "     Measured on the FULL window, train and holdout together — a",
          "     slippage sweep is an arithmetic transformation of returns, not a",
          "     hypothesis being tested, so it does not need a clean holdout and",
          "     is better served by every trade available. The full-window net is",
          "     lower than the holdout net quoted above, which makes the execution",
          "     budget below the more conservative of the two figures.", "",
          f"     {'extra cost':>12}{'hit':>9}{'net':>10}{'t':>8}",
          f"     {'-' * 39}"]
    for row in curve:
        if not row["n"]:
            continue
        t = "n/a" if row["t"] is None else f"{row['t']:+.2f}"
        L.append(f"     {row['slippage']:>11.2f}%{row['hit']:>8.1%}"
                 f"{row['net']:>+9.3f}%{t:>8}")
    if be.get("status") == "measured" and be["breakeven_extra_pct"] is not None:
        L += ["", f"     Breakeven at {be['breakeven_extra_pct']:+.3f}% of extra "
                  f"round-trip friction"]
        if be["as_multiple_of_assumed_cost"] is not None:
            L.append(f"     — {be['as_multiple_of_assumed_cost']:.2f}x the friction "
                     f"already assumed,")
        if be["as_share_of_daily_move"] is not None:
            L.append(f"     — {be['as_share_of_daily_move']:.1%} of the average "
                     f"{be['mean_daily_move']:.2f}% daily move on these sessions.")
        L += ["",
              "     Read the last line as the real execution budget. Shortfall scales",
              "     with volatility rather than with a fixed tick, so the share of the",
              "     daily move is what has to hold, not the basis points."]
    L += ["", "  Nothing here is executed. No order is placed or recommended.",
          "  Not investment advice."]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    from .. import config

    rows = build_paths(start=config.LEARNING_TRAIN_START or None)
    print(format_report(slippage_curve(rows), breakeven(rows),
                        settlement_comparison(rows)))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
