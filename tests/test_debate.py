"""Tests for the adversarial analyst pass and its head-to-head scoring."""
import tempfile

from oracle import db
from oracle.analysis import debate as dbt
from oracle.analysis import variant_scoring as vs


# ── prompt construction ──────────────────────────────────────────────────
def test_bear_must_see_the_bull_argument():
    """A counter-case that never read the case isn't a rebuttal."""
    p = dbt.bear_prompt("CONTEXT", "semis look strong on the SOXX move")
    assert "CONTEXT" in p
    assert "semis look strong on the SOXX move" in p


def test_synthesis_sees_both_sides():
    p = dbt.synthesis_prompt("CONTEXT", "BULLCASE", "BEARCASE")
    assert "BULLCASE" in p and "BEARCASE" in p and "CONTEXT" in p


def test_synthesis_preserves_the_single_pass_contract_verbatim():
    """If the two variants asked for different output shapes, a measured
    difference in accuracy could be the shape rather than the reasoning."""
    contract = "Return STRICT JSON with exactly this shape: {...}"
    sys_prompt = dbt.synthesis_system(contract)
    assert sys_prompt.endswith(contract)
    assert len(sys_prompt) > len(contract)      # framing was prepended, not swapped


def test_long_arguments_are_clipped():
    long = "x" * 9000
    assert len(dbt.clip(long)) < 4000
    assert dbt.clip("short") == "short"
    assert dbt.clip(None) == ""


# ── the three turns ──────────────────────────────────────────────────────
def test_run_debate_runs_three_turns_in_order():
    seen = []

    def complete(system, user, model):
        seen.append(system)
        return {"calls": [], "text": f"argued-{len(seen)}"}, {"total_tokens": 10}

    parsed, usages, transcript = dbt.run_debate("CTX", "CONTRACT", complete, "m")
    assert len(seen) == 3
    assert seen[0] == dbt.BULL_SYSTEM
    assert seen[1] == dbt.BEAR_SYSTEM
    assert seen[2].endswith("CONTRACT")
    assert [u["call_type"] for u in usages] == [
        "analyst-debate-bull", "analyst-debate-bear", "analyst-debate-synthesis"]
    assert transcript["bull"] and transcript["bear"]
    assert "calls" in parsed


def test_only_the_synthesis_produces_calls():
    """The advocates argue; they never vote. Recording an advocacy turn as a call
    would put a deliberately one-sided view into the scored record."""
    calls_seen = []

    def complete(system, user, model):
        calls_seen.append(system)
        return {"calls": [{"sector": "semis", "direction": "bullish"}]}, {}

    parsed, _u, _t = dbt.run_debate("CTX", "CONTRACT", complete, "m")
    # run_debate returns exactly one parsed payload — the last turn's.
    assert isinstance(parsed, dict)
    assert len(calls_seen) == 3


def test_prose_wrapped_in_json_is_still_readable():
    assert dbt._as_text({"text": "hello"}) == "hello"
    assert dbt._as_text("plain") == "plain"
    assert dbt._as_text(None) == ""


# ── persistence: both variants must survive ──────────────────────────────
def _call(date, sector, direction, variant):
    return {"trade_date": date, "sector": sector, "provider": "p", "model": "m",
            "direction": direction, "conviction": "med", "tradeable_etf": "KWEB",
            "key_drivers": "[]", "rationale": "r", "variant": variant,
            "created_at": "t"}


def test_both_variants_coexist_for_one_session():
    """Regression: llm_calls was uniquely keyed on (trade_date, sector), so the
    debate would have overwritten the single pass and the comparison could never
    have been made."""
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    db.upsert_llm_call(_call("2026-08-10", "semis", "bullish", "single"), tmp)
    db.upsert_llm_call(_call("2026-08-10", "semis", "bearish", "debate"), tmp)

    conn = db.connect(tmp)
    rows = conn.execute("SELECT variant, direction FROM llm_calls "
                        "WHERE trade_date='2026-08-10' AND sector='semis'").fetchall()
    got = {r["variant"]: r["direction"] for r in rows}
    assert got == {"single": "bullish", "debate": "bearish"}


def test_variant_defaults_to_single_for_callers_that_omit_it():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    row = _call("2026-08-10", "semis", "bullish", "single")
    row.pop("variant")
    db.upsert_llm_call(row, tmp)
    conn = db.connect(tmp)
    assert conn.execute("SELECT variant FROM llm_calls").fetchone()["variant"] == "single"


# ── head-to-head scoring ─────────────────────────────────────────────────
def test_only_paired_sessions_are_scored():
    """If one variant abstains on the hard days, comparing raw hit rates rewards
    the abstention rather than the reasoning."""
    calls = {
        ("d1", "semis"): {"single": "bullish", "debate": "bullish"},
        ("d2", "semis"): {"single": "bearish"},                     # unpaired
        ("d3", "semis"): {"debate": "bullish"},                     # unpaired
    }
    actuals = {("d1", "semis"): "bullish", ("d2", "semis"): "bearish",
               ("d3", "semis"): "bearish"}
    res = vs.compare(calls, actuals)
    assert res["paired"] == 1
    assert res["stats"]["single"]["n"] == 1
    assert res["stats"]["debate"]["n"] == 1


def test_disagreements_are_counted_and_attributed():
    calls = {
        ("d1", "s"): {"single": "bullish", "debate": "bearish"},   # debate right
        ("d2", "s"): {"single": "bullish", "debate": "bullish"},   # agree
        ("d3", "s"): {"single": "bearish", "debate": "bullish"},   # single right
    }
    actuals = {("d1", "s"): "bearish", ("d2", "s"): "bullish", ("d3", "s"): "bearish"}
    res = vs.compare(calls, actuals)
    assert res["disagreements"] == 2
    assert res["challenger_won_disagreements"] == 1


def test_verdict_needs_enough_paired_calls_before_concluding():
    calls = {("d1", "s"): {"single": "bullish", "debate": "bullish"}}
    res = vs.compare(calls, {("d1", "s"): "bullish"})
    assert res["verdict"] == "insufficient"
    assert "not yet decidable" in vs.format_report(res)


def test_a_challenger_that_does_not_clear_the_bar_is_deleted(monkeypatch):
    """Same bar the parameter learner demands of any change."""
    monkeypatch.setattr(vs, "MIN_PAIRED_CALLS", 4)
    calls, actuals = {}, {}
    for i in range(8):
        k = (f"d{i}", "s")
        calls[k] = {"single": "bullish", "debate": "bullish"}
        actuals[k] = "bullish" if i < 5 else "bearish"
    res = vs.compare(calls, actuals)
    assert res["edge_gain"] == 0.0          # identical calls -> no gain
    assert res["verdict"] == "delete"
    assert "delete" in vs.format_report(res)


def test_a_clearly_better_challenger_is_adopted(monkeypatch):
    monkeypatch.setattr(vs, "MIN_PAIRED_CALLS", 4)
    calls, actuals = {}, {}
    for i in range(20):
        k = (f"d{i:02}", "s")
        actuals[k] = "bullish" if i % 2 else "bearish"
        calls[k] = {"single": "bullish",            # right half the time
                    "debate": actuals[k]}           # always right
    res = vs.compare(calls, actuals)
    assert res["stats"]["debate"]["hit_rate"] == 1.0
    assert res["verdict"] == "adopt"


# ── the flag ─────────────────────────────────────────────────────────────
def test_debate_is_off_by_default(monkeypatch):
    from oracle.analysis import llm_analyst

    monkeypatch.delenv("ORACLE_ANALYST_DEBATE", raising=False)
    assert llm_analyst.debate_enabled() is False
    monkeypatch.setenv("ORACLE_ANALYST_DEBATE", "1")
    assert llm_analyst.debate_enabled() is True


def test_debate_usage_is_shaped_for_the_meter():
    """Regression: run_debate spread the usage fields flat while the meter reads
    entry["usage"], so every debate turn metered zero. Cost is the entire risk
    of this experiment — it cannot be the thing that goes unrecorded."""
    def complete(system, user, model):
        return {"calls": []}, {"prompt_tokens": 11, "completion_tokens": 3,
                               "cached_tokens": 0, "total_tokens": 14}

    _parsed, usages, _t = dbt.run_debate("CTX", "CONTRACT", complete, "m")
    assert len(usages) == 3
    for u in usages:
        assert set(u) == {"call_type", "usage"}
        assert u["usage"]["total_tokens"] == 14


def test_scoring_self_heals_an_unmigrated_db():
    """The variant column arrives by migration; a restored older state DB lacks
    it until init_db runs. Reading straight through crashed on exactly the DBs
    this report is most useful for."""
    import sqlite3
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.executescript("""
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL, sector TEXT NOT NULL,
            provider TEXT, model TEXT, direction TEXT NOT NULL,
            conviction TEXT NOT NULL, tradeable_etf TEXT, key_drivers TEXT,
            rationale TEXT, top_pick TEXT, created_at TEXT NOT NULL,
            UNIQUE (trade_date, sector));
        INSERT INTO llm_calls (trade_date,sector,direction,conviction,created_at)
        VALUES ('2026-08-10','semis','bullish','med','t');
    """)
    conn.commit(); conn.close()

    got = vs.load_variant_calls(tmp)
    assert got[("2026-08-10", "semis")] == {"single": "bullish"}
