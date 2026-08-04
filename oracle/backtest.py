"""Backtesting + evaluation engine (the "test prediction ability" gate).

Replays historical US+China+news data (whatever is in the DB) through the
*current* model, scores every prediction against the actual China close, and —
crucially — measures it against naive baselines and for statistical
significance. This is how you learn whether the system has any real edge
*before* risking money, instead of waiting months live.

Honest by design:
  * every strategy (model + baselines) is scored on the *same* actuals, so the
    comparison is apples-to-apples;
  * a paper-trading return and an annualized Sharpe are reported, not just
    accuracy;
  * a binomial p-value flags whether a hit-rate above 50% is real or luck
    (55% on 20 bets is noise);
  * neutral calls take no position — a strategy is not rewarded for abstaining.

    python -m oracle.backtest [start_date] [end_date]

The metric functions are pure (operate on record dicts) so they're unit-tested
without a database.
"""
from __future__ import annotations

import math
import sys
from math import comb

from . import db
from .analysis.pipeline import CHINA_SECTORS, build_signals
from .analysis.scoring import score_sector
from .reflection.scoring import actual_sector_move
from .reflection.stats import CONFIDENCE_P, direction_from_move

# A raw US-spillover reading beyond this counts as directional for the naive
# US baseline (below it, call it neutral).
_US_BAND = 0.05


# ── data collection ──────────────────────────────────────────────────────
def _dates_with_actuals(db_path) -> list[str]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM china_close ORDER BY trade_date"
        ).fetchall()
        return [r["trade_date"] for r in rows]
    finally:
        conn.close()


def collect_records(start=None, end=None, db_path=None) -> list[dict]:
    """Replay the model over every historical date that has an actual China
    close. Returns one record per (date, sector) that could be scored."""
    weights = db.get_weights(db_path)
    records: list[dict] = []
    for d in _dates_with_actuals(db_path):
        if start and d < start:
            continue
        if end and d > end:
            continue
        us_rows = db.get_rows_for_date("us_close", d, db_path)
        news_rows = db.get_rows_for_date("news", d, db_path)
        macro = db.macro_event_dates(d, db_path)
        china_rows = db.get_rows_for_date("china_close", d, db_path)
        signals = build_signals(us_rows, news_rows, macro)
        for sector in CHINA_SECTORS:
            move = actual_sector_move(china_rows, sector)
            if move is None:
                continue
            sig = signals[sector]
            pred = score_sector(sig, weights)
            records.append({
                "date": d, "sector": sector,
                "actual_dir": direction_from_move(move),
                "actual_move": round(move, 4),
                "model_dir": pred.direction, "model_conf": pred.confidence,
                "us_spillover": sig.us_spillover,
            })
    _add_persistence(records)
    _add_llm_calls(records, db_path)
    return records


def _add_persistence(records: list[dict]) -> None:
    """Attach each record's previous-day actual direction for the persistence
    baseline (mutates in place)."""
    last: dict[str, str] = {}
    for r in sorted(records, key=lambda x: (x["sector"], x["date"])):
        r["prev_actual_dir"] = last.get(r["sector"])
        last[r["sector"]] = r["actual_dir"]


def _add_llm_calls(records: list[dict], db_path) -> None:
    """Attach the recorded AI-analyst direction per (date, sector), if any
    (mutates in place). The LLM strategy is scored ONLY on dates it actually
    produced a call — an honest forward/out-of-sample read, since we don't
    replay the LLM over history."""
    by_date: dict[str, list[dict]] = {}
    for r in records:
        by_date.setdefault(r["date"], []).append(r)
    for d, recs in by_date.items():
        calls = {c["sector"]: c["direction"] for c in db.llm_calls_for_date(d, db_path)}
        for r in recs:
            r["llm_dir"] = calls.get(r["sector"])  # None -> skipped, like persistence


# ── strategies (a predicted direction per record) ────────────────────────
def _dir_model(r):        return r["model_dir"]
def _dir_always_bull(r):  return "bullish"


def _dir_us_naive(r):
    s = r["us_spillover"]
    return "bullish" if s > _US_BAND else "bearish" if s < -_US_BAND else "neutral"


def _dir_persistence(r):  return r.get("prev_actual_dir")  # None on day 1 -> skipped
def _dir_llm(r):          return r.get("llm_dir")          # None if no LLM call -> skipped


STRATEGIES = {
    "model": _dir_model,
    "baseline: always-bullish": _dir_always_bull,
    "baseline: US-direction": _dir_us_naive,
    "baseline: persistence": _dir_persistence,
}

# Name for the recorded-AI-analyst strategy, added dynamically when the DB has
# any llm_calls (see run_backtest / costsim).
LLM_STRATEGY = "llm (recorded)"


def has_llm_calls(records: list[dict]) -> bool:
    return any(r.get("llm_dir") for r in records)


# ── pure metric helpers ──────────────────────────────────────────────────
def binom_p_value(hits: int, n: int, p: float = 0.5) -> float | None:
    """One-sided P(X >= hits) under a fair coin — is accuracy > 50% real?"""
    if n == 0:
        return None
    if n > 1500:  # normal approx for large n
        mean, sd = n * p, (n * p * (1 - p)) ** 0.5
        z = (hits - 0.5 - mean) / sd
        return round(0.5 * math.erfc(z / 2 ** 0.5), 6)
    tail = sum(comb(n, i) for i in range(hits, n + 1))
    return round(tail / 2 ** n, 6)


def annualized_sharpe(daily_returns: list[float]) -> float | None:
    """Annualized Sharpe of a daily equal-weight paper portfolio (returns in %)."""
    n = len(daily_returns)
    if n < 2:
        return None
    mean = sum(daily_returns) / n
    std = (sum((x - mean) ** 2 for x in daily_returns) / (n - 1)) ** 0.5
    if std == 0:
        return None
    return round((mean / std) * (252 ** 0.5), 3)


def evaluate(records: list[dict], dir_fn) -> dict:
    """Score one strategy over the records: directional accuracy, paper P&L,
    annualized Sharpe, and significance of the directional edge."""
    scored = correct = nn = nn_correct = 0
    daily: dict[str, list[float]] = {}
    for r in records:
        pd = dir_fn(r)
        if pd is None:
            continue
        scored += 1
        hit = pd == r["actual_dir"]
        correct += hit
        if pd != "neutral":                     # took a position
            nn += 1
            nn_correct += hit
            ret = r["actual_move"] if pd == "bullish" else -r["actual_move"]
            daily.setdefault(r["date"], []).append(ret)

    all_rets = [ret for day in daily.values() for ret in day]
    daily_port = [sum(day) / len(day) for day in daily.values()]  # equal-weight/day
    return {
        "scored": scored,
        "accuracy": round(correct / scored, 4) if scored else None,
        "bets": nn,
        "bet_accuracy": round(nn_correct / nn, 4) if nn else None,
        "p_value": binom_p_value(nn_correct, nn),
        "mean_return_pct": round(sum(all_rets) / len(all_rets), 4) if all_rets else None,
        "total_return_pct": round(sum(all_rets), 4) if all_rets else 0.0,
        "sharpe": annualized_sharpe(daily_port),
    }


def calibration(records: list[dict]) -> dict:
    """Model-only: is 'high confidence' actually right more often than 'low'?
    Per bucket: n, accuracy, implied probability, mean Brier."""
    buckets: dict[str, list[bool]] = {}
    for r in records:
        buckets.setdefault(r["model_conf"], []).append(r["model_dir"] == r["actual_dir"])
    out = {}
    for conf, hits in buckets.items():
        n = len(hits)
        acc = sum(hits) / n
        p = CONFIDENCE_P.get(conf, 0.5)
        brier = sum((p - (1.0 if h else 0.0)) ** 2 for h in hits) / n
        out[conf] = {"n": n, "accuracy": round(acc, 4),
                     "implied_p": p, "brier": round(brier, 4)}
    return out


def run_backtest(start=None, end=None, db_path=None) -> dict:
    records = collect_records(start, end, db_path)
    dates = sorted({r["date"] for r in records})
    # The recorded AI analyst is scored as a strategy only when calls exist.
    strategies = dict(STRATEGIES)
    if has_llm_calls(records):
        strategies[LLM_STRATEGY] = _dir_llm
    report = {
        "window": {"start": dates[0] if dates else None,
                   "end": dates[-1] if dates else None,
                   "trading_days": len(dates)},
        "strategies": {name: evaluate(records, fn) for name, fn in strategies.items()},
        "calibration": calibration(records),
        "n_records": len(records),
    }
    # Attach the cost-aware, compounded, drawdown-aware view (imported lazily to
    # avoid a circular import — costsim reuses this module's helpers).
    try:
        from . import costsim
        report["cost_aware"] = costsim.run_cost_backtest(start, end, db_path)
    except Exception as exc:  # never let the friction layer break the base report
        report["cost_aware"] = {"error": str(exc)}
    return report


# ── reporting ────────────────────────────────────────────────────────────
def format_report(report: dict) -> str:
    w = report["window"]
    lines = [
        "China Market Oracle — backtest",
        f"window: {w['start']} → {w['end']}  ({w['trading_days']} trading days, "
        f"{report['n_records']} scored predictions)",
        "",
        f"{'strategy':<26}{'acc':>7}{'bets':>7}{'bet-acc':>9}{'p-val':>9}"
        f"{'ret/bet%':>10}{'total%':>9}{'sharpe':>8}",
        "-" * 85,
    ]
    for name, m in report["strategies"].items():
        def s(v, pct=False):
            if v is None:
                return "—"
            return f"{v*100:.0f}%" if pct else f"{v}"
        lines.append(
            f"{name:<26}{s(m['accuracy'], True):>7}{m['bets']:>7}"
            f"{s(m['bet_accuracy'], True):>9}{s(m['p_value']):>9}"
            f"{s(m['mean_return_pct']):>10}{s(m['total_return_pct']):>9}"
            f"{s(m['sharpe']):>8}"
        )
    lines += ["", "model calibration (does confidence track accuracy?):"]
    if report["calibration"]:
        for conf in ("high", "med", "low"):
            c = report["calibration"].get(conf)
            if c:
                lines.append(f"  {conf:<5} n={c['n']:<4} acc={c['accuracy']*100:.0f}%  "
                             f"implied={c['implied_p']*100:.0f}%  brier={c['brier']}")
    else:
        lines.append("  (no scored predictions yet)")
    lines += [
        "",
        "Read it honestly: the model earns its keep only if it beats the "
        "baselines on bet-accuracy AND Sharpe, with a small p-value (edge is "
        "real, not luck). Not investment advice.",
    ]
    llm = report["strategies"].get(LLM_STRATEGY)
    if llm is not None:
        lines += [
            "",
            f"AI analyst ('{LLM_STRATEGY}') is scored only on the {llm['bets']} "
            "bet(s) it actually placed — a forward, out-of-sample read (the LLM "
            "is not replayed over history). Treat it as signal only once its "
            "bet count is large enough for the p-value to mean anything.",
        ]
    ca = report.get("cost_aware")
    if ca and "error" not in ca:
        from .costsim import format_cost_report
        lines += ["", format_cost_report(ca)]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    start = argv[0] if len(argv) > 0 else None
    end = argv[1] if len(argv) > 1 else None
    report = run_backtest(start, end)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
