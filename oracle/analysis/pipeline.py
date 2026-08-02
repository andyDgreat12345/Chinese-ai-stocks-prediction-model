"""run_analysis pipeline (spec §4, Phase 3).

Turns the day's ingested `us_close` + `news` rows into per-China-sector
predictions:

    US sector moves  ─┐
    news sentiment   ─┼─► build_signals() ─► score_sector() ─► predictions
    macro events     ─┘

`build_signals` is a pure function of the fetched rows so it is testable on
fixtures; `run_analysis` does the DB read/write around it.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import config, db
from .scoring import SectorSignals, score_sector

# China sectors we produce a directional read for (v1 universe).
CHINA_SECTORS = ["broad", "growth", "semis", "energy", "financials"]

# Which US sector tags spill over into each China sector (spec §4.1). This is
# the v1 *assumption*; the reflection loop (§4b-ii) replaces it with measured
# correlation coefficients once enough data accumulates.
CHINA_SPILLOVER_SOURCES: dict[str, list[str]] = {
    "broad": ["broad", "tech"],
    "growth": ["tech"],
    "semis": ["semis", "tech"],
    "energy": ["energy"],
    "financials": ["financials"],
}

# News categories that bear on each China sector (spec §4b-ii buckets).
SECTOR_NEWS_CATEGORIES: dict[str, list[str]] = {
    "broad": ["fed_policy", "macro_data", "china_stimulus", "tariffs"],
    "growth": ["fed_policy", "china_stimulus"],
    "semis": ["chip_export", "tariffs"],
    "energy": ["macro_data", "tariffs"],
    "financials": ["fed_policy", "china_stimulus"],
}

# A US daily move of this magnitude (%) counts as a full-strength (±1) signal.
SPILLOVER_SCALE_PCT = 2.0


def _clamp(x: float) -> float:
    return max(-1.0, min(1.0, x))


def build_signals(
    us_rows: list[dict],
    news_rows: list[dict],
    macro_events: list[dict],
) -> dict[str, SectorSignals]:
    """Compose component signals per China sector from fetched rows. Pure."""
    # US % change averaged per sector tag.
    us_by_sector: dict[str, list[float]] = {}
    for r in us_rows:
        if r.get("sector") is None or r.get("pct_change") is None:
            continue
        us_by_sector.setdefault(r["sector"], []).append(r["pct_change"])

    # News sentiment grouped by category.
    sent_by_category: dict[str, list[float]] = {}
    for r in news_rows:
        if r.get("category") is None or r.get("sentiment") is None:
            continue
        sent_by_category.setdefault(r["category"], []).append(r["sentiment"])

    has_macro = len(macro_events) > 0

    signals: dict[str, SectorSignals] = {}
    for sector in CHINA_SECTORS:
        # US spillover: mean of mapped US sector moves, scaled + clamped.
        moves: list[float] = []
        for tag in CHINA_SPILLOVER_SOURCES.get(sector, []):
            moves.extend(us_by_sector.get(tag, []))
        spillover = 0.0
        if moves:
            spillover = _clamp((sum(moves) / len(moves)) / SPILLOVER_SCALE_PCT)

        # Sentiment: mean sentiment across the sector's relevant categories.
        sents: list[float] = []
        for cat in SECTOR_NEWS_CATEGORIES.get(sector, []):
            sents.extend(sent_by_category.get(cat, []))
        sentiment = _clamp(sum(sents) / len(sents)) if sents else 0.0

        signals[sector] = SectorSignals(
            us_spillover=round(spillover, 4),
            sentiment=round(sentiment, 4),
            macro=0.0,               # events flagged, not directionally scored in v1
            macro_flag=has_macro,
        )
    return signals


_MAGNITUDE_DELTA = {"small": 0.02, "med": 0.05, "large": 0.10}


def _apply_suggested_adjustments(weights: dict[str, float], reflections: list[dict]) -> dict:
    """Apply the most recent reflection's suggested weight nudge, then
    renormalize. Only reached when config.AUTO_APPLY_WEIGHT_ADJUSTMENTS is on;
    until then suggestions are review-only (spec §4b-iii, human-in-the-loop)."""
    import json

    w = dict(weights)
    try:
        adj = json.loads(reflections[0].get("suggested_adjustment") or "{}")
    except (json.JSONDecodeError, TypeError):
        return w
    signal, direction = adj.get("signal"), adj.get("direction")
    if signal not in w or direction not in ("increase", "decrease"):
        return w
    delta = _MAGNITUDE_DELTA.get(adj.get("magnitude"), 0.02)
    w[signal] = max(0.0, w[signal] + (delta if direction == "increase" else -delta))
    total = sum(w.values()) or 1.0
    return {k: round(v / total, 4) for k, v in w.items()}


def run_analysis(trade_date: str | None = None) -> int:
    """Job entrypoint: read the day's data, score every sector, persist
    predictions. Returns the number of predictions written. Never raises."""
    now = datetime.now(timezone.utc)
    trade_date = trade_date or now.date().isoformat()
    created_at = now.isoformat()
    try:
        us_rows = db.get_rows_for_date("us_close", trade_date)
        news_rows = db.get_rows_for_date("news", trade_date)
        macro = db.macro_event_dates(trade_date)
        weights = db.get_weights()

        # Retrieve recent reflections before predicting (spec §4b-iii). Weight
        # adjustments they suggest are applied only if auto-apply is enabled;
        # otherwise they inform review, not this run's weights.
        recent = db.recent_reflections(5)
        if recent:
            print(f"run_analysis: considering {len(recent)} recent reflection(s)")
            if config.AUTO_APPLY_WEIGHT_ADJUSTMENTS:
                weights = _apply_suggested_adjustments(weights, recent)

        signals = build_signals(us_rows, news_rows, macro)
        written = 0
        for sector, sig in signals.items():
            pred = score_sector(sig, weights)
            db.upsert_prediction({
                "trade_date": trade_date,
                "sector": sector,
                "direction": pred.direction,
                "confidence": pred.confidence,
                "composite_score": pred.composite,
                "us_spillover": sig.us_spillover,
                "sentiment_score": sig.sentiment,
                "macro_flag": 1 if sig.macro_flag else 0,
                "rationale": pred.rationale,
                "created_at": created_at,
            })
            written += 1
        print(f"run_analysis: wrote {written} predictions for {trade_date}")
        return written
    except Exception as e:  # noqa: BLE001 — job must not crash scheduler
        print(f"run_analysis FAILED: {e!r}")
        return 0
