"""Run the trader simulation on the system's own historical calls.

    python -m oracle.simulator.run [--short] [--conviction low|med|high]
                                   [--stop 3] [--target 6] [--positions 3]
                                   [--hold 5] [--cash 100000]

Loads every prediction the model would have made (via the backtest replay, which
already forbids lookahead), pairs each with its sector's tradeable instrument,
and walks a disciplined trader through the history.
"""
from __future__ import annotations

import sys

from .. import config, db
from . import engine
from .trader import TraderRules


def load_inputs(db_path=None) -> tuple[dict, dict, list]:
    """(calls_by_date, bars_by_symbol, ordered_dates) from real history."""
    from ..backtest import collect_records

    db.init_db(db_path)
    records = collect_records(db_path=db_path)

    calls_by_date: dict[str, list] = {}
    for r in records:
        symbol = config.CHINA_SECTOR_ETFS.get(r["sector"])
        if not symbol:
            continue
        calls_by_date.setdefault(r["date"], []).append({
            "sector": r["sector"], "symbol": symbol,
            "direction": r["model_dir"], "confidence": r["model_conf"],
        })

    bars: dict[str, dict] = {}
    for sector, symbol in config.CHINA_SECTOR_ETFS.items():
        rows = db.close_series("china_close", symbol=symbol, limit=100000,
                               db_path=db_path)
        bars[symbol] = {r["trade_date"]: {"o": r.get("open"), "h": r.get("high"),
                                          "l": r.get("low"), "c": r["close"]}
                        for r in rows if r["close"] is not None}
    dates = sorted(calls_by_date)
    return calls_by_date, bars, dates


def main(argv: list[str]) -> int:
    def opt(name, default, cast=float):
        if name in argv:
            i = argv.index(name)
            if i + 1 < len(argv):
                return cast(argv[i + 1])
        return default

    rules = TraderRules(
        min_conviction=opt("--conviction", "med", str),
        stop_loss_pct=opt("--stop", 3.0),
        take_profit_pct=opt("--target", 6.0),
        max_positions=int(opt("--positions", 3)),
        max_hold_days=int(opt("--hold", 5)),
        risk_per_trade_pct=opt("--risk", 2.0),
        allow_short="--short" in argv,
    )
    cash = opt("--cash", 100_000.0)

    calls, bars, dates = load_inputs()
    if not dates:
        print("no historical calls to simulate — run the backfill first")
        return 1
    print(f"simulating {len(dates)} sessions, {len(bars)} instruments\n")
    result = engine.simulate(calls, bars, dates, rules, cash)
    print(engine.format_report(result))

    # With more candidates than slots, the order they are listed in silently
    # decides who gets filled. Always report how much of the result that is.
    if "--no-order-check" not in argv:
        from . import ranking as rk
        print()
        print(rk.format_order_sensitivity(
            rk.order_sensitivity(calls, bars, dates, rules, cash)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
