"""Tests for the single-name pick layer (universe validation, migration, report)."""
import json
import tempfile

from oracle import config, db, report as rp
from oracle.analysis import llm_analyst as la


# ── parse/validate the pick against the vetted universe ───────────────────
def test_parse_keeps_valid_pick_and_fills_from_config():
    raw = {"calls": [{"sector": "semis", "direction": "bullish", "conviction": "high",
                      "key_drivers": [], "rationale": "r",
                      "top_pick": {"ticker": "688981.SS", "note": "foundry leader"}}]}
    call = la.parse_calls(raw, "2026-08-04")[0]
    assert call["top_pick"]["name"] == "SMIC"              # filled from config
    assert call["top_pick"]["tradeable"] == "HK:0981"      # authoritative
    assert call["top_pick"]["note"] == "foundry leader"


def test_parse_drops_hallucinated_or_wrong_sector_ticker():
    raw = {"calls": [
        {"sector": "semis", "direction": "bullish", "conviction": "high",
         "key_drivers": [], "rationale": "r", "top_pick": {"ticker": "FAKE"}},
        {"sector": "energy", "direction": "bullish", "conviction": "low",
         "key_drivers": [], "rationale": "r", "top_pick": {"ticker": "688981.SS"}},  # semis name, wrong sector
    ]}
    calls = {c["sector"]: c for c in la.parse_calls(raw, "2026-08-04")}
    assert calls["semis"]["top_pick"] is None
    assert calls["energy"]["top_pick"] is None


def test_parse_null_pick_is_none():
    raw = {"calls": [{"sector": "broad", "direction": "neutral", "conviction": "low",
                      "key_drivers": [], "rationale": "r", "top_pick": None}]}
    assert la.parse_calls(raw, "2026-08-04")[0]["top_pick"] is None


# ── schema migration: old DB missing the top_pick column self-heals ───────
def test_init_db_migrates_top_pick_column():
    tmp = tempfile.mktemp(suffix=".db")
    conn = db.connect(tmp)
    conn.executescript(
        """CREATE TABLE llm_calls (id INTEGER PRIMARY KEY AUTOINCREMENT,
             trade_date TEXT, sector TEXT, provider TEXT, model TEXT, direction TEXT,
             conviction TEXT, tradeable_etf TEXT, key_drivers TEXT, rationale TEXT,
             created_at TEXT, UNIQUE(trade_date, sector))""")
    conn.commit()
    conn.close()
    db.init_db(tmp)                                        # should ADD COLUMN top_pick
    cols = {r["name"] for r in db.connect(tmp).execute("PRAGMA table_info(llm_calls)")}
    assert "top_pick" in cols
    # and the write path now round-trips it
    db.upsert_llm_call({
        "trade_date": "2026-08-04", "sector": "semis", "provider": "deepseek",
        "model": "deepseek-chat", "direction": "bullish", "conviction": "high",
        "tradeable_etf": "KWEB", "key_drivers": "[]", "rationale": "r",
        "top_pick": json.dumps({"ticker": "688981.SS", "name": "SMIC"}),
        "created_at": "t"}, db_path=tmp)
    row = db.llm_calls_for_date("2026-08-04", db_path=tmp)[0]
    assert json.loads(row["top_pick"])["name"] == "SMIC"


# ── report renders the specific name to watch ─────────────────────────────
def test_report_shows_name_to_watch(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    db.upsert_prediction({
        "trade_date": "2026-08-04", "sector": "semis", "direction": "bullish",
        "confidence": "high", "composite_score": 0.6, "us_spillover": 0.5,
        "sentiment_score": 0.1, "macro_flag": 0, "rationale": "r", "created_at": "t"},
        db_path=tmp)
    db.upsert_llm_call({
        "trade_date": "2026-08-04", "sector": "semis", "provider": "deepseek",
        "model": "deepseek-chat", "direction": "bullish", "conviction": "high",
        "tradeable_etf": "KWEB", "key_drivers": "[]", "rationale": "chips",
        "top_pick": json.dumps({"ticker": "688981.SS", "name": "SMIC",
                                "tradeable": "HK:0981", "note": "foundry"}),
        "created_at": "t"}, db_path=tmp)
    md = rp.format_markdown(rp.run_report("2026-08-04", db_path=tmp))
    assert "name to watch: SMIC (688981.SS" in md and "HK:0981" in md
