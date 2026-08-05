"""Tests for the empirical learning loop (walk-forward fitting + guarded adoption)."""
import tempfile

from oracle import config, db
from oracle.learning import autotune as at, walkforward as wf


def _rec(date, sector="semis", us=0.0, sent=0.0, macro=0.0, move=0.0):
    return {"date": date, "sector": sector, "us_spillover": us, "sentiment": sent,
            "macro": macro, "actual_move": move,
            "actual_dir": "bullish" if move > 0.1 else ("bearish" if move < -0.1 else "neutral")}


# ── pure scoring ──────────────────────────────────────────────────────────
def test_predict_dir_respects_threshold():
    p = {"us_spillover": 1.0, "sentiment": 0.0, "macro": 0.0, "threshold": 0.5}
    assert wf.predict_dir((0.6, 0, 0), p) == "bullish"
    assert wf.predict_dir((-0.6, 0, 0), p) == "bearish"
    assert wf.predict_dir((0.4, 0, 0), p) == "neutral"      # below threshold: abstain


def test_score_params_neutral_is_not_a_win():
    # A record the params abstain on must not count as a hit or a bet.
    recs = [_rec("d1", us=0.01, move=1.0)]
    p = {"us_spillover": 1.0, "sentiment": 0.0, "macro": 0.0, "threshold": 0.5}
    s = wf.score_params(recs, p, min_bets=1)
    assert s["bets"] == 0 and s["hits"] == 0 and s["hit_rate"] is None


def test_score_params_edge_rewards_accuracy_and_sample():
    good = [_rec(f"d{i}", us=1.0, move=1.0) for i in range(20)]     # always right
    p = {"us_spillover": 1.0, "sentiment": 0.0, "macro": 0.0, "threshold": 0.1}
    s = wf.score_params(good, p, min_bets=5)
    assert s["hit_rate"] == 1.0 and s["bets"] == 20
    # 20 correct bets must score better than 5 correct bets (sample matters)
    s_small = wf.score_params(good[:5], p, min_bets=5)
    assert s["edge_t"] > s_small["edge_t"]


def test_score_params_below_min_bets_is_rejected():
    recs = [_rec("d1", us=1.0, move=1.0)]
    p = {"us_spillover": 1.0, "sentiment": 0.0, "macro": 0.0, "threshold": 0.1}
    assert wf.score_params(recs, p, min_bets=10)["edge_t"] == float("-inf")


# ── honest splitting ──────────────────────────────────────────────────────
def test_split_holdout_is_by_date_and_takes_the_most_recent():
    recs = [_rec(f"2026-08-{d:02d}") for d in range(1, 11)]
    pool, hold = wf.split_holdout(recs, 3)
    assert {r["date"] for r in hold} == {"2026-08-08", "2026-08-09", "2026-08-10"}
    assert len(pool) == 7
    # a day is never split across the boundary
    assert not ({r["date"] for r in pool} & {r["date"] for r in hold})


def test_split_holdout_noop_when_too_short():
    recs = [_rec("d1"), _rec("d2")]
    pool, hold = wf.split_holdout(recs, 5)
    assert hold == [] and len(pool) == 2


def test_walk_forward_folds_always_validate_after_training():
    recs = [_rec(f"2026-{m:02d}-{d:02d}") for m in (5, 6) for d in range(1, 29)]
    folds = wf.walk_forward_folds(recs, n_folds=3, min_train_days=20)
    assert folds
    for train, val in folds:
        assert max(r["date"] for r in train) < min(r["date"] for r in val)


def test_walk_forward_folds_empty_when_history_too_short():
    assert wf.walk_forward_folds([_rec("d1"), _rec("d2")], min_train_days=40) == []


# ── selection + bounded blending ──────────────────────────────────────────
def test_select_params_finds_the_predictive_signal():
    # sentiment perfectly predicts the move; us_spillover is pure noise.
    recs = []
    for i in range(120):
        s = 1.0 if i % 2 == 0 else -1.0
        recs.append(_rec(f"2026-{1 + i // 30:02d}-{1 + i % 30:02d}",
                         us=-s, sent=s, move=s * 2))
    best = wf.select_params(recs, n_folds=3)
    assert best is not None
    assert best["sentiment"] > best["us_spillover"]     # learned which one works


def test_blend_is_bounded_and_renormalized():
    inc = {"us_spillover": 1.0, "sentiment": 0.0, "macro": 0.0, "threshold": 0.15}
    cand = {"us_spillover": 0.0, "sentiment": 1.0, "macro": 0.0, "threshold": 0.05}
    out = wf.blend(inc, cand, 0.5)
    assert abs(sum(out[k] for k in ("us_spillover", "sentiment", "macro")) - 1.0) < 1e-6
    assert 0.0 < out["sentiment"] < 1.0        # moved, but not all the way
    assert inc["threshold"] > out["threshold"] > cand["threshold"]


def test_blend_zero_step_keeps_incumbent():
    inc = {"us_spillover": 0.6, "sentiment": 0.3, "macro": 0.1, "threshold": 0.15}
    out = wf.blend(inc, {"us_spillover": 0, "sentiment": 1, "macro": 0, "threshold": 0.9}, 0.0)
    assert out["us_spillover"] == 0.6 and out["threshold"] == 0.15


# ── the guard: adopt only on proven out-of-sample gain ────────────────────
def _seeded_db(monkeypatch):
    tmp = tempfile.mktemp(suffix=".db")
    monkeypatch.setattr(config, "DB_PATH", tmp)
    db.init_db(tmp)
    return tmp


def test_tune_refuses_when_holdout_too_small(monkeypatch):
    tmp = _seeded_db(monkeypatch)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_RECORDS", 20)
    inc = {"us_spillover": 0.45, "sentiment": 0.35, "macro": 0.2, "threshold": 0.15}
    recs = [_rec(f"2026-08-{d:02d}") for d in range(1, 6)]
    row = at.tune_sector("semis", recs, inc, "2026-08-10", "t", db_path=tmp)
    assert row["adopted"] == 0 and "holdout too small" in row["reason"]
    # incumbent untouched
    assert db.get_model_params("semis", tmp)["threshold"] == 0.15


def test_tune_refuses_when_no_out_of_sample_gain(monkeypatch):
    tmp = _seeded_db(monkeypatch)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_RECORDS", 5)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_BETS", 1)
    monkeypatch.setattr(config, "LEARNING_MIN_IMPROVEMENT", 99.0)   # impossible bar
    inc = {"us_spillover": 0.45, "sentiment": 0.35, "macro": 0.2, "threshold": 0.15}
    recs = [_rec(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", us=1.0, move=1.0)
            for i in range(120)]
    row = at.tune_sector("semis", recs, inc, "2026-08-10", "t", db_path=tmp)
    assert row["adopted"] == 0
    # A refusal must not persist a LEARNED change. The dead-signal pin may still
    # be written — it is behaviour-preserving (weight x 0 = 0), not a fit.
    stored = db.all_model_params(tmp).get("semis")
    if stored is not None:
        assert stored["us_spillover"] == inc["us_spillover"]
        assert stored["threshold"] == inc["threshold"]
        assert stored["sentiment"] == 0.0          # pinned, never fitted


def test_tune_adopts_and_persists_when_genuinely_better(monkeypatch):
    tmp = _seeded_db(monkeypatch)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_RECORDS", 5)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_BETS", 1)
    monkeypatch.setattr(config, "LEARNING_MIN_IMPROVEMENT", 0.0)
    monkeypatch.setattr(config, "LEARNING_HOLDOUT_DAYS", 20)
    monkeypatch.setattr(config, "LEARNING_STEP", 1.0)
    # An incumbent that abstains on everything (threshold 0.9) vs. a signal that
    # is perfectly predictive — any sane fit must beat it.
    inc = {"us_spillover": 0.45, "sentiment": 0.35, "macro": 0.2, "threshold": 0.9}
    recs = []
    for i in range(150):
        s = 1.0 if i % 3 else -1.0
        recs.append(_rec(f"2026-{1 + i // 30:02d}-{1 + i % 30:02d}",
                         us=s, sent=s, move=s * 1.5))
    row = at.tune_sector("semis", recs, inc, "2026-08-10", "t", db_path=tmp)
    assert row["adopted"] == 1 and "adopted:" in row["reason"]
    stored = db.get_model_params("semis", tmp)
    assert stored["threshold"] < 0.9                      # learned to stop abstaining
    # the incumbent never bet, so it has no hit-rate; the adopted set does
    assert row["hit_before"] is None and row["hit_after"] is not None


def test_rollback_restores_previous_params(monkeypatch):
    tmp = _seeded_db(monkeypatch)
    db.set_model_params("semis", {"us_spillover": 0.9, "sentiment": 0.1,
                                  "macro": 0.0, "threshold": 0.05}, "t", tmp)
    db.record_learning({
        "run_date": "2026-08-10", "sector": "semis",
        "params_before": '{"us_spillover": 0.45, "sentiment": 0.35, '
                         '"macro": 0.2, "threshold": 0.15}',
        "params_after": '{"us_spillover": 0.9}', "score_before": 0.1,
        "score_after": 0.5, "hit_before": 0.5, "hit_after": 0.6, "n_holdout": 30,
        "adopted": 1, "reason": "x", "created_at": "t"}, db_path=tmp)
    assert at.rollback("semis", db_path=tmp) is True
    assert db.get_model_params("semis", tmp)["threshold"] == 0.15   # reverted


# ── the learned params actually drive production predictions ──────────────
def test_learned_params_change_the_prediction(monkeypatch):
    from oracle.analysis.scoring import SectorSignals, score_sector
    sig = SectorSignals(us_spillover=0.2, sentiment=0.0)
    # default threshold 0.15 with weight .45 -> composite .09 -> neutral
    assert score_sector(sig).direction == "neutral"
    # a learned set that trusts US spillover and bets earlier -> bullish
    learned = {"us_spillover": 1.0, "sentiment": 0.0, "macro": 0.0, "threshold": 0.15}
    assert score_sector(sig, learned, learned["threshold"]).direction == "bullish"


def test_get_model_params_falls_back_to_defaults(monkeypatch):
    tmp = _seeded_db(monkeypatch)
    p = db.get_model_params("semis", tmp)
    assert p["us_spillover"] == 0.45 and p["threshold"] == 0.15     # untouched default
    db.set_model_params("*", {"threshold": 0.05}, "t", tmp)
    assert db.get_model_params("semis", tmp)["threshold"] == 0.05   # global applies
    db.set_model_params("semis", {"threshold": 0.22}, "t", tmp)
    assert db.get_model_params("semis", tmp)["threshold"] == 0.22   # sector wins
    assert db.get_model_params("energy", tmp)["threshold"] == 0.05  # others unaffected


def test_cooldown_blocks_a_second_adoption_too_soon(monkeypatch):
    tmp = _seeded_db(monkeypatch)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_RECORDS", 5)
    monkeypatch.setattr(config, "LEARNING_ADOPT_COOLDOWN_DAYS", 5)
    db.record_learning({
        "run_date": "2026-08-08", "sector": "semis", "params_before": "{}",
        "params_after": "{}", "score_before": 0.0, "score_after": 1.0,
        "hit_before": 0.5, "hit_after": 0.6, "n_holdout": 45, "adopted": 1,
        "reason": "x", "created_at": "t"}, db_path=tmp)
    inc = {"us_spillover": 0.45, "sentiment": 0.35, "macro": 0.2, "threshold": 0.15}
    recs = [_rec(f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", us=1.0, move=1.0)
            for i in range(120)]
    row = at.tune_sector("semis", recs, inc, "2026-08-10", "t", db_path=tmp)  # 2d later
    assert row["adopted"] == 0 and "cooldown" in row["reason"]


def test_cooldown_allows_adoption_once_elapsed(monkeypatch):
    tmp = _seeded_db(monkeypatch)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_RECORDS", 5)
    monkeypatch.setattr(config, "LEARNING_ADOPT_COOLDOWN_DAYS", 5)
    db.record_learning({
        "run_date": "2026-08-01", "sector": "semis", "params_before": "{}",
        "params_after": "{}", "score_before": 0.0, "score_after": 1.0,
        "hit_before": 0.5, "hit_after": 0.6, "n_holdout": 45, "adopted": 1,
        "reason": "x", "created_at": "t"}, db_path=tmp)
    inc = {"us_spillover": 0.45, "sentiment": 0.35, "macro": 0.2, "threshold": 0.15}
    recs = [_rec("2026-08-20")]
    row = at.tune_sector("semis", recs, inc, "2026-08-20", "t", db_path=tmp)  # 19d later
    assert "cooldown" not in row["reason"]        # gate passed (fails later on data)


# ── sparse-signal guard ───────────────────────────────────────────────────
def test_signal_coverage_counts_non_zero_share():
    recs = [{"us_spillover": 1.0, "sentiment": 0.0}] * 9 + \
           [{"us_spillover": 1.0, "sentiment": 0.5}]
    cov = wf.signal_coverage(recs)
    assert cov["us_spillover"] == 1.0
    assert cov["sentiment"] == 0.1


def test_sparse_signal_is_not_weightable():
    """Regression: news ingestion went live on one day of a 369-day history, so
    `sentiment` was non-zero on 0.3% of records. 'Varies at all' admitted it as a
    real parameter, but a weight fitted to a single day acts as a threshold
    rescale on every other one — the same backdoor `macro` opened."""
    recs = [{"us_spillover": 1.0, "sentiment": 0.0}] * 99 + \
           [{"us_spillover": 1.0, "sentiment": 0.9}]
    live = wf.live_signals(recs)
    assert "us_spillover" in live
    assert "sentiment" not in live, "1% coverage must not earn a weight"


def test_signal_becomes_weightable_once_it_has_history():
    """The gate must open on its own — no code change the day news matures."""
    recs = [{"us_spillover": 1.0, "sentiment": 0.0}] * 80 + \
           [{"us_spillover": 1.0, "sentiment": 0.7}] * 20
    assert "sentiment" in wf.live_signals(recs)


def test_pin_dead_signals_zeroes_without_renormalizing():
    """Renormalizing would scale the surviving weights up against a fixed
    threshold — an unvalidated change to the abstain rate, which is the backdoor
    threshold dial in reverse. A dead signal contributes 0 either way, so pinning
    must leave current behaviour bit-for-bit identical."""
    pinned = wf.pin_dead_signals(
        {"us_spillover": 0.5, "sentiment": 0.5, "threshold": 0.2},
        live={"us_spillover"})
    assert pinned["sentiment"] == 0.0
    assert pinned["us_spillover"] == 0.5        # untouched, NOT rescaled to 1.0


def test_unearned_weight_cannot_steer_predictions_when_a_feed_wakes_up():
    """Regression: DEFAULT_WEIGHTS hand-sets sentiment=0.35, and blend() carries
    it forward through every adoption. While the feed was dead the weight was
    inert; the day news started producing values it would have begun moving live
    predictions without ever having been fitted."""
    incumbent = {"us_spillover": 0.65, "sentiment": 0.35, "threshold": 0.15}
    # One record in fifty carries sentiment — below the 5% coverage floor.
    recs = [{"us_spillover": 1.0, "sentiment": 0.0}] * 49 + \
           [{"us_spillover": 1.0, "sentiment": 0.9}]
    pinned = wf.pin_dead_signals(incumbent, wf.live_signals(recs))
    assert pinned["sentiment"] == 0.0
    # ...and it switches itself on once the feed has real history.
    mature = [{"us_spillover": 1.0, "sentiment": 0.4}] * 10
    assert "sentiment" in wf.live_signals(mature)


def test_pin_persists_even_when_the_incumbent_is_kept(monkeypatch):
    """Adoption is gated by cooldown and a holdout gain, so a sector can sit for
    weeks on an incumbent carrying an unearned weight. The pin must not wait for
    an adoption that may never come."""
    tmp = _seeded_db(monkeypatch)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_RECORDS", 5)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_BETS", 1)
    monkeypatch.setattr(config, "LEARNING_MIN_IMPROVEMENT", 99.0)   # never adopt
    monkeypatch.setattr(config, "LEARNING_HOLDOUT_DAYS", 20)
    inc = {"us_spillover": 0.45, "sentiment": 0.35, "macro": 0.2, "threshold": 0.2}
    # sentiment/macro are identically zero across the history -> not weightable
    recs = [_rec(f"2026-{1 + i // 30:02d}-{1 + i % 30:02d}",
                 us=(1.0 if i % 3 else -1.0), sent=0.0,
                 move=(1.5 if i % 3 else -1.5)) for i in range(150)]
    row = at.tune_sector("semis", recs, inc, "2026-08-10", "t", db_path=tmp)
    assert row["adopted"] == 0
    stored = db.get_model_params("semis", tmp)
    assert stored["sentiment"] == 0.0 and stored["macro"] == 0.0
    assert stored["us_spillover"] == 0.45      # live weight untouched


def test_pin_applies_even_while_a_sector_is_in_cooldown(monkeypatch):
    """The guards all return early. A sector in cooldown must still get pinned,
    or it keeps an unearned weight for exactly as long as the cooldown lasts."""
    tmp = _seeded_db(monkeypatch)
    monkeypatch.setattr(config, "LEARNING_MIN_HOLDOUT_RECORDS", 5)
    monkeypatch.setattr(config, "LEARNING_HOLDOUT_DAYS", 20)
    monkeypatch.setattr(config, "LEARNING_ADOPT_COOLDOWN_DAYS", 999)
    db.record_learning({
        "run_date": "2026-08-09", "sector": "semis",
        "params_before": "{}", "params_after": "{}",
        "score_before": None, "score_after": None, "hit_before": None,
        "hit_after": None, "n_holdout": 45, "adopted": 1,
        "reason": "prior adoption", "created_at": "t"}, tmp)

    inc = {"us_spillover": 0.45, "sentiment": 0.35, "macro": 0.2, "threshold": 0.2}
    recs = [_rec(f"2026-{1 + i // 30:02d}-{1 + i % 30:02d}",
                 us=(1.0 if i % 3 else -1.0), sent=0.0,
                 move=(1.5 if i % 3 else -1.5)) for i in range(150)]
    row = at.tune_sector("semis", recs, inc, "2026-08-10", "t", db_path=tmp)
    assert row["adopted"] == 0 and "cooldown" in row["reason"]
    assert db.get_model_params("semis", tmp)["sentiment"] == 0.0
