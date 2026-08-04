"""Cost-aware paper-trading simulator — the honest referee before real money.

The base backtest (``oracle/backtest.py``) reports **gross** returns: it sums
each bet's raw % move with zero friction and never compounds. Real trading pays
commission, crosses the bid/ask (slippage), can only deploy the capital it has,
and lives or dies by its **drawdowns**. This module layers those realities on
top of the very same replayed records ``collect_records()`` already produces and
reports what actually matters:

  * **net** total return (after round-trip costs, **compounded** day over day),
  * **net** annualized Sharpe,
  * **maximum drawdown** — the number a gross backtest hides,
  * the **break-even** friction: how expensive trading can get before the edge
    is eaten entirely (a small break-even = a fragile edge).

Position sizing is explicit: each day the capital is spread **equal-weight**
across that day's directional bets and scaled by a gross-exposure knob, so a day
with one idea and a day with five are compared on the same capital base.

Like ``backtest.py`` this is pure functions over record dicts — no DB, no
network — so it is unit-tested directly.

**Not investment advice.** A positive net number here is a research result, not
a promise: live fills, borrow/short costs, taxes, dividends, FX on the US-listed
proxies, and regime change are *not* modeled, and the A-share sector ETFs stand
in for instruments a foreign investor would actually buy (see
``config.SECTOR_TRADEABLE_ETF``).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from . import config, db
from .backtest import (
    LLM_STRATEGY,
    STRATEGIES,
    _dir_llm,
    annualized_sharpe,
    collect_records,
    has_llm_calls,
)

# 1 basis point = 0.01 percentage point. Moves in the records are in percent
# (1.5 == +1.5%), so bps → percent is a ×0.01 scale.
_BPS_TO_PCT = 0.01


@dataclass(frozen=True)
class TradingCosts:
    """Per-*side* friction in basis points. A bet is a round trip (enter + exit),
    so the modeled cost is ``2 × (commission + slippage)``.

    Defaults are deliberately conservative-but-realistic for a liquid ETF via a
    retail broker: ~2.5 bps commission + ~5 bps slippage per side ⇒ 15 bps
    round-trip (0.15%). Raise them to stress-test; lower them only with a real
    fills study to back it up."""

    commission_bps: float = 2.5
    slippage_bps: float = 5.0

    @property
    def round_trip_pct(self) -> float:
        return 2 * (self.commission_bps + self.slippage_bps) * _BPS_TO_PCT


@dataclass(frozen=True)
class Sizing:
    """How capital is deployed. ``gross_exposure`` is the fraction of capital put
    to work each day (1.0 = fully invested, spread across that day's bets)."""

    gross_exposure: float = 1.0


# ── pure helpers ──────────────────────────────────────────────────────────
def equity_curve(daily_returns_pct: list[float]) -> list[float]:
    """Compound a series of daily %-returns into an equity curve starting at 1.0."""
    curve, v = [], 1.0
    for r in daily_returns_pct:
        v *= 1 + r / 100.0
        curve.append(v)
    return curve


def max_drawdown_pct(curve: list[float]) -> float:
    """Largest peak-to-trough decline of an equity curve, as a (negative) %."""
    if not curve:
        return 0.0
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, (v - peak) / peak)
    return mdd * 100.0


def _daily_signed_moves(records: list[dict], dir_fn) -> dict[str, list[float]]:
    """Group each day's *taken* bets into their signed realized moves (long a
    bullish call earns +move, a bearish call earns −move). Neutral/None = no
    position, so it never appears."""
    by_date: dict[str, list[float]] = {}
    for r in records:
        d = dir_fn(r)
        if not d or d == "neutral":
            continue
        signed = r["actual_move"] if d == "bullish" else -r["actual_move"]
        by_date.setdefault(r["date"], []).append(signed)
    return by_date


def _net_daily_returns(by_date, gross_exposure, cost_pct) -> list[float]:
    """Per-day portfolio %-return: equal-weight across the day's bets, each bet
    charged the round-trip cost, scaled by gross exposure. Date-ordered."""
    out = []
    for d in sorted(by_date):
        moves = by_date[d]
        net = sum(m - cost_pct for m in moves) / len(moves)
        out.append(gross_exposure * net)
    return out


def breakeven_roundtrip_bps(by_date, gross_exposure) -> float | None:
    """The round-trip friction (in bps) at which the compounded net return hits
    zero. Larger = sturdier edge. None if it never turns positive even free;
    ``0.0`` if it is already underwater with no costs at all."""
    if not by_date:
        return None

    def net_total(cost_pct: float) -> float:
        curve = equity_curve(_net_daily_returns(by_date, gross_exposure, cost_pct))
        return (curve[-1] - 1) * 100 if curve else 0.0

    if net_total(0.0) <= 0:
        return 0.0
    hi_pct = 50.0  # 5000 bps round trip — absurd upper bound
    if net_total(hi_pct) > 0:
        return None  # unrealistically robust; don't report a bogus number
    lo, hi = 0.0, hi_pct
    for _ in range(60):  # bisection to sub-bp precision
        mid = (lo + hi) / 2
        if net_total(mid) > 0:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2 / _BPS_TO_PCT, 1)  # round-trip pct → bps


def simulate(records, dir_fn, costs=TradingCosts(), sizing=Sizing()) -> dict:
    """Run one strategy through the cost model and report net performance."""
    by_date = _daily_signed_moves(records, dir_fn)
    cost_pct = costs.round_trip_pct
    n_trades = sum(len(v) for v in by_date.values())

    gross_daily = _net_daily_returns(by_date, sizing.gross_exposure, 0.0)
    net_daily = _net_daily_returns(by_date, sizing.gross_exposure, cost_pct)
    gross_curve, net_curve = equity_curve(gross_daily), equity_curve(net_daily)
    gross_total = (gross_curve[-1] - 1) * 100 if gross_curve else 0.0
    net_total = (net_curve[-1] - 1) * 100 if net_curve else 0.0

    return {
        "trading_days": len(by_date),
        "n_trades": n_trades,
        "gross_total_return_pct": round(gross_total, 4),
        "net_total_return_pct": round(net_total, 4),
        "cost_drag_pct": round(gross_total - net_total, 4),
        "net_sharpe": annualized_sharpe(net_daily),
        "max_drawdown_pct": round(max_drawdown_pct(net_curve), 4),
        "round_trip_cost_bps": round(cost_pct / _BPS_TO_PCT, 1),
        "breakeven_roundtrip_bps": breakeven_roundtrip_bps(by_date, sizing.gross_exposure),
    }


# Strategies worth the cost-aware treatment: the model, and the one baseline it
# actually has to beat (following Wall Street overnight).
_COST_STRATEGIES = ("model", "baseline: US-direction")


def run_cost_backtest(start=None, end=None, db_path=None,
                      costs=TradingCosts(), sizing=Sizing()) -> dict:
    records = collect_records(start, end, db_path)
    strat_fns = {name: STRATEGIES[name] for name in _COST_STRATEGIES}
    if has_llm_calls(records):        # add the recorded AI analyst when present
        strat_fns[LLM_STRATEGY] = _dir_llm
    return {
        "costs": {"commission_bps": costs.commission_bps,
                  "slippage_bps": costs.slippage_bps,
                  "round_trip_bps": round(costs.round_trip_pct / _BPS_TO_PCT, 1)},
        "gross_exposure": sizing.gross_exposure,
        "strategies": {name: simulate(records, fn, costs, sizing)
                       for name, fn in strat_fns.items()},
    }


# ── reporting ─────────────────────────────────────────────────────────────
def format_cost_report(report: dict) -> str:
    c = report["costs"]
    lines = [
        "cost-aware paper trading (net of friction, compounded):",
        f"  costs: {c['commission_bps']}bps commission + {c['slippage_bps']}bps "
        f"slippage per side = {c['round_trip_bps']}bps round trip; "
        f"gross exposure {report['gross_exposure']:.0%}",
        "",
        f"  {'strategy':<26}{'trades':>7}{'gross%':>9}{'net%':>9}"
        f"{'sharpe':>8}{'maxDD%':>9}{'breakeven':>11}",
        "  " + "-" * 77,
    ]
    for name, m in report["strategies"].items():
        def s(v):
            return "—" if v is None else f"{v}"
        be = m["breakeven_roundtrip_bps"]
        be_s = "—" if be is None else f"{be}bps"
        lines.append(
            f"  {name:<26}{m['n_trades']:>7}{s(m['gross_total_return_pct']):>9}"
            f"{s(m['net_total_return_pct']):>9}{s(m['net_sharpe']):>8}"
            f"{s(m['max_drawdown_pct']):>9}{be_s:>11}"
        )
    lines += [
        "",
        "  breakeven = round-trip friction at which the net edge vanishes; a "
        "small number means a fragile edge.",
        "  Proxies only — A-share sector ETFs stand in for the foreign-tradeable "
        "funds (config.SECTOR_TRADEABLE_ETF).",
        "  " + config.DISCLAIMER,
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    # Usage: python -m oracle.costsim [commission_bps] [slippage_bps] [gross_exposure]
    commission = float(argv[0]) if len(argv) > 0 else 2.5
    slippage = float(argv[1]) if len(argv) > 1 else 5.0
    exposure = float(argv[2]) if len(argv) > 2 else 1.0
    report = run_cost_backtest(
        costs=TradingCosts(commission_bps=commission, slippage_bps=slippage),
        sizing=Sizing(gross_exposure=exposure),
    )
    print(format_cost_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
