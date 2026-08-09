"""The simulation loop — replay the system's calls through a trader's discipline.

One session at a time, in order:

  1. **age and exit** every open position against *this* session's bar (stop,
     target, time limit, or the model flipping against it);
  2. **then consider entries** from the calls made for this session, filling at
     the session's open where we have it, otherwise its close;
  3. mark the book to that session's close and record equity.

Exits are processed before entries so freed capital and freed position slots are
available the same day — which is what actually happens — while the entry price
is never chosen with knowledge of where the bar ended.

**On lookahead.** The call for session *d* is produced pre-open from data through
*d−1* (the pipeline enforces this via ``close_series(before=…)``). The fill is at
*d*'s open. So nothing in the decision uses information from the bar it trades.
The one modelled unknown is intraday ordering, resolved pessimistically in
``trader.check_exit``.

Verification compares the result against **buy-and-hold on the same instruments
over the same window** — a strategy that beats nothing is not a strategy.

**Not investment advice.**
"""
from __future__ import annotations

from . import ranking as rk
from . import trader as tr
from .trader import Position, PortfolioState, Trade, TraderRules


def _fill_price(bar: dict) -> float | None:
    """Fill at the open when the source gave us one; otherwise the close."""
    o, c = bar.get("o"), bar.get("c")
    return o if o is not None else c


def simulate(calls_by_date: dict, bars: dict, dates: list[str],
             rules: TraderRules | None = None,
             starting_cash: float = 100_000.0,
             edge_book=None, edge_floor: float = 0.0) -> dict:
    """Run the simulation.

    ``calls_by_date``: {date: [ {sector, symbol, direction, confidence}, ... ]}
    ``bars``:          {symbol: {date: {o,h,l,c}}}
    ``dates``:         the ordered session calendar to walk.
    ``edge_book``:     optional ``ranking.EdgeBook``. When given, contested slots
                       go to the sector with the best edge measured on records
                       STRICTLY BEFORE the session being traded. Without it the
                       original config order is preserved, so the ranking's
                       effect stays measurable against an unranked baseline.
    ``edge_floor``:    when > 0, refuse candidates below this measured edge.
    """
    rules = rules or TraderRules()
    st = PortfolioState(cash=starting_cash)

    for date in dates:
        # ── 1. age + exit open positions on today's bar ───────────────────
        todays_calls = {c["sector"]: c for c in calls_by_date.get(date, [])}
        for sector in list(st.positions):
            pos = st.positions[sector]
            bar = bars.get(pos.symbol, {}).get(date)
            if not bar:
                continue
            pos.held_days += 1
            signal = todays_calls.get(sector, {}).get("direction")
            decision = tr.check_exit(pos, bar, rules, signal)
            if not decision:
                continue
            reason, price = decision
            _close(st, sector, date, price, reason, rules)

        # ── 2. consider new entries ───────────────────────────────────────
        equity = _equity(st, bars, date)
        todays = calls_by_date.get(date, [])
        if edge_book is not None:
            todays = rk.edge_floor_filter(todays, edge_book, date, edge_floor)
            todays = rk.rank_calls(todays, edge_book, date)
        for call in todays:
            if not tr.wants_entry(call, rules, set(st.positions)):
                continue
            bar = bars.get(call["symbol"], {}).get(date)
            if not bar:
                continue
            price = _fill_price(bar)
            if not price or price <= 0:
                continue
            shares = tr.position_size(equity, price, rules)
            cost = shares * price
            if shares <= 0 or cost > st.cash:
                continue                     # cannot afford it — sit out
            stop, target = tr.levels(price, call["direction"], rules)
            st.cash -= cost + tr.apply_cost(cost, rules)
            st.positions[call["sector"]] = Position(
                sector=call["sector"], symbol=call["symbol"],
                direction=call["direction"], entry_date=date, entry_price=price,
                shares=shares, stop=stop, target=target)

        st.equity_curve.append((date, round(_equity(st, bars, date), 2)))

    # ── close anything still open at the end of the test ──────────────────
    if dates:
        last = dates[-1]
        for sector in list(st.positions):
            pos = st.positions[sector]
            bar = bars.get(pos.symbol, {}).get(last)
            if bar and bar.get("c"):
                _close(st, sector, last, bar["c"], "end-of-test", rules)

    return _summarize(st, bars, dates, rules, starting_cash)


def _close(st: PortfolioState, sector: str, date: str, price: float,
           reason: str, rules: TraderRules) -> None:
    pos = st.positions.pop(sector)
    gross = pos.shares * price
    st.cash += gross - tr.apply_cost(gross, rules)
    if pos.direction == "bullish":
        pnl = (price - pos.entry_price) * pos.shares
    else:
        pnl = (pos.entry_price - price) * pos.shares
    entry_val = pos.entry_price * pos.shares
    st.trades.append(Trade(
        sector=sector, symbol=pos.symbol, direction=pos.direction,
        entry_date=pos.entry_date, exit_date=date, entry_price=round(pos.entry_price, 4),
        exit_price=round(price, 4), shares=round(pos.shares, 4), reason=reason,
        pnl=round(pnl, 2), pnl_pct=round(pnl / entry_val * 100, 4) if entry_val else 0.0))


def _equity(st: PortfolioState, bars: dict, date: str) -> float:
    total = st.cash
    for pos in st.positions.values():
        bar = bars.get(pos.symbol, {}).get(date)
        px = (bar or {}).get("c") or pos.entry_price
        total += pos.shares * px
    return total


def buy_and_hold(bars: dict, symbols: list[str], dates: list[str],
                 starting_cash: float = 100_000.0) -> float | None:
    """Equal-weight buy-and-hold over the same window — the benchmark to beat.

    Each symbol is bought at **its own first available bar**, not at the window's
    first date. With full history the window opens in 1990 and no sector ETF
    existed yet, so requiring a bar on day one made every symbol unusable: the
    benchmark silently returned the starting cash, printed +0.00%, and the report
    declared victory over it. An unbalanced panel is the normal case once history
    is deep, so the benchmark has to buy each instrument when it lists.

    Cash earmarked for a symbol sits idle until that symbol's first bar, which is
    what actually happens to a buyer waiting for a listing.

    Returns None when no symbol has a usable pair of bars — the caller must then
    report "n/a" rather than claim a comparison it cannot make.
    """
    if not dates:
        return None
    last_date = dates[-1]
    date_set = set(dates)
    usable = []
    for s in symbols:
        series = bars.get(s) or {}
        if not (series.get(last_date) or {}).get("c"):
            continue
        first = next((d for d in dates
                      if d in date_set and (series.get(d) or {}).get("c")), None)
        if first is not None:
            usable.append((s, first))
    if not usable:
        return None
    per = starting_cash / len(usable)
    total = 0.0
    for s, first in usable:
        total += per * (bars[s][last_date]["c"] / bars[s][first]["c"])
    return total


def _summarize(st: PortfolioState, bars: dict, dates: list[str],
               rules: TraderRules, starting_cash: float) -> dict:
    trades = st.trades
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    final = st.equity_curve[-1][1] if st.equity_curve else starting_cash
    gross_win = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))

    peak, mdd = starting_cash, 0.0
    for _d, eq in st.equity_curve:
        peak = max(peak, eq)
        mdd = min(mdd, (eq - peak) / peak)

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.reason] = reasons.get(t.reason, 0) + 1

    symbols = sorted({t.symbol for t in trades}) or sorted(bars)
    bh = buy_and_hold(bars, symbols, dates, starting_cash)

    return {
        "starting_cash": starting_cash,
        "final_equity": round(final, 2),
        "return_pct": round((final / starting_cash - 1) * 100, 4),
        "buy_and_hold_equity": None if bh is None else round(bh, 2),
        "buy_and_hold_return_pct": (None if bh is None
                                    else round((bh / starting_cash - 1) * 100, 4)),
        # None, not False: "we could not compare" is not "we lost".
        "beat_buy_and_hold": None if bh is None else final > bh,
        "n_trades": len(trades),
        "win_rate": round(len(wins) / len(trades), 4) if trades else None,
        "avg_win_pct": round(sum(t.pnl_pct for t in wins) / len(wins), 4) if wins else None,
        "avg_loss_pct": round(sum(t.pnl_pct for t in losses) / len(losses), 4) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
        "max_drawdown_pct": round(mdd * 100, 4),
        "exit_reasons": reasons,
        "sessions": len(dates),
        "equity_curve": st.equity_curve,
        "trades": [t.__dict__ for t in trades],
        "rules": rules.__dict__,
    }


def format_report(s: dict) -> str:
    def pct(v):
        return "—" if v is None else f"{v:+.2f}%"

    r = s["rules"]
    win_rate = "—" if s["win_rate"] is None else f"{s['win_rate'] * 100:.0f}%"
    lines = [
        "China Market Oracle — trader simulation",
        f"  rules: conviction ≥ {r['min_conviction']}, risk {r['risk_per_trade_pct']}%/trade, "
        f"stop {r['stop_loss_pct']}%, target {r['take_profit_pct']}%, "
        f"max {r['max_positions']} positions, hold ≤ {r['max_hold_days']}d, "
        f"{r['cost_bps']}bps round trip"
        + (", shorting ON" if r["allow_short"] else ", long-only"),
        "",
        f"  sessions            {s['sessions']}",
        f"  trades              {s['n_trades']}",
        f"  win rate            {win_rate}",
        f"  avg win / avg loss  {pct(s['avg_win_pct'])} / {pct(s['avg_loss_pct'])}",
        f"  profit factor       {s['profit_factor'] if s['profit_factor'] is not None else '—'}",
        f"  max drawdown        {s['max_drawdown_pct']:.2f}%",
        "",
        f"  final equity        {s['final_equity']:,.2f}  ({pct(s['return_pct'])})",
        (f"  buy & hold          {s['buy_and_hold_equity']:,.2f}  "
         f"({pct(s['buy_and_hold_return_pct'])})"
         if s['buy_and_hold_equity'] is not None else
         "  buy & hold          n/a  (no instrument spans this window)"),
        (f"  verdict             "
         f"{'BEAT buy & hold' if s['beat_buy_and_hold'] else 'did NOT beat buy & hold'}"
         if s['beat_buy_and_hold'] is not None else
         "  verdict             no benchmark — comparison not available"),
        "",
        f"  exits: {s['exit_reasons'] or '(none)'}",
        "",
        "  Inputs are lookahead-free: each session is decided from the PRIOR US",
        "  close and prior China closes only, and filled at that session's open.",
        "  Still unmodeled: live fills, slippage beyond the fixed bps, taxes,",
        "  borrow costs, and regime change.",
        "",
        "  A simulated curve is a research result, not a promise — live fills, "
        "taxes and regime change are not modeled. Not investment advice.",
    ]
    return "\n".join(lines)
