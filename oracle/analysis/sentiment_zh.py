"""Chinese-language news sentiment + category classification.

Why this exists: A-share prices are set domestically — roughly 97% domestic
ownership and >80% retail volume — and the research literature is explicit that
**domestic investors rely on Chinese-language/state media while foreign investors
rely on global sources**. Our other feeds are the English, foreign-facing ones,
so they describe the market to the wrong audience. This module reads the media
the actual price-setters read.

Category names are deliberately identical to the English engine's, **including
the "general" fallback** — a divergence there would silently split one bucket
across two vocabularies in the news-impact table.

Design notes:

  * **No segmentation dependency.** Chinese has no whitespace, so the English
    word-boundary approach fails. Rather than pull in a segmenter (jieba etc.),
    terms are matched as **substrings** — which for a curated lexicon of 2–4
    character finance terms is both accurate and dependency-free.
  * **Negation and intensity are handled positionally**: a negator or intensifier
    is applied when it appears in the few characters immediately *before* a
    matched term ("不及预期" = below expectations, "大幅下跌" = sharply fell), which
    is where Chinese places them.
  * **Longest-match-first** so 涨停 (limit-up) is not double counted as 涨.

Scores land in roughly −1..+1 to match ``analysis/sentiment.py``, so the two
engines are interchangeable downstream.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Sentiment lexicon (finance-tilted, simplified Chinese) ────────────────
POSITIVE_ZH: dict[str, float] = {
    "涨停": 1.4, "大涨": 1.3, "飙升": 1.3, "暴涨": 1.3, "创新高": 1.2,
    "上涨": 1.0, "走强": 1.0, "反弹": 1.0, "回暖": 0.9, "利好": 1.2,
    "增长": 0.8, "上升": 0.8, "提振": 0.9, "超预期": 1.2, "突破": 0.9,
    "复苏": 0.9, "宽松": 0.8, "降准": 0.9, "降息": 0.8, "刺激": 0.7,
    "增持": 0.9, "回购": 0.8, "扩张": 0.7, "盈利": 0.8, "看好": 1.0,
    "乐观": 0.9, "强劲": 0.9, "提升": 0.7, "受益": 0.8, "企稳": 0.7,
    # A-share price-action idiom, added after scoring live headlines.
    # 低开高走 ("opened low, closed high") is a bullish intraday reversal and
    # must outrank the 低开 inside it — longest-match-first handles that.
    "低开高走": 1.2, "收涨": 1.0, "走高": 0.9, "爆发": 1.0, "利多": 1.1,
}
NEGATIVE_ZH: dict[str, float] = {
    "跌停": -1.4, "大跌": -1.3, "暴跌": -1.3, "重挫": -1.3, "创新低": -1.2,
    "下跌": -1.0, "走弱": -1.0, "回落": -0.9, "利空": -1.2, "下滑": -0.9,
    "下降": -0.8, "亏损": -1.1, "承压": -0.9, "低于预期": -1.2, "萎缩": -1.0,
    "衰退": -1.3, "危机": -1.3, "违约": -1.2, "减持": -0.9, "抛售": -1.2,
    "风险": -0.7, "担忧": -0.9, "疲软": -1.0, "拖累": -0.9, "制裁": -1.0,
    "关税": -0.7, "管制": -0.8, "监管趋严": -0.9, "警告": -0.8, "退市": -1.2,
    # Common set phrases that are NOT a negator plus a lexicon term, so the
    # positional negation rule below can never catch them — they must be listed.
    "不及预期": -1.2, "不达预期": -1.2, "逊于预期": -1.1, "不景气": -1.0,
    # Trade/export restriction vocabulary. Added after a live headline —
    # "美国拟禁止进口中国新型数据中心设备" — scored a flat 0.0: the lexicon had
    # 出口管制 but no word for an outright ban.
    "禁止": -1.0, "禁令": -1.1, "封禁": -1.1, "限制": -0.7,
    # Bearish price action, the mirror of the additions above.
    "高开低走": -1.2, "收跌": -1.0, "走低": -0.9, "下挫": -1.1, "跳水": -1.2,
}
# Applied when they appear immediately before a matched term.
NEGATORS_ZH = ("不", "未", "无", "没有", "难以", "免于")
INTENSIFIERS_ZH = {"大幅": 1.4, "显著": 1.3, "急剧": 1.4, "小幅": 0.6, "略": 0.5,
                   "微": 0.6}
_LOOKBACK = 4          # chars before a term where a modifier can sit

# ── Categories — same buckets as the English engine, so the news-impact
# table (§4b-ii) aggregates both languages into one signal. ───────────────
CATEGORY_KEYWORDS_ZH: list[tuple[str, tuple[str, ...]]] = [
    ("fed_policy", ("美联储", "加息", "降息", "利率决议", "鲍威尔", "货币政策",
                    "议息")),
    ("chip_export", ("芯片", "半导体", "光刻", "出口管制", "英伟达", "台积电",
                     "国产替代")),
    ("tariffs", ("关税", "贸易战", "贸易摩擦", "制裁", "出口禁令", "反倾销",
                 "禁止进口", "进口禁令", "出口限制", "加征")),
    ("china_stimulus", ("降准", "降息", "刺激", "宽松", "国常会", "央行",
                        "政策支持", "稳增长", "专项债")),
    ("earnings", ("财报", "业绩", "营收", "净利", "预告", "季报", "年报")),
    ("macro_data", ("GDP", "CPI", "PPI", "PMI", "社融", "经济数据", "工业增加值",
                    "进出口")),
]


@dataclass(frozen=True)
class NewsSignalZH:
    sentiment: float
    category: str
    matched: tuple


def classify_category_zh(text: str) -> str:
    """First matching category wins, in listed order. Pure."""
    if not text:
        return "general"
    upper = text.upper()          # macro tickers like GDP/CPI arrive latin
    for category, keywords in CATEGORY_KEYWORDS_ZH:
        for kw in keywords:
            if kw in text or kw.upper() in upper:
                return category
    return "general"


def _modifier(text: str, start: int) -> tuple[float, bool]:
    """(intensity multiplier, negated) from the characters just before `start`."""
    window = text[max(0, start - _LOOKBACK):start]
    negated = any(neg in window for neg in NEGATORS_ZH)
    mult = 1.0
    for word, m in INTENSIFIERS_ZH.items():
        if word in window:
            mult = m
            break
    return mult, negated


def score_sentiment_zh(text: str) -> tuple[float, tuple]:
    """Score Chinese text to roughly −1..+1, with the matched terms. Pure.

    Terms are matched longest-first so 涨停 is not also counted as 涨, and each
    character position is consumed once so overlapping terms cannot double-count.
    """
    if not text:
        return 0.0, ()
    lexicon = {**POSITIVE_ZH, **NEGATIVE_ZH}
    terms = sorted(lexicon, key=len, reverse=True)
    used = [False] * len(text)
    total, hits, matched = 0.0, 0, []

    for term in terms:
        start = 0
        while True:
            i = text.find(term, start)
            if i < 0:
                break
            if any(used[i:i + len(term)]):
                start = i + 1
                continue          # overlaps something already scored
            for j in range(i, i + len(term)):
                used[j] = True
            mult, negated = _modifier(text, i)
            weight = lexicon[term] * mult
            if negated:
                weight = -weight * 0.8      # negation flips, slightly damped
            total += weight
            hits += 1
            matched.append(term)
            start = i + len(term)

    if not hits:
        return 0.0, ()
    # Average, then clamp — long articles must not outweigh short headlines.
    return max(-1.0, min(1.0, total / max(hits, 1) / 1.3)), tuple(matched)


def analyze_zh(headline: str, summary: str = "") -> NewsSignalZH:
    """Headline weighted double vs. the summary — the headline carries the call."""
    h_score, h_terms = score_sentiment_zh(headline)
    s_score, s_terms = score_sentiment_zh(summary)
    if h_terms and s_terms:
        score = (h_score * 2 + s_score) / 3
    else:
        score = h_score or s_score
    return NewsSignalZH(
        sentiment=round(max(-1.0, min(1.0, score)), 4),
        category=classify_category_zh(f"{headline} {summary}"),
        matched=tuple(h_terms) + tuple(s_terms),
    )


def is_chinese(text: str) -> bool:
    """True when the text contains CJK characters — used to route a headline to
    this engine instead of the English one."""
    return any("一" <= ch <= "鿿" for ch in text or "")
