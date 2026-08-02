"""Optional LLM backend for the daily reflection (spec §4b-iii).

The reflection loop runs deterministically by default (rule-based generator in
reflect.py). This module provides an optional LLM upgrade behind the same
`generate_reflection(llm=...)` seam, honoring the spec's guardrails:

  * the LLM only *interprets* real data we hand it — it never invents the
    predicted/actual numbers (those are filled in deterministically from the
    context, §7b: "don't let any LLM research from memory");
  * it's provider-agnostic — Claude (via the official Anthropic SDK) or DeepSeek
    (its OpenAI-compatible endpoint), selected by env var;
  * if no provider/key is configured, or the call fails, the caller falls back
    to the deterministic generator — so the loop never depends on the LLM.

Configuration (env):
    ORACLE_LLM_PROVIDER = claude | deepseek | (unset -> rule-based)
    ORACLE_LLM_MODEL    = model id override
        claude default: claude-opus-5   (spec suggests Sonnet-tier is enough —
                         set ORACLE_LLM_MODEL=claude-sonnet-5 to use it)
        deepseek default: deepseek-chat
    ANTHROPIC_API_KEY   (claude)   /   DEEPSEEK_API_KEY (deepseek)
"""
from __future__ import annotations

import json
import os
from typing import Callable

_SYSTEM = (
    "You are the reflection step of a China-market prediction system. You are "
    "given the day's per-sector predictions, the actual China closes, and how "
    "each component signal agreed or disagreed with the actual move. Interpret "
    "ONLY this data — never invent tickers, numbers, or facts not provided. "
    "Return a concise, honest post-mortem as JSON with exactly these keys: "
    "signals_that_worked (array of strings), signals_that_missed (array), "
    "likely_reason_for_miss (string), suggested_weight_adjustment (object with "
    "signal, direction one of increase/decrease/none, magnitude one of "
    "small/med/large), confidence_in_this_reflection (one of low/med/high). "
    "Base confidence on how many sectors were actually scored."
)

# JSON schema for Claude structured outputs (interpretation fields only).
_SCHEMA = {
    "type": "object",
    "properties": {
        "signals_that_worked": {"type": "array", "items": {"type": "string"}},
        "signals_that_missed": {"type": "array", "items": {"type": "string"}},
        "likely_reason_for_miss": {"type": "string"},
        "suggested_weight_adjustment": {
            "type": "object",
            "properties": {
                "signal": {"type": "string"},
                "direction": {"type": "string", "enum": ["increase", "decrease", "none"]},
                "magnitude": {"type": "string", "enum": ["small", "med", "large"]},
            },
            "required": ["signal", "direction", "magnitude"],
            "additionalProperties": False,
        },
        "confidence_in_this_reflection": {"type": "string", "enum": ["low", "med", "high"]},
    },
    "required": [
        "signals_that_worked", "signals_that_missed", "likely_reason_for_miss",
        "suggested_weight_adjustment", "confidence_in_this_reflection",
    ],
    "additionalProperties": False,
}


def _prompt(ctx: dict) -> str:
    """Render the deterministic context into a plain-text prompt. The numbers
    come from the DB, not the model."""
    lines = [f"Trade date: {ctx['trade_date']}", "", "Per-sector predicted vs actual:"]
    for r in ctx["rows"]:
        lines.append(
            f"  {r['sector']}: predicted {r['predicted']} ({r['confidence']} conf), "
            f"actual {r['actual']} (move {r['actual_move']:+.2f}%), "
            f"correct={r['correct']}"
        )
    lines.append("")
    lines.append("Per-signal agreement with the actual direction this session:")
    for sig in ("us_spillover", "sentiment_score"):
        lines.append(f"  {sig}: agreed {ctx['agree'][sig]}, disagreed {ctx['disagree'][sig]}")
    return "\n".join(lines)


def _assemble(ctx: dict, interp: dict) -> dict:
    """Merge the LLM's interpretation with the deterministic facts (date,
    predicted, actual) into the full reflection schema reflect.py persists."""
    return {
        "date": ctx["trade_date"],
        "predicted": {r["sector"]: r["predicted"] for r in ctx["rows"]},
        "actual": {r["sector"]: r["actual"] for r in ctx["rows"]},
        "signals_that_worked": interp.get("signals_that_worked", []),
        "signals_that_missed": interp.get("signals_that_missed", []),
        "likely_reason_for_miss": interp.get("likely_reason_for_miss", ""),
        "suggested_weight_adjustment": interp.get("suggested_weight_adjustment", {}),
        "confidence_in_this_reflection": interp.get("confidence_in_this_reflection", "low"),
    }


# ── Claude backend (official Anthropic SDK) ──────────────────────────────
def _claude_interpret(ctx: dict) -> dict:
    import anthropic  # lazy import so the package stays optional

    model = os.environ.get("ORACLE_LLM_MODEL") or "claude-opus-5"
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=model,
        max_tokens=4096,  # headroom: thinking is on by default on Opus/Sonnet 5
        system=_SYSTEM,
        # effort low — this is a small, well-scoped interpretation task
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
        messages=[{"role": "user", "content": _prompt(ctx)}],
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("claude refused the reflection request")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


# ── DeepSeek backend (OpenAI-compatible endpoint, stdlib only) ───────────
def _deepseek_interpret(ctx: dict) -> dict:
    import urllib.request

    key = os.environ["DEEPSEEK_API_KEY"]
    model = os.environ.get("ORACLE_LLM_MODEL") or "deepseek-chat"
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _prompt(ctx)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — fixed host
        payload = json.loads(r.read())
    return json.loads(payload["choices"][0]["message"]["content"])


def get_reflection_llm() -> Callable[[dict], dict] | None:
    """Return an `llm(ctx) -> full reflection dict` callable per env config, or
    None when no provider (or its key) is configured — in which case the caller
    uses the deterministic rule-based generator."""
    provider = (os.environ.get("ORACLE_LLM_PROVIDER") or "").strip().lower()

    if provider == "claude":
        # SDK resolves ANTHROPIC_API_KEY / ant-login profile itself; we only
        # gate on the env var to keep the "not configured -> fallback" path.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return None
        return lambda ctx: _assemble(ctx, _claude_interpret(ctx))

    if provider == "deepseek":
        if not os.environ.get("DEEPSEEK_API_KEY"):
            return None
        return lambda ctx: _assemble(ctx, _deepseek_interpret(ctx))

    return None
