"""Multi-pass reasoning chain — the "real analyst desk" upgrade.

The default analyst is a single LLM call: data in, per-sector calls out. That's
one shallow pass. This runs the same inputs through three chained passes, the way
a real desk works, so the final call is argued rather than blurted:

  1. **Macro thesis** — a strategist reads the whole overnight tape + news + web
     research into a regime view (risk-on/off), the key risks, and a per-sector
     bias. (Optionally on a reasoning model — ``ORACLE_ANALYST_THESIS_MODEL``.)
  2. **Sector deep-dive** — an analyst builds an explicit bull case AND bear case
     for every sector against that thesis, then a preliminary call — free to
     disagree with the thesis where the sector's own evidence warrants.
  3. **Devil's advocate / risk** — a risk officer stress-tests each preliminary
     call, downgrades conviction where the bear case is strong or evidence thin,
     and emits the FINAL calls in the canonical schema the rest of the system
     already consumes.

``run_chain`` is pure orchestration over a ``complete_fn(system, user, model) ->
(parsed_json, usage)`` injected by the caller, so it unit-tests with a stub — no
network. Each pass's token usage is returned so the caller can meter all three
(this is ~3× the single-pass token cost — hence the meter shipped first, and why
the chain is opt-in via ``ORACLE_ANALYST_MODE=chain``).

**Not investment advice.** The extra rigor sharpens a probabilistic lean; it is
never a guarantee or a buy/sell instruction.
"""
from __future__ import annotations

import json
from typing import Callable

# ── pass 1: macro thesis ──────────────────────────────────────────────────
_THESIS_SYSTEM = (
    "You are the chief strategist of a China-equity desk. From one trading day's "
    "US closes, overnight world news, macro calendar, fresh web-search snippets, "
    "and how the desk's recent predictions fared, write a concise MACRO THESIS for "
    "the coming China session. Reason ONLY from the provided data plus general "
    "market mechanics — do not invent specifics. Return STRICT JSON: "
    '{"regime": "risk-on"|"risk-off"|"mixed", "thesis": <2-3 sentences>, '
    '"key_risks": [<short strings>], "sector_bias": {<sector>: '
    '"constructive"|"cautious"|"neutral"}}.'
)

# ── pass 2: per-sector deep-dive ──────────────────────────────────────────
_DEEPDIVE_SYSTEM = (
    "You are a sector analyst. Given the macro thesis and the day's data, for EACH "
    "listed China sector build BOTH the bull case and the bear case from the "
    "evidence, then a preliminary directional call. Weigh the thesis but DISAGREE "
    "with it where the sector's own evidence warrants — do not just echo it. Reason "
    "only from provided data. Return STRICT JSON: "
    '{"calls": [{"sector": <one of the sectors>, "direction": '
    '"bullish"|"neutral"|"bearish", "conviction": "low"|"med"|"high", '
    '"bull_case": <short>, "bear_case": <short>, "key_drivers": [<short strings>], '
    '"rationale": <one sentence>}]}.'
)

# ── pass 3: devil's advocate / risk officer (emits the FINAL calls) ────────
_RISK_SYSTEM = (
    "You are the desk's risk officer. Stress-test each preliminary sector call: "
    "what concretely would make it wrong? Downgrade conviction where the bear case "
    "is strong or the evidence is thin, and flip a call only when clearly "
    "warranted. Then emit the FINAL calls. Output is a probabilistic lean for a "
    "human to weigh — NOT investment advice, NOT a guarantee, NEVER a buy/sell "
    "instruction. Return STRICT JSON with EXACTLY this shape: "
    '{"calls": [{"sector": <one of the sectors>, "direction": '
    '"bullish"|"neutral"|"bearish", "conviction": "low"|"med"|"high", '
    '"key_drivers": [<short strings>], "rationale": <one sentence including the '
    'main risk>}], "market_note": <one sentence overall context>}. '
    "Default to neutral/low conviction when inputs are thin or conflicting."
)


def _data_payload(ctx: dict) -> str:
    keep = ("us_closes", "news", "macro_events", "web_research", "recent_performance")
    return json.dumps({k: ctx.get(k) for k in keep}, ensure_ascii=False)


def thesis_prompt(ctx: dict) -> str:
    return (f"Trade date (China session to call): {ctx['trade_date']}\n"
            f"Sectors: {', '.join(ctx['sectors'])}\n\nData (JSON):\n{_data_payload(ctx)}")


def deepdive_prompt(ctx: dict, thesis: dict) -> str:
    return (f"Trade date: {ctx['trade_date']}\n"
            f"Sectors to cover: {', '.join(ctx['sectors'])}\n"
            f"Foreign-tradeable proxy per sector: {json.dumps(ctx.get('sector_tradeable_etf', {}))}\n\n"
            f"Macro thesis (JSON):\n{json.dumps(thesis, ensure_ascii=False)}\n\n"
            f"Data (JSON):\n{_data_payload(ctx)}")


def risk_prompt(ctx: dict, thesis: dict, deepdive: dict) -> str:
    return (f"Sectors: {', '.join(ctx['sectors'])}\n\n"
            f"Macro thesis (JSON):\n{json.dumps(thesis, ensure_ascii=False)}\n\n"
            f"Preliminary sector calls with bull/bear cases (JSON):\n"
            f"{json.dumps(deepdive, ensure_ascii=False)}\n\n"
            "Produce the FINAL stress-tested calls in the required JSON shape.")


def run_chain(ctx: dict, complete_fn: Callable[[str, str, str], tuple[dict, dict]],
              work_model: str, thesis_model: str | None = None) -> tuple[dict, list[dict]]:
    """Run the three passes and return (final_calls_json, usages). `usages` is a
    list of {pass, model, usage} so the caller can meter each pass. The final JSON
    is in the canonical analyst shape ({"calls": [...], "market_note": ...})."""
    thesis_model = thesis_model or work_model
    usages: list[dict] = []

    thesis, u = complete_fn(_THESIS_SYSTEM, thesis_prompt(ctx), thesis_model)
    usages.append({"pass": "thesis", "model": thesis_model, "usage": u})

    deep, u = complete_fn(_DEEPDIVE_SYSTEM, deepdive_prompt(ctx, thesis), work_model)
    usages.append({"pass": "deepdive", "model": work_model, "usage": u})

    final, u = complete_fn(_RISK_SYSTEM, risk_prompt(ctx, thesis, deep), work_model)
    usages.append({"pass": "risk", "model": work_model, "usage": u})

    # Carry the strategist's one-line regime read into market_note if the risk pass
    # didn't set one, so the daily report still has overall context.
    if isinstance(final, dict) and not final.get("market_note") and isinstance(thesis, dict):
        final["market_note"] = thesis.get("thesis")
    return final, usages
