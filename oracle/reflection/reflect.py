"""(iii) The reflection log — logical (spec §4b-iii).

After (i) scoring and (ii) influence measurement run, produce a structured daily
reflection: which signals worked, which missed, the likely reason, and a
*suggested* weight adjustment. The spec earmarks one Sonnet-tier LLM call here,
but the default generator is deterministic and offline so the loop runs (and is
testable) with no external dependency — pass an `llm` callable to upgrade it.

Two guarantees from the spec are honored:
  * weight adjustments are LOGGED for review, never silently auto-applied
    (config.AUTO_APPLY_WEIGHT_ADJUSTMENTS gates that, default off);
  * the entry is appended to a persistent, human-readable JSONL + markdown log —
    the accumulation that makes month 6 better than month 1.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

from .. import config, db
from .scoring import actual_sector_move
from .stats import direction_from_move

SIGNALS = ("us_spillover", "sentiment_score")
_LABEL = {"us_spillover": "us_spillover", "sentiment_score": "sentiment"}


def _sign(x: float) -> int:
    return 1 if x > 1e-9 else (-1 if x < -1e-9 else 0)


def _magnitude(n: int) -> str:
    n = abs(n)
    return "small" if n <= 1 else ("med" if n <= 3 else "large")


def build_context(trade_date: str) -> dict:
    """Assemble everything the reflection needs from the DB. Returns a dict of
    predictions with their actual outcomes + per-signal agreement tallies."""
    preds = db.predictions_for_date(trade_date)
    china_rows = db.get_rows_for_date("china_close", trade_date)

    rows = []
    agree = {s: 0 for s in SIGNALS}
    disagree = {s: 0 for s in SIGNALS}
    for p in preds:
        move = actual_sector_move(china_rows, p["sector"])
        if move is None:
            continue
        actual_dir = direction_from_move(move)
        actual_sign = _sign(move) if actual_dir != "neutral" else 0
        rows.append({
            "sector": p["sector"], "predicted": p["direction"],
            "confidence": p["confidence"], "actual": actual_dir,
            "actual_move": round(move, 4), "correct": p["direction"] == actual_dir,
        })
        if actual_sign == 0:
            continue
        for sig in SIGNALS:
            s = _sign(p[sig] or 0.0)
            if s == 0:
                continue
            (agree if s == actual_sign else disagree)[sig] += 1

    return {"trade_date": trade_date, "rows": rows, "agree": agree, "disagree": disagree}


def rule_based_reflection(ctx: dict) -> dict:
    """Deterministic reflection in the spec's JSON schema (§4b-iii)."""
    rows, agree, disagree = ctx["rows"], ctx["agree"], ctx["disagree"]
    net = {s: agree[s] - disagree[s] for s in SIGNALS}

    worked = [_LABEL[s] for s in SIGNALS if net[s] > 0]
    missed = [_LABEL[s] for s in SIGNALS if net[s] < 0]

    # Suggested adjustment: penalize the worst signal, else reward the best.
    worst = min(SIGNALS, key=lambda s: net[s])
    best = max(SIGNALS, key=lambda s: net[s])
    if net[worst] < 0:
        suggestion = {"signal": _LABEL[worst], "direction": "decrease",
                      "magnitude": _magnitude(net[worst])}
    elif net[best] > 0:
        suggestion = {"signal": _LABEL[best], "direction": "increase",
                      "magnitude": _magnitude(net[best])}
    else:
        suggestion = {"signal": None, "direction": "none", "magnitude": "small"}

    us_missed = net["us_spillover"] < 0
    sent_missed = net["sentiment_score"] < 0
    if us_missed and sent_missed:
        reason = "both signals wrong — likely a domestic policy/event not captured by US spillover or news"
    elif us_missed and not sent_missed:
        reason = "US spillover signal overridden by domestic/news factors"
    elif sent_missed and not us_missed:
        reason = "news sentiment misread; US spillover was the better guide"
    else:
        reason = "predictions largely tracked actuals; no dominant miss"

    scored = len(rows)
    confidence = "high" if scored >= 4 else ("med" if scored >= 2 else "low")

    return {
        "date": ctx["trade_date"],
        "predicted": {r["sector"]: r["predicted"] for r in rows},
        "actual": {r["sector"]: r["actual"] for r in rows},
        "signals_that_worked": worked,
        "signals_that_missed": missed,
        "likely_reason_for_miss": reason,
        "suggested_weight_adjustment": suggestion,
        "confidence_in_this_reflection": confidence,
    }


def _persist(reflection: dict, trade_date: str) -> None:
    created_at = datetime.now(timezone.utc).isoformat()
    db.insert_reflection({
        "trade_date": trade_date,
        "predicted_json": json.dumps(reflection.get("predicted", {})),
        "actual_json": json.dumps(reflection.get("actual", {})),
        "signals_that_worked": json.dumps(reflection.get("signals_that_worked", [])),
        "signals_that_missed": json.dumps(reflection.get("signals_that_missed", [])),
        "likely_reason_for_miss": reflection.get("likely_reason_for_miss"),
        "suggested_adjustment": json.dumps(reflection.get("suggested_weight_adjustment", {})),
        "reflection_confidence": reflection.get("confidence_in_this_reflection"),
        "created_at": created_at,
    })
    # Human-readable accumulation: JSONL + a rendered markdown note.
    with open(config.REFLECTION_LOG, "a") as f:
        f.write(json.dumps(reflection) + "\n")
    _render_markdown(reflection, created_at)


def _render_markdown(reflection: dict, created_at: str) -> None:
    d = reflection["date"]
    adj = reflection.get("suggested_weight_adjustment", {})
    md = [
        f"# Reflection — {d}", "",
        f"*Generated {created_at}. {config.DISCLAIMER}*", "",
        f"- **Signals that worked:** {', '.join(reflection['signals_that_worked']) or '—'}",
        f"- **Signals that missed:** {', '.join(reflection['signals_that_missed']) or '—'}",
        f"- **Likely reason for miss:** {reflection['likely_reason_for_miss']}",
        f"- **Suggested weight adjustment:** {adj.get('signal')} → {adj.get('direction')} ({adj.get('magnitude')})",
        f"- **Confidence in this reflection:** {reflection['confidence_in_this_reflection']}",
        "",
        "| Sector | Predicted | Actual |", "|---|---|---|",
    ]
    for sector, pred in reflection.get("predicted", {}).items():
        actual = reflection.get("actual", {}).get(sector, "—")
        md.append(f"| {sector} | {pred} | {actual} |")
    md.append("")
    md.append("> Suggested adjustment is logged for review, not auto-applied "
              f"(AUTO_APPLY_WEIGHT_ADJUSTMENTS = {config.AUTO_APPLY_WEIGHT_ADJUSTMENTS}).")

    notes_dir = config.DATA_DIR / "reflections"
    notes_dir.mkdir(parents=True, exist_ok=True)
    (notes_dir / f"{d}.md").write_text("\n".join(md))


_UNSET = object()


def generate_reflection(
    trade_date: str | None = None,
    llm: Callable[[dict], dict] | None = _UNSET,  # type: ignore[assignment]
) -> dict | None:
    """Job entrypoint: build context, generate the reflection, persist it.
    Returns the reflection dict. Never raises.

    `llm` resolution: if left unset, an optional LLM backend is auto-selected
    from env config (spec §4b-iii — Sonnet-tier is enough; provider-agnostic via
    reflection/llm.py). Pass an explicit callable to override, or None to force
    the deterministic rule-based generator. Any LLM failure falls back to
    rule-based, so the loop never depends on the LLM.
    """
    trade_date = trade_date or datetime.now(timezone.utc).date().isoformat()
    if llm is _UNSET:
        from .llm import get_reflection_llm
        llm = get_reflection_llm()
    try:
        ctx = build_context(trade_date)
        if not ctx["rows"]:
            print(f"generate_reflection: no scored predictions for {trade_date}, skipping")
            return None

        reflection = None
        if llm is not None:
            try:
                reflection = llm(ctx)
            except Exception as e:  # noqa: BLE001 — fall back, don't skip the day
                print(f"generate_reflection: LLM backend failed ({e!r}); using rule-based")
        if reflection is None:
            reflection = rule_based_reflection(ctx)

        _persist(reflection, trade_date)
        print(f"generate_reflection: wrote reflection for {trade_date} "
              f"(confidence: {reflection.get('confidence_in_this_reflection')})")
        return reflection
    except Exception as e:  # noqa: BLE001
        print(f"generate_reflection FAILED: {e!r}")
        return None
