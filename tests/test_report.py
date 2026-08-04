"""Tests for the daily action report (what to lean toward / what to avoid)."""
import json
import tempfile

from oracle import config, db, report as rp


# ── pure merge ────────────────────────────────────────────────────────────
def _pred(sector, direction, confidence="med"):
    return {"sector": sector, "direction": direction, "confidence": confidence,
            "rationale": "rule note"}


def _call(sector, direction, conviction="high", drivers=("a", "b"), rationale="ai note"):
    return {"sector": sector, "direction": direction, "conviction": conviction,
            "key_drivers": json.dumps(list(drivers)), "rationale": rationale}


def test_merge_agreement_is_high_conviction_consider():
    m = rp.merge_sector("semis", _pred("semis", "bullish", "med"),
                        _call("semis", "bullish", "high"))
    assert m["stance"] == "consider"
    assert m["agree"] is True
    assert m["conviction"] == "high"            # stronger of the two
    assert m["rank"] == 4                        # high(3) + agreement bump
    assert m["source"] == "rule + AI agree"


def test_merge_disagreement_demotes_to_watch():
    m = rp.merge_sector("energy", _pred("energy", "bullish"),
                        _call("energy", "bearish"))
    assert m["consensus"] is None
    assert m["stance"] == "watch"
    assert m["agree"] is False


def test_merge_bearish_consensus_is_avoid():
    m = rp.merge_sector("financials", _pred("financials", "bearish"),
                        _call("financials", "bearish", "med"))
    assert m["stance"] == "avoid"


def test_merge_llm_only_and_rule_only():
    llm_only = rp.merge_sector("growth", None, _call("growth", "bullish", "med"))
    assert llm_only["stance"] == "consider" and llm_only["source"] == "AI analyst"
    rule_only = rp.merge_sector("broad", _pred("broad", "bearish"), None)
    assert rule_only["stance"] == "avoid" and rule_only["source"] == "rule-based"


def test_merge_neutral_is_watch_and_empty_is_none():
    assert rp.merge_sector("broad", _pred("broad", "neutral"), None)["stance"] == "watch"
    assert rp.merge_sector("broad", None, None) is None


# ── report assembly ───────────────────────────────────────────────────────
def test_build_report_buckets_and_sorting():
    preds = [_pred("semis", "bullish", "high"), _pred("energy", "bearish"),
             _pred("growth", "bullish", "low"), _pred("broad", "neutral")]
    calls = [_call("semis", "bullish", "high"), _call("energy", "bearish"),
             _call("growth", "bearish")]  # growth: rule bull vs AI bear -> mixed
    rep = rp.build_report("2026-08-04", preds, calls, us_rows=[
        {"pct_change": 1.2}, {"pct_change": -0.3}])
    assert [m["sector"] for m in rep["consider"]] == ["semis"]     # only clean bull
    assert [m["sector"] for m in rep["avoid"]] == ["energy"]
    assert {m["sector"] for m in rep["watch"]} == {"growth", "broad"}  # mixed + neutral
    assert rep["analyst_enabled"] is True
    assert "higher" in rep["us_summary"]


def test_format_markdown_has_sections_and_disclaimer():
    rep = rp.build_report("2026-08-04", [_pred("semis", "bullish")], [], us_rows=[])
    md = rp.format_markdown(rep)
    assert "daily outlook (2026-08-04)" in md
    assert "Leaning constructive" in md and "Leaning cautious" in md
    assert config.DISCLAIMER in md
    # AI analyst off -> the enablement hint shows
    assert "AI analyst is **not enabled**" in md


def test_format_markdown_no_hint_when_analyst_enabled():
    rep = rp.build_report("2026-08-04", [_pred("semis", "bullish")],
                          [_call("semis", "bullish")], us_rows=[])
    assert "not enabled" not in rp.format_markdown(rep)


# ── end-to-end over a seeded DB ────────────────────────────────────────────
def test_run_report_end_to_end(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    db.upsert_market_close("us_close", [
        {"trade_date": "2026-08-04", "symbol": "SOXX", "sector": "semis",
         "close": 100.0, "pct_change": 2.4, "fetched_at": "t"}], db_path=tmp)
    db.upsert_prediction({
        "trade_date": "2026-08-04", "sector": "semis", "direction": "bullish",
        "confidence": "high", "composite_score": 0.7, "us_spillover": 0.6,
        "sentiment_score": 0.2, "macro_flag": 0, "rationale": "r", "created_at": "t"},
        db_path=tmp)
    db.upsert_llm_call({
        "trade_date": "2026-08-04", "sector": "semis", "provider": "deepseek",
        "model": "deepseek-chat", "direction": "bullish", "conviction": "high",
        "tradeable_etf": "KWEB", "key_drivers": json.dumps(["SOXX +2.4%"]),
        "rationale": "chips strong", "created_at": "t"}, db_path=tmp)

    rep = rp.run_report("2026-08-04", db_path=tmp)
    assert [m["sector"] for m in rep["consider"]] == ["semis"]
    md = rp.format_markdown(rep)
    assert "Semiconductors" in md and "KWEB" in md
