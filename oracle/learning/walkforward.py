"""Walk-forward parameter fitting — the honest core of the learning loop.

The model's prediction is a weighted sum of component signals bucketed by an
abstain threshold. Those four numbers (``us_spillover``, ``sentiment``, ``macro``
weights + ``threshold``) were hand-set guesses. This module *fits them from the
data*, and — critically — measures the fit on days the search never saw, because
a parameter set tuned and scored on the same history will always look brilliant
and then fail live.

The protocol, in order:

  1. **Holdout split.** The most recent ``holdout_days`` trading days are cut off
     and reserved. Nothing in the search ever touches them.
  2. **Walk-forward folds** over the remaining history: expanding train window,
     the next slice as validation. A candidate's score is its *mean validation*
     score across folds — always predicting forward, never backward.
  3. **Selection** picks the best candidate by that mean validation score.
  4. **Final judgement** re-scores that winner on the untouched holdout. Only
     that number is used to decide adoption (see ``autotune``), because steps 2–3
     have already "seen" their validation slices through the act of choosing.

Objective: ``edge_t`` = (hit_rate − 0.5) · √n_bets — a t-statistic-like score that
rewards being right *and* being right often enough to matter, so it can't be won
by a lucky one-bet parameter set. A ``min_bets`` floor rejects degenerate corners.

Everything here is a pure function of record dicts (as produced by
``backtest.collect_records``) — no DB, no network, no clock — so it is fully
unit-testable and deterministic.

**Not investment advice.** Fitted parameters are a research artifact; a better
holdout number is evidence, not a promise about tomorrow.
"""
from __future__ import annotations

from math import sqrt

# Search grid. Coarse on purpose: with a few hundred scored days per sector, a
# fine grid would fit noise. Weights step 0.1 over the simplex; threshold spans
# "almost always bet" to "only on strong conviction".
_WEIGHT_STEP = 0.1
# 0.02 … 0.50. The upper end matters: a sector whose best move is to bet rarely
# must be able to say so without being clipped by the edge of the grid.
_THRESHOLDS = [round(0.02 * i, 3) for i in range(1, 26)]


SIGNAL_KEYS = ("us_spillover", "sentiment", "macro",
               "rsi_signal", "momentum_signal", "trend_signal")


def signal_tuple(rec: dict) -> tuple[float, ...]:
    """The full component-signal vector for one replayed record. Pure."""
    return tuple(float(rec.get(k) or 0.0) for k in SIGNAL_KEYS)


def predict_dir(sig: tuple[float, ...], params: dict) -> str:
    """Direction a parameter set would have called for one signal vector."""
    composite = sum(float(params.get(k, 0.0)) * s
                    for k, s in zip(SIGNAL_KEYS, sig))
    composite = max(-1.0, min(1.0, composite))
    thr = params["threshold"]
    if composite >= thr:
        return "bullish"
    if composite <= -thr:
        return "bearish"
    return "neutral"


def score_params(records: list[dict], params: dict, min_bets: int = 10) -> dict:
    """Score one parameter set over records. Neutral = no bet (never counted as a
    win). Returns hit-rate, bet count, mean return per *record*, and the edge_t
    objective (-inf when the bet count is below ``min_bets``)."""
    bets = hits = 0
    total_return = 0.0
    for r in records:
        d = predict_dir(signal_tuple(r), params)
        if d == "neutral":
            continue
        bets += 1
        if d == r["actual_dir"]:
            hits += 1
        move = r.get("actual_move") or 0.0
        total_return += move if d == "bullish" else -move
    n = len(records)
    hit_rate = hits / bets if bets else None
    if bets < min_bets or hit_rate is None:
        edge = float("-inf")
    else:
        edge = (hit_rate - 0.5) * sqrt(bets)
    return {
        "n_records": n, "bets": bets, "hits": hits,
        "hit_rate": None if hit_rate is None else round(hit_rate, 4),
        "mean_return_per_record": round(total_return / n, 4) if n else 0.0,
        "total_return_pct": round(total_return, 4),
        "edge_t": edge,
    }


def sample_params(live: set[str], n: int = 160, thresholds: list[float] | None = None,
                  seed: int = 7) -> list[dict]:
    """Deterministic sample of weight vectors over the live signals × thresholds.

    A full simplex grid is fine for 2–3 signals but explodes past that, so beyond
    a small set we sample instead: every single-signal axis (so a lone dominant
    feature is always tried), the equal-weight blend, and ``n`` seeded random
    points. The fixed seed keeps runs reproducible — a learning system whose
    proposals change when nothing else did is impossible to audit."""
    import random

    names = [k for k in SIGNAL_KEYS if k in live] or ["us_spillover"]
    thrs = thresholds if thresholds is not None else _THRESHOLDS
    vectors: list[dict] = []
    for nm in names:                                   # each signal alone
        vectors.append({k: (1.0 if k == nm else 0.0) for k in names})
    if len(names) > 1:                                 # equal-weight blend
        vectors.append({k: 1.0 / len(names) for k in names})
    rng = random.Random(seed)
    for _ in range(n):
        raw = [rng.random() for _ in names]
        tot = sum(raw) or 1.0
        vectors.append({k: v / tot for k, v in zip(names, raw)})

    out = []
    for vec in vectors:
        base = {k: round(vec.get(k, 0.0), 4) for k in SIGNAL_KEYS}
        for thr in thrs:
            out.append({**base, "threshold": thr})
    return out


# A signal must be non-zero on at least this share of records to be weightable.
# "Varies at all" is too weak a test: news ingestion went live on one day of a
# 369-day history, so `sentiment` was non-zero on 0.3% of records and instantly
# qualified — a weight on it would be fitted almost entirely to a single day
# while behaving as a threshold rescale on every other one. That is the same
# backdoor `macro` opened, arriving through a different door.
MIN_SIGNAL_COVERAGE = 0.05


def signal_coverage(records: list[dict]) -> dict[str, float]:
    """Share of records on which each signal is non-zero. Pure."""
    n = len(records)
    if not n:
        return {name: 0.0 for name in SIGNAL_KEYS}
    out = {}
    for idx, name in enumerate(SIGNAL_KEYS):
        hits = sum(1 for r in records if abs(signal_tuple(r)[idx]) > 1e-9)
        out[name] = hits / n
    return out


def live_signals(records: list[dict],
                 min_coverage: float = MIN_SIGNAL_COVERAGE) -> set[str]:
    """Which component signals carry enough information to be worth a weight.

    ``macro`` is currently emitted as a constant 0.0 by ``build_signals`` (events
    are flagged, not directionally scored). A weight on a dead signal is not a
    real parameter — it silently rescales the composite, which the search would
    exploit as a backdoor threshold dial, wasting a search dimension and
    producing learned weights that misrepresent what the model is doing. So the
    grid only spans signals that carry information, and this re-widens
    automatically the day macro becomes a live directional signal.

    A *sparse* signal is the same hazard wearing a disguise, so presence is not
    enough — a signal must also clear ``min_coverage``. A newly-wired feed earns
    its weight once it has history, not on its first day."""
    coverage = signal_coverage(records)
    live = {name for name, c in coverage.items() if c >= min_coverage}
    return live or {"us_spillover"}


def candidate_params(signals: set[str] | None = None) -> list[dict]:
    """The search grid: weights over the *informative* signals × thresholds. Pure.

    Dead signals are pinned to weight 0 so the threshold — not a phantom weight —
    does all the abstain-rate work."""
    live = signals or {"us_spillover", "sentiment", "macro"}
    steps = int(round(1.0 / _WEIGHT_STEP))
    out = []
    for i in range(steps + 1):
        for j in range(steps + 1 - i):
            k = steps - i - j
            w = {"us_spillover": i * _WEIGHT_STEP, "sentiment": j * _WEIGHT_STEP,
                 "macro": k * _WEIGHT_STEP}
            if any(w[name] > 0 for name in ("us_spillover", "sentiment", "macro")
                   if name not in live):
                continue                      # no weight on a signal that can't inform
            if sum(w[name] for name in live) <= 0:
                continue                      # must weight something
            for thr in _THRESHOLDS:
                out.append({"us_spillover": round(w["us_spillover"], 3),
                            "sentiment": round(w["sentiment"], 3),
                            "macro": round(w["macro"], 3),
                            "threshold": thr})
    return out


def split_holdout(records: list[dict], holdout_days: int) -> tuple[list[dict], list[dict]]:
    """Split by DATE (not row) so a day is never half-in, half-out. The most
    recent ``holdout_days`` trading days become the untouched holdout."""
    dates = sorted({r["date"] for r in records})
    if holdout_days <= 0 or len(dates) <= holdout_days:
        return records, []
    cut = set(dates[-holdout_days:])
    return ([r for r in records if r["date"] not in cut],
            [r for r in records if r["date"] in cut])


def walk_forward_folds(records: list[dict], n_folds: int = 3,
                       min_train_days: int = 40) -> list[tuple[list[dict], list[dict]]]:
    """Expanding-window folds: train on everything before a cut date, validate on
    the slice after it. Always forward in time. Empty list when history is too
    short to make an honest fold."""
    dates = sorted({r["date"] for r in records})
    if len(dates) < min_train_days + n_folds:
        return []
    remaining = len(dates) - min_train_days
    slice_len = max(1, remaining // n_folds)
    folds = []
    for f in range(n_folds):
        train_end = min_train_days + f * slice_len
        val_end = min(train_end + slice_len, len(dates))
        if train_end >= val_end:
            break
        train_dates = set(dates[:train_end])
        val_dates = set(dates[train_end:val_end])
        folds.append(([r for r in records if r["date"] in train_dates],
                      [r for r in records if r["date"] in val_dates]))
    return folds


def select_params(records: list[dict], n_folds: int = 3, min_bets: int = 10,
                  candidates: list[dict] | None = None) -> dict | None:
    """Pick the parameter set with the best MEAN VALIDATION score across
    walk-forward folds. Returns None when history can't support honest folds.

    (Note the train slice isn't used to fit anything directly — the grid is fixed
    — but the fold structure still guarantees every score is measured on days
    after the ones used to establish that the regime holds.)"""
    folds = walk_forward_folds(records, n_folds)
    if not folds:
        return None
    fold_min_bets = max(3, min_bets // max(1, len(folds)))

    def rank(cands: list[dict]) -> list[tuple[float, dict]]:
        scored = []
        for p in cands:
            total, ok = 0.0, True
            for _train, val in folds:
                s = score_params(val, p, min_bets=fold_min_bets)
                if s["edge_t"] == float("-inf"):
                    ok = False
                    break              # fails the bet floor in some fold
                total += s["edge_t"]
            if ok:
                scored.append((total / len(folds), p))
        scored.sort(key=lambda t: -t[0])
        return scored

    if candidates is not None:
        ranked = rank(candidates)
        return ranked[0][1] if ranked else None

    live = live_signals(records)
    # Stage 1 — coarse: many weight vectors, few thresholds.
    coarse_thrs = _THRESHOLDS[1::4]
    ranked = rank(sample_params(live, thresholds=coarse_thrs))
    if not ranked:
        return None
    # Stage 2 — refine the best few weight vectors over the full threshold grid.
    fine = []
    for _score, p in ranked[:5]:
        for thr in _THRESHOLDS:
            fine.append({**p, "threshold": thr})
    ranked_fine = rank(fine)
    return ranked_fine[0][1] if ranked_fine else ranked[0][1]


def blend(incumbent: dict, candidate: dict, step: float) -> dict:
    """Move only ``step`` of the way from the incumbent toward the candidate, then
    renormalize the weights. Bounded steps keep one noisy fit from throwing the
    model across the parameter space; the blended result is re-validated before
    adoption, so what gets judged is exactly what gets applied."""
    out = {}
    for k in (*SIGNAL_KEYS, "threshold"):
        a, b = float(incumbent.get(k, 0.0)), float(candidate.get(k, 0.0))
        out[k] = a + step * (b - a)
    total = sum(out[k] for k in SIGNAL_KEYS)
    if total > 0:
        for k in SIGNAL_KEYS:
            out[k] = round(out[k] / total, 4)
    out["threshold"] = round(max(0.01, out["threshold"]), 4)
    return out


def pin_dead_signals(params: dict, live: set[str]) -> dict:
    """Zero the weight of every signal not in ``live``, then renormalize. Pure.

    The search grid already pins dead signals, but ``blend`` pulls the incumbent
    forward, and the incumbent's ancestor is the hand-set ``DEFAULT_WEIGHTS``
    (``sentiment`` 0.35). While a signal is identically zero that inherited weight
    is inert and invisible. The hazard is the handover: the moment the feed starts
    producing values, a weight that was never fitted — because the search could
    not touch a constant — begins moving live predictions, and it keeps doing so
    until coverage is high enough for the tuner to judge it.

    Pinning closes that window. A signal contributes only once it has earned a
    weight on held-out days.

    **Deliberately does not renormalize.** A dead signal contributes
    ``weight × 0 = 0``, so zeroing its weight cannot change any current
    prediction — but scaling the surviving weights back up to sum to 1 would:
    it raises the composite against a fixed threshold, which is an unvalidated
    change to the abstain rate. That is the backdoor threshold dial this module
    exists to prevent, running in reverse. Leaving the total below 1 keeps
    behaviour on today's data bit-for-bit identical and changes only what happens
    when the signal wakes up.
    """
    out = dict(params)
    for k in SIGNAL_KEYS:
        if k not in live:
            out[k] = 0.0
    return out
