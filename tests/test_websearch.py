"""Tests for the live web-search layer (pure builders + fail-soft orchestration)."""
import tempfile

from oracle import config, db
from oracle.analysis import websearch as ws, llm_analyst as la


# ── pure: query building + normalization ──────────────────────────────────
def test_build_queries_market_plus_per_sector_and_cap():
    qs = ws.build_queries("2026-08-04", sectors=["semis", "energy"], max_queries=10)
    assert qs[0].startswith("China A-share")
    assert any("semiconductor" in q for q in qs) and any("energy" in q for q in qs)
    assert all("2026-08-04" in q for q in qs)
    # cap is honored
    assert len(ws.build_queries("2026-08-04", max_queries=2)) == 2


def test_normalize_tavily_and_brave():
    tav = ws.normalize_tavily({"results": [
        {"title": "T", "url": "http://a", "content": "body", "published_date": "2026-08-04"}]})
    assert tav[0]["snippet"] == "body" and tav[0]["url"] == "http://a"
    brave = ws.normalize_brave({"web": {"results": [
        {"title": "B", "url": "http://b", "description": "desc"}]}})
    assert brave[0]["title"] == "B" and brave[0]["snippet"] == "desc"


def test_dedupe_drops_repeat_urls_and_caps():
    rows = [{"url": "u1"}, {"url": "u1"}, {"url": "u2"}, {"url": "u3"}]
    assert [r["url"] for r in ws._dedupe(rows, 2)] == ["u1", "u2"]


# ── orchestration: no-op / injected / fail-soft ───────────────────────────
def test_run_search_noop_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ORACLE_SEARCH_PROVIDER", raising=False)
    assert ws.run_search("2026-08-04") == ([], None, 0)


def test_run_search_with_injected_provider():
    results, provider, n = ws.run_search(
        "2026-08-04", search_one=lambda q: [{"title": "t", "url": "http://x/" + q, "snippet": "s"}])
    assert provider == "test"
    assert n == config.SEARCH_MAX_QUERIES
    assert len(results) == n            # one unique url per query


def test_run_search_failsoft_on_provider_error():
    def boom(q):
        raise RuntimeError("search down")
    # every query errors -> no results, but no exception bubbles up
    assert ws.run_search("2026-08-04", search_one=boom) == ([], "test", config.SEARCH_MAX_QUERIES)


# ── integration: search context reaches the analyst + is metered ──────────
def test_analyst_uses_search_and_meters_it(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    monkeypatch.setattr(config, "SEARCH_PRICE_PER_QUERY", 0.01)
    db.init_db(tmp)

    seen = {}

    def fake_llm(ctx):
        seen["web"] = ctx.get("web_research")
        return {"calls": [{"sector": "semis", "direction": "bullish", "conviction": "high",
                           "key_drivers": [], "rationale": "r"}], "market_note": "n"}

    n = la.run_llm_analysis(
        "2026-08-04", llm=fake_llm,
        search_one=lambda q: [{"title": "China chips rally", "url": "http://x/" + q,
                               "snippet": "fresh context"}])
    assert n == 1
    assert seen["web"] and seen["web"][0]["snippet"] == "fresh context"   # search fed in

    # search was metered: 6 queries * $0.01
    s = db.llm_usage_summary(tmp)
    assert s["all_time"]["cost_usd"] == round(config.SEARCH_MAX_QUERIES * 0.01, 6)
    assert any(m["model"].startswith("search:") for m in s["by_model"])
