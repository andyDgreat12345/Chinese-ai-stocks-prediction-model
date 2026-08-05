"""The wide correlation sweep — and the statistics that keep it honest.

Testing every US↔China pair across several lags produces thousands of
correlations. At p < 0.05, **one test in twenty clears the bar by chance alone**:
sweep 4,000 pairs and expect ~200 "significant" results from pure noise. A wide
sweep without multiple-comparison control is not research, it is a machine for
generating confident nonsense. So every candidate here must survive four filters,
in order:

  1. **Sample floor** — enough paired observations to estimate anything.
  2. **Significance** — a two-sided p-value from the Fisher z-transform.
  3. **False-discovery control** — Benjamini–Hochberg across the *entire* sweep,
     so the reported q-value already accounts for how many tests were run.
  4. **Out-of-sample stability** — the history is split in half; a pair is kept
     only if both halves agree in sign. A relationship that reverses across the
     split is not a relationship.

Tradeability is enforced separately: lag 0 pairs are reported but flagged, since
the Chinese close precedes the US close by ~14h on the same date, so a same-day
correlation cannot be acted on (see ``analysis/pairs.py``).

Everything is pure and stdlib-only — no scipy, no network — so the whole sweep is
deterministic and unit-testable.

**Not investment advice.** A pair surviving all four filters is a hypothesis worth
studying, not a trade.
"""
from __future__ import annotations

import math

MIN_PAIRS = 40          # below this, don't even test
DEFAULT_LAGS = (0, 1, 2, 3)
FDR_Q = 0.10            # accepted false-discovery rate among reported hits


# ── core statistics (stdlib only) ─────────────────────────────────────────
def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2 or n != len(ys):
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def _norm_sf(z: float) -> float:
    """Upper-tail probability of the standard normal."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def corr_p_value(r: float, n: int) -> float | None:
    """Two-sided p-value for a correlation via the Fisher z-transform.

    z = atanh(r)·√(n−3) is approximately standard normal under H0: ρ = 0. Chosen
    over the exact t-test because it needs no incomplete-beta function, and the
    two agree closely at the sample sizes we care about (n ≥ 40)."""
    if n is None or n < 4 or r is None:
        return None
    r = max(-0.999999, min(0.999999, r))
    z = math.atanh(r) * math.sqrt(n - 3)
    return min(1.0, 2.0 * _norm_sf(abs(z)))


def benjamini_hochberg(p_values: list[float], q: float = FDR_Q) -> tuple[list[float], float | None]:
    """Return (q-values, largest p that passes) controlling the false-discovery
    rate at `q` across ALL tests. The q-value is the FDR at which that test would
    just be called significant — i.e. it already accounts for the sweep's width."""
    m = len(p_values)
    if not m:
        return [], None
    order = sorted(range(m), key=lambda i: p_values[i])
    qvals = [1.0] * m
    running = 1.0
    threshold = None
    # walk from the largest p downward so the q-value stays monotone
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        val = min(1.0, p_values[i] * m / rank)
        running = min(running, val)
        qvals[i] = running
        if threshold is None and p_values[i] <= q * rank / m:
            threshold = p_values[i]
    return qvals, threshold


# ── pairing + splitting ───────────────────────────────────────────────────
def aligned_returns(us_by_date: dict[str, float], cn_by_date: dict[str, float],
                    lag: int) -> tuple[list[float], list[float], list[str]]:
    """Pair the China move on session t with the US move on session t−lag, over
    the dates both markets traded. Returns (us, china, china_dates)."""
    common = sorted(set(us_by_date) & set(cn_by_date))
    xs, ys, dates = [], [], []
    for i in range(lag, len(common)):
        u, c = us_by_date[common[i - lag]], cn_by_date[common[i]]
        if u is None or c is None:
            continue
        xs.append(u)
        ys.append(c)
        dates.append(common[i])
    return xs, ys, dates


def split_half_stability(xs: list[float], ys: list[float]) -> dict:
    """Correlate each half of the history separately. A genuine relationship
    shows the same sign in both; one that flips is noise that happened to average
    out. This is the cheapest out-of-sample check available."""
    n = len(xs)
    if n < 2 * MIN_PAIRS // 2:
        return {"stable": False, "r_first": None, "r_second": None}
    mid = n // 2
    r1 = pearson(xs[:mid], ys[:mid])
    r2 = pearson(xs[mid:], ys[mid:])
    if r1 is None or r2 is None:
        return {"stable": False, "r_first": r1, "r_second": r2}
    stable = (r1 > 0) == (r2 > 0)
    return {"stable": stable, "r_first": round(r1, 4), "r_second": round(r2, 4)}


# ── the sweep ─────────────────────────────────────────────────────────────
def sweep(us_series: dict[str, dict[str, float]],
          cn_series: dict[str, dict[str, float]],
          lags=DEFAULT_LAGS, min_pairs: int = MIN_PAIRS, q: float = FDR_Q) -> dict:
    """Test every (US symbol × China symbol × lag) combination.

    ``us_series``/``cn_series`` map symbol -> {date: pct_change}. Returns the full
    result set plus the count of tests run, so the report can state how much
    multiplicity the q-values are correcting for."""
    raw: list[dict] = []
    for us_sym, us_map in us_series.items():
        for cn_sym, cn_map in cn_series.items():
            for lag in lags:
                xs, ys, _dates = aligned_returns(us_map, cn_map, lag)
                if len(xs) < min_pairs:
                    continue
                r = pearson(xs, ys)
                if r is None:
                    continue
                raw.append({
                    "us_symbol": us_sym, "china_symbol": cn_sym, "lag": lag,
                    "r": round(r, 4), "n": len(xs),
                    "p_value": corr_p_value(r, len(xs)),
                    **{f"split_{k}": v for k, v in split_half_stability(xs, ys).items()},
                })
    qvals, threshold = benjamini_hochberg([row["p_value"] for row in raw], q)
    for row, qv in zip(raw, qvals):
        row["q_value"] = round(qv, 6)
        row["significant"] = qv <= q
        row["tradeable"] = row["lag"] >= 1
        # The full bar: significant after multiplicity AND sign-stable AND usable.
        row["survives"] = bool(row["significant"] and row["split_stable"]
                               and row["tradeable"])
    return {"tests": len(raw), "fdr_q": q, "p_threshold": threshold, "results": raw}


def survivors(result: dict, limit: int | None = None) -> list[dict]:
    """Pairs that cleared every filter, strongest first."""
    rows = [r for r in result["results"] if r["survives"]]
    rows.sort(key=lambda r: -abs(r["r"]))
    return rows[:limit] if limit else rows


def group_summary(result: dict, group_of) -> list[dict]:
    """Per US-group hit-rate — how many of each hypothesis group's tests survived.
    Reading the control groups here is how you tell insight from beta."""
    agg: dict[str, dict] = {}
    for r in result["results"]:
        g = group_of(r["us_symbol"])
        a = agg.setdefault(g, {"group": g, "tests": 0, "survivors": 0, "best_r": 0.0})
        a["tests"] += 1
        if r["survives"]:
            a["survivors"] += 1
            if abs(r["r"]) > abs(a["best_r"]):
                a["best_r"] = r["r"]
    for a in agg.values():
        a["survival_rate"] = round(a["survivors"] / a["tests"], 4) if a["tests"] else 0.0
    return sorted(agg.values(), key=lambda a: -a["survival_rate"])
