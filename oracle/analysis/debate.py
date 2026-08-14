"""Adversarial analyst pass — a bull case, a bear case, then a synthesis.

Adapted from the deliberation structure in TauricResearch/TradingAgents. The
single-pass analyst produces one view with nothing arguing against it, which is
the condition under which a language model is most prone to assemble a tidy story
out of whatever the inputs happen to suggest. Forcing an explicit counter-case
and then making a third turn engage both is a cheap check on that.

Three turns per session (not per sector), so the cost is ~3x the single pass on a
base that has spent $0.13 to date:

  1. **Bull** — argues the constructive case across all sectors.
  2. **Bear** — sees the bull's argument and must rebut it specifically.
  3. **Synthesis** — reads both and issues the actual per-sector calls, in the
     same JSON shape the single-pass analyst emits, so everything downstream
     (parsing, validation, scoring) is unchanged.

**What this is not.** The bull and bear are instructed to advocate, not to
assess: each argues its side regardless of where the evidence points. That
sharpens the reasoning available to the synthesis turn but does NOT make the
debate a probability estimate, and the two advocacy turns are deliberately never
recorded as calls. Only the synthesis produces a direction. The rule-based
composite remains the calibrated quantity; this is a check on it, not a
replacement for it.

**Why it records under a variant.** Both analysts run on identical inputs and
both are written to `llm_calls` under different `variant` values, so the existing
backtest scores them head-to-head on the same sessions. A structural improvement
that cannot be measured against what it replaces is a preference, not a finding —
and this codebase has already been wrong often enough about which appealing
change survives contact with a holdout.

Prompt building and parsing are pure functions; the network lives in the caller.

**Not investment advice.**
"""
from __future__ import annotations

import json

VARIANT_SINGLE = "single"
VARIANT_DEBATE = "debate"

# The advocacy turns produce prose, not calls. Capped so a long argument cannot
# crowd the synthesis prompt (and the bill) — the synthesis needs the reasoning,
# not every word of it.
_MAX_ARGUMENT_CHARS = 3000

BULL_SYSTEM = (
    "You are the bull analyst on a China A-share desk. Argue the constructive "
    "case for the coming session as strongly as the evidence permits.\n"
    "Ground every claim in the supplied data — cite the specific US sector move, "
    "headline, or indicator you are relying on. Name the sectors where the "
    "constructive case is weakest; a bull who claims everything is strong is "
    "useless to the desk.\n"
    "Prose only, no JSON, under 400 words."
)

BEAR_SYSTEM = (
    "You are the bear analyst on a China A-share desk. Argue the cautious case "
    "for the coming session, and rebut the bull's argument specifically — quote "
    "the claim you are attacking and say what it overlooks.\n"
    "Ground every claim in the supplied data. Name the sectors where the bull is "
    "right and you have no case; a bear who is negative on everything is useless "
    "to the desk.\n"
    "Prose only, no JSON, under 400 words."
)

# Prepended to the single-pass system prompt for the synthesis turn. It is a
# PREFIX rather than a replacement so the output contract — every field, the
# JSON shape, the watch-list rules — is byte-identical between the two variants.
# If the two asked for different shapes, a measured difference in accuracy could
# be the shape rather than the reasoning, and the comparison would prove nothing.
SYNTHESIS_PREFIX = (
    "You have heard a bull analyst and a bear analyst argue this same session "
    "from the same data. Weigh both. Where you side with one, say which claim "
    "decided it; where they are evenly matched, that is a neutral call and you "
    "should make one. Do not split the difference to seem balanced, and do not "
    "adopt whichever argument was longer.\n\n"
)


def synthesis_system(single_pass_system: str) -> str:
    """The synthesis turn's system prompt: debate framing + the unchanged
    single-pass contract. Pure."""
    return SYNTHESIS_PREFIX + single_pass_system


def clip(text: str, limit: int = _MAX_ARGUMENT_CHARS) -> str:
    """Trim an advocacy turn for re-injection. Pure."""
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + " […]"


def bull_prompt(base_prompt: str) -> str:
    """Turn 1 — the constructive case, from the same context the single pass sees."""
    return f"{base_prompt}\n\nArgue the constructive case for this session."


def bear_prompt(base_prompt: str, bull_argument: str) -> str:
    """Turn 2 — the cautious case, which must engage turn 1."""
    return (
        f"{base_prompt}\n\n"
        "The bull analyst argued:\n"
        f"---\n{clip(bull_argument)}\n---\n\n"
        "Argue the cautious case and rebut the bull specifically."
    )


def synthesis_prompt(base_prompt: str, bull_argument: str,
                     bear_argument: str) -> str:
    """Turn 3 — the calls, on the same context both advocates saw."""
    return (
        f"{base_prompt}\n\n"
        "Bull analyst:\n"
        f"---\n{clip(bull_argument)}\n---\n\n"
        "Bear analyst:\n"
        f"---\n{clip(bear_argument)}\n---\n\n"
        "Now issue the desk's per-sector calls."
    )


def run_debate(base_prompt: str, single_pass_system: str, complete,
               model: str) -> tuple[dict, list[dict], dict]:
    """Run the three turns. ``complete(system, user, model) -> (parsed, usage)``.

    Returns ``(parsed_calls_json, per_turn_usages, transcript)``. The caller
    persists the calls; the transcript is returned for the record rather than
    thrown away, because a debate whose reasoning is unavailable cannot be
    audited when it disagrees with the single pass.
    """
    usages: list[dict] = []

    # Usage is nested, not spread: the meter reads `entry["usage"]`, and a flat
    # dict silently metered nothing — which is the one thing an experiment whose
    # whole risk is cost must not get wrong.
    bull_raw, u1 = complete(BULL_SYSTEM, bull_prompt(base_prompt), model)
    usages.append({"call_type": "analyst-debate-bull", "usage": u1})
    bull = _as_text(bull_raw)

    bear_raw, u2 = complete(BEAR_SYSTEM, bear_prompt(base_prompt, bull), model)
    usages.append({"call_type": "analyst-debate-bear", "usage": u2})
    bear = _as_text(bear_raw)

    parsed, u3 = complete(
        synthesis_system(single_pass_system),
        synthesis_prompt(base_prompt, bull, bear),
        model,
    )
    usages.append({"call_type": "analyst-debate-synthesis", "usage": u3})

    return parsed, usages, {"bull": bull, "bear": bear}


def _as_text(raw) -> str:
    """The advocacy turns are prose, but the shared completer parses JSON. Accept
    either so a provider that wraps prose in a JSON envelope still works."""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("text", "content", "argument", "raw"):
            if isinstance(raw.get(key), str):
                return raw[key]
        return json.dumps(raw, ensure_ascii=False)
    return str(raw or "")
