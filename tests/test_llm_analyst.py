"""Tests for the optional LLM research desk (pure functions + stubbed provider)."""
import json
import tempfile

from oracle import config, db
from oracle.analysis import llm_analyst as la


# ── pure: context + prompt ────────────────────────────────────────────────
def test_build_context_shapes_and_caps():
    us = [{"symbol": "SOXX", "sector": "semis", "pct_change": 2.0},
          {"symbol": "X", "sector": "semis", "pct_change": None}]  # null dropped
    news = [{"category": "chip_export", "sentiment": 0.5, "headline": f"h{i}"}
            for i in range(40)]  # capped to 25
    ctx = la.build_context("2026-08-03", us, news, [{"category": "cpi", "description": "CPI"}], [])
    assert ctx["trade_date"] == "2026-08-03"
    assert ctx["sectors"] == la.CHINA_SECTORS
    assert len(ctx["us_closes"]) == 1              # null-change row dropped
    assert len(ctx["news"]) == 25                  # capped
    assert ctx["sector_tradeable_etf"]["semis"] == config.SECTOR_TRADEABLE_ETF["semis"]


def test_prompt_includes_date_and_json():
    ctx = la.build_context("2026-08-03", [], [], [], [])
    p = la._prompt(ctx)
    assert "2026-08-03" in p
    assert "Inputs (JSON):" in p


# ── pure: parsing/validation ──────────────────────────────────────────────
def test_parse_calls_validates_and_fills_etf():
    raw = {"calls": [
        {"sector": "semis", "direction": "bullish", "conviction": "high",
         "key_drivers": ["US semis +2%", "positive chip news"], "rationale": "spillover"},
        {"sector": "energy", "direction": "sideways", "conviction": "low",  # bad direction
         "key_drivers": [], "rationale": "x"},
        {"sector": "not_a_sector", "direction": "bullish", "conviction": "low",
         "key_drivers": [], "rationale": "x"},   # unknown sector dropped
    ], "market_note": "n"}
    calls = la.parse_calls(raw, "2026-08-03")
    assert [c["sector"] for c in calls] == ["semis"]      # only the valid, known one
    assert calls[0]["tradeable_etf"] == config.SECTOR_TRADEABLE_ETF["semis"]
    assert calls[0]["key_drivers"] == ["US semis +2%", "positive chip news"]


def test_parse_calls_dedups_and_caps_drivers():
    raw = {"calls": [
        {"sector": "broad", "direction": "bearish", "conviction": "med",
         "key_drivers": [f"d{i}" for i in range(10)], "rationale": "r"},
        {"sector": "broad", "direction": "bullish", "conviction": "high",  # dup -> ignored
         "key_drivers": [], "rationale": "r2"},
    ]}
    calls = la.parse_calls(raw, "2026-08-03")
    assert len(calls) == 1 and calls[0]["direction"] == "bearish"
    assert len(calls[0]["key_drivers"]) == 6              # capped at 6


# ── env gating ─────────────────────────────────────────────────────────────
def test_get_analyst_llm_disabled_without_provider(monkeypatch):
    monkeypatch.delenv("ORACLE_ANALYST_PROVIDER", raising=False)
    assert la.get_analyst_llm() is None


def test_get_analyst_llm_deepseek_needs_key(monkeypatch):
    monkeypatch.setenv("ORACLE_ANALYST_PROVIDER", "deepseek")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert la.get_analyst_llm() is None
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    fn, provider, model = la.get_analyst_llm()
    assert provider == "deepseek" and model == "deepseek-chat"


# ── job: no-op when unconfigured, persists when a provider is given ─────────
def test_run_llm_analysis_noop_when_unconfigured(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    monkeypatch.delenv("ORACLE_ANALYST_PROVIDER", raising=False)
    db.init_db(tmp)
    assert la.run_llm_analysis("2026-08-03") == 0        # skipped, no crash


def test_run_llm_analysis_persists_stubbed_calls(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    db.upsert_market_close("us_close", [
        {"trade_date": "2026-08-03", "symbol": "SOXX", "sector": "semis",
         "close": 100.0, "pct_change": 2.0, "fetched_at": "t"}], db_path=tmp)

    # Stub the provider: return a fixed structured analysis (no network).
    def fake_llm(ctx):
        assert ctx["trade_date"] == "2026-08-03"          # gets the real context
        return {"calls": [
            {"sector": "semis", "direction": "bullish", "conviction": "high",
             "key_drivers": ["US semis rallied"], "rationale": "overnight spillover"},
            {"sector": "broad", "direction": "neutral", "conviction": "low",
             "key_drivers": [], "rationale": "mixed"},
        ], "market_note": "risk-on"}

    n = la.run_llm_analysis("2026-08-03", llm=fake_llm)
    assert n == 2
    rows = db.llm_calls_for_date("2026-08-03", db_path=tmp)
    by_sector = {r["sector"]: r for r in rows}
    assert by_sector["semis"]["direction"] == "bullish"
    assert by_sector["semis"]["tradeable_etf"] == config.SECTOR_TRADEABLE_ETF["semis"]
    assert json.loads(by_sector["semis"]["key_drivers"]) == ["US semis rallied"]
    assert by_sector["semis"]["provider"] == "test"


def test_run_llm_analysis_failsoft_on_provider_error(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)

    def boom(ctx):
        raise RuntimeError("api down")

    assert la.run_llm_analysis("2026-08-03", llm=boom) == 0   # swallowed, no crash
