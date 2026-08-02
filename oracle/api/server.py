"""Local FastAPI serving the dashboard's data contract (spec §5, Phase 4).

The same engine that runs the batch jobs also serves these read-only endpoints;
the dashboard polls them and does no live compute of its own.

    GET /api/prediction  -> latest per-sector predictions + disclaimer
    GET /api/heatmap     -> sectors colored by predicted score
    GET /api/history     -> recent predictions joined with actual outcomes
    GET /api/accuracy    -> rolling directional hit-rate
    GET /api/health      -> liveness

Run:  uvicorn oracle.api.server:app --port 8000
"""
from __future__ import annotations

from fastapi import FastAPI

from .. import config, db

app = FastAPI(title="China Market Oracle", version="0.1.0")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "disclaimer": config.DISCLAIMER}


@app.get("/api/prediction")
def prediction() -> dict:
    preds = db.latest_predictions()
    trade_date = preds[0]["trade_date"] if preds else None
    return {
        "trade_date": trade_date,
        "disclaimer": config.DISCLAIMER,
        "predictions": [
            {
                "sector": p["sector"],
                "direction": p["direction"],
                "confidence": p["confidence"],
                "composite_score": p["composite_score"],
                "rationale": p["rationale"],
                "signals": {
                    "us_spillover": p["us_spillover"],
                    "sentiment": p["sentiment_score"],
                    "macro_flag": bool(p["macro_flag"]),
                },
            }
            for p in preds
        ],
    }


@app.get("/api/heatmap")
def heatmap() -> dict:
    """Sectors colored by the *predicted* China-sector score, not raw price (§5)."""
    preds = db.latest_predictions()
    return {
        "trade_date": preds[0]["trade_date"] if preds else None,
        "disclaimer": config.DISCLAIMER,
        "cells": [
            {
                "sector": p["sector"],
                "score": p["composite_score"],   # -1..1, drives the color
                "direction": p["direction"],
                "confidence": p["confidence"],
            }
            for p in preds
        ],
    }


@app.get("/api/history")
def history(limit: int = 200) -> dict:
    return {"disclaimer": config.DISCLAIMER, "rows": db.prediction_history(limit)}


@app.get("/api/accuracy")
def accuracy() -> dict:
    """Rolling directional hit-rate from scored predictions (spec §4.5, §5)."""
    rows = [r for r in db.prediction_history(1000) if r.get("correct") is not None]
    scored = len(rows)
    hits = sum(1 for r in rows if r["correct"])
    by_sector: dict[str, dict] = {}
    for r in rows:
        s = by_sector.setdefault(r["sector"], {"scored": 0, "hits": 0})
        s["scored"] += 1
        s["hits"] += int(r["correct"])
    return {
        "disclaimer": config.DISCLAIMER,
        "overall": {
            "scored": scored,
            "hits": hits,
            "hit_rate": round(hits / scored, 4) if scored else None,
        },
        "by_sector": {
            k: {**v, "hit_rate": round(v["hits"] / v["scored"], 4)}
            for k, v in by_sector.items()
        },
    }
