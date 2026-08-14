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
