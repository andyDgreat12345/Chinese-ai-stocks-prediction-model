"""Tests for sector-edge ranking and order-sensitivity measurement."""
from oracle.simulator import ranking as rk
from oracle.simulator.trader import TraderRules


def _rec(date, sector, hit, direction="bullish"):
    return {"date": date, "sector": sector, "model_dir": direction,
            "actual_dir": direction if hit else "bearish"}


# ── no lookahead: the whole point ────────────────────────────────────────
def test_edge_uses_only_records_strictly_before_the_date():
    """Ranking sectors by their full-history hit rate and then replaying that
    history is circular — it uses the answer to pick the question."""
    recs = [_rec("2026-01-01", "a", True), _rec("2026-01-02", "a", True),
            _rec("2026-01-03", "a", False)]
    book = rk.EdgeBook(recs, prior_strength=0)
    assert book.edge("a", "2026-01-01") == 0.5      # nothing known yet
    assert book.edge("a", "2026-01-02") == 1.0      # one hit known
    assert book.edge("a", "2026-01-03") == 1.0      # two hits; the miss is future
    assert book.edge("a", "2026-01-04") == 2 / 3    # now the miss counts


def test_a_sector_with_no_history_reads_as_a_coin_flip():
    book = rk.EdgeBook([], prior_strength=0)
    assert book.edge("unknown", "2026-01-01") == 0.5
    assert book.sample_size("unknown", "2026-01-01") == 0


def test_abstentions_do_not_count_as_evidence():
    recs = [{"date": "2026-01-01", "sector": "a", "model_dir": "neutral",
             "actual_dir": "bullish"}]
    book = rk.EdgeBook(recs, prior_strength=0)
    assert book.sample_size("a", "2026-02-01") == 0


# ── shrinkage ────────────────────────────────────────────────────────────
def test_small_samples_are_pulled_toward_a_coin_flip():
    """4 hits in 6 is a 67% raw rate and essentially no evidence."""
    recs = [_rec(f"2026-01-{i:02}", "a", i <= 4) for i in range(1, 7)]
    book = rk.EdgeBook(recs, prior_strength=40)
    edge = book.edge("a", "2026-02-01")
    assert 0.50 < edge < 0.56, edge          # nudged, not believed
    raw = rk.EdgeBook(recs, prior_strength=0).edge("a", "2026-02-01")
    assert abs(raw - 4 / 6) < 1e-9


def test_a_long_record_eventually_outweighs_the_prior():
    recs = [_rec(f"2026-{1 + i // 28:02}-{1 + i % 28:02}", "a", i % 4 != 0)
            for i in range(400)]
    book = rk.EdgeBook(recs, prior_strength=40)
    assert book.edge("a", "2027-12-31") > 0.70      # ~75% real rate shows through


# ── ranking ──────────────────────────────────────────────────────────────
def test_ranking_puts_the_better_sector_first():
    recs = ([_rec(f"2026-01-{i:02}", "good", True) for i in range(1, 29)]
            + [_rec(f"2026-01-{i:02}", "bad", False) for i in range(1, 29)])
    book = rk.EdgeBook(recs, prior_strength=4)
    calls = [{"sector": "bad"}, {"sector": "good"}]
    assert [c["sector"] for c in rk.rank_calls(calls, book, "2026-03-01")] == ["good", "bad"]


def test_without_a_book_the_original_order_is_preserved():
    """Ranking must be opt-in, or its effect cannot be measured against the
    unranked baseline."""
    calls = [{"sector": "z"}, {"sector": "a"}]
    assert rk.rank_calls(calls, None, "2026-01-01") == calls


def test_ties_break_stably():
    book = rk.EdgeBook([], prior_strength=40)      # everything is 0.5
    calls = [{"sector": "z"}, {"sector": "a"}, {"sector": "m"}]
    assert [c["sector"] for c in rk.rank_calls(calls, book, "2026-01-01")] == ["a", "m", "z"]


def test_ranking_never_changes_how_many_candidates_there_are():
    recs = [_rec("2026-01-01", "a", True)]
    book = rk.EdgeBook(recs)
    calls = [{"sector": "a"}, {"sector": "b"}, {"sector": "c"}]
    assert len(rk.rank_calls(calls, book, "2026-06-01")) == 3


# ── the floor is a stronger claim than the ranking ───────────────────────
def test_floor_refuses_weak_candidates_but_only_when_asked():
    recs = [_rec(f"2026-01-{i:02}", "bad", False) for i in range(1, 29)]
    book = rk.EdgeBook(recs, prior_strength=2)
    calls = [{"sector": "bad"}, {"sector": "unknown"}]
    assert len(rk.edge_floor_filter(calls, book, "2026-03-01", 0.0)) == 2   # off
    kept = rk.edge_floor_filter(calls, book, "2026-03-01", 0.50)
    assert [c["sector"] for c in kept] == ["unknown"]


# ── order sensitivity ────────────────────────────────────────────────────
def test_order_sensitivity_flags_a_result_that_is_really_an_ordering(monkeypatch):
    """Caught a real problem: CHINA_SECTORS lists the higher-edge original
    sectors first, so the headline return sat above every random ordering."""
    from oracle.simulator import engine

    calls = {"d1": [{"sector": "a"}, {"sector": "b"}]}
    seq = iter([90.0] + [10.0] * 12)     # actual beats every shuffle

    monkeypatch.setattr(engine, "simulate",
                        lambda *a, **k: {"return_pct": next(seq)})
    s = rk.order_sensitivity(calls, {}, ["d1"], TraderRules(), 100.0, seeds=12)
    assert s["percentile"] == 1.0
    assert "ordering artifact" in rk.format_order_sensitivity(s)


def test_order_sensitivity_is_quiet_when_ordering_does_not_matter(monkeypatch):
    from oracle.simulator import engine

    seq = iter([50.0] + [40.0, 60.0] * 6)
    monkeypatch.setattr(engine, "simulate",
                        lambda *a, **k: {"return_pct": next(seq)})
    s = rk.order_sensitivity({"d1": [{"sector": "a"}]}, {}, ["d1"],
                             TraderRules(), 100.0, seeds=12)
    assert s["percentile"] < 0.9
    assert "not carrying the result" in rk.format_order_sensitivity(s)
