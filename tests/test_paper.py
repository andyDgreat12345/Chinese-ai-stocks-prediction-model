"""Tests for the forward paper ledger."""
import tempfile

from oracle import db, paper


def _bars(rows, symbol="510300", sector="broad", path=None):
    db.upsert_market_close("china_close", [
        {"trade_date": d, "symbol": symbol, "sector": sector, "open": o,
         "high": max(o, c), "low": min(o, c), "close": c, "pct_change": p,
         "fetched_at": "t"} for d, o, c, p in rows], db_path=path)


# ── the rule ─────────────────────────────────────────────────────────────
def test_entry_conditions_are_both_required():
    assert paper.qualifies(-1.5, -0.5) is True
    assert paper.qualifies(-0.5, -0.5) is False      # prior body too shallow
    assert paper.qualifies(-1.5, -0.1) is False      # gap too shallow
    assert paper.qualifies(None, -0.5) is False
    assert paper.qualifies(-1.5, None) is False


def test_boundaries_are_inclusive_as_validated():
    assert paper.qualifies(paper.PRIOR_BODY_MAX, paper.GAP_MAX) is True


def test_costs_are_charged_before_calling_it_a_win():
    net, outcome = paper.evaluate(0.10)      # +0.10% gross, 0.15% costs
    assert net == -0.05 and outcome == "loss"
    net, outcome = paper.evaluate(0.50)
    assert net == 0.35 and outcome == "win"
    assert paper.evaluate(None) == (None, "open")


# ── the forward-only guarantee ───────────────────────────────────────────
def test_record_writes_only_one_session_not_all_of_history():
    """Regression: record() over the full ten years wrote 1,127 rows and then
    displayed them under 'forward (live)' — the retrospective result relabelled,
    which is exactly the self-deception this ledger exists to prevent."""
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    # three consecutive qualifying setups
    _bars([("2026-01-01", 10.0, 10.0, 0.0),
           ("2026-01-02", 10.0, 9.8, -2.0),     # prior body -2%
           ("2026-01-03", 9.7, 9.9, 0.0),       # gap -1.02% -> qualifies
           ("2026-01-04", 9.8, 9.6, -2.0),
           ("2026-01-05", 9.5, 9.7, 0.0)], path=tmp)

    n = paper.record(db_path=tmp)               # no date -> latest only
    conn = db.connect(tmp)
    dates = [r["trade_date"] for r in conn.execute(
        "SELECT trade_date FROM paper_trades")]
    assert len(dates) == n
    assert set(dates) <= {"2026-01-05"}, "must not backfill earlier sessions"


def test_record_is_idempotent():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    _bars([("2026-01-01", 10.0, 10.0, 0.0),
           ("2026-01-02", 10.0, 9.8, -2.0),
           ("2026-01-03", 9.7, 9.9, 0.0)], path=tmp)
    a = paper.record("2026-01-03", db_path=tmp)
    paper.record("2026-01-03", db_path=tmp)
    conn = db.connect(tmp)
    assert conn.execute("SELECT COUNT(*) c FROM paper_trades").fetchone()["c"] == a


def test_corporate_actions_never_become_setups():
    """A share conversion is not a gap down."""
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    _bars([("2026-01-01", 10.0, 10.0, 0.0),
           ("2026-01-02", 10.0, 9.8, -2.0),
           ("2026-01-03", 2.5, 2.6, 0.0)], path=tmp)   # -74% "gap"
    assert paper.record("2026-01-03", db_path=tmp) == 0


def test_record_survives_an_empty_database():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    assert paper.record(db_path=tmp) == 0


# ── reporting ────────────────────────────────────────────────────────────
def test_empty_forward_record_says_so_rather_than_showing_a_number():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    text = paper.format_report(paper.summary(tmp))
    assert "No settled forward trades yet" in text
    assert "retrospective holdout" in text, "must always show what it is tested against"


def test_summary_aggregates_settled_trades():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    conn = db.connect(tmp)
    for i, net in enumerate([0.5, -0.2, 0.4, 0.3]):
        conn.execute(
            "INSERT INTO paper_trades (trade_date, sector, strategy, net_pct,"
            " outcome, recorded_at) VALUES (?,?,?,?,?,?)",
            (f"2026-01-{i + 1:02}", "broad", paper.STRATEGY, net,
             "win" if net > 0 else "loss", "t"))
    conn.commit()
    s = paper.summary(tmp)
    assert s["n"] == 4
    assert s["hit_rate"] == 0.75
    assert s["best"] == 0.5 and s["worst"] == -0.2
    assert "Still thin" in paper.format_report(s)


def test_open_trades_are_excluded_from_the_summary():
    tmp = tempfile.mktemp(suffix=".db")
    db.init_db(tmp)
    conn = db.connect(tmp)
    conn.execute("INSERT INTO paper_trades (trade_date, sector, strategy,"
                 " net_pct, outcome, recorded_at) VALUES ('d','s',?,NULL,'open','t')",
                 (paper.STRATEGY,))
    conn.commit()
    assert paper.summary(tmp)["n"] == 0


# ── T+1 settlement leg ────────────────────────────────────────────────────
# The ledger records both legs because the settlement question is unresolved.
# The forward record takes months to fill, so keeping only the same-session leg
# risks discovering at the end that the wait recorded an unexecutable number.

def _seed_setup(db_path, dates, prices):
    """Write bars that make the rule fire on dates[1]."""
    from oracle import db as _db
    _db.init_db(db_path)
    rows = [{"trade_date": d, "symbol": "510300", "sector": "broad",
             "close": c, "open": o, "high": max(o, c), "low": min(o, c),
             "pct_change": 0.0, "fetched_at": "x"}
            for d, (o, c) in zip(dates, prices)]
    _db.upsert_market_close("china_close", rows, db_path=db_path)


def test_t1_leg_settles_at_the_next_open_not_the_close(tmp_path):
    p = str(tmp_path / "t.db")
    # day0 body -2% (qualifies as prior), day1 gaps -1% then rises,
    # day2 opens lower than day1's open.
    _seed_setup(p, ["2026-01-05", "2026-01-06", "2026-01-07"],
                [(100.0, 98.0), (97.02, 99.0), (96.0, 96.0)])
    from oracle import paper
    rows = paper.scan("2026-01-06", db_path=p)
    assert len(rows) == 1
    r = rows[0]
    # T+0 exits at 99.0 from 97.02; T+1 exits at the next open, 96.0
    assert r["body_pct"] > 0 and r["outcome"] == "win"
    assert r["exit_price_t1"] == 96.0
    assert r["net_pct_t1"] < 0 and r["outcome_t1"] == "loss"


def test_t1_leg_stays_open_until_the_next_session_exists(tmp_path):
    p = str(tmp_path / "t.db")
    _seed_setup(p, ["2026-02-05", "2026-02-06"],
                [(100.0, 98.0), (97.02, 99.0)])
    from oracle import paper
    r = paper.scan("2026-02-06", db_path=p)[0]
    assert r["outcome"] == "win"          # same-session leg is settled
    assert r["outcome_t1"] == "open"      # next open does not exist yet
    assert r["net_pct_t1"] is None


def test_settle_pending_never_inserts_rows(tmp_path):
    """The load-bearing restriction: settling must not become a backfill.

    The forward ledger's value is that entries were written before outcomes
    were known. A settle pass that could insert would quietly recreate the
    backfill bug this module was already rewritten once to remove.
    """
    from oracle import db as _db, paper
    p = str(tmp_path / "t.db")
    _seed_setup(p, ["2026-03-04", "2026-03-05", "2026-03-06"],
                [(100.0, 98.0), (97.02, 99.0), (96.0, 96.0)])
    # No rows recorded at all — settling must find nothing to do and add nothing.
    assert paper.settle_pending(db_path=p) == 0
    conn = _db.connect(p)
    try:
        n = conn.execute("SELECT COUNT(*) c FROM paper_trades").fetchone()["c"]
    finally:
        conn.close()
    assert n == 0


def test_settle_pending_closes_a_leg_recorded_earlier(tmp_path):
    from oracle import db as _db, paper
    p = str(tmp_path / "t.db")
    # Record on a day when the next open does not exist yet.
    _seed_setup(p, ["2026-04-06", "2026-04-07"],
                [(100.0, 98.0), (97.02, 99.0)])
    assert paper.record("2026-04-07", db_path=p) == 1
    assert paper.summary(db_path=p, leg="t1")["n"] == 0
    # The next session arrives; the leg settles without a new row appearing.
    _seed_setup(p, ["2026-04-08"], [(96.0, 96.0)])
    assert paper.settle_pending(db_path=p) == 1
    conn = _db.connect(p)
    try:
        n = conn.execute("SELECT COUNT(*) c FROM paper_trades").fetchone()["c"]
    finally:
        conn.close()
    assert n == 1                                   # updated, not inserted
    assert paper.summary(db_path=p, leg="t1")["n"] == 1


def test_summary_legs_are_independent(tmp_path):
    from oracle import paper
    p = str(tmp_path / "t.db")
    _seed_setup(p, ["2026-05-06", "2026-05-07", "2026-05-08"],
                [(100.0, 98.0), (97.02, 99.0), (96.0, 96.0)])
    paper.record("2026-05-07", db_path=p)
    t0 = paper.summary(db_path=p)
    t1 = paper.summary(db_path=p, leg="t1")
    assert t0["leg"] == "t0" and t1["leg"] == "t1"
    assert t0["mean_net_pct"] > 0 > t1["mean_net_pct"]


def test_report_shows_both_legs_and_the_settlement_caveat():
    from oracle import paper
    text = paper.format_report({"n": 0}, {"n": 0})
    assert "T+0" in text and "T+1" in text
    assert "settlement question is unresolved" in text
    assert "Not investment advice" in text
