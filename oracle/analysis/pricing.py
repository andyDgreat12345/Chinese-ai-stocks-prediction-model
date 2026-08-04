"""LLM token → USD cost estimation (the meter's arithmetic).

Pure functions over token counts and the rate table in ``config.LLM_PRICES``.
Kept deliberately small and provider-agnostic: the analyst (and any future
deep-research passes) hand it the tokens a provider reported, and it returns an
estimated dollar cost, splitting prompt tokens into the cheaper cache-hit tier
when the provider tells us how many were served from cache.

**Estimate only.** Off-peak discounts, tiered/volume pricing, and mid-month rate
changes are not modeled — the provider's invoice is the source of truth. This
exists so deeper research can be run with a live cost signal, not a surprise.
"""
from __future__ import annotations

from .. import config


def rates(model: str) -> dict:
    """Per-1M-token rates for a model, falling back to a generic tier."""
    return config.LLM_PRICES.get(model, config.LLM_PRICE_FALLBACK)


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int,
             cached_tokens: int = 0) -> float:
    """Estimated USD cost of one call. ``cached_tokens`` (a subset of
    ``prompt_tokens``) is billed at the cheaper cache-hit rate; the rest of the
    prompt at the cache-miss (``input``) rate; completion at the ``output`` rate."""
    r = rates(model)
    cached = max(0, min(cached_tokens, prompt_tokens))
    miss = max(0, prompt_tokens - cached)
    dollars = (
        cached * r.get("cache_hit", r["input"])
        + miss * r["input"]
        + completion_tokens * r["output"]
    ) / 1_000_000
    return round(dollars, 6)
