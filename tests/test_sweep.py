"""Tests for the wide correlation sweep — especially its false-discovery control."""
import math
import random

from oracle.research import sweep as sw
from oracle.research import universe as uni


def _dates(n, start=1):
    return [f"2026-{1 + (start + i) // 28:02d}-{1 + (start + i) % 28:02d}" for i in range(n)]


def _series(vals, dates=None):
    dates = dates or _dates(len(vals))
    return dict(zip(dates, vals))


# ── core statistics ───────────────────────────────────────────────────────
def test_pearson_perfect_and_degenerate():
    assert round(sw.pearson([1, 2, 3], [2, 4, 6]), 6) == 1.0
    assert round(sw.pearson([1, 2, 3], [6, 4, 2]), 6) == -1.0
    assert sw.pearson([1, 1, 1], [1, 2, 3]) is None      # zero variance
    assert sw.pearson([1], [1]) is None


def test_p_value_shrinks_with_strength_and_sample():
    weak = sw.corr_p_value(0.10, 100)
    strong = sw.corr_p_value(0.60, 100)
    assert strong < weak
    # same r, more data -> more significant
    assert sw.corr_p_value(0.30, 400) < sw.corr_p_value(0.30, 50)
    assert sw.corr_p_value(0.5, 3) is None               # too few points


def test_p_value_of_zero_correlation_is_one():
    assert abs(sw.corr_p_value(0.0, 100) - 1.0) < 1e-9


# ── the multiple-comparisons guard (the point of the module) ─────────────
def test_bh_is_stricter_than_raw_p():
    # 100 tests, one at p=0.01: on its own that reads "significant"; inside a
    # sweep of 100 it is not, which is the entire point of the correction.
    ps = [0.01] + [0.5] * 99
    qvals, _thr = sw.benjamini_hochberg(ps, q=0.10)
    assert qvals[0] > 0.01                    # inflated by multiplicity
    assert qvals[0] > 0.10                    # fails the FDR bar it "passed" raw


def test_bh_keeps_a_genuinely_strong_set():
    ps = [1e-8] * 10 + [0.5] * 90
    qvals, _ = sw.benjamini_hochberg(ps, q=0.10)
    assert all(q < 0.10 for q in qvals[:10])  # real effects survive correction


def test_bh_qvalues_are_monotone_and_handle_empty():
    ps = [0.001, 0.02, 0.04, 0.3, 0.8]
    qvals, _ = sw.benjamini_hochberg(ps, q=0.1)
    ordered = [q for _p, q in sorted(zip(ps, qvals))]
    assert ordered == sorted(ordered)          # never decreases with p
    assert sw.benjamini_hochberg([], 0.1) == ([], None)


# ── pairing + stability ───────────────────────────────────────────────────
def test_aligned_returns_shifts_us_back_by_lag():
    us = _series([1.0, 2.0, 3.0], _dates(3))
    cn = _series([10.0, 20.0, 30.0], _dates(3))
    xs, ys, dates = sw.aligned_returns(us, cn, 1)
    assert xs == [1.0, 2.0] and ys == [20.0, 30.0]
    assert dates == _dates(3)[1:]


def test_aligned_returns_uses_only_common_dates():
    us = {"d1": 1.0, "d2": 2.0, "d9": 9.0}
    cn = {"d2": 20.0, "d9": 90.0}
    xs, ys, _ = sw.aligned_returns(us, cn, 0)
    assert xs == [2.0, 9.0] and ys == [20.0, 90.0]


def test_split_half_flags_a_sign_reversal():
    n = sw.MIN_PAIRS
    xs = list(range(n))
    # first half positively related, second half negatively -> not stable
    ys = list(range(n // 2)) + list(range(n // 2, 0, -1)) + [0] * (n - n // 2 - n // 2)
    out = sw.split_half_stability(xs, ys[:n])
    assert out["stable"] is False


def test_split_half_accepts_a_consistent_relationship():
    n = 120
    xs = [float(i) for i in range(n)]
    ys = [2.0 * i for i in range(n)]
    assert sw.split_half_stability(xs, ys)["stable"] is True


# ── the sweep end to end ──────────────────────────────────────────────────
def test_sweep_finds_a_planted_lagged_signal():
    rng = random.Random(11)
    n = 300
    dates = _dates(n)
    us_vals = [rng.gauss(0, 1) for _ in range(n)]
    # china day t follows US day t-1, plus noise
    cn_vals = [0.0] + [0.8 * us_vals[i - 1] + rng.gauss(0, 0.4) for i in range(1, n)]
    res = sw.sweep({"LEAD": _series(us_vals, dates)},
                   {"CN": _series(cn_vals, dates)}, lags=(0, 1, 2))
    surv = sw.survivors(res)
    assert surv, "planted lag-1 signal should survive every filter"
    assert surv[0]["lag"] == 1 and surv[0]["r"] > 0.5
    assert surv[0]["split_stable"] is True


def test_sweep_rejects_pure_noise_after_fdr():
    """The critical property: many independent noise pairs must yield ~no
    survivors once the false-discovery correction is applied."""
    rng = random.Random(5)
    n = 200
    dates = _dates(n)
    us = {f"U{i}": _series([rng.gauss(0, 1) for _ in range(n)], dates) for i in range(12)}
    cn = {f"C{j}": _series([rng.gauss(0, 1) for _ in range(n)], dates) for j in range(12)}
    res = sw.sweep(us, cn, lags=(0, 1, 2))
    assert res["tests"] > 300                 # a genuinely wide sweep
    # with ~430 noise tests, uncorrected p<0.05 would hand back ~20 "hits"
    naive = [r for r in res["results"] if (r["p_value"] or 1) < 0.05]
    assert len(naive) >= 5                    # the trap is real...
    assert len(sw.survivors(res)) <= 2        # ...and the correction closes it


def test_sweep_marks_lag_zero_untradeable():
    rng = random.Random(3)
    n = 200
    dates = _dates(n)
    v = [rng.gauss(0, 1) for _ in range(n)]
    res = sw.sweep({"U": _series(v, dates)}, {"C": _series(v, dates)}, lags=(0,))
    row = res["results"][0]
    assert row["r"] > 0.99 and row["significant"] is True
    assert row["tradeable"] is False and row["survives"] is False   # perfect but useless


def test_group_summary_separates_controls():
    rng = random.Random(9)
    n = 200
    dates = _dates(n)
    us_vals = [rng.gauss(0, 1) for _ in range(n)]
    cn_vals = [0.0] + [0.8 * us_vals[i - 1] + rng.gauss(0, 0.3) for i in range(1, n)]
    res = sw.sweep({"FXI": _series(us_vals, dates), "EEM": _series(
        [rng.gauss(0, 1) for _ in range(n)], dates)},
        {"C": _series(cn_vals, dates)}, lags=(1,))
    groups = {g["group"]: g for g in sw.group_summary(res, uni.group_of)}
    assert groups["china_proxy"]["survival_rate"] > groups["control_em"]["survival_rate"]


# ── universe hygiene ──────────────────────────────────────────────────────
def test_every_symbol_has_a_stated_hypothesis():
    for sym in uni.us_symbols():
        assert uni.rationale_of(sym) and uni.group_of(sym) != "unknown", sym


def test_controls_are_flagged():
    assert uni.is_control("EEM") and uni.is_control("EFA")
    assert not uni.is_control("FXI")


# ── regression: the research backfill must not break the prediction pipeline ──
def test_research_backfill_preserves_pipeline_sector_tags(monkeypatch, tmp_path):
    """The research universe reuses symbols the daily pipeline depends on
    (^GSPC, SOXX, XLE...). build_signals maps China sectors onto US tags like
    'semis'/'energy'; if the research backfill relabels them with its own group
    names ('us_sector'), every spillover signal silently becomes 0 and every
    daily call goes neutral. It happened — this locks it shut."""
    from oracle import config, db
    from oracle.ingestion.us_market import SECTOR_TAGS
    from oracle.research import run as rr

    dbp = tmp_path / "s.db"
    monkeypatch.setattr(config, "DB_PATH", dbp)
    db.init_db(dbp)

    monkeypatch.setattr(rr, "backfill_research_universe",
                        rr.backfill_research_universe)   # keep the real one
    # stub the network: one bar per symbol
    import oracle.backfill as bf
    monkeypatch.setattr(bf, "_download_us", lambda sym, days: object())
    monkeypatch.setattr(bf, "_us_records", lambda df: [
        {"date": "2026-08-03", "close": 100.0, "open": 99.0, "high": 101.0, "low": 98.0},
        {"date": "2026-08-04", "close": 102.0, "open": 100.0, "high": 103.0, "low": 99.0}])

    rr.backfill_research_universe(days=5)

    conn = db.connect(dbp)
    stored = {r["symbol"]: r["sector"] for r in
              conn.execute("SELECT DISTINCT symbol, sector FROM us_close")}
    conn.close()
    for sym, expected in SECTOR_TAGS.items():
        if sym in stored:
            assert stored[sym] == expected, (
                f"{sym} was relabelled {stored[sym]!r}, breaking the pipeline "
                f"mapping which needs {expected!r}")
