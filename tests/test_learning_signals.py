"""Technical signals as learnable features + the no-lookahead guarantee."""
import tempfile

from oracle import config, db
from oracle.analysis import technicals as ta
from oracle.analysis.scoring import SectorSignals, score_sector
from oracle.learning import walkforward as wf


# ── indicators → normalized model signals ─────────────────────────────────
def test_rsi_signal_is_mean_reverting():
    # oversold (low RSI) should read BULLISH; overbought bearish.
    assert ta.technical_signals({"rsi": 20})["rsi_signal"] > 0
    assert ta.technical_signals({"rsi": 80})["rsi_signal"] < 0
    assert ta.technical_signals({"rsi": 50})["rsi_signal"] == 0


def test_momentum_signal_is_trend_following():
    assert ta.technical_signals({"momentum_10d": 8})["momentum_signal"] > 0
    assert ta.technical_signals({"momentum_10d": -8})["momentum_signal"] < 0


def test_signals_are_clamped_and_default_to_zero():
    s = ta.technical_signals({"rsi": 0, "momentum_10d": 999, "trend": "uptrend"})
    assert s["rsi_signal"] <= 1.0 and s["momentum_signal"] <= 1.0
    assert s["trend_signal"] == 1.0
    blank = ta.technical_signals({})       # nothing computable -> contributes nothing
    assert blank == {"rsi_signal": 0.0, "momentum_signal": 0.0, "trend_signal": 0.0}


# ── the scorer can weight them, and old 3-signal weights are unchanged ────
def test_score_sector_weights_technical_signals():
    sig = SectorSignals(us_spillover=0.0, sentiment=0.0, rsi_signal=1.0)
    w = {"us_spillover": 0.0, "sentiment": 0.0, "macro": 0.0, "rsi_signal": 1.0}
    assert score_sector(sig, w, 0.5).direction == "bullish"


def test_legacy_three_signal_weights_behave_identically():
    sig = SectorSignals(us_spillover=0.5, sentiment=0.5)
    legacy = {"us_spillover": 0.45, "sentiment": 0.35, "macro": 0.20}
    # technical fields default to 0 and carry no weight -> same composite as before
    assert score_sector(sig, legacy).composite == round(0.45 * 0.5 + 0.35 * 0.5, 4)


# ── learner picks up the new signals ──────────────────────────────────────
def test_live_signals_detects_technical_features():
    recs = [{"date": "d1", "us_spillover": 0.0, "sentiment": 0.0, "macro": 0.0,
             "rsi_signal": 0.4, "momentum_signal": 0.0, "trend_signal": 0.0}]
    assert wf.live_signals(recs) == {"rsi_signal"}


def test_sample_params_only_weights_live_signals():
    for p in wf.sample_params({"rsi_signal"}, n=5):
        assert p["us_spillover"] == 0.0 and p["sentiment"] == 0.0
        assert p["rsi_signal"] > 0


def test_sample_params_is_deterministic():
    a = wf.sample_params({"us_spillover", "rsi_signal"}, n=20)
    b = wf.sample_params({"us_spillover", "rsi_signal"}, n=20)
    assert a == b                       # reproducible proposals, auditable runs


def test_learner_finds_a_technical_signal_when_it_predicts():
    # rsi_signal perfectly predicts; us_spillover is noise pointing the other way.
    recs = []
    for i in range(150):
        s = 1.0 if i % 2 == 0 else -1.0
        recs.append({"date": f"2026-{1 + i // 30:02d}-{1 + i % 30:02d}",
                     "sector": "semis", "us_spillover": -s, "sentiment": 0.0,
                     "macro": 0.0, "rsi_signal": s, "momentum_signal": 0.0,
                     "trend_signal": 0.0, "actual_move": s * 2,
                     "actual_dir": "bullish" if s > 0 else "bearish"})
    best = wf.select_params(recs, n_folds=3)
    assert best is not None
    assert best["rsi_signal"] > best["us_spillover"]


# ── the lookahead guard (the highest-risk correctness property) ───────────
def test_close_series_before_excludes_the_target_day():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    for i, d in enumerate(["2026-07-01", "2026-07-02", "2026-07-03"]):
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "512480", "sector": "semis",
             "close": 10.0 + i, "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
    incl = db.close_series("china_close", symbol="512480", end="2026-07-02", db_path=tmp)
    excl = db.close_series("china_close", symbol="512480", before="2026-07-02", db_path=tmp)
    assert [r["close"] for r in incl] == [10.0, 11.0]     # end= includes the day
    assert [r["close"] for r in excl] == [10.0]           # before= excludes it


def test_backtest_technicals_never_use_the_target_days_close(monkeypatch):
    from oracle.backtest import _technicals_before

    # A flat history then a huge jump ON the target day. If the jump leaked in,
    # momentum/trend would move; using only prior closes they must not.
    hist = [(f"2026-07-{d:02d}", 10.0) for d in range(1, 21)]
    hist.append(("2026-07-21", 100.0))          # the target day's own close
    sigs = _technicals_before(hist, "2026-07-21")
    assert sigs["momentum_signal"] == 0.0       # flat prior history only
    # and including it would have changed the answer, proving the test bites
    leaked = ta.signals_from_closes([c for _d, c in hist])
    assert leaked["momentum_signal"] != 0.0


def test_collect_records_carries_technical_signals(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    dates = [f"2026-06-{d:02d}" for d in range(1, 29)]
    for i, d in enumerate(dates):
        db.upsert_market_close("us_close", [
            {"trade_date": d, "symbol": "SOXX", "sector": "semis",
             "close": 100.0, "pct_change": 1.0, "fetched_at": "t"}], db_path=tmp)
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "512480", "sector": "semis",
             "close": 10.0 + i * 0.3, "pct_change": 1.2, "fetched_at": "t"}], db_path=tmp)
    from oracle.backtest import collect_records
    recs = collect_records(db_path=tmp)
    assert recs
    assert all("rsi_signal" in r and "trend_signal" in r for r in recs)
    # later records have enough history for a real trend read
    assert any(r["trend_signal"] != 0.0 for r in recs)
