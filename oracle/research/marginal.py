"""Marginal analysis — is there any *condition* under which the tradeable
segment is predictable?

``analysis/segments.py`` established the uncomfortable headline: the gap is
71% predictable but unreachable (owning it means entering before the US session
that drives it), while the **body** — open to close, the only part a position
entered at the open actually earns — runs 49.3%, a coin flip. Averaged over
everything, the system has no directional edge it can trade.

"Averaged over everything" is the loophole this module tests. A coin flip overall
can still hide a subset that works. So each session is labelled with conditions
known **before the open** and the body hit rate is measured within every bucket.

**The danger is obvious and is the reason for most of this file.** Slicing 21,000
sessions enough ways will always produce a bucket at 60%, and acting on it is how
backtests are manufactured. Three defences, the same ones the correlation sweep
uses:

  1. **Pre-registered conditions.** The list below is fixed and deliberately
     short. Buckets are quantiles of the conditioning variable, not boundaries
     chosen after seeing the outcome.
  2. **Benjamini-Hochberg across every bucket tested**, so a reported q-value
     already accounts for how many slices were looked at.
  3. **Split-half stability** — the history is halved and a bucket is kept only
     if both halves agree in sign. A subset that works in one half and reverses
     in the other is noise that happened to average out.

Only conditions observable **at the open** are admissible. The gap qualifies (it
is complete by definition once the session opens); the day's range does not.

**Not investment advice.** A surviving bucket is a hypothesis, not a trade.
"""
from __future__ import annotations

from math import sqrt

from . import sweep as sw

# Bars beyond this are corporate actions or bad prints, not market moves. One
# unadjusted share conversion at -75% dominates any statistic it lands in.
OUTLIER_LIMIT_PCT = 11.0

# Minimum observations before a bucket is testable at all.
MIN_BUCKET = 150

# How many quantile buckets per continuous condition. Coarse on purpose: finer
# slicing buys resolution the sample cannot support and multiplicity we then pay
# for in the correction.
N_BUCKETS = 5


def _quantile_buckets(rows: list[dict], key: str, n: int) -> list[tuple[str, list[dict]]]:
    """Split rows into n equal-count buckets by `key`. Pure."""
    usable = [r for r in rows if r.get(key) is not None]
    if len(usable) < n:
        return []
    usable.sort(key=lambda r: r[key])
    out, N = [], len(usable)
    for i in range(n):
        chunk = usable[i * N // n:(i + 1) * N // n]
        if not chunk:
            continue
        label = f"{key} [{chunk[0][key]:+.2f},{chunk[-1][key]:+.2f}]"
        out.append((label, chunk))
    return out


def _categorical_buckets(rows: list[dict], key: str) -> list[tuple[str, list[dict]]]:
    groups: dict = {}
    for r in rows:
        v = r.get(key)
        if v is not None:
            groups.setdefault(v, []).append(r)
    return [(f"{key}={k}", v) for k, v in sorted(groups.items(), key=lambda kv: str(kv[0]))]


# Pre-registered conditions, all observable at the open. Adding to this list
# widens the multiplicity correction — which is the honest cost of looking.
CONDITIONS = (
    ("gap", "quantile"),              # does the body fade or follow the gap?
    ("signal_strength", "quantile"),  # does model conviction matter?
    ("prior_body", "quantile"),       # intraday continuation or reversal?
    ("volatility", "quantile"),       # regime
    ("sector", "categorical"),
    ("weekday", "categorical"),
)


def _hit_stats(chunk: list[dict]) -> tuple[int, float | None]:
    """(n, share of sessions whose body rose). Pure."""
    vals = [r["body"] for r in chunk if r.get("body") is not None]
    if not vals:
        return 0, None
    return len(vals), sum(1 for v in vals if v > 0) / len(vals)


def _p_value(hit: float, n: int) -> float:
    """Two-sided p for a hit rate against a coin flip, normal approximation."""
    if n < 2:
        return 1.0
    z = (hit - 0.5) / (0.5 / sqrt(n))
    return min(1.0, 2.0 * sw._norm_sf(abs(z)))


def analyse(rows: list[dict], q: float = sw.FDR_Q,
            min_bucket: int = MIN_BUCKET) -> dict:
    """Test every pre-registered bucket, FDR-correct across all of them.

    ``rows``: one dict per sector-session with at least ``body`` plus the
    conditioning keys. Pure.
    """
    rows = [r for r in rows
            if r.get("body") is not None
            and abs(r.get("body") or 0) <= OUTLIER_LIMIT_PCT
            and abs(r.get("gap") or 0) <= OUTLIER_LIMIT_PCT]

    mid = len(rows) // 2
    first_half = set(id(r) for r in sorted(rows, key=lambda r: r.get("date", ""))[:mid])

    results = []
    for key, kind in CONDITIONS:
        buckets = (_quantile_buckets(rows, key, N_BUCKETS) if kind == "quantile"
                   else _categorical_buckets(rows, key))
        for label, chunk in buckets:
            n, hit = _hit_stats(chunk)
            if n < min_bucket or hit is None:
                continue
            a = [r for r in chunk if id(r) in first_half]
            b = [r for r in chunk if id(r) not in first_half]
            _, hit_a = _hit_stats(a)
            _, hit_b = _hit_stats(b)
            stable = (hit_a is not None and hit_b is not None
                      and (hit_a > 0.5) == (hit_b > 0.5))
            results.append({
                "condition": key, "bucket": label, "n": n,
                "hit_rate": round(hit, 4),
                "edge_t": round((hit - 0.5) * sqrt(n), 3),
                "mean_body_pct": round(sum(r["body"] for r in chunk) / len(chunk), 4),
                "p_value": _p_value(hit, n),
                "half_1": None if hit_a is None else round(hit_a, 4),
                "half_2": None if hit_b is None else round(hit_b, 4),
                "stable": stable,
            })

    qvals, _ = sw.benjamini_hochberg([r["p_value"] for r in results], q)
    for r, qv in zip(results, qvals):
        r["q_value"] = round(qv, 6)
        r["significant"] = qv <= q
        r["survives"] = bool(r["significant"] and r["stable"])
    return {"tests": len(results), "fdr_q": q, "rows": results,
            "n_sessions": len(rows)}


def survivors(result: dict) -> list[dict]:
    rows = [r for r in result["rows"] if r["survives"]]
    rows.sort(key=lambda r: -abs(r["hit_rate"] - 0.5))
    return rows


def format_report(result: dict) -> str:
    L = ["Marginal analysis — conditions under which the BODY is predictable", "",
         f"  {result['n_sessions']} sector-sessions, {result['tests']} buckets tested, "
         f"FDR q={result['fdr_q']}",
         f"  (bars beyond ±{OUTLIER_LIMIT_PCT:.0f}% excluded as corporate actions)", ""]
    surv = survivors(result)
    L.append(f"  {len(surv)} bucket(s) survived significance + FDR + split-half stability")
    L.append("")
    L.append(f"  {'bucket':34}{'n':>7}{'hit':>8}{'q':>10}{'half1/half2':>16}")
    L.append(f"  {'-' * 76}")
    shown = surv or sorted(result["rows"], key=lambda r: r["q_value"])[:10]
    for r in shown:
        # BOTH halves must be present: a bucket landing entirely in one half of
        # the history has one side and not the other.
        halves = (f"{r['half_1']:.0%}/{r['half_2']:.0%}"
                  if r["half_1"] is not None and r["half_2"] is not None else "n/a")
        L.append(f"  {r['bucket'][:32]:34}{r['n']:>7}{r['hit_rate']:>7.1%}"
                 f"{r['q_value']:>10.4f}{halves:>16}")
    if not surv:
        L += ["",
              "  Nothing survived. The best rows above are shown for reference only —",
              "  they are what the FDR correction is designed to reject. On this",
              "  evidence the body is not predictable under any tested condition,",
              "  and no marginal rule should be traded."]
    L += ["", "  Not investment advice."]
    return "\n".join(L)


def build_rows(db_path=None, start: str | None = None) -> list[dict]:
    """One row per sector-session, labelled with conditions known AT THE OPEN.

    ``volatility`` and ``prior_body`` are taken from the PREVIOUS session, and
    ``signal_strength`` from the model's own call — all available before the bar
    being predicted. Nothing here may read the session it labels.
    """
    from datetime import date

    from .. import db
    from ..analysis.segments import build_segments
    from ..backtest import collect_records

    segs = build_segments(db_path=db_path, start=start)
    strength = {(r["sector"], r["date"]): abs(float(r.get("model_conf_score") or 0.0)
                                              or abs(float(r.get("us_spillover") or 0.0)))
                for r in collect_records(start=start, db_path=db_path)}

    rows = []
    for sector, by_date in segs.items():
        dates = sorted(by_date)
        for i, d in enumerate(dates):
            s = by_date[d]
            if s.body is None:
                continue
            prev = by_date[dates[i - 1]] if i else None
            window = [by_date[x].range_pct for x in dates[max(0, i - 20):i]]
            window = [w for w in window if w is not None]
            try:
                wd = date.fromisoformat(d).weekday()
            except ValueError:
                wd = None
            rows.append({
                "sector": sector, "date": d,
                "body": s.body, "gap": s.gap,
                "prior_body": prev.body if prev else None,
                "volatility": (sum(window) / len(window)) if window else None,
                "signal_strength": strength.get((sector, d)),
                "weekday": wd,
            })
    return rows


def main(argv: list[str]) -> int:
    from .. import config

    start = config.LEARNING_TRAIN_START or None
    print(format_report(analyse(build_rows(start=start))))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))


# ── simulation test ───────────────────────────────────────────────────────
# Round-trip friction, matching the trader simulator.
COST_BPS = 15.0


def simulate_rule(rows: list[dict], predicate, cost_bps: float = COST_BPS,
                  holdout_frac: float = 0.3) -> dict:
    """Buy at the open and sell at the close on sessions matching `predicate`.

    This is the only honest way to price a marginal bucket. Two things make the
    raw hit rate misleading on its own:

      * the body carries a **positive baseline drift** (+0.109%/session over the
        window), so a rule must beat buy-every-session, not beat zero;
      * a 15bps round trip is 0.15%, which is larger than most of the lift on
        offer — an edge that looks real can still be unprofitable.

    The window is split chronologically and only the holdout decides. Pure.
    """
    usable = sorted([r for r in rows
                     if r.get("body") is not None
                     and abs(r["body"]) <= OUTLIER_LIMIT_PCT],
                    key=lambda r: r.get("date", ""))
    if not usable:
        return {"status": "no_data"}
    cut = int(len(usable) * (1 - holdout_frac))
    parts = {"train": usable[:cut], "holdout": usable[cut:]}

    out = {"status": "measured", "cost_pct": cost_bps / 100.0}
    for name, part in parts.items():
        picked = [r for r in part if predicate(r)]
        every = part
        def stats(chunk):
            if not chunk:
                return {"n": 0, "gross": None, "net": None, "hit": None}
            gross = sum(r["body"] for r in chunk) / len(chunk)
            return {"n": len(chunk),
                    "gross": round(gross, 4),
                    "net": round(gross - cost_bps / 100.0, 4),
                    "hit": round(sum(1 for r in chunk if r["body"] > 0) / len(chunk), 4)}
        out[name] = {"rule": stats(picked), "baseline_all_sessions": stats(every)}
    return out


def format_rule_test(name: str, res: dict) -> str:
    if res.get("status") != "measured":
        return f"  {name}: {res.get('status')}"
    L = [f"  {name}", f"    round-trip cost assumed: {res['cost_pct']:.2f}%"]
    for part in ("train", "holdout"):
        r, b = res[part]["rule"], res[part]["baseline_all_sessions"]
        L.append(f"    {part:8} rule   n={r['n']:5} hit={_p(r['hit'])} "
                 f"gross={_f(r['gross'])} net={_f(r['net'])}")
        L.append(f"    {'':8} every  n={b['n']:5} hit={_p(b['hit'])} "
                 f"gross={_f(b['gross'])} net={_f(b['net'])}")
    return "\n".join(L)


def _p(x):
    return "  n/a" if x is None else f"{x:5.1%}"


def _f(x):
    return "    n/a" if x is None else f"{x:+7.4f}%"
