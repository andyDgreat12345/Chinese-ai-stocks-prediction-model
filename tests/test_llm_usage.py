"""Tests for the DeepSeek/Claude token + cost meter."""
import tempfile

from oracle import config, db, usage
from oracle.analysis import llm_analyst as la, pricing


# ── pure cost math ────────────────────────────────────────────────────────
def test_cost_splits_cache_hit_and_miss():
    # deepseek-chat: input 0.27, cache_hit 0.07, output 1.10 per 1M
    # 1,000,000 prompt (400k cached) + 500,000 completion
    c = pricing.cost_usd("deepseek-chat", 1_000_000, 500_000, cached_tokens=400_000)
    # 600k miss*0.27 + 400k hit*0.07 + 500k out*1.10 = 0.162 + 0.028 + 0.55
    assert round(c, 6) == round(0.162 + 0.028 + 0.55, 6)


def test_cost_unknown_model_uses_fallback():
    c = pricing.cost_usd("mystery-model", 1_000_000, 0)
    assert c == round(config.LLM_PRICE_FALLBACK["input"], 6)


def test_cost_caps_cached_at_prompt():
    # cached can't exceed prompt tokens
    c = pricing.cost_usd("deepseek-chat", 100, 0, cached_tokens=10_000)
    assert c == pricing.cost_usd("deepseek-chat", 100, 0, cached_tokens=100)


# ── analyst records usage when the backend reports it ─────────────────────
def test_run_llm_analysis_records_usage(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)

    def fake_llm_with_usage(ctx):
        parsed = {"calls": [
            {"sector": "semis", "direction": "bullish", "conviction": "high",
             "key_drivers": ["x"], "rationale": "r"}], "market_note": "n"}
        usage = {"prompt_tokens": 1000, "completion_tokens": 500,
                 "cached_tokens": 200, "total_tokens": 1500}
        return parsed, usage

    n = la.run_llm_analysis("2026-08-03", llm=fake_llm_with_usage)
    assert n == 1
    s = db.llm_usage_summary(tmp)
    assert s["all_time"]["calls"] == 1
    assert s["all_time"]["tokens"] == 1500
    # fallback rates (model "test"): 800*0.30 + 200*0.08 + 500*1.20 per 1M
    expected = round((800 * 0.30 + 200 * 0.08 + 500 * 1.20) / 1e6, 6)
    assert s["all_time"]["cost_usd"] == expected


def test_run_llm_analysis_plain_dict_records_no_usage(monkeypatch):
    # A stub returning just the parsed dict (no usage) must still persist calls
    # but record zero usage rows — back-compat with the older contract.
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    la.run_llm_analysis("2026-08-03", llm=lambda ctx: {"calls": [
        {"sector": "broad", "direction": "neutral", "conviction": "low",
         "key_drivers": [], "rationale": "r"}], "market_note": "n"})
    assert db.llm_usage_summary(tmp)["all_time"]["calls"] == 0


# ── summary + formatting ──────────────────────────────────────────────────
def test_usage_summary_windows_and_by_model(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    db.record_llm_usage({
        "trade_date": "2026-08-03", "call_type": "analyst", "provider": "deepseek",
        "model": "deepseek-chat", "prompt_tokens": 100, "completion_tokens": 50,
        "cached_tokens": 0, "total_tokens": 150, "cost_usd": 0.001,
        "created_at": "2020-01-01T00:00:00+00:00"}, db_path=tmp)  # old -> not in 7d
    s = db.llm_usage_summary(tmp)
    assert s["all_time"]["calls"] == 1
    assert s["today"]["calls"] == 0            # created long ago
    assert s["by_model"][0]["model"] == "deepseek-chat"
    text = usage.format_usage(s)
    assert "AI research spend" in text and "deepseek-chat" in text
