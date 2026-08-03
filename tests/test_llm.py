"""Tests for the optional reflection-LLM adapter (spec §4b-iii).

No real API calls — provider resolution is env-gated and generate_reflection is
driven with stub callables.
"""
import tempfile

from oracle import config, db
from oracle.reflection import llm as llm_mod
from oracle.reflection.reflect import generate_reflection


def test_no_provider_returns_none(monkeypatch):
    monkeypatch.delenv("ORACLE_LLM_PROVIDER", raising=False)
    assert llm_mod.get_reflection_llm() is None


def test_claude_without_key_returns_none(monkeypatch):
    monkeypatch.setenv("ORACLE_LLM_PROVIDER", "claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm_mod.get_reflection_llm() is None


def test_deepseek_with_key_returns_callable(monkeypatch):
    monkeypatch.setenv("ORACLE_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert callable(llm_mod.get_reflection_llm())


def test_assemble_keeps_facts_and_takes_interpretation():
    ctx = {
        "trade_date": "2026-08-02",
        "rows": [{"sector": "broad", "predicted": "bullish", "actual": "bearish"}],
        "agree": {}, "disagree": {},
    }
    interp = {
        "signals_that_worked": ["sentiment"],
        "signals_that_missed": ["us_spillover"],
        "likely_reason_for_miss": "domestic policy",
        "suggested_weight_adjustment": {"signal": "us_spillover", "direction": "decrease", "magnitude": "small"},
        "confidence_in_this_reflection": "med",
    }
    out = llm_mod._assemble(ctx, interp)
    # facts come from ctx, not the model
    assert out["predicted"] == {"broad": "bullish"}
    assert out["actual"] == {"broad": "bearish"}
    # interpretation comes from the model
    assert out["signals_that_missed"] == ["us_spillover"]
    assert out["confidence_in_this_reflection"] == "med"


def _seed(tmp):
    db.init_db(tmp)
    db.upsert_market_close("china_close", [
        {"trade_date": "2026-08-02", "symbol": "sh000001", "sector": "broad",
         "close": 100.0, "pct_change": 1.5, "fetched_at": "t"}], db_path=tmp)
    db.upsert_prediction({
        "trade_date": "2026-08-02", "sector": "broad", "direction": "bullish",
        "confidence": "high", "composite_score": 0.5, "us_spillover": 0.6,
        "sentiment_score": 0.3, "macro_flag": 0, "rationale": "r", "created_at": "t",
    }, db_path=tmp)


def test_generate_reflection_uses_explicit_llm(monkeypatch, tmp_path):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    monkeypatch.setattr(config, "REFLECTION_LOG", tmp_path / "r.jsonl")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _seed(tmp)

    def fake_llm(ctx):
        return {**{"date": ctx["trade_date"], "predicted": {}, "actual": {}},
                "signals_that_worked": ["LLM-MARKER"], "signals_that_missed": [],
                "likely_reason_for_miss": "from llm",
                "suggested_weight_adjustment": {"signal": "sentiment", "direction": "increase", "magnitude": "small"},
                "confidence_in_this_reflection": "high"}

    refl = generate_reflection("2026-08-02", llm=fake_llm)
    assert refl["signals_that_worked"] == ["LLM-MARKER"]  # llm output used


def test_generate_reflection_falls_back_when_llm_raises(monkeypatch, tmp_path):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    monkeypatch.setattr(config, "REFLECTION_LOG", tmp_path / "r.jsonl")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    _seed(tmp)

    def broken_llm(ctx):
        raise RuntimeError("api down")

    refl = generate_reflection("2026-08-02", llm=broken_llm)
    # rule-based fallback still produced a valid reflection
    assert refl is not None
    assert "confidence_in_this_reflection" in refl
