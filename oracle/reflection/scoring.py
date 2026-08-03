"""(i) Prediction scoring — mathematical (spec §4b-i).

Runs after the actual China close is in. For every prediction on a date, find
the actual sector move, record whether the directional call was right, and a
Brier term so calibration (not just accuracy) is measurable: a system that's
55% accurate but calls everything "high confidence" must be visibly
miscalibrated, not hidden behind one hit-rate number.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .. import db
from .stats import CONFIDENCE_P, brier, direction_from_move


def actual_sector_move(china_rows: list[dict], sector: str) -> float | None:
    """Mean actual % change for a China sector from that day's close rows.
    None if we have no actual data for the sector (e.g. sector-index ingestion
    not yet wired) — such predictions are left unscored rather than guessed."""
    moves = [r["pct_change"] for r in china_rows
             if r.get("sector") == sector and r.get("pct_change") is not None]
    if not moves:
        return None
    return sum(moves) / len(moves)


def score_predictions(trade_date: str | None = None) -> int:
    """Score all predictions for `trade_date` against the actual China close.
    Returns the number scored. Never raises."""
    now = datetime.now(timezone.utc)
    trade_date = trade_date or now.date().isoformat()
    scored_at = now.isoformat()
    try:
        preds = db.predictions_for_date(trade_date)
        china_rows = db.get_rows_for_date("china_close", trade_date)
        n = 0
        for p in preds:
            move = actual_sector_move(china_rows, p["sector"])
            if move is None:
                continue  # no actual for this sector — honestly left unscored
            actual_dir = direction_from_move(move)
            correct = p["direction"] == actual_dir
            db.insert_prediction_score({
                "prediction_id": p["id"],
                "actual_direction": actual_dir,
                "actual_pct_change": round(move, 4),
                "correct": 1 if correct else 0,
                "confidence_p": CONFIDENCE_P.get(p["confidence"], 0.5),
                "brier": brier(p["confidence"], correct),
                "scored_at": scored_at,
            })
            n += 1
        print(f"score_predictions: scored {n} predictions for {trade_date}")
        return n
    except Exception as e:  # noqa: BLE001
        print(f"score_predictions FAILED: {e!r}")
        return 0
