"""Optional LLM research desk — the AI analyst (spec §4.2 / §7b).

The rule-based pipeline (scoring.py + pipeline.py) always runs and is the
system's baseline. This module adds an *optional* LLM analyst that reads the
same day's data — US closes, overnight news, macro calendar, and the reflection
log (what has worked before) — and produces a structured per-sector
buy/sell/hold call with conviction, key drivers, and the foreign-tradeable ETF
each call maps to.

Design, mirroring reflection/llm.py:
  * provider-agnostic — DeepSeek (default; its OpenAI-compatible endpoint) or
    Claude (official Anthropic SDK), selected by env var;
  * **off by default** — if no provider/key is configured the job no-ops, so
    the rule-based path is never affected;
  * the LLM's calls are recorded in a SEPARATE table (`llm_calls`), never
    overwriting the rule-based `predictions`, so the backtest can measure the
    analyst's edge against the rule-based model and the naive baselines before
    a cent of real money rides on it;
  * fail-soft — any error or refusal leaves the day's rule-based prediction as
    the sole record.

The prompt-building and parsing are pure functions (no I/O) so they unit-test
without a network or DB.

Configuration (env):
    ORACLE_ANALYST_PROVIDER = deepseek | claude | (unset -> disabled)
    ORACLE_ANALYST_MODEL    = model id override
        deepseek default: deepseek-chat
        claude default:   claude-opus-5
    DEEPSEEK_API_KEY  (deepseek)  /  ANTHROPIC_API_KEY (claude)

NOTE ON "DEEP SEARCH": DeepSeek's *API* does not expose the live web search in
its consumer app. The analyst reasons over the news we already ingest (RSS) plus
the market data and reflection memory. A live web-search feed is a clean future
add-on, not a dependency here.

**Not investment advice.** Output is a probabilistic directional signal for the
user to weigh — not a buy/sell instruction, not a guarantee, never auto-executed.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Callable

from .. import config, db
from .pipeline import CHINA_SECTORS

_SYSTEM = (
    "You are the research desk of a China-equity prediction system. You are given "
    "one trading day's inputs: US market closes by sector, overnight world-news "
    "headlines (with a category and a sentiment score), any scheduled macro events, "
    "per-sector TECHNICAL INDICATORS (trend, RSI, MACD, momentum) computed on each "
    "China sector's own price history, a short memory of how the system's recent "
    "predictions fared, and (when available) fresh WEB SEARCH results giving current "
    "context and other analysts' views. Your job is to "
    "produce a next-session directional call for each listed China sector. Ground "
    "each call in the SPECIFIC technicals (cite the RSI/MACD/trend/momentum) and the "
    "relevant US move or news — be concrete and technical, not vague. Use the "
    "per-sector `us_link` label (measured): for a sector that FOLLOWS the US, weight "
    "the overnight US move heavily; for one that DIVERGES or is independent, do NOT "
    "assume it tracks Wall Street — lean on its own technicals, domestic news, and "
    "catalysts instead. Reason "
    "ONLY from the data provided (including the technicals and web-search snippets) "
    "plus general market mechanics — do NOT invent "
    "specific prices, figures, tickers, or events that were not given. Prefer recent "
    "web results over stale ones and do not over-weight a single headline. Output is a "
    "probabilistic lean for a human to weigh, NOT investment advice, NOT a guarantee, "
    "and NEVER a buy/sell instruction. For each sector also name the single best "
    "candidate to WATCH from the provided `stock_candidates` for that sector (pick "
    "the one the evidence favors; use its EXACT ticker; set it to null if none is "
    "compelling) — a name to research, not a buy order. Return STRICT JSON with "
    "exactly this shape: "
    '{"calls": [{"sector": <one of the given sectors>, "direction": '
    '"bullish"|"neutral"|"bearish", "conviction": "low"|"med"|"high", '
    '"key_drivers": [<short strings>], "rationale": <one or two sentences>, '
    '"top_pick": {"ticker": <exact ticker from that sector\'s candidates>, '
    '"note": <one phrase why this name>} | null}], '
    '"market_note": <one sentence overall context>}. '
    "Base conviction on how much the evidence actually supports the call — default "
    "to neutral/low when the inputs are thin or conflicting."
)

# JSON schema for Claude structured outputs (analyst fields only).
_SCHEMA = {
    "type": "object",
    "properties": {
        "calls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sector": {"type": "string"},
                    "direction": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
                    "conviction": {"type": "string", "enum": ["low", "med", "high"]},
                    "key_drivers": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
                "required": ["sector", "direction", "conviction", "key_drivers", "rationale"],
                "additionalProperties": False,
            },
        },
        "market_note": {"type": "string"},
    },
    "required": ["calls", "market_note"],
    "additionalProperties": False,
}

_VALID_DIR = {"bullish", "neutral", "bearish"}
_VALID_CONV = {"low", "med", "high"}


# ── pure context assembly + prompt (unit-tested) ─────────────────────────
def build_context(trade_date: str, us_rows: list[dict], news_rows: list[dict],
                  macro_events: list[dict], reflections: list[dict],
                  web_research: list[dict] | None = None,
                  technicals: dict | None = None,
                  us_link: dict | None = None) -> dict:
    """Assemble the deterministic context handed to the model. Pure.

    ``web_research`` is the optional live-search layer: a list of
    {title, url, snippet, published}. Empty/omitted when search is disabled.
    ``technicals`` is the per-sector indicator snapshot (trend/RSI/MACD/momentum)
    from the sector's own price history — the hard technical state to reason over."""
    us = [
        {"symbol": r.get("symbol"), "sector": r.get("sector"), "pct_change": r.get("pct_change")}
        for r in us_rows if r.get("pct_change") is not None
    ]
    news = [
        {"category": r.get("category"), "sentiment": r.get("sentiment"),
         "headline": r.get("headline")}
        for r in news_rows if r.get("headline")
    ][:25]  # cap to keep the prompt (and cost) bounded
    macro = [
        {"category": m.get("category"), "description": m.get("description")}
        for m in macro_events
    ]
    memory = [
        {"date": r.get("trade_date"),
         "worked": r.get("signals_that_worked"),
         "missed": r.get("signals_that_missed"),
         "reason": r.get("likely_reason_for_miss")}
        for r in reflections
    ][:5]
    web = [
        {"title": r.get("title"), "snippet": r.get("snippet"),
         "url": r.get("url"), "published": r.get("published")}
        for r in (web_research or []) if r.get("snippet") or r.get("title")
    ][:12]  # cap so the search context can't blow up the prompt
    return {
        "trade_date": trade_date,
        "sectors": list(CHINA_SECTORS),
        "sector_tradeable_etf": dict(config.SECTOR_TRADEABLE_ETF),
        "us_closes": us,
        "news": news,
        "macro_events": macro,
        "recent_performance": memory,
        "web_research": web,
        "technicals": technicals or {},
        "us_link": us_link or {},
        "stock_candidates": dict(config.SECTOR_STOCKS),
    }


def _prompt(ctx: dict) -> str:
    """Render the context as a compact JSON block the model reasons over."""
    payload = {k: v for k, v in ctx.items() if k != "sector_tradeable_etf"}
    lines = [
        f"Trade date (China session to call): {ctx['trade_date']}",
        f"Sectors to call: {', '.join(ctx['sectors'])}",
        "Foreign-tradeable proxy per sector (for context, label each call): "
        + json.dumps(ctx["sector_tradeable_etf"]),
        "",
        "Inputs (JSON):",
        json.dumps(payload, ensure_ascii=False),
    ]
    return "\n".join(lines)


def parse_calls(raw: dict, trade_date: str) -> list[dict]:
    """Validate + normalize the model's JSON into per-sector call rows. Pure.
    Unknown sectors are dropped; each listed sector appears at most once. The
    tradeable ETF is filled deterministically from config, not from the model."""
    out: dict[str, dict] = {}
    for c in raw.get("calls", []) or []:
        sector = c.get("sector")
        if sector not in CHINA_SECTORS or sector in out:
            continue
        direction = c.get("direction")
        conviction = c.get("conviction")
        if direction not in _VALID_DIR or conviction not in _VALID_CONV:
            continue
        drivers = c.get("key_drivers") or []
        if not isinstance(drivers, list):
            drivers = [str(drivers)]
        out[sector] = {
            "sector": sector,
            "direction": direction,
            "conviction": conviction,
            "tradeable_etf": config.SECTOR_TRADEABLE_ETF.get(sector),
            "key_drivers": [str(d) for d in drivers][:6],
            "rationale": str(c.get("rationale") or "")[:600],
            "top_pick": _validate_pick(sector, c.get("top_pick")),
        }
    return [out[s] for s in CHINA_SECTORS if s in out]


def _candidate_forms(cand: dict) -> set[str]:
    """The ticker spellings a model might use for one candidate — primary ticker,
    its exchange-suffix-stripped root, and the foreign-tradeable form (US ADR or
    HK line, with/without the 'HK:' prefix). Used for lenient matching."""
    forms = {cand["ticker"].upper(), cand["ticker"].upper().split(".")[0]}
    if cand.get("tradeable"):
        tr = cand["tradeable"].upper()
        forms |= {tr, tr.replace("HK:", ""), tr.replace("HK:", "").split(".")[0]}
    return {f for f in forms if f}


def _validate_pick(sector: str, pick) -> dict | None:
    """Keep the model's single-name pick only if it resolves to a name in the
    sector's vetted candidate list (blocks hallucinated names); match leniently by
    ticker spelling OR company name, then fill ticker/name/tradeable authoritatively
    from config, keeping only the model's short note."""
    if not isinstance(pick, dict):
        return None
    tick = str(pick.get("ticker") or "").strip().upper()
    tbase = tick.split(".")[0].replace("HK:", "")
    name = str(pick.get("name") or "").strip().lower()
    for cand in config.SECTOR_STOCKS.get(sector, []):
        forms = _candidate_forms(cand)
        if (tick and (tick in forms or tbase in forms)) or \
           (name and (name in cand["name"].lower() or cand["name"].lower() in name)):
            return {"ticker": cand["ticker"], "name": cand["name"],
                    "tradeable": cand["tradeable"], "note": str(pick.get("note") or "")[:200]}
    return None


# ── provider backends ────────────────────────────────────────────────────
def _extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating stray prose around it
    (the reasoner model can't be forced into strict JSON mode)."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            return json.loads(text[i:j + 1])
        raise


def _deepseek_complete(system: str, user: str, model: str) -> tuple[dict, dict]:
    """One DeepSeek chat completion → (parsed_json, usage). Generic (any system +
    user), so both the single-pass analyst and the multi-pass chain use it. The
    `deepseek-reasoner` model rejects response_format/temperature, so those are
    dropped for it and the JSON is recovered leniently from the answer."""
    import urllib.request

    key = os.environ["DEEPSEEK_API_KEY"]
    is_reasoner = "reasoner" in model
    msg = {"model": model, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user}]}
    if not is_reasoner:
        msg["response_format"] = {"type": "json_object"}
        msg["temperature"] = 0.4
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions", data=json.dumps(msg).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 — fixed host
        payload = json.loads(r.read())
    content = payload["choices"][0]["message"]["content"]
    return _extract_json(content), _usage_from_openai(payload.get("usage"))


def _deepseek_analyze(ctx: dict) -> tuple[dict, dict]:
    """Single-pass DeepSeek analyst (the default mode)."""
    model = os.environ.get("ORACLE_ANALYST_MODEL") or "deepseek-chat"
    return _deepseek_complete(_SYSTEM, _prompt(ctx), model)


def _usage_from_openai(u: dict | None) -> dict:
    """Normalize an OpenAI-compatible `usage` block (DeepSeek included). DeepSeek
    reports cache hits as `prompt_cache_hit_tokens`; OpenAI nests them under
    `prompt_tokens_details.cached_tokens`."""
    u = u or {}
    cached = (u.get("prompt_cache_hit_tokens")
              or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    prompt = u.get("prompt_tokens", 0)
    completion = u.get("completion_tokens", 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "cached_tokens": cached,
            "total_tokens": u.get("total_tokens", prompt + completion)}


def _claude_analyze(ctx: dict) -> dict:
    import anthropic  # lazy import so the package stays optional

    model = os.environ.get("ORACLE_ANALYST_MODEL") or "claude-opus-5"
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_SYSTEM,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": _prompt(ctx)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("claude refused the analysis request")
    text = next(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    cached = getattr(u, "cache_read_input_tokens", 0) or 0
    usage = {"prompt_tokens": u.input_tokens, "completion_tokens": u.output_tokens,
             "cached_tokens": cached,
             "total_tokens": u.input_tokens + u.output_tokens}
    return json.loads(text), usage


def _claude_complete(system: str, user: str, model: str) -> tuple[dict, dict]:
    """Generic Claude completion → (parsed_json, usage), for the multi-pass chain
    (JSON asked for in-prompt and extracted, since each pass has its own shape)."""
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model, max_tokens=4096, system=system,
        messages=[{"role": "user", "content": user}])
    if resp.stop_reason == "refusal":
        raise RuntimeError("claude refused the analysis request")
    text = next(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    usage = {"prompt_tokens": u.input_tokens, "completion_tokens": u.output_tokens,
             "cached_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
             "total_tokens": u.input_tokens + u.output_tokens}
    return _extract_json(text), usage


def get_analyst_llm() -> tuple[Callable[[dict], dict], str, str] | None:
    """Return (analyze_fn, provider, model) per env config, or None when the
    analyst is not configured (in which case the job no-ops and the rule-based
    pipeline stands alone)."""
    provider = (os.environ.get("ORACLE_ANALYST_PROVIDER") or "").strip().lower()
    if provider == "deepseek":
        if not os.environ.get("DEEPSEEK_API_KEY"):
            return None
        model = os.environ.get("ORACLE_ANALYST_MODEL") or "deepseek-chat"
        return (_deepseek_analyze, "deepseek", model)
    if provider == "claude":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        model = os.environ.get("ORACLE_ANALYST_MODEL") or "claude-opus-5"
        return (_claude_analyze, "claude", model)
    return None


def get_completer() -> tuple[Callable[[str, str, str], tuple[dict, dict]], str, str] | None:
    """Return (complete_fn, provider, work_model) for the multi-pass chain, or None
    when unconfigured. complete_fn(system, user, model) → (parsed_json, usage)."""
    provider = (os.environ.get("ORACLE_ANALYST_PROVIDER") or "").strip().lower()
    if provider == "deepseek" and os.environ.get("DEEPSEEK_API_KEY"):
        return (_deepseek_complete, "deepseek",
                os.environ.get("ORACLE_ANALYST_MODEL") or "deepseek-chat")
    if provider == "claude" and os.environ.get("ANTHROPIC_API_KEY"):
        return (_claude_complete, "claude",
                os.environ.get("ORACLE_ANALYST_MODEL") or "claude-opus-5")
    return None


def chain_enabled() -> bool:
    return (os.environ.get("ORACLE_ANALYST_MODE") or "single").strip().lower() == "chain"


def debate_enabled() -> bool:
    """Whether to ALSO run the adversarial bull/bear pass (off by default).

    Deliberately additive: the single pass still runs and is still recorded, so
    the two are scored on identical sessions. Turning this on adds cost; it never
    replaces the incumbent analyst until the measurement says it should.
    """
    return config._env_flag("ORACLE_ANALYST_DEBATE", False)


# ── job entrypoint ───────────────────────────────────────────────────────
def _record_calls(calls, trade_date, provider, model, created_at, variant) -> None:
    """Persist parsed calls under one analyst variant."""
    for c in calls:
        db.upsert_llm_call({
            "trade_date": trade_date,
            "sector": c["sector"],
            "provider": provider,
            "model": model,
            "direction": c["direction"],
            "conviction": c["conviction"],
            "tradeable_etf": c["tradeable_etf"],
            "key_drivers": json.dumps(c["key_drivers"]),
            "rationale": c["rationale"],
            "top_pick": json.dumps(c["top_pick"]) if c.get("top_pick") else None,
            "variant": variant,
            "created_at": created_at,
        })



def _meter(trade_date, created_at, call_type, provider, model, usage) -> None:
    """Record one call's tokens + estimated cost in the spend meter (best-effort;
    metering must never lose the calls we already persisted)."""
    if not usage:
        return
    try:
        from .pricing import cost_usd
        cost = cost_usd(model, usage["prompt_tokens"], usage["completion_tokens"],
                        usage.get("cached_tokens", 0))
        db.record_llm_usage({
            "trade_date": trade_date, "call_type": call_type,
            "provider": provider, "model": model,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "cached_tokens": usage.get("cached_tokens", 0),
            "total_tokens": usage["total_tokens"],
            "cost_usd": cost, "created_at": created_at})
        print(f"run_llm_analysis: {call_type} {usage['total_tokens']} tokens "
              f"(~${cost:.4f}) recorded")
    except Exception as e:  # noqa: BLE001
        print(f"run_llm_analysis: {call_type} metering skipped ({e!r})")


def run_llm_analysis(trade_date: str | None = None, llm=None, search_one=None,
                     complete=None) -> int:
    """Optional AI-analyst pass. Reads the day's data + reflection memory, runs an
    optional live web search for fresh context, then asks the LLM for per-sector
    calls — either a single pass (default) or the multi-pass reasoning chain
    (thesis → sector deep-dive → risk) when ORACLE_ANALYST_MODE=chain. Records the
    calls in `llm_calls` and meters every LLM pass. Returns the number of calls
    written; no-ops (returns 0) when the analyst is not configured. Never raises."""
    from . import analyst_chain, debate, divergence as dv, technicals as ta, websearch

    now = datetime.now(timezone.utc)
    trade_date = trade_date or now.date().isoformat()
    created_at = now.isoformat()

    # Chain mode when ORACLE_ANALYST_MODE=chain (or a completer is injected for
    # tests); single-pass otherwise.
    use_chain = complete is not None or (chain_enabled() and llm is None)
    if use_chain:
        resolved = (complete, "test", "test") if complete is not None else get_completer()
        mode = "chain"
    else:
        resolved = get_analyst_llm() if llm is None else (llm, "test", "test")
        mode = "single"
    if resolved is None:
        print("run_llm_analysis: analyst not configured "
              "(set ORACLE_ANALYST_PROVIDER=deepseek|claude + its API key) — skipping")
        return 0
    engine_fn, provider, model = resolved

    try:
        # Optional live web search (fail-soft: empty results if disabled/erroring).
        web, search_provider, n_queries = websearch.run_search(trade_date, search_one)
        if search_provider:
            print(f"run_llm_analysis: web search ({search_provider}) ran "
                  f"{n_queries} queries → {len(web)} results")

        # Per-sector technical indicators from each sector ETF's own price history
        # (filter by the canonical symbol so scales don't mix).
        techs = {}
        for sector in CHINA_SECTORS:
            symbol = config.CHINA_SECTOR_ETFS.get(sector)
            if not symbol:
                continue
            series = [r["close"] for r in
                      db.close_series("china_close", symbol=symbol, limit=120, end=trade_date)
                      if r["close"] is not None]
            if len(series) >= 2:
                techs[sector] = ta.compute_indicators(series)

        # Measured US-follows-vs-diverges label per sector (fail-soft).
        try:
            us_link = dv.classify_sectors(end=trade_date)
        except Exception:  # noqa: BLE001
            us_link = {}

        ctx = build_context(
            trade_date,
            db.get_rows_for_date("us_close", trade_date),
            db.get_rows_for_date("news", trade_date),
            db.macro_event_dates(trade_date),
            db.recent_reflections(5),
            web_research=web,
            technicals=techs,
            us_link=us_link,
        )

        # Produce the parsed calls, plus a list of per-call usages to meter.
        pass_usages: list[dict] = []
        if mode == "chain":
            thesis_model = os.environ.get("ORACLE_ANALYST_THESIS_MODEL") or model
            parsed, usages = analyst_chain.run_chain(ctx, engine_fn, model, thesis_model)
            print(f"run_llm_analysis: multi-pass chain ran {len(usages)} passes")
            pass_usages = [{"call_type": f"analyst-{u['pass']}", "model": u["model"],
                            "usage": u["usage"]} for u in usages]
        else:
            # Backends return (parsed, usage); a test stub may return just the parsed
            # dict — treat that as "no usage reported".
            result = engine_fn(ctx)
            parsed, usage = result if isinstance(result, tuple) else (result, None)
            pass_usages = [{"call_type": "analyst", "model": model, "usage": usage}]

        calls = parse_calls(parsed, trade_date)
        _record_calls(calls, trade_date, provider, model, created_at,
                      debate.VARIANT_SINGLE)

        # Optional adversarial pass on the SAME context. Recorded under its own
        # variant so both survive and the backtest can score them head-to-head;
        # fail-soft, because an experiment must never cost the incumbent call.
        if debate_enabled():
            try:
                completer = complete or (get_completer() or (None,))[0]
                if completer is None:
                    print("run_llm_analysis: debate needs a completer — skipping")
                else:
                    d_parsed, d_usages, transcript = debate.run_debate(
                        _prompt(ctx), _SYSTEM, completer, model)
                    d_calls = parse_calls(d_parsed, trade_date)
                    _record_calls(d_calls, trade_date, provider, model, created_at,
                                  debate.VARIANT_DEBATE)
                    pass_usages += [{"call_type": u["call_type"], "model": model,
                                     "usage": u.get("usage")} for u in d_usages]
                    print(f"run_llm_analysis: debate wrote {len(d_calls)} call(s) "
                          f"({len(transcript['bull'])}+{len(transcript['bear'])} chars argued)")
            except Exception as e:  # noqa: BLE001 — never lose the single pass
                print(f"run_llm_analysis: debate pass failed: {e!r}")

        # Meter every LLM pass (best-effort).
        for pu in pass_usages:
            _meter(trade_date, created_at, pu["call_type"], provider, pu["model"], pu["usage"])

        # Meter the web search too (per-query cost from config; 0 on a free tier).
        if search_provider and n_queries:
            try:
                search_cost = round(n_queries * config.SEARCH_PRICE_PER_QUERY, 6)
                db.record_llm_usage({
                    "trade_date": trade_date, "call_type": "search",
                    "provider": search_provider, "model": f"search:{search_provider}",
                    "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                    "total_tokens": 0, "cost_usd": search_cost, "created_at": created_at})
            except Exception as e:  # noqa: BLE001
                print(f"run_llm_analysis: search metering skipped ({e!r})")

        print(f"run_llm_analysis: wrote {len(calls)} {provider} call(s) "
              f"({model}) for {trade_date}")
        return len(calls)
    except Exception as e:  # noqa: BLE001 — analyst must never crash the pipeline
        print(f"run_llm_analysis FAILED (rule-based prediction stands): {e!r}")
        return 0
