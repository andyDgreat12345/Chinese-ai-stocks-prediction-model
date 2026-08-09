"""Tests for the trader simulator — sizing, stops, discipline, and honesty."""
from oracle.simulator import engine
from oracle.simulator.trader import (
    Position, TraderRules, apply_cost, check_exit, levels, position_size, wants_entry,
)


def _bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


# ── sizing ────────────────────────────────────────────────────────────────
def test_risk_sizing_risks_exactly_the_configured_fraction():
    r = TraderRules(risk_per_trade_pct=2.0, stop_loss_pct=4.0, max_position_pct=100)
    shares = position_size(100_000, 10.0, r)
    # risking 2% (=2000) with a 4% stop (=0.40/share) -> 5000 shares
    assert round(shares) == 5000
    assert round(shares * 10.0 * 0.04) == 2000        # loss at stop == the risk


def test_wider_stop_buys_fewer_shares():
    tight = position_size(100_000, 10.0, TraderRules(stop_loss_pct=2.0, max_position_pct=100))
    wide = position_size(100_000, 10.0, TraderRules(stop_loss_pct=8.0, max_position_pct=100))
    assert tight > wide


def test_max_position_cap_binds():
    r = TraderRules(risk_per_trade_pct=50.0, stop_loss_pct=1.0, max_position_pct=40)
    shares = position_size(100_000, 10.0, r)
    assert shares * 10.0 <= 40_000 + 1e-6            # never exceeds the cap


def test_size_is_zero_on_bad_price():
    assert position_size(100_000, 0.0, TraderRules()) == 0.0


# ── levels ────────────────────────────────────────────────────────────────
def test_levels_sit_on_the_correct_side_per_direction():
    s, t = levels(100.0, "bullish", TraderRules(stop_loss_pct=3, take_profit_pct=6))
    assert s == 97.0 and t == 106.0
    s, t = levels(100.0, "bearish", TraderRules(stop_loss_pct=3, take_profit_pct=6))
    assert s == 103.0 and t == 94.0


# ── exits: the honesty-critical logic ─────────────────────────────────────
def _pos(direction="bullish", entry=100.0, stop=97.0, target=106.0, held=0):
    return Position("semis", "512480", direction, "d1", entry, 100, stop, target, held)


def test_stop_is_taken_when_the_low_pierces_it():
    out = check_exit(_pos(), _bar(100, 101, 96, 99), TraderRules())
    assert out == ("stop", 97.0)


def test_target_is_taken_when_the_high_reaches_it():
    out = check_exit(_pos(), _bar(100, 107, 99.5, 106.5), TraderRules())
    assert out == ("target", 106.0)


def test_ambiguous_bar_resolves_to_the_stop_not_the_target():
    """A bar spanning BOTH levels must resolve pessimistically — assuming the
    target came first would systematically inflate every reported result."""
    out = check_exit(_pos(), _bar(100, 108, 95, 104), TraderRules())
    assert out == ("stop", 97.0)


def test_signal_flip_closes_the_position_at_the_close():
    out = check_exit(_pos(), _bar(100, 101, 99, 100.5), TraderRules(), signal_dir="bearish")
    assert out == ("signal-flip", 100.5)


def test_time_stop_fires_after_max_hold():
    r = TraderRules(max_hold_days=3)
    assert check_exit(_pos(held=2), _bar(100, 101, 99, 100), r) is None
    assert check_exit(_pos(held=3), _bar(100, 101, 99, 100), r)[0] == "time"


def test_bearish_stop_and_target_invert():
    p = _pos("bearish", 100.0, 103.0, 94.0)
    assert check_exit(p, _bar(100, 104, 99, 101), TraderRules()) == ("stop", 103.0)
    assert check_exit(p, _bar(100, 101, 93, 94), TraderRules()) == ("target", 94.0)


def test_missing_ohlc_falls_back_to_close():
    out = check_exit(_pos(), {"o": None, "h": None, "l": None, "c": 96.0}, TraderRules())
    assert out == ("stop", 97.0)          # close-only bar still respects the stop


# ── entry discipline ──────────────────────────────────────────────────────
def test_entry_requires_conviction_floor():
    r = TraderRules(min_conviction="high")
    assert not wants_entry({"sector": "semis", "direction": "bullish",
                            "confidence": "med"}, r, set())
    assert wants_entry({"sector": "semis", "direction": "bullish",
                        "confidence": "high"}, r, set())


def test_entry_refused_when_book_is_full_or_already_exposed():
    r = TraderRules(max_positions=2, min_conviction="low")
    call = {"sector": "semis", "direction": "bullish", "confidence": "high"}
    assert not wants_entry(call, r, {"a", "b"})            # full
    assert not wants_entry(call, r, {"semis"})             # already in it


def test_neutral_never_traded_and_bearish_needs_shorting_enabled():
    r = TraderRules(min_conviction="low")
    assert not wants_entry({"sector": "s", "direction": "neutral",
                            "confidence": "high"}, r, set())
    bear = {"sector": "s", "direction": "bearish", "confidence": "high"}
    assert not wants_entry(bear, r, set())
    assert wants_entry(bear, TraderRules(min_conviction="low", allow_short=True), set())


def test_costs_charged_per_side():
    assert apply_cost(10_000, TraderRules(cost_bps=20)) == 10.0   # half of 20bps


# ── the engine end to end ─────────────────────────────────────────────────
def test_winning_trade_hits_target_and_grows_equity():
    dates = ["d1", "d2"]
    calls = {"d1": [{"sector": "semis", "symbol": "X", "direction": "bullish",
                     "confidence": "high"}]}
    bars = {"X": {"d1": _bar(100, 100, 100, 100),      # entry at open 100
                  "d2": _bar(100, 107, 100, 107)}}     # target 106 hit
    out = engine.simulate(calls, bars, dates, TraderRules(cost_bps=0), 100_000)
    assert out["n_trades"] == 1
    assert out["trades"][0]["reason"] == "target"
    assert out["final_equity"] > 100_000
    assert out["win_rate"] == 1.0


def test_losing_trade_is_capped_near_the_configured_risk():
    dates = ["d1", "d2"]
    calls = {"d1": [{"sector": "semis", "symbol": "X", "direction": "bullish",
                     "confidence": "high"}]}
    bars = {"X": {"d1": _bar(100, 100, 100, 100),
                  "d2": _bar(100, 100, 90, 92)}}       # gaps through the stop
    r = TraderRules(cost_bps=0, risk_per_trade_pct=2.0, stop_loss_pct=3.0,
                    max_position_pct=100)
    out = engine.simulate(calls, bars, dates, r, 100_000)
    assert out["trades"][0]["reason"] == "stop"
    loss_pct = (out["final_equity"] / 100_000 - 1) * 100
    assert -2.5 < loss_pct < -1.5                       # ~the 2% risked


def test_position_limit_is_respected_across_sectors():
    dates = ["d1"]
    calls = {"d1": [{"sector": s, "symbol": s, "direction": "bullish",
                     "confidence": "high"} for s in ("a", "b", "c", "d")]}
    bars = {s: {"d1": _bar(10, 10, 10, 10)} for s in ("a", "b", "c", "d")}
    out = engine.simulate(calls, bars, dates, TraderRules(max_positions=2), 100_000)
    # 4 calls, cap of 2 -> only 2 positions opened (both closed at end-of-test)
    assert out["n_trades"] == 2


def test_low_conviction_calls_are_ignored_entirely():
    dates = ["d1", "d2"]
    calls = {"d1": [{"sector": "semis", "symbol": "X", "direction": "bullish",
                     "confidence": "low"}]}
    bars = {"X": {"d1": _bar(100, 100, 100, 100), "d2": _bar(100, 120, 100, 120)}}
    out = engine.simulate(calls, bars, dates, TraderRules(min_conviction="med"), 100_000)
    assert out["n_trades"] == 0 and out["final_equity"] == 100_000


def test_buy_and_hold_benchmark_is_computed():
    dates = ["d1", "d2"]
    bars = {"X": {"d1": _bar(100, 100, 100, 100), "d2": _bar(100, 100, 100, 110)}}
    assert round(engine.buy_and_hold(bars, ["X"], dates, 100_000)) == 110_000


def test_report_renders_and_states_the_verdict():
    dates = ["d1", "d2"]
    calls = {"d1": [{"sector": "semis", "symbol": "X", "direction": "bullish",
                     "confidence": "high"}]}
    bars = {"X": {"d1": _bar(100, 100, 100, 100), "d2": _bar(100, 107, 100, 107)}}
    text = engine.format_report(engine.simulate(calls, bars, dates, TraderRules(), 100_000))
    assert "trader simulation" in text and "buy & hold" in text
    assert "Not investment advice" in text


# ── benchmark on an unbalanced panel ──────────────────────────────────────
def test_buy_and_hold_buys_each_symbol_at_its_own_first_bar():
    """With full history the window opens in 1990 and no sector ETF existed yet.
    Requiring a bar on day one made every symbol unusable, so the benchmark
    returned the starting cash, printed +0.00%, and the report declared victory
    over it."""
    from oracle.simulator.engine import buy_and_hold

    dates = ["d1", "d2", "d3"]
    bars = {
        "old": {"d1": {"c": 100.0}, "d2": {"c": 110.0}, "d3": {"c": 120.0}},
        "new": {"d2": {"c": 50.0}, "d3": {"c": 100.0}},      # lists on d2
    }
    bh = buy_and_hold(bars, ["old", "new"], dates, 100_000.0)
    # old: 120/100 = 1.2x on 50k; new: 100/50 = 2.0x on 50k
    assert bh == 50_000 * 1.2 + 50_000 * 2.0


def test_buy_and_hold_reports_none_when_nothing_spans_the_window():
    from oracle.simulator.engine import buy_and_hold

    assert buy_and_hold({}, ["x"], ["d1", "d2"]) is None
    assert buy_and_hold({"x": {"d1": {"c": 1.0}}}, ["x"], ["d1", "d2"]) is None
    assert buy_and_hold({"x": {"d1": {"c": 1.0}}}, ["x"], []) is None


def test_report_says_na_rather_than_claiming_a_beat():
    from oracle.simulator import engine

    res = engine.simulate({}, {}, ["d1", "d2"], starting_cash=1000.0)
    assert res["beat_buy_and_hold"] is None, "cannot-compare must not read as lost"
    text = engine.format_report(res)
    assert "n/a" in text and "no benchmark" in text
