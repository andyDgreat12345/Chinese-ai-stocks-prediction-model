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
CHINA_SECTORS = ["broad", "growth", "semis", "energy", "financials",
                 "healthcare", "consumer", "brokers", "defense", "newenergy"]

# Which US sector tags spill over into each China sector (spec §4.1). This is
# the v1 *assumption*; the reflection loop (§4b-ii) replaces it with measured
# correlation coefficients once enough data accumulates.
CHINA_SPILLOVER_SOURCES: dict[str, list[str]] = {
    "broad": ["broad", "tech"],
    "growth": ["tech"],
    "semis": ["semis", "tech"],
    "energy": ["energy"],
    "financials": ["financials"],
    "healthcare": ["healthcare"],
    "consumer": ["staples"],
    "brokers": ["financials"],
    # Deliberately mapped to industrials rather than "broad": A-share defense is
    # driven by domestic procurement and policy, so if the divergence hypothesis
    # is right this sector should show a WEAK US link. Mapping it to a broad
    # index would manufacture a correlation that is really just market beta.
    "defense": ["industrials"],
    "newenergy": ["tech", "energy"],
}

# News categories that bear on each China sector (spec §4b-ii buckets).
SECTOR_NEWS_CATEGORIES: dict[str, list[str]] = {
    "broad": ["fed_policy", "macro_data", "china_stimulus", "tariffs"],
    "growth": ["fed_policy", "china_stimulus"],
    "semis": ["chip_export", "tariffs"],
    "energy": ["macro_data", "tariffs"],
    "financials": ["fed_policy", "china_stimulus"],
    "healthcare": ["china_stimulus", "macro_data"],
    "consumer": ["china_stimulus", "macro_data"],
    "brokers": ["china_stimulus", "fed_policy"],
    "defense": ["tariffs", "china_stimulus"],
    "newenergy": ["china_stimulus", "tariffs", "chip_export"],
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


def attach_technicals(signals: dict, trade_date: str, db_path=None) -> dict:
    """Add each sector's technical signals, computed only from closes STRICTLY
    BEFORE `trade_date` — the same rule the backtest replay uses, so live and
    replayed predictions are produced from identical information. Fail-soft: a
    sector with too little history keeps its zeroed technical fields."""
    from dataclasses import replace

    from .. import config, db
    from . import technicals as ta

    out = {}
    for sector, sig in signals.items():
        symbol = config.CHINA_SECTOR_ETFS.get(sector)
        try:
            closes = [r["close"] for r in db.close_series(
                "china_close", symbol=symbol, limit=120, before=trade_date,
                db_path=db_path) if r["close"] is not None]
            out[sector] = (replace(sig, **ta.signals_from_closes(closes))
                           if len(closes) >= 2 else sig)
        except Exception:  # noqa: BLE001 — technicals are additive, never fatal
            out[sector] = sig
    return out


def pre_open_refresh(trade_date: str | None = None) -> int:
    """09:15 CST confidence re-check (spec §2). Re-scores today's sectors on the
    latest news (any 05:00–09:15 breaking headlines the job just pulled) and
    adjusts each morning prediction's *confidence only* — direction and the
    stored morning component signals are preserved so the §4b-i audit trail
    stays honest. If breaking news would flip a call, confidence is dropped to
    'low' and the contradiction is noted. Returns the number adjusted.

    Pure DB I/O (no network) — the breaking-news fetch lives in the job wrapper.
    """
    now = datetime.now(timezone.utc)
    trade_date = trade_date or now.date().isoformat()
    try:
        us_rows = db.get_rows_for_date("us_close", trade_date)
        news_rows = db.get_rows_for_date("news", trade_date)
        macro = db.macro_event_dates(trade_date)
        weights = db.get_weights()
        signals = build_signals(us_rows, news_rows, macro)

        adjusted = 0
        for p in db.predictions_for_date(trade_date):
            sig = signals.get(p["sector"])
            if sig is None:
                continue
            fresh = score_sector(sig, weights)
            new_conf, note = p["confidence"], None
            if fresh.direction != p["direction"]:
                new_conf = "low"
                note = f"pre-open: breaking news contradicts morning {p['direction']} call"
            elif fresh.confidence != p["confidence"]:
                new_conf = fresh.confidence
                note = f"pre-open: confidence {p['confidence']} -> {new_conf} on breaking news"
            if note is None:
                continue
            db.upsert_prediction({
                "trade_date": p["trade_date"], "sector": p["sector"],
                "direction": p["direction"], "confidence": new_conf,
                "composite_score": p["composite_score"],
                "us_spillover": p["us_spillover"],
                "sentiment_score": p["sentiment_score"],
                "macro_flag": p["macro_flag"],
                "rationale": f"{p['rationale']} | {note}",
                "created_at": p["created_at"],
            })
            adjusted += 1
        print(f"pre_open_refresh: adjusted {adjusted} prediction(s) for {trade_date}")
        return adjusted
    except Exception as e:  # noqa: BLE001
        print(f"pre_open_refresh FAILED: {e!r}")
        return 0


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
        signals = attach_technicals(signals, trade_date)
        written = 0
        for sector, sig in signals.items():
            # Per-sector LEARNED parameters (weights + abstain threshold) when the
            # tuner has adopted any; otherwise this falls back to the global
            # weights row and the hand-set defaults, unchanged.
            p = db.get_model_params(sector)
            pred = score_sector(sig, p, p.get("threshold"))
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
