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
                "top_pick": _json.loads(r["top_pick"]) if r["top_pick"] else None,
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


@app.get("/api/charts")
def charts(days: int = 90) -> dict:
    """Per-sector price bars + indicator lines + the calibrated forecast cone —
    everything the dashboard's candlestick chart draws.

    ``bars`` carry OHLC when the source provided it (real candlesticks) and just
    a close otherwise (the chart falls back to a line rather than inventing a
    bar). ``forecast`` is the empirical distribution of what happened on past days
    with the same call — measured, never extrapolated."""
    from ..analysis import technicals as ta
    from ..analysis.forecast import sector_forecasts
    from ..analysis.pipeline import CHINA_SECTORS

    db.init_db()          # self-heal: a restored state DB may predate the OHLC columns
    out: dict[str, dict] = {}
    for sector in CHINA_SECTORS:
        symbol = config.CHINA_SECTOR_ETFS.get(sector)
        if not symbol:
            continue
        rows = db.close_series("china_close", symbol=symbol, limit=days)
        bars = [{"d": r["trade_date"], "o": r.get("open"), "h": r.get("high"),
                 "l": r.get("low"), "c": r["close"]}
                for r in rows if r.get("close") is not None]
        closes = [b["c"] for b in bars]
        ind = ta.compute_indicators(closes) if len(closes) >= 2 else {}
        out[sector] = {
            "symbol": symbol, "bars": bars,
            "has_ohlc": any(b["o"] is not None and b["h"] is not None for b in bars),
            "sma20": ind.get("sma20"), "sma50": ind.get("sma50"),
            "rsi": ind.get("rsi"), "trend": ind.get("trend"),
            "technical_note": ind.get("technical_note"),
        }

    # Today's calls + the empirical outcome distribution behind each.
    preds = {p["sector"]: p["direction"] for p in db.latest_predictions()}
    try:
        from ..backtest import collect_records
        fc = sector_forecasts(collect_records(), preds)
    except Exception:  # noqa: BLE001 — charts must render even if the replay fails
        fc = {}
    for sector, dist in fc.items():
        if sector in out:
            out[sector]["forecast"] = dist
            out[sector]["call"] = preds.get(sector)
    return {"disclaimer": config.DISCLAIMER, "days": days, "sectors": out}


@app.get("/api/simulation")
def simulation() -> dict:
    """Paper-trading simulation of the system's own historical calls, run through
    human trader discipline (conviction floor, risk sizing, stops, position and
    holding limits) and benchmarked against buy-and-hold.

    Inputs are lookahead-free — each session is decided from the prior US close
    and prior China closes only."""
    from ..simulator import engine as sim
    from ..simulator.run import load_inputs
    from ..simulator.trader import TraderRules

    db.init_db()
    try:
        calls, bars, dates = load_inputs()
        if not dates:
            return {"disclaimer": config.DISCLAIMER, "available": False,
                    "reason": "no historical calls yet — run the backfill"}
        rules = TraderRules()
        r = sim.simulate(calls, bars, dates, rules)
    except Exception as exc:  # noqa: BLE001 — a panel must never break the site
        return {"disclaimer": config.DISCLAIMER, "available": False,
                "reason": str(exc)}
    # Trim the payload: the curve is charted, individual trades are not.
    curve = r["equity_curve"]
    step = max(1, len(curve) // 240)          # cap the points we ship
    return {
        "disclaimer": config.DISCLAIMER, "available": True,
        "rules": r["rules"], "sessions": r["sessions"], "n_trades": r["n_trades"],
        "win_rate": r["win_rate"], "avg_win_pct": r["avg_win_pct"],
        "avg_loss_pct": r["avg_loss_pct"], "profit_factor": r["profit_factor"],
        "max_drawdown_pct": r["max_drawdown_pct"],
        "starting_cash": r["starting_cash"], "final_equity": r["final_equity"],
        "return_pct": r["return_pct"],
        "buy_and_hold_return_pct": r["buy_and_hold_return_pct"],
        "beat_buy_and_hold": r["beat_buy_and_hold"],
        "exit_reasons": r["exit_reasons"],
        "equity_curve": curve[::step],
        "recent_trades": r["trades"][-12:],
    }


@app.get("/api/proven-hubs")
def proven_hubs(days: int = 90, limit: int = 6) -> dict:
    """Sweep-proven pairs grouped into HUBS — one US symbol with every China
    instrument it leads, so a US symbol that appeared many times is shown once
    with all its counterparts overlaid instead of repeated per pair.

    Every series carries a label (name · sector, plus the company when the
    instrument is a specific company) so a chart legend never shows bare codes."""
    from ..analysis import pairs as pr
    from ..research import labels as lb

    db.init_db()
    rows = db.proven_pairs()
    if not rows:
        return {"disclaimer": config.DISCLAIMER, "hubs": [], "n_pairs": 0}

    by_us: dict[str, list[dict]] = {}
    for r in rows:
        by_us.setdefault(r["us_symbol"], []).append(r)
    # Hubs with the most counterparts first — those are the ones worth overlaying.
    order = sorted(by_us.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit]

    def series(table, symbol):
        return [(x["trade_date"], x["close"]) for x in
                db.close_series(table, symbol=symbol, limit=days + 5)
                if x["close"] is not None]

    hubs = []
    for us_sym, prs in order:
        us_s = series("us_close", us_sym)
        if len(us_s) < 5:
            continue
        legs = []
        for p in sorted(prs, key=lambda x: -abs(x.get("current_r") or x["r_discovered"] or 0)):
            cn_s = series("china_close", p["china_symbol"])
            if len(cn_s) < 5:
                continue
            aligned = pr.align_with_lag(us_s, cn_s, p["lag"])
            if not aligned["dates"]:
                continue
            legs.append({
                "china_symbol": p["china_symbol"],
                "china_label": lb.label(p["china_symbol"]),
                "lag": p["lag"],
                "r_discovered": p["r_discovered"],
                "current_r": p["current_r"],
                "refresh_count": p["refresh_count"] or 0,
                "refreshed_on": p["refreshed_on"],
                "q_value": p["q_value"],
                "china": aligned["china"],
                "us": aligned["us"],
            })
        if legs:
            hubs.append({"us_symbol": us_sym, "us_label": lb.label(us_sym),
                         "n_counterparts": len(legs), "legs": legs})
    return {"disclaimer": config.DISCLAIMER, "days": days,
            "n_pairs": len(rows), "hubs": hubs}


@app.get("/api/correlation-accumulated")
def correlation_accumulated(predictive_only: bool = True) -> dict:
    """US↔China links ranked by how well they have HELD UP over time, not by
    today's biggest number. reliability = |mean r| × sign-persistence × maturity."""
    from ..reflection.correlation import accumulated_leaderboard

    db.init_db()
    rows = accumulated_leaderboard(predictive_only=predictive_only)
    return {
        "disclaimer": config.DISCLAIMER,
        "predictive_only": predictive_only,
        "n": len(rows),
        "rows": rows,
    }


@app.get("/api/pairs")
def pairs(limit: int = 6, days: int = 90) -> dict:
    """Established US↔China correlations rendered as paired, lag-aligned series.

    Tradeable (lag>=1) relationships are ranked FIRST: the strongest raw numbers on
    the leaderboard are same-day, and same-day is a mirage here — the US closes
    ~14h after China, so that bar doesn't exist yet when China closes."""
    from ..analysis import pairs as pr
    from ..reflection.correlation import accumulated_leaderboard

    db.init_db()
    # Prefer the ACCUMULATED ranking — links that have held up across many days,
    # not today's biggest number. Falls back to the daily snapshot until enough
    # observations have accumulated (the reflection pass records one set/day).
    acc = accumulated_leaderboard(predictive_only=True)
    source = "accumulated"
    if acc:
        ranked = [{"us_symbol": r["us_symbol"], "china_symbol": r["china_symbol"],
                   "correlation": r["mean_correlation"], "best_lag": r["lag"],
                   "sample_size": r["sample_size"], "window_days": 0,
                   "reliability": r["reliability"], "persistence": r["persistence"],
                   "n_observations": r["n_observations"]} for r in acc[:limit]]
    else:
        source = "snapshot (accumulation still building)"
        rows = [r for r in db.leaderboard(True) if r.get("correlation") is not None]
        best: dict[tuple, dict] = {}
        for r in rows:
            k = (r["us_symbol"], r["china_symbol"])
            if k not in best or abs(r["correlation"]) > abs(best[k]["correlation"]):
                best[k] = r
        ranked = pr.rank_pairs(list(best.values()))[:limit]

    def series(table, symbol):
        return [(x["trade_date"], x["close"]) for x in
                db.close_series(table, symbol=symbol, limit=days + 5)
                if x["close"] is not None]

    out = []
    for r in ranked:
        us_s = series("us_close", r["us_symbol"])
        cn_s = series("china_close", r["china_symbol"])
        if len(us_s) < 5 or len(cn_s) < 5:
            continue
        built = pr.build_pair(r, us_s, cn_s, limit=days)
        from ..research import labels as lb
        built["us_label"] = lb.label(r["us_symbol"])
        built["china_label"] = lb.label(r["china_symbol"])
        # carry the accumulation stats through when they exist
        for k in ("reliability", "persistence", "n_observations"):
            if r.get(k) is not None:
                built[k] = r[k]
        out.append(built)
    return {
        "disclaimer": config.DISCLAIMER,
        "ranking_source": source,
        "min_sample": config.MIN_CORRELATION_SAMPLE,
        "timing": {
            "china_close_utc": pr.CHINA_CLOSE_UTC, "us_close_utc": pr.US_CLOSE_UTC,
            "hours_us_after_china": pr.HOURS_US_AFTER_CHINA,
        },
        "pairs": out,
    }


@app.get("/api/learning")
def learning() -> dict:
    """Self-improvement state (§4b): the per-sector parameters the walk-forward
    tuner has adopted, and the ledger of every attempt — the auditable record of
    whether the model is actually getting better on held-out days."""
    history = db.learning_history(40)
    adopted = [h for h in history if h["adopted"]]
    return {
        "disclaimer": config.DISCLAIMER,
        "enabled": config.LEARNING_ENABLED,
        "holdout_days": config.LEARNING_HOLDOUT_DAYS,
        "params": db.all_model_params(),
        "n_attempts": len(history),
        "n_adopted": len(adopted),
        "history": [
            {"run_date": h["run_date"], "sector": h["sector"],
             "adopted": bool(h["adopted"]), "hit_before": h["hit_before"],
             "hit_after": h["hit_after"], "n_holdout": h["n_holdout"],
             "reason": h["reason"]}
            for h in history
        ],
    }


@app.get("/api/llm-usage")
def llm_usage() -> dict:
    """AI research spend meter (§4.2 cost governance): token + estimated-USD
    totals for today / last 7d / all time, and a per-model breakdown."""
    s = db.llm_usage_summary()
    s["disclaimer"] = config.DISCLAIMER
    return s


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


@app.get("/api/paper")
def paper_strategy() -> dict:
    """Forward, out-of-sample record for the validated mean-reversion rule.

    Shown beside the retrospective holdout it is meant to confirm or refute, so
    the live number can never be read on its own. Every other test of this rule
    reuses the same ten years; this is the only row that could not have been
    fitted. Records what the rule would have done — no order is placed."""
    from .. import paper

    db.init_db()
    try:
        s = paper.summary()
        s_t1 = paper.summary(leg="t1")
        return {
            "disclaimer": config.DISCLAIMER,
            "strategy": paper.STRATEGY,
            "rule": {"prior_body_max": paper.PRIOR_BODY_MAX,
                     "gap_max": paper.GAP_MAX,
                     "cost_pct": paper.COST_PCT},
            "holdout_reference": paper.HOLDOUT_REFERENCE,
            "holdout_reference_t1": paper.HOLDOUT_REFERENCE_T1,
            "forward": s, "forward_t1": s_t1,
            "report": paper.format_report(s, s_t1),
        }
    except Exception as e:  # noqa: BLE001
        return {"disclaimer": config.DISCLAIMER, "error": str(e), "forward": {"n": 0}}


@app.get("/api/execution")
def execution_realism() -> dict:
    """Whether the validated rule can be executed, and what friction it survives.

    Two threats that never appear in a hit rate. Settlement: every instrument
    here is a domestic A-share equity ETF, and mainland equities settle T+1, so
    the rule's same-session exit may not be placeable at all — the answer is
    priced both ways rather than assumed. Slippage: how much extra friction the
    edge absorbs before it is gone, expressed against the daily move because
    shortfall scales with volatility rather than with a fixed tick.

    Fail-soft: a research endpoint must never take the dashboard down."""
    from ..research import execution as ex
    from ..research.exit_horizon import build_paths

    try:
        rows = build_paths(start=config.LEARNING_TRAIN_START or None)
        curve = ex.slippage_curve(rows)
        be = ex.breakeven(rows)
        sett = ex.settlement_comparison(rows)
        return {
            "disclaimer": config.DISCLAIMER,
            "settlement": sett, "slippage": curve, "breakeven": be,
            "report": ex.format_report(curve, be, sett),
        }
    except Exception as e:  # noqa: BLE001
        return {"disclaimer": config.DISCLAIMER, "error": str(e)}


@app.get("/api/exit-horizon")
def exit_horizon() -> dict:
    """How long the validated rule should hold, and where the market's return sits.

    The rule's same-session exit was never tested against alternatives — it is
    simply the horizon it was found on. This scores four exits on the same
    holdout, each against the drift of holding every session that long, and
    carries the overnight/intraday decomposition that explains the answer.

    Fail-soft: a research endpoint must never take the dashboard down."""
    from ..research import exit_horizon as eh

    try:
        rows = eh.build_paths(start=config.LEARNING_TRAIN_START or None)
        res = eh.simulate(rows)
        v = eh.verdict(res)
        return {
            "disclaimer": config.DISCLAIMER,
            "verdict": v, "drift": res.get("drift"),
            "premium": res.get("premium"),
            "holdout": res.get("holdout"),
            "report": eh.format_report(res, v),
        }
    except Exception as e:  # noqa: BLE001
        return {"disclaimer": config.DISCLAIMER, "error": str(e)}


@app.get("/api/objective")
def learning_objective() -> dict:
    """Whether the learner's scored target is the one a trade can earn.

    The loop scores against close-to-close; a position entered at the open earns
    only the body. This reports each signal's edge against both, which is how
    the system's founding signal turns out to carry t=+5.8 on the scored
    objective and t=+0.1 on the tradeable one.

    Fail-soft: a research endpoint must never take the dashboard down."""
    from ..learning import objective as ob

    try:
        edges, a = ob.measure()
        return {"disclaimer": config.DISCLAIMER, "edges": edges,
                "alignment": a, "report": ob.format_report(edges, a)}
    except Exception as e:  # noqa: BLE001
        return {"disclaimer": config.DISCLAIMER, "error": str(e)}


@app.get("/api/regimes")
def regimes() -> dict:
    """Whether the edge is broad or lives in one corner of the data.

    Scores the validated rule across six pre-registered families and reports
    agreement within each. Deliberately not a search: no bucket is ever selected
    to trade, and the sign test is run per family because the families overlap
    and pooling them counts every trade once per family.

    Fail-soft: a research endpoint must never take the dashboard down."""
    from ..research import regimes as rg
    from ..research.exit_horizon import build_paths

    try:
        res = rg.analyse(build_paths(start=config.LEARNING_TRAIN_START or None))
        return {"disclaimer": config.DISCLAIMER, "result": res,
                "report": rg.format_report(res)}
    except Exception as e:  # noqa: BLE001
        return {"disclaimer": config.DISCLAIMER, "error": str(e)}


@app.get("/api/segments")
def segments() -> dict:
    """Where the model's accuracy actually sits inside the daily bar, and which
    part of it a position entered at the open can capture. The gap is the most
    predictable segment and the least reachable — the capturability flag is the
    number to read first."""
    from ..analysis import segments as seg
    from ..backtest import collect_records

    db.init_db()
    try:
        start = config.LEARNING_TRAIN_START or None
        rows = seg.score_segments(collect_records(start=start),
                                  seg.build_segments(start=start))
        return {"disclaimer": config.DISCLAIMER, "segments": rows,
                "report": seg.format_segment_report(rows)}
    except Exception as e:  # noqa: BLE001
        return {"disclaimer": config.DISCLAIMER, "error": str(e), "segments": []}


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
