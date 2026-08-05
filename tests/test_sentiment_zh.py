"""Tests for the Chinese-language sentiment + category engine."""
from oracle.analysis import sentiment_zh as zh
from oracle.analysis.sentiment import analyze_any


# ── direction ─────────────────────────────────────────────────────────────
def test_positive_and_negative_terms_score_correctly():
    assert zh.score_sentiment_zh("上涨")[0] > 0
    assert zh.score_sentiment_zh("下跌")[0] < 0
    assert zh.score_sentiment_zh("涨停")[0] > zh.score_sentiment_zh("上涨")[0]
    assert zh.score_sentiment_zh("跌停")[0] < zh.score_sentiment_zh("下跌")[0]


def test_neutral_text_scores_zero():
    score, matched = zh.score_sentiment_zh("今日召开新闻发布会")
    assert score == 0.0 and matched == ()
    assert zh.score_sentiment_zh("")[0] == 0.0


# ── the Chinese-specific mechanics ────────────────────────────────────────
def test_longest_match_wins_so_terms_are_not_double_counted():
    """涨停 must score as itself, not also as 涨 — overlapping matches would
    inflate every headline containing a compound term."""
    _score, matched = zh.score_sentiment_zh("涨停")
    assert matched == ("涨停",)


def test_negation_flips_the_sign():
    """Two mechanisms: positional negation (不 before a lexicon term) and set
    phrases that are listed outright because they are not decomposable."""
    assert zh.score_sentiment_zh("看好")[0] > 0
    assert zh.score_sentiment_zh("不看好")[0] < 0        # positional negation
    assert zh.score_sentiment_zh("超预期")[0] > 0
    assert zh.score_sentiment_zh("不及预期")[0] < 0      # listed set phrase
    assert zh.score_sentiment_zh("不达预期")[0] < 0


def test_intensifier_amplifies_and_diminisher_dampens():
    base = abs(zh.score_sentiment_zh("下跌")[0])
    strong = abs(zh.score_sentiment_zh("大幅下跌")[0])
    weak = abs(zh.score_sentiment_zh("小幅下跌")[0])
    assert strong > base > weak


def test_scores_stay_clamped_on_a_pile_of_terms():
    s, _ = zh.score_sentiment_zh("大涨涨停飙升暴涨创新高利好突破")
    assert -1.0 <= s <= 1.0


# ── categories, shared vocabulary with the English engine ────────────────
def test_categories_match_the_english_buckets():
    assert zh.classify_category_zh("美联储加息决议") == "fed_policy"
    assert zh.classify_category_zh("芯片出口管制升级") == "chip_export"
    assert zh.classify_category_zh("央行降准释放流动性") in ("china_stimulus", "fed_policy")
    assert zh.classify_category_zh("公司发布年报业绩") == "earnings"
    assert zh.classify_category_zh("今日天气晴朗") == "general"


def test_latin_macro_tickers_are_caught():
    assert zh.classify_category_zh("11月CPI数据公布") == "macro_data"


# ── headline weighting + language routing ────────────────────────────────
def test_headline_outweighs_summary():
    strong_head = zh.analyze_zh("涨停潮", "小幅下跌")
    strong_body = zh.analyze_zh("小幅下跌", "涨停潮")
    assert strong_head.sentiment > strong_body.sentiment


def test_is_chinese_detects_cjk():
    assert zh.is_chinese("沪指大涨") is True
    assert zh.is_chinese("Nasdaq rallies") is False
    assert zh.is_chinese("") is False


def test_analyze_any_routes_by_language():
    cn = analyze_any("沪指大涨，半导体涨停")
    en = analyze_any("Nasdaq rallies on strong earnings")
    assert cn.sentiment > 0 and en.sentiment > 0
    # a bearish headline in either language reads bearish
    assert analyze_any("A股大跌 芯片承压").sentiment < 0
    assert analyze_any("Stocks plunge on recession fears").sentiment < 0


def test_analyze_any_categories_are_interchangeable():
    """Both engines must emit the SAME category names, or the news-impact table
    would split one signal across two vocabularies."""
    assert analyze_any("芯片出口管制").category == "chip_export"
    assert analyze_any("chip export control tightened").category == "chip_export"


def test_both_engines_share_one_category_vocabulary():
    """Caught live: the Chinese engine fell back to "other" while the English one
    fell back to "general", so uncategorised news split into two buckets in the
    news-impact table. The full key sets must match, not just one example."""
    from oracle.analysis import sentiment as en

    assert {c for c, _ in zh.CATEGORY_KEYWORDS_ZH} == {c for c, _ in en.CATEGORY_KEYWORDS}
    # ...and the fallback bucket, which no keyword list would reveal.
    assert zh.classify_category_zh("今日天气晴朗") == en.classify_category("sunny weather today")


# ── ingestion routes each headline to the right engine ───────────────────
def test_parse_feed_handles_mixed_language_batch():
    from oracle.ingestion.news import parse_feed

    rows = parse_feed("mixed", {"entries": [
        {"title": "沪指大涨 半导体板块涨停潮", "summary": "市场情绪回暖"},
        {"title": "Nasdaq falls on weak earnings", "summary": ""},
    ]}, "t", "2026-08-05")
    assert len(rows) == 2
    assert rows[0]["sentiment"] > 0 and rows[1]["sentiment"] < 0
    assert all(r["category"] for r in rows)


def test_fetch_world_news_includes_chinese_feeds_by_default(monkeypatch):
    from oracle import config
    from oracle.ingestion import news as nz

    seen = []
    monkeypatch.setattr(nz, "_download", lambda url: seen.append(url) or {"entries": []})
    monkeypatch.setattr(nz, "insert_news", lambda rows: 0)
    nz.fetch_world_news()
    for url in config.NEWS_FEEDS_ZH.values():
        assert url in seen, "Chinese feeds must be pulled by default"
    for url in config.NEWS_FEEDS.values():
        assert url in seen, "English feeds must still be pulled"


def test_one_dead_feed_does_not_sink_the_others(monkeypatch):
    from oracle.ingestion import news as nz

    def flaky(url):
        if "eastmoney" in url:
            raise OSError("blocked")
        return {"entries": [{"title": "Stocks rally", "summary": ""}]}

    written = {}
    monkeypatch.setattr(nz, "_download", flaky)
    monkeypatch.setattr(nz, "insert_news", lambda rows: written.setdefault("n", len(rows)))
    nz.fetch_world_news()
    assert written.get("n", 0) > 0        # the reachable feeds still landed


def test_explicit_feeds_are_honoured_verbatim(monkeypatch):
    """Naming your sources must not silently merge the Chinese defaults in."""
    from oracle.ingestion import news as nz

    seen = []
    monkeypatch.setattr(nz, "_download", lambda url: seen.append(url) or {"entries": []})
    monkeypatch.setattr(nz, "insert_news", lambda rows: 0)
    nz.fetch_world_news(feeds={"only": "http://example.test/feed.xml"})
    assert seen == ["http://example.test/feed.xml"]


def test_include_chinese_false_drops_the_zh_feeds(monkeypatch):
    from oracle import config
    from oracle.ingestion import news as nz

    seen = []
    monkeypatch.setattr(nz, "_download", lambda url: seen.append(url) or {"entries": []})
    monkeypatch.setattr(nz, "insert_news", lambda rows: 0)
    nz.fetch_world_news(include_chinese=False)
    assert not any(u in seen for u in config.NEWS_FEEDS_ZH.values())
    assert all(u in seen for u in config.NEWS_FEEDS.values())


# ── regressions found by scoring real headlines off the live feeds ────────
def test_export_ban_vocabulary_is_not_neutral():
    """Live miss: the lexicon knew 出口管制 but had no word for an outright ban,
    so a US import-ban headline scored a flat 0.0 and landed in 'other'."""
    sig = analyze_any("外媒：美国拟禁止进口中国新型数据中心设备")
    assert sig.sentiment < 0
    assert sig.category == "tariffs"


def test_intraday_reversal_idiom_reads_directionally():
    """低开高走 / 高开低走 are standard A-share price-action idiom and must
    outrank the 低开 / 高开 nested inside them."""
    assert zh.score_sentiment_zh("低开高走")[0] > 0
    assert zh.score_sentiment_zh("低开高走")[1] == ("低开高走",)
    assert analyze_any("两市股指高开低走 沪指收跌0.8%").sentiment < 0


def test_index_close_headline_reads_strongly_bullish():
    assert analyze_any("A股三大指数收涨 创业板指飙升5.64% 140只股涨停").sentiment > 0.5
