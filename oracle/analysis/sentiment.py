"""Lexicon-based news sentiment + category classification (spec §4.2, v1).

Deliberately simple and offline: a keyword lexicon scored to roughly -1..1, and
a keyword-based bucketing of headlines into the macro/news categories the
reflection loop's news-impact table tracks (spec §4b-ii). No LLM call, no
network, no ongoing cost — fastest path to ship, and fully testable.

Swappable later for an LLM-per-batch classifier without touching callers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ── Sentiment lexicon (finance-tilted). Weights are indicative, not tuned. ──
POSITIVE = {
    "gain": 1.0, "gains": 1.0, "rally": 1.2, "rallies": 1.2, "surge": 1.3,
    "surges": 1.3, "soar": 1.3, "jump": 1.0, "jumps": 1.0, "rise": 0.8,
    "rises": 0.8, "rose": 0.8, "up": 0.5, "higher": 0.8, "beat": 1.0,
    "beats": 1.0, "record": 0.9, "strong": 0.9, "growth": 0.8, "boost": 0.9,
    "optimism": 1.0, "upgrade": 1.0, "stimulus": 0.8, "recovery": 0.9,
    "outperform": 1.0, "bullish": 1.2,
}
NEGATIVE = {
    "loss": -1.0, "losses": -1.0, "fall": -0.8, "falls": -0.8, "fell": -0.8,
    "drop": -1.0, "drops": -1.0, "plunge": -1.3, "plunges": -1.3, "slump": -1.2,
    "tumble": -1.2, "sink": -1.1, "down": -0.5, "lower": -0.8, "miss": -1.0,
    "misses": -1.0, "weak": -0.9, "slowdown": -1.0, "recession": -1.3,
    "fear": -1.0, "fears": -1.0, "selloff": -1.2, "sell-off": -1.2,
    "downgrade": -1.0, "sanction": -1.0, "sanctions": -1.0, "tariff": -0.7,
    "tariffs": -0.7, "ban": -0.9, "curb": -0.7, "curbs": -0.7, "bearish": -1.2,
    "default": -1.1, "crisis": -1.3, "warning": -0.8,
}
_NEGATORS = {"no", "not", "never", "without", "avoid", "avoids", "ease", "eases"}

# ── News categories (spec §4b-ii). First match wins, in listed order. ──────
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("fed_policy", ("fed", "federal reserve", "fomc", "rate cut", "rate hike",
                    "interest rate", "powell", "monetary")),
    ("chip_export", ("chip", "semiconductor", "export control", "nvidia",
                     "asml", "lithography", "tsmc")),
    ("tariffs", ("tariff", "trade war", "sanction", "export ban", "customs")),
    ("china_stimulus", ("stimulus", "pboc", "reserve requirement", "rrr",
                        "beijing", "state council", "liquidity injection")),
    ("earnings", ("earnings", "revenue", "profit", "quarterly", "guidance",
                  "eps")),
    ("macro_data", ("cpi", "inflation", "pmi", "gdp", "jobs", "payroll",
                    "unemployment", "trade data")),
]

_WORD = re.compile(r"[a-z][a-z\-]*")


@dataclass(frozen=True)
class NewsSignal:
    sentiment: float   # -1..1
    category: str      # one of CATEGORY_KEYWORDS keys, or "general"


def classify_category(text: str) -> str:
    low = text.lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in low for kw in keywords):
            return category
    return "general"


def score_sentiment(text: str) -> float:
    """Return a sentiment score in [-1, 1]. Simple negation handling: a negator
    in the preceding two tokens flips a hit's sign."""
    tokens = _WORD.findall(text.lower())
    if not tokens:
        return 0.0

    total = 0.0
    hits = 0
    for i, tok in enumerate(tokens):
        weight = POSITIVE.get(tok) or NEGATIVE.get(tok)
        if weight is None:
            continue
        window = tokens[max(0, i - 2):i]
        if any(w in _NEGATORS for w in window):
            weight = -weight
        total += weight
        hits += 1

    if hits == 0:
        return 0.0
    # Average per hit, then squash toward [-1, 1] so a pile-up can't exceed range.
    avg = total / hits
    return max(-1.0, min(1.0, avg))


def analyze(headline: str, summary: str = "") -> NewsSignal:
    text = f"{headline}. {summary}".strip()
    return NewsSignal(
        sentiment=round(score_sentiment(text), 4),
        category=classify_category(text),
    )
