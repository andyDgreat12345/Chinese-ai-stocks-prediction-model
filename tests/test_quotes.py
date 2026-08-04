"""Tests for the near-real-time quote layer (pure parse + fail-soft fetch)."""
import json
import urllib.request

from oracle import report as rp
from oracle.ingestion import quotes as q


# ── symbol mapping ────────────────────────────────────────────────────────
def test_sina_symbol_maps_each_market():
    assert q.sina_symbol("688981.SS") == "sh688981"
    assert q.sina_symbol("002371.SZ") == "sz002371"
    assert q.sina_symbol("0700.HK") == "hk00700"       # zero-padded to 5
    assert q.sina_symbol("BABA") == "gb_baba"
    assert q.sina_symbol("???") is None


# ── line parsing per market ───────────────────────────────────────────────
def test_parse_a_share_line_and_pct():
    line = 'var hq_str_sh688981="中芯国际,50.00,49.50,51.20,51.50,49.80,x,y";'
    p = q.parse_sina_line(line)
    assert p["price"] == 51.20 and p["prev_close"] == 49.50
    assert p["pct_change"] == round((51.20 / 49.50 - 1) * 100, 2)


def test_parse_hk_line_uses_field_6():
    line = 'var hq_str_hk00700="TENCENT,腾讯控股,300.0,295.0,305.0,299.0,302.5,z";'
    p = q.parse_sina_line(line)
    assert p["name"] == "腾讯控股" and p["price"] == 302.5 and p["prev_close"] == 295.0


def test_parse_us_line_price():
    line = 'var hq_str_gb_baba="ALIBABA,120.50,2.50,2026-08-04,3.00,118.0,121.0,117.5";'
    p = q.parse_sina_line(line)
    assert p["name"] == "ALIBABA" and p["price"] == 120.50


def test_parse_empty_payload_is_none():
    assert q.parse_sina_line('var hq_str_sh000000="";') is None
    assert q.parse_sina_line("garbage") is None


# ── fetch: disabled + fail-soft + happy path ──────────────────────────────
def test_fetch_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ORACLE_QUOTES_PROVIDER", raising=False)
    assert q.fetch_quotes(["688981.SS"]) == {}


def test_fetch_failsoft_on_network_error(monkeypatch):
    monkeypatch.setenv("ORACLE_QUOTES_PROVIDER", "sina")

    def boom(*a, **k):
        raise OSError("blocked")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert q.fetch_quotes(["688981.SS"]) == {}       # swallowed, no raise


def test_fetch_maps_back_to_original_tickers(monkeypatch):
    monkeypatch.setenv("ORACLE_QUOTES_PROVIDER", "sina")
    body = ('var hq_str_sh688981="SMIC,50,49.5,51.2,51.5,49.8";\n'
            'var hq_str_gb_baba="ALIBABA,120.5,2.5,t,3,118,121,117";\n').encode()

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return body
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: FakeResp())
    out = q.fetch_quotes(["688981.SS", "BABA"])
    assert out["688981.SS"]["price"] == 51.2
    assert out["BABA"]["price"] == 120.5


# ── report renders the picked name's latest quote ─────────────────────────
def test_report_shows_latest_quote_for_pick():
    call = {"sector": "growth", "direction": "bullish", "conviction": "high",
            "key_drivers": "[]", "rationale": "r",
            "top_pick": json.dumps({"ticker": "BABA", "name": "Alibaba",
                                    "tradeable": "BABA", "note": "x"})}
    pred = {"sector": "growth", "direction": "bullish", "confidence": "high",
            "rationale": "r"}
    quotes = {"BABA": {"price": 120.5, "pct_change": 2.13}}
    rep = rp.build_report("2026-08-04", [pred], [call], quotes=quotes)
    md = rp.format_markdown(rep)
    assert "latest: 120.5 (+2.13%)" in md
