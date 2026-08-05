"""World-news RSS ingestion via feedparser (spec §3).

Pulls headlines + first paragraph from configured feeds, tags each with a
lexicon sentiment score and a news category, and persists. Parsing is isolated
from the network fetch so it can be tested on a fixture feed dict.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import config
from ..analysis.sentiment import analyze, analyze_any
from ..db import insert_news
from ._retry import with_retries


@with_retries(attempts=4, base_delay=2.0)
def _download(url: str):
    """Fetch + parse one RSS feed. feedparser imported lazily."""
    import feedparser

    return feedparser.parse(url)


def _first_paragraph(entry: dict) -> str:
    """Best-effort first paragraph / summary from a feed entry (spec §3:
    headlines + first paragraph only)."""
    text = entry.get("summary") or entry.get("description") or ""
    # RSS summaries are often HTML; keep it crude — strip tags, first sentence-ish.
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]


def parse_feed(source: str, parsed_feed, fetched_at: str,
               trade_date: str) -> list[dict]:
    """Turn a parsed feed (object or dict with `.entries`) into news rows.
    Pure/testable: accepts anything exposing an `entries` list of dict-likes."""
    entries = getattr(parsed_feed, "entries", None)
    if entries is None and isinstance(parsed_feed, dict):
        entries = parsed_feed.get("entries", [])
    rows = []
    for e in entries or []:
        get = e.get if isinstance(e, dict) else (lambda k, d=None: getattr(e, k, d))
        headline = (get("title") or "").strip()
        if not headline:
            continue
        summary = _first_paragraph(e if isinstance(e, dict) else vars(e))
        # Route by language: Chinese headlines score on the Chinese
        # lexicon, everything else on the English one. Both emit the
        # same category vocabulary, so downstream aggregation is unified.
        sig = analyze_any(headline, summary)
        rows.append({
            "trade_date": trade_date,
            "source": source,
            "category": sig.category,
            "headline": headline,
            "summary": summary,
            "sentiment": sig.sentiment,
            "fetched_at": fetched_at,
        })
    return rows


def fetch_world_news(feeds: dict[str, str] | None = None,
                     include_chinese: bool = True) -> int:
    """Job entrypoint: pull every feed, tag, persist. Never raises.

    By default this covers BOTH the English/global feeds and the Chinese-language
    domestic feeds (config.NEWS_FEEDS_ZH). The Chinese sources matter more for
    A-shares — the market is ~97% domestically owned and >80% retail by volume,
    and domestic investors read domestic media. Each source fails soft on its own,
    so a blocked feed costs that source's headlines and nothing else."""
    if feeds is None:
        # Default set = English/global + Chinese domestic.
        feeds = dict(config.NEWS_FEEDS)
        if include_chinese:
            feeds.update(config.NEWS_FEEDS_ZH)
    else:
        # An explicit feed map is honoured verbatim — callers that name their
        # sources get exactly those, not those plus a silent Chinese merge.
        feeds = dict(feeds)
    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat()
    trade_date = now.date().isoformat()

    all_rows: list[dict] = []
    for source, url in feeds.items():
        try:
            parsed = _download(url)
            all_rows.extend(parse_feed(source, parsed, fetched_at, trade_date))
        except Exception as e:  # noqa: BLE001
            print(f"fetch_world_news: {source} failed: {e!r}")
    try:
        n = insert_news(all_rows)
        print(f"fetch_world_news: wrote {n} new headlines for {trade_date}")
        return n
    except Exception as e:  # noqa: BLE001
        print(f"fetch_world_news FAILED: {e!r}")
        return 0
