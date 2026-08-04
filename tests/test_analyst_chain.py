"""Tests for the multi-pass reasoning chain (pure orchestration + integration)."""
import tempfile

from oracle import config, db
from oracle.analysis import analyst_chain as ch, llm_analyst as la


def _ctx():
    return {"trade_date": "2026-08-04", "sectors": ["semis", "energy"],
            "sector_tradeable_etf": {"semis": "KWEB", "energy": "FXI"},
            "us_closes": [{"symbol": "SOXX", "pct_change": 2.0}], "news": [],
            "macro_events": [], "web_research": [{"title": "chips", "snippet": "rally"}],
            "recent_performance": []}


def _usage(n=100):
    return {"prompt_tokens": n, "completion_tokens": n // 2, "cached_tokens": 0,
            "total_tokens": n + n // 2}


def _staged_complete(record):
    """A stub completer that returns a canned output per pass, keyed off the
    system prompt, and records (system-tag, model) calls."""
    def complete(system, user, model):
        if "chief strategist" in system:
            record.append(("thesis", model))
            return {"regime": "risk-on", "thesis": "US risk-on spills over.",
                    "key_risks": ["gap"], "sector_bias": {"semis": "constructive"}}, _usage(120)
        if "sector analyst" in system:
            record.append(("deepdive", model))
            return {"calls": [{"sector": "semis", "direction": "bullish", "conviction": "high",
                               "bull_case": "b", "bear_case": "r", "key_drivers": ["SOXX +2%"],
                               "rationale": "spillover"}]}, _usage(200)
        record.append(("risk", model))
        return {"calls": [{"sector": "semis", "direction": "bullish", "conviction": "med",
                           "key_drivers": ["SOXX +2%"], "rationale": "spillover, gap risk"}],
                "market_note": "constructive but gappy"}, _usage(80)
    return complete


# ── pure prompt builders ──────────────────────────────────────────────────
def test_prompts_carry_the_right_material():
    ctx = _ctx()
    assert "2026-08-04" in ch.thesis_prompt(ctx) and "web_research" in ch.thesis_prompt(ctx)
    dd = ch.deepdive_prompt(ctx, {"regime": "risk-on"})
    assert "risk-on" in dd and "KWEB" in dd
    rk = ch.risk_prompt(ctx, {"regime": "risk-on"}, {"calls": []})
    assert "FINAL" in rk


# ── orchestration ─────────────────────────────────────────────────────────
def test_run_chain_three_passes_and_models():
    record = []
    final, usages = ch.run_chain(_ctx(), _staged_complete(record),
                                 work_model="deepseek-chat", thesis_model="deepseek-reasoner")
    # three passes, thesis on the reasoner model, work passes on the chat model
    assert [r[0] for r in record] == ["thesis", "deepdive", "risk"]
    assert record[0][1] == "deepseek-reasoner"
    assert record[1][1] == "deepseek-chat" and record[2][1] == "deepseek-chat"
    # final is the canonical shape from the risk pass
    assert final["calls"][0]["sector"] == "semis"
    assert final["market_note"] == "constructive but gappy"
    assert [u["pass"] for u in usages] == ["thesis", "deepdive", "risk"]


def test_run_chain_backfills_market_note_from_thesis():
    def complete(system, user, model):
        if "chief strategist" in system:
            return {"thesis": "regime read"}, _usage()
        if "sector analyst" in system:
            return {"calls": []}, _usage()
        return {"calls": []}, _usage()   # risk pass omits market_note
    final, _ = ch.run_chain(_ctx(), complete, work_model="m")
    assert final["market_note"] == "regime read"


# ── integration through run_llm_analysis ──────────────────────────────────
def test_run_llm_analysis_chain_mode_persists_and_meters(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    db.upsert_market_close("us_close", [
        {"trade_date": "2026-08-04", "symbol": "SOXX", "sector": "semis",
         "close": 100.0, "pct_change": 2.0, "fetched_at": "t"}], db_path=tmp)

    n = la.run_llm_analysis("2026-08-04", complete=_staged_complete([]))
    assert n == 1
    assert db.llm_calls_for_date("2026-08-04", db_path=tmp)[0]["direction"] == "bullish"

    # all three passes metered as distinct call types
    s = db.llm_usage_summary(tmp)
    assert s["all_time"]["calls"] == 3
    call_types = {row["model"] for row in db.connect(tmp).execute(
        "SELECT model FROM llm_usage")}   # model recorded per pass
    # and the call_type column carries the pass name
    types = {r["call_type"] for r in db.connect(tmp).execute("SELECT call_type FROM llm_usage")}
    assert types == {"analyst-thesis", "analyst-deepdive", "analyst-risk"}
