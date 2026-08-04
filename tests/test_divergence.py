"""Tests for the US-follows-vs-diverges classifier."""
import tempfile

from oracle import config, db
from oracle.analysis import divergence as dv


# ── pure labelling ────────────────────────────────────────────────────────
def test_classify_needs_min_sample(monkeypatch):
    monkeypatch.setattr(config, "MIN_CORRELATION_SAMPLE", 30)
    out = dv.classify_pairs([0.1, 0.2, -0.1], [0.2, 0.3, -0.2])   # only 3 points
    assert out["label"] == "insufficient data" and out["n"] == 3


def test_classify_follows_when_positively_correlated(monkeypatch):
    monkeypatch.setattr(config, "MIN_CORRELATION_SAMPLE", 10)
    xs = [i / 10 for i in range(-15, 15)]
    ys = [x * 2 for x in xs]                     # perfectly positively correlated
    out = dv.classify_pairs(xs, ys)
    assert out["label"] == "follows US" and out["r"] == 1.0


def test_classify_diverges_when_negatively_correlated(monkeypatch):
    monkeypatch.setattr(config, "MIN_CORRELATION_SAMPLE", 10)
    xs = [i / 10 for i in range(-15, 15)]
    ys = [-x for x in xs]                        # anti-correlated → diverges
    out = dv.classify_pairs(xs, ys)
    assert out["label"] == "diverges from US" and out["r"] == -1.0


def test_summary_line_formats():
    assert dv.summary_line({"label": "follows US", "r": 0.61, "n": 90}) == \
        "follows US (r=0.61, n=90)"
    assert dv.summary_line({"label": "insufficient data", "r": None, "n": 4}) == \
        "insufficient data (n=4)"


# ── end-to-end over a seeded DB ───────────────────────────────────────────
def test_classify_sectors_over_history(monkeypatch):
    monkeypatch.setattr(config, "MIN_CORRELATION_SAMPLE", 3)
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    # semis: US SOXX up ⇒ China semis up the same day (a "follows" relationship).
    days = [("2026-08-01", 2.0, 2.2), ("2026-08-02", -1.5, -1.4),
            ("2026-08-03", 1.0, 1.1), ("2026-08-04", -0.8, -0.9)]
    for d, us_pct, cn_pct in days:
        db.upsert_market_close("us_close", [
            {"trade_date": d, "symbol": "SOXX", "sector": "semis",
             "close": 100.0, "pct_change": us_pct, "fetched_at": "t"}], db_path=tmp)
        db.upsert_market_close("china_close", [
            {"trade_date": d, "symbol": "512480", "sector": "semis",
             "close": 1.0, "pct_change": cn_pct, "fetched_at": "t"}], db_path=tmp)
    out = dv.classify_sectors(db_path=tmp)
    assert out["semis"]["label"] == "follows US"
    assert out["semis"]["n"] == 4
