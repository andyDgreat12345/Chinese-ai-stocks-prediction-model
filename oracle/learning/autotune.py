"""The learning job — fit, judge out-of-sample, adopt only if genuinely better.

This is what turns the reflection loop from a diary into a machine that learns.
Every run, per sector:

  1. replay all scored history (``backtest.collect_records``);
  2. reserve the most recent ``LEARNING_HOLDOUT_DAYS`` as an untouched holdout;
  3. walk-forward-select a candidate parameter set on the rest;
  4. **blend** it a bounded ``LEARNING_STEP`` toward the incumbent, so one noisy
     fit can't throw the model across the parameter space;
  5. score the incumbent AND the blended candidate **on the same holdout**;
  6. adopt only if the candidate beats the incumbent by ``LEARNING_MIN_IMPROVEMENT``
     with at least ``LEARNING_MIN_HOLDOUT_RECORDS`` records behind it;
  7. write the attempt — adopted or refused — to the learning ledger.

Refusals are the point, not a failure: most days the data will not justify a
change, and a system that only moves on evidence is the one whose accuracy curve
actually rises instead of oscillating. Because step 6 compares like with like on
data neither set was chosen on, "better" here means better *out-of-sample*.

Safety properties, all deliberate:
  * bounded step — no wild jumps;
  * the exact parameters judged are the exact parameters stored;
  * full audit trail with before/after metrics, so any regression is traceable
    and reversible (``python -m oracle.learning.autotune --rollback <sector>``);
  * fail-soft — any error leaves the incumbent parameters untouched.

**Not investment advice.** A rising holdout hit-rate is measured research
progress, not a promise of future returns.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from .. import config, db
from ..analysis.pipeline import CHINA_SECTORS
from . import walkforward as wf

_PARAM_KEYS = (*wf.SIGNAL_KEYS, "threshold")


def _clean(p: dict) -> dict:
    return {k: round(float(p.get(k, 0.0)), 4) for k in _PARAM_KEYS}


def tune_sector(sector: str, records: list[dict], incumbent: dict,
                run_date: str, created_at: str, db_path=None) -> dict:
    """Fit → judge on holdout → adopt or refuse, for one sector. Returns the
    ledger row that was written (never raises for ordinary data shortfalls)."""
    search_pool, holdout = wf.split_holdout(records, config.LEARNING_HOLDOUT_DAYS)

    # Drop any weight on a signal that has not earned one yet. This runs BEFORE
    # the guards below, because every one of them can return early — a sector in
    # cooldown, or one short on holdout, would otherwise keep an unearned weight
    # indefinitely, and the whole risk is that a waking feed starts steering it.
    # blend() pulls the incumbent forward and the incumbent descends from the
    # hand-set DEFAULT_WEIGHTS (sentiment 0.35), so this is where that inheritance
    # gets cut. Behaviour-preserving (a dead signal contributes 0 regardless), so
    # it is a safety normalization rather than a learned change.
    live = wf.live_signals(records)
    pinned = wf.pin_dead_signals(incumbent, live)
    if pinned != incumbent:
        db.set_model_params(sector, pinned, created_at, db_path)
        incumbent = pinned

    def ledger(adopted: bool, after: dict, before_s=None, after_s=None,
               reason: str = "") -> dict:
        row = {
            "run_date": run_date, "sector": sector,
            "params_before": json.dumps(_clean(incumbent)),
            "params_after": json.dumps(_clean(after)),
            "score_before": None if before_s is None else _fin(before_s["edge_t"]),
            "score_after": None if after_s is None else _fin(after_s["edge_t"]),
            "hit_before": None if before_s is None else before_s["hit_rate"],
            "hit_after": None if after_s is None else after_s["hit_rate"],
            "n_holdout": len(holdout),
            "adopted": 1 if adopted else 0,
            "reason": reason, "created_at": created_at,
        }
        db.record_learning(row, db_path)
        return row

    if len(holdout) < config.LEARNING_MIN_HOLDOUT_RECORDS:
        return ledger(False, incumbent,
                      reason=f"holdout too small ({len(holdout)} < "
                             f"{config.LEARNING_MIN_HOLDOUT_RECORDS}) — need more history")

    waited = _days_since_last_adoption(sector, run_date, db_path)
    if waited is not None and waited < config.LEARNING_ADOPT_COOLDOWN_DAYS:
        return ledger(False, incumbent,
                      reason=f"cooldown: last adopted {waited}d ago (< "
                             f"{config.LEARNING_ADOPT_COOLDOWN_DAYS}d) — letting the "
                             "holdout refresh before changing again")

    candidate = wf.select_params(search_pool, n_folds=config.LEARNING_FOLDS)
    if candidate is None:
        return ledger(False, incumbent,
                      reason="not enough history for honest walk-forward folds")

    # Drop any weight on a signal that has not earned one yet. blend() pulls the
    # incumbent forward and the incumbent descends from the hand-set
    # DEFAULT_WEIGHTS (sentiment 0.35), so without this a never-fitted weight
    # would start steering predictions the moment its feed came alive — before
    # the tuner has the coverage to judge it. Both sides are pinned so the
    # holdout comparison is like-for-like.
    proposed = wf.pin_dead_signals(
        wf.blend(incumbent, candidate, config.LEARNING_STEP), live)
    before = wf.score_params(holdout, incumbent, min_bets=1)
    after = wf.score_params(holdout, proposed, min_bets=1)

    if after["bets"] < config.LEARNING_MIN_HOLDOUT_BETS:
        return ledger(False, incumbent, before, after,
                      reason=f"candidate bets only {after['bets']}x on the holdout "
                             f"(< {config.LEARNING_MIN_HOLDOUT_BETS}) — too thin to trust")

    gain = _fin(after["edge_t"]) - _fin(before["edge_t"])
    if gain < config.LEARNING_MIN_IMPROVEMENT:
        return ledger(False, incumbent, before, after,
                      reason=f"no out-of-sample gain (edge {gain:+.3f} < "
                             f"{config.LEARNING_MIN_IMPROVEMENT}) — keeping incumbent")

    db.set_model_params(sector, proposed, created_at, db_path)
    return ledger(True, proposed, before, after,
                  reason=f"adopted: holdout edge {_fin(before['edge_t']):.3f} → "
                         f"{_fin(after['edge_t']):.3f} (+{gain:.3f}), hit-rate "
                         f"{_pct(before['hit_rate'])} → {_pct(after['hit_rate'])} "
                         f"on {after['bets']} bets")


def _days_since_last_adoption(sector: str, run_date: str, db_path=None) -> int | None:
    """Calendar days since this sector last adopted a change, or None if never."""
    from datetime import date

    for row in db.learning_history(500, db_path):
        if row["sector"] != sector or not row["adopted"]:
            continue
        try:
            a = date.fromisoformat(row["run_date"])
            b = date.fromisoformat(run_date)
            return (b - a).days
        except (ValueError, TypeError):
            return None
    return None


def _fin(x) -> float:
    """edge_t can be -inf when a set never bets; treat that as the worst score."""
    return -99.0 if x is None or x == float("-inf") else float(x)


def _pct(x) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def run_autotune(run_date: str | None = None, db_path=None) -> dict:
    """Job entrypoint: tune every sector. Returns a summary. Never raises."""
    now = datetime.now(timezone.utc)
    run_date = run_date or now.date().isoformat()
    created_at = now.isoformat()

    if not config.LEARNING_ENABLED:
        print("run_autotune: learning disabled (config.LEARNING_ENABLED) — skipping")
        return {"enabled": False, "adopted": 0, "rows": []}

    try:
        db.init_db(db_path)
        from ..backtest import collect_records
        all_records = collect_records(db_path=db_path)
    except Exception as e:  # noqa: BLE001 — learning must never break the pipeline
        print(f"run_autotune FAILED to load history ({e!r}) — parameters unchanged")
        return {"enabled": True, "adopted": 0, "rows": [], "error": str(e)}

    by_sector: dict[str, list[dict]] = {}
    for r in all_records:
        by_sector.setdefault(r["sector"], []).append(r)

    rows, adopted = [], 0
    for sector in CHINA_SECTORS:
        recs = by_sector.get(sector, [])
        if not recs:
            continue
        try:
            incumbent = db.get_model_params(sector, db_path)
            row = tune_sector(sector, recs, incumbent, run_date, created_at, db_path)
            rows.append(row)
            adopted += row["adopted"]
            flag = "ADOPTED" if row["adopted"] else "kept"
            print(f"run_autotune[{sector}]: {flag} — {row['reason']}")
        except Exception as e:  # noqa: BLE001 — one sector must not sink the rest
            print(f"run_autotune[{sector}] FAILED ({e!r}) — parameters unchanged")
    print(f"run_autotune: {adopted} of {len(rows)} sector(s) improved out-of-sample")
    return {"enabled": True, "adopted": adopted, "rows": rows}


def rollback(sector: str, db_path=None) -> bool:
    """Revert a sector to the parameters from before its last adopted change."""
    db.init_db(db_path)
    for row in db.learning_history(500, db_path):
        if row["sector"] == sector and row["adopted"]:
            before = json.loads(row["params_before"])
            db.set_model_params(sector, before,
                                datetime.now(timezone.utc).isoformat(), db_path)
            print(f"rollback[{sector}]: restored {before}")
            return True
    print(f"rollback[{sector}]: no adopted change found — nothing to revert")
    return False


def format_learning_report(history: list[dict], params: dict) -> str:
    """Human-readable learning state: current parameters + recent attempts."""
    lines = ["China Market Oracle — learning state", "",
             "current per-sector parameters (learned; '*' = global default):"]
    if params:
        lines.append(f"  {'sector':<12}{'us':>6}{'sent':>6}{'macro':>6}{'rsi':>6}"
                     f"{'mom':>6}{'trend':>7}{'thresh':>8}  updated")
        for sector, p in sorted(params.items()):
            lines.append(
                f"  {sector:<12}{p.get('us_spillover', 0):>6.2f}"
                f"{p.get('sentiment', 0):>6.2f}{p.get('macro', 0):>6.2f}"
                f"{p.get('rsi_signal', 0):>6.2f}{p.get('momentum_signal', 0):>6.2f}"
                f"{p.get('trend_signal', 0):>7.2f}{p.get('threshold', 0):>8.3f}"
                f"  {str(p.get('updated_at'))[:10]}")
    else:
        lines.append("  (none tuned yet — running on the hand-set defaults)")

    lines += ["", "recent tuning attempts (holdout = days the search never saw):"]
    if not history:
        lines.append("  (no tuning runs recorded yet)")
    for row in history[:12]:
        mark = "✓ adopted" if row["adopted"] else "· kept"
        hb, ha = _pct(row["hit_before"]), _pct(row["hit_after"])
        lines.append(f"  {row['run_date']} {row['sector']:<12} {mark:<10} "
                     f"holdout hit {hb} → {ha}  (n={row['n_holdout']})")
        lines.append(f"      {row['reason']}")
    lines += ["",
              "A change is adopted only when it beats the incumbent on a holdout "
              "window the search never saw — most runs correctly refuse. "
              "Not investment advice."]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--rollback":
        if len(argv) < 2:
            print("usage: python -m oracle.learning.autotune --rollback <sector>")
            return 1
        return 0 if rollback(argv[1]) else 1
    if argv and argv[0] == "--report":
        db.init_db()
        print(format_learning_report(db.learning_history(50), db.all_model_params()))
        return 0
    run_autotune()
    print()
    print(format_learning_report(db.learning_history(20), db.all_model_params()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
