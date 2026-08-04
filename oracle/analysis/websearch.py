"""Live web search for the AI analyst — the "bolt search onto DeepSeek" layer.

DeepSeek's *app* can search the web; its developer *API* cannot. So to give our
API-based analyst fresh, current context (breaking news, what other desks are
saying) we run the search ourselves and hand the results to DeepSeek as extra
provided data. This is the standard search-augmented pattern: WE fetch, the model
reasons.

Design mirrors the analyst itself:
  * provider-agnostic — Tavily (default; built for LLMs) or Brave, chosen by env;
  * **off by default** — no ``ORACLE_SEARCH_PROVIDER`` / key ⇒ the analyst runs on
    the ingested RSS news alone, exactly as before;
  * **fail-soft** — any search error returns no results, never crashing the
    analyst (the day's call just proceeds without web context);
  * metered — the caller records queries in the spend meter, so search cost is
    visible next to token cost.

Query building and result normalization are pure (no network) so they unit-test
without hitting an API. Only ``_tavily``/``_brave`` touch the network.
"""
from __future__ import annotations

import json
import os
from typing import Callable

from .. import config
from .pipeline import CHINA_SECTORS

# Search phrasing per China sector — plain words a news index understands.
_SECTOR_QUERY = {
    "broad": "broad market and CSI 300",
    "growth": "internet, tech and growth",
    "semis": "semiconductor and chip",
    "energy": "energy and commodities",
    "financials": "banks and financials",
}


# ── pure: query construction + result normalization ───────────────────────
def build_queries(trade_date: str, sectors=CHINA_SECTORS, max_queries: int | None = None) -> list[str]:
    """Deterministic search queries for a trading day: one market-wide, one per
    sector, capped so cost stays bounded. Pure."""
    cap = max_queries if max_queries is not None else config.SEARCH_MAX_QUERIES
    queries = [f"China A-share stock market outlook and key drivers {trade_date}"]
    for s in sectors:
        queries.append(f"China {_SECTOR_QUERY.get(s, s)} sector stocks news outlook {trade_date}")
    return queries[:max(1, cap)]


def _dedupe(results: list[dict], limit: int) -> list[dict]:
    """Drop duplicate URLs, keep order, cap the total handed to the prompt. Pure."""
    seen, out = set(), []
    for r in results:
        url = r.get("url")
        if url in seen:
            continue
        seen.add(url)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def _norm(title, url, snippet, published=None) -> dict:
    return {"title": str(title or "")[:200], "url": str(url or ""),
            "snippet": str(snippet or "")[:400],
            "published": str(published) if published else None}


def normalize_tavily(payload: dict) -> list[dict]:
    return [_norm(r.get("title"), r.get("url"), r.get("content"), r.get("published_date"))
            for r in (payload.get("results") or [])]


def normalize_brave(payload: dict) -> list[dict]:
    return [_norm(r.get("title"), r.get("url"), r.get("description"), r.get("age"))
            for r in ((payload.get("web") or {}).get("results") or [])]


# ── provider backends (network) ───────────────────────────────────────────
def _http_json(req) -> dict:
    import urllib.request
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — fixed hosts
        return json.loads(r.read())


def _tavily(query: str) -> list[dict]:
    import urllib.request

    body = json.dumps({
        "api_key": os.environ["TAVILY_API_KEY"],
        "query": query,
        "max_results": config.SEARCH_MAX_RESULTS,
        "search_depth": "basic",
        "topic": "news",
        "days": 3,
    }).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search", data=body,
        headers={"Content-Type": "application/json"})
    return normalize_tavily(_http_json(req))


def _brave(query: str) -> list[dict]:
    import urllib.parse
    import urllib.request

    qs = urllib.parse.urlencode({"q": query, "count": config.SEARCH_MAX_RESULTS})
    req = urllib.request.Request(
        f"https://api.search.brave.com/res/v1/web/search?{qs}",
        headers={"Accept": "application/json",
                 "X-Subscription-Token": os.environ["BRAVE_API_KEY"]})
    return normalize_brave(_http_json(req))


def get_search_provider() -> tuple[Callable[[str], list[dict]], str] | None:
    """Return (search_one_fn, provider) per env config, or None when search is
    not configured (the analyst then runs without web context)."""
    provider = (os.environ.get("ORACLE_SEARCH_PROVIDER") or "").strip().lower()
    if provider == "tavily" and os.environ.get("TAVILY_API_KEY"):
        return (_tavily, "tavily")
    if provider == "brave" and os.environ.get("BRAVE_API_KEY"):
        return (_brave, "brave")
    return None


def run_search(trade_date: str, search_one=None) -> tuple[list[dict], str | None, int]:
    """Search the day's queries and return (results, provider, n_queries).
    No-ops to ([], None, 0) when unconfigured. Fail-soft: a provider error on a
    query is swallowed (that query just yields nothing)."""
    if search_one is None:
        resolved = get_search_provider()
        if resolved is None:
            return [], None, 0
        search_one, provider = resolved
    else:
        provider = "test"

    queries = build_queries(trade_date)
    collected: list[dict] = []
    for q in queries:
        try:
            collected.extend(search_one(q))
        except Exception as e:  # noqa: BLE001 — one bad query must not sink the rest
            print(f"websearch: query failed ({e!r})")
    results = _dedupe(collected, config.SEARCH_MAX_RESULTS * 3)
    return results, provider, len(queries)
