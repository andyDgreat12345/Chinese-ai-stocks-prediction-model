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

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .. import config, db

app = FastAPI(title="China Market Oracle", version="0.1.0")

# ── Dashboard (terminal UI, spec §5) ─────────────────────────────────────
_DASH = Path(__file__).resolve().parent.parent / "dashboard"
if (_DASH / "static").is_dir():
    app.mount("/static", StaticFiles(directory=_DASH / "static"), name="static")


@app.get("/")
def index():
    """Serve the terminal dashboard."""
    return FileResponse(_DASH / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "disclaimer": config.DISCLAIMER}


@app.get("/api/markets")
def markets() -> dict:
    """Latest raw US closes, split into indices / sector ETFs / metals, for the
    global-markets and precious-metals panels (spec §5, retained panels)."""
    rows = db.latest_market_rows("us_close")
    pick = lambda tags: [  # noqa: E731
        {"symbol": r["symbol"], "sector": r["sector"],
         "close": r["close"], "pct_change": r["pct_change"]}
        for r in rows if r["sector"] in tags
    ]
    return {
        "trade_date": rows[0]["trade_date"] if rows else None,
        "disclaimer": config.DISCLAIMER,
        "indices": pick({"broad", "tech"}),
        "sectors": pick({"energy", "financials", "semis"}),
        "metals": pick({"gold", "silver"}),
    }


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


@app.get("/api/llm-calls")
def llm_calls() -> dict:
    """Latest AI research-desk calls (spec §4.2). Empty when no analyst provider
    is configured — the rule-based prediction stands on its own then."""
    import json as _json

    rows = db.latest_llm_calls()
    return {
        "trade_date": rows[0]["trade_date"] if rows else None,
        "disclaimer": config.DISCLAIMER,
        "provider": rows[0]["provider"] if rows else None,
        "model": rows[0]["model"] if rows else None,
        "calls": [
            {
                "sector": r["sector"],
                "direction": r["direction"],
                "conviction": r["conviction"],
                "tradeable_etf": r["tradeable_etf"],
                "key_drivers": _json.loads(r["key_drivers"] or "[]"),
                "rationale": r["rationale"],
            }
            for r in rows
        ],
    }


@app.get("/api/report")
def report() -> dict:
    """The daily action report (spec §4.2): the rule-based prediction and the AI
    analyst merged per sector and bucketed into lean-constructive / lean-cautious
    / watch — the same read that's emailed after the US close, for the dashboard."""
    from ..report import run_report

    r = run_report()
    r["disclaimer"] = config.DISCLAIMER
    return r


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


@app.get("/api/leaderboard")
def leaderboard(established_only: bool = False) -> dict:
    """US→China correlation leaderboard (spec §4b-ii). Established correlations
    (>= min sample) are flagged distinctly from noisy low-sample ones."""
    return {"disclaimer": config.DISCLAIMER,
            "min_sample": config.MIN_CORRELATION_SAMPLE,
            "rows": db.leaderboard(established_only)}


@app.get("/api/news-impact")
def news_impact() -> dict:
    """News-category → typical China-sector move, with sample size + variance."""
    return {"disclaimer": config.DISCLAIMER, "rows": db.news_impact_table()}


@app.get("/api/reflections")
def reflections(limit: int = 30) -> dict:
    """Recent reflection-log entries (spec §4b-iii)."""
    return {"disclaimer": config.DISCLAIMER, "rows": db.recent_reflections(limit)}


@app.get("/api/weights")
def weights() -> dict:
    """Current model weights vs. the reflection loop's suggested weights, so the
    dashboard can show the proposed-self-correction diff (spec §4b-iii)."""
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT signal, current_weight, suggested_weight FROM weights")]
    finally:
        conn.close()
    return {"auto_apply": config.AUTO_APPLY_WEIGHT_ADJUSTMENTS, "rows": rows}


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
