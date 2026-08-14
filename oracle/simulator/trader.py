"""Human trader logic — the rules a disciplined person actually follows.

The cost-aware backtest (``oracle/costsim.py``) answers "what if a machine took
every signal, equal-weight, forever". No human trades that way, and the number it
produces flatters the system: it never runs out of capital, never sits out a weak
setup, never gets stopped out, and holds an unlimited number of positions at once.

This module models the other thing — a person with finite money and normal
discipline:

  * **selective**: acts only on calls at or above a conviction floor, because a
    low-conviction call is not worth a slot;
  * **risk-sized**: each position risks a fixed *fraction of equity* to its stop,
    so position size falls out of the stop distance rather than being guessed;
  * **protected**: a hard stop-loss and a take-profit on every trade;
  * **capacity-limited**: at most N positions at once — a real person cannot
    watch twenty, and capital is finite;
  * **time-limited**: a trade that has done nothing for N sessions is closed, so
    capital is not dead money;
  * **cost-paying**: commission + slippage on entry and exit.

Because the trades are checked against **intraday highs and lows**, a stop can be
hit on the day it is set — the honest sequence. Where a bar touches both stop and
target, the **stop is assumed first**: without intraday ordering we cannot know
which came first, and assuming the good one would systematically overstate results.

All pure functions over bar dicts — no DB, no clock, no network.

**Not investment advice.** A simulated equity curve is a research artifact.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_CONV_RANK = {"low": 1, "med": 2, "high": 3}


@dataclass(frozen=True)
class TraderRules:
    """The discipline. Every default is deliberately conservative."""

    # Ignore low-conviction calls entirely. Raising this to "high" is tempting
    # and measurably WRONG: the high tier has the better DIRECTIONAL accuracy
    # (54.5% vs 51.7%) but trading only the high tier scores worse on every
    # metric that matters — 688 trades at 46.9% / PF 1.18 / +20.3%, against
    # 1,173 at 49.2% / PF 1.29 / +125.4% at "med".
    #
    # That is not a contradiction, it is the system's central finding restated:
    # a strong composite means a large US move, and the accuracy that buys sits
    # in the overnight GAP, which entry at the open forfeits. Selecting harder
    # on a signal whose edge is unreachable concentrates into exactly the
    # sessions where the reachable part is worst.
    min_conviction: str = "med"
    risk_per_trade_pct: float = 2.0   # % of equity risked to the stop
    stop_loss_pct: float = 3.0        # hard stop, % from entry
    take_profit_pct: float = 6.0      # target, % from entry (2:1 vs the stop)
    max_positions: int = 3            # a person cannot watch more than a few
    max_hold_days: int = 5            # dead money is closed out
    max_position_pct: float = 40.0    # cap on any single position vs equity
    cost_bps: float = 15.0            # round-trip commission + slippage
    allow_short: bool = False         # bearish calls sit out unless enabled

    def accepts(self, conviction: str) -> bool:
        return _CONV_RANK.get(conviction, 0) >= _CONV_RANK.get(self.min_conviction, 2)


@dataclass
class Position:
    sector: str
    symbol: str
    direction: str          # bullish / bearish
    entry_date: str
    entry_price: float
    shares: float
    stop: float
    target: float
    held_days: int = 0


@dataclass
class Trade:
    sector: str
    symbol: str
    direction: str
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    shares: float
    reason: str             # stop / target / time / signal-flip / end-of-test
    pnl: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class PortfolioState:
    cash: float
    positions: dict = field(default_factory=dict)   # sector -> Position
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)  # [(date, equity)]


# ── sizing ────────────────────────────────────────────────────────────────
def position_size(equity: float, price: float, rules: TraderRules) -> float:
    """Fixed-fractional risk sizing: risk `risk_per_trade_pct` of equity to the
    stop, so a wider stop buys fewer shares. Capped by `max_position_pct` so one
    idea can never dominate the book."""
    if price <= 0 or rules.stop_loss_pct <= 0:
        return 0.0
    risk_cash = equity * rules.risk_per_trade_pct / 100.0
    risk_per_share = price * rules.stop_loss_pct / 100.0
    shares = risk_cash / risk_per_share
    max_shares = (equity * rules.max_position_pct / 100.0) / price
    return max(0.0, min(shares, max_shares))


def levels(entry: float, direction: str, rules: TraderRules) -> tuple[float, float]:
    """(stop, target) for a position, on the correct side for its direction."""
    s, t = rules.stop_loss_pct / 100.0, rules.take_profit_pct / 100.0
    if direction == "bullish":
        return entry * (1 - s), entry * (1 + t)
    return entry * (1 + s), entry * (1 - t)


# ── exit decision ─────────────────────────────────────────────────────────
def check_exit(pos: Position, bar: dict, rules: TraderRules,
               signal_dir: str | None = None) -> tuple[str, float] | None:
    """Decide whether `pos` closes on this bar and at what price.

    Order of checks matters and is deliberately pessimistic: **stop before
    target**. When a single daily bar spans both levels we have no intraday
    sequence, and resolving the ambiguity in our favour would inflate every
    result in the report."""
    high = bar.get("h") if bar.get("h") is not None else bar.get("c")
    low = bar.get("l") if bar.get("l") is not None else bar.get("c")
    close = bar.get("c")
    if close is None:
        return None

    if pos.direction == "bullish":
        if low is not None and low <= pos.stop:
            return ("stop", pos.stop)
        if high is not None and high >= pos.target:
            return ("target", pos.target)
    else:
        if high is not None and high >= pos.stop:
            return ("stop", pos.stop)
        if low is not None and low <= pos.target:
            return ("target", pos.target)

    # the model flipped against an open position — a real trader respects that
    if signal_dir and signal_dir != "neutral" and signal_dir != pos.direction:
        return ("signal-flip", close)
    if pos.held_days >= rules.max_hold_days:
        return ("time", close)
    return None


# ── entry decision ────────────────────────────────────────────────────────
def wants_entry(call: dict, rules: TraderRules, open_sectors: set) -> bool:
    """Would a disciplined trader open this position today?"""
    if call["sector"] in open_sectors:
        return False                        # already exposed to this sector
    if len(open_sectors) >= rules.max_positions:
        return False                        # book is full
    if not rules.accepts(call.get("confidence") or call.get("conviction") or "low"):
        return False                        # below the conviction floor
    direction = call.get("direction")
    if direction == "bullish":
        return True
    if direction == "bearish":
        return rules.allow_short            # bearish sits out unless shorting on
    return False                            # never trade a neutral call


def apply_cost(value: float, rules: TraderRules) -> float:
    """Half the round-trip cost, charged on each side of a trade."""
    return value * (rules.cost_bps / 2.0) / 10000.0
