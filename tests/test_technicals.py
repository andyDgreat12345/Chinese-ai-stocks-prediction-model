"""Tests for the technical-indicator engine + its wiring into report/analyst."""
import tempfile

from oracle import config, db, report as rp
from oracle.analysis import technicals as ta


# ── pure indicators ───────────────────────────────────────────────────────
def test_sma_ema_none_when_short():
    assert ta.sma([1, 2], 5) is None
    assert ta.ema([1, 2], 5) is None
    assert ta.sma([1, 2, 3, 4, 5], 5) == 3.0


def test_rsi_all_gains_is_100_all_losses_low():
    up = list(range(1, 40))                 # strictly rising
    assert ta.rsi(up) == 100.0
    down = list(range(40, 1, -1))           # strictly falling
    assert ta.rsi(down) is not None and ta.rsi(down) < 5


def test_rsi_none_when_insufficient():
    assert ta.rsi([1, 2, 3], 14) is None


def test_momentum_percent():
    vals = [100.0] * 10 + [110.0]           # +10% over the last 10 steps
    assert ta.momentum(vals, 10) == 10.0


def test_macd_hist_sign_tracks_trend():
    # An *accelerating* uptrend gives a positive histogram (bullish momentum);
    # a pure linear ramp is the degenerate ~0 case, so use curvature here.
    accelerating = [1.02 ** i for i in range(60)]
    m = ta.macd(accelerating)
    assert m is not None and m["macd"] > 0 and m["hist"] > 0
    # an accelerating *decline* flips the histogram negative
    declining = [1000.0 - 1.02 ** i for i in range(60)]
    assert ta.macd(declining)["hist"] < 0


def test_compute_indicators_summary_uptrend():
    rising = [float(i) for i in range(1, 80)]
    ind = ta.compute_indicators(rising)
    assert ind["trend"] == "uptrend"
    assert ind["rsi_state"] in ("overbought", "neutral")
    assert "RSI" in ind["technical_note"]


def test_compute_indicators_insufficient():
    assert ta.compute_indicators([100.0])["technical_note"] == "insufficient history"


# ── DB series + report wiring ─────────────────────────────────────────────
def test_close_series_is_chronological_and_capped(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    for i, d in enumerate(["2026-07-01", "2026-07-02", "2026-07-03"]):
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "512480", "sector": "semis",
             "close": 10.0 + i, "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
    s = db.close_series("china_close", symbol="512480", limit=120, db_path=tmp)
    assert [r["close"] for r in s] == [10.0, 11.0, 12.0]        # oldest → newest
    # `end` caps to on-or-before the date
    s2 = db.close_series("china_close", symbol="512480", end="2026-07-02", db_path=tmp)
    assert [r["close"] for r in s2] == [10.0, 11.0]


def test_report_includes_technical_note(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    # seed ~40 rising days of semis history + a prediction for the latest date
    dates = [f"2026-06-{d:02d}" for d in range(1, 29)] + [f"2026-07-{d:02d}" for d in range(1, 13)]
    for i, d in enumerate(dates):
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "512480", "sector": "semis",
             "close": 10.0 + i * 0.2, "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
    last = dates[-1]
    db.upsert_prediction({
        "trade_date": last, "sector": "semis", "direction": "bullish",
        "confidence": "high", "composite_score": 0.6, "us_spillover": 0.5,
        "sentiment_score": 0.1, "macro_flag": 0, "rationale": "r", "created_at": "t"},
        db_path=tmp)
    rep = rp.run_report(last, db_path=tmp)
    md = rp.format_markdown(rep)
    assert "technicals:" in md and "RSI" in md
