"""Forward paper record for the mean-reversion rule.

The rule — enter at the open when the previous session's body closed at or below
-0.96% AND today gapped down at or below -0.40%, exit at the close — survived
every retrospective test available: Benjamini-Hochberg correction across 35
pre-registered buckets, split-half sign stability, a chronological holdout
(n=332, 59.6% hit, +0.312% net, t=+2.40), instruments it was never derived from,
and a 6x6 parameter surface on which all 36 cells are positive.

None of that is forward evidence. Every one of those tests reuses the same ten
years, and no amount of re-slicing fixes that — only sessions that did not exist
when the rule was found can. This module accumulates those.

Two design decisions carry the honesty:

  * **The entry is written before the outcome is known.** A row appears the
    moment a setup fires, with ``outcome='open'``, and is settled later. A ledger
    that only records the entries it turns out to like is a backtest wearing a
    ledger's clothes.
  * **It never places or recommends an order.** The ledger records what the rule
    would have done. Position sizing, execution and the decision to risk money
    are not modelled here and are not this system's to make.

Costs are charged at the same 15bps round trip the trader simulator assumes, so
the forward number is directly comparable to the retrospective one.

**Not investment advice.** This is a research ledger.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import config, db
from .analysis.segments import decompose

STRATEGY = "mean_reversion_v1"

# The rule, exactly as validated. These are deliberately module constants rather
# than tunable config: the moment they become adjustable, the forward record
# stops testing the rule that was validated and starts testing whatever they were
# last set to.
PRIOR_BODY_MAX = -0.96
GAP_MAX = -0.40

# Same round-trip friction the trader simulator charges, so forward and
# retrospective numbers mean the same thing.
COST_PCT = 0.15

# A move beyond the instrument's daily limit is a corporate action, not a return.
# Sharing the ingestion guard rather than re-deriving it keeps one definition.
from .ingestion.china_market import daily_limit_for  # noqa: E402


def qualifies(prior_body: float | None, gap: float | None) -> bool:
    """Does this session's open meet the entry conditions? Pure.

    Both inputs are known before the open — the prior session has closed and the
    gap is complete by definition once trading starts — so acting on them
    involves no lookahead.
    """
    if prior_body is None or gap is None:
        return False
    return prior_body <= PRIOR_BODY_MAX and gap <= GAP_MAX


def evaluate(body_pct: float | None, cost_pct: float = COST_PCT) -> tuple[float | None, str]:
    """(net return, outcome) for a completed session. Pure."""
    if body_pct is None:
        return None, "open"
    net = round(body_pct - cost_pct, 4)
    return net, "win" if net > 0 else "loss"


def _sector_bars(sector: str, symbol: str, db_path=None) -> list[dict]:
    return sorted(db.close_series("china_close", symbol=symbol, limit=10 ** 6,
                                  db_path=db_path),
                  key=lambda r: r["trade_date"])


def latest_session(db_path=None) -> str | None:
    """The most recent China session that has bars."""
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(trade_date) d FROM china_close").fetchone()
    finally:
        conn.close()
    return row["d"] if row else None


def scan(trade_date: str | None = None, db_path=None) -> list[dict]:
    """Rows for every sector whose setup fired on ``trade_date``.

    ``trade_date=None`` scans EVERY session, which is a research view — see
    ``record``, which deliberately does not use it that way.
    """
    out = []
    for sector, symbol in config.CHINA_SECTOR_ETFS.items():
        bars = _sector_bars(sector, symbol, db_path)
        if len(bars) < 2:
            continue
        limit = daily_limit_for(symbol)
        for i in range(1, len(bars)):
            row, prev = bars[i], bars[i - 1]
            if trade_date and row["trade_date"] != trade_date:
                continue
            seg = decompose(prev["close"], dict(row))
            prev_seg = decompose(bars[i - 2]["close"] if i >= 2 else None, dict(prev))
            # Skip artifacts outright: a share conversion is not a setup.
            if seg.gap is None or abs(seg.gap) > limit:
                continue
            if prev_seg.body is not None and abs(prev_seg.body) > limit:
                continue
            if not qualifies(prev_seg.body, seg.gap):
                continue
            net, outcome = evaluate(seg.body)
            # The same trade under T+1: sold at the NEXT session's open, which
            # does not exist yet on the day the setup fires. It stays 'open'
            # until a later run can see it.
            nxt = bars[i + 1] if i + 1 < len(bars) else None
            t1_exit = nxt.get("open") if nxt else None
            entry = row.get("open")
            t1_pct = None
            if t1_exit is not None and entry:
                t1_pct = round((float(t1_exit) / float(entry) - 1.0) * 100.0, 4)
                if abs(t1_pct) > limit:      # a conversion is not a return
                    t1_pct, t1_exit = None, None
            net_t1, outcome_t1 = evaluate(t1_pct)
            out.append({
                "trade_date": row["trade_date"], "sector": sector,
                "strategy": STRATEGY,
                "prior_body": prev_seg.body, "gap": seg.gap,
                "entry_price": entry, "exit_price": row.get("close"),
                "body_pct": seg.body, "net_pct": net, "outcome": outcome,
                "exit_price_t1": t1_exit, "net_pct_t1": net_t1,
                "outcome_t1": outcome_t1,
            })
    return out


def record(trade_date: str | None = None, db_path=None) -> int:
    """Job step: write the setups that fired on ONE session.

    Defaults to the latest session, never to all of history. That restriction is
    the whole point: running this over the full ten years wrote 1,127 rows and
    then displayed them under "forward (live)", which is the retrospective result
    relabelled — exactly the self-deception this ledger exists to prevent. The
    forward record can only grow one session at a time, because that is the only
    way it stays forward.

    Idempotent on (trade_date, sector, strategy). Never raises — a research
    ledger must not be able to break the daily pipeline.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        db.init_db(db_path)
        target = trade_date or latest_session(db_path)
        if target is None:
            print("paper.record: no China sessions on record — nothing to scan")
            return 0
        rows = scan(target, db_path)
        conn = db.connect(db_path)
        try:
            for r in rows:
                conn.execute(
                    """INSERT INTO paper_trades
                           (trade_date, sector, strategy, prior_body, gap,
                            entry_price, exit_price, body_pct, net_pct, outcome,
                            exit_price_t1, net_pct_t1, outcome_t1,
                            recorded_at, settled_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(trade_date, sector, strategy) DO UPDATE SET
                            exit_price=excluded.exit_price,
                            body_pct=excluded.body_pct,
                            net_pct=excluded.net_pct,
                            outcome=excluded.outcome,
                            exit_price_t1=excluded.exit_price_t1,
                            net_pct_t1=excluded.net_pct_t1,
                            outcome_t1=excluded.outcome_t1,
                            settled_at=excluded.settled_at""",
                    (r["trade_date"], r["sector"], r["strategy"], r["prior_body"],
                     r["gap"], r["entry_price"], r["exit_price"], r["body_pct"],
                     r["net_pct"], r["outcome"], r["exit_price_t1"],
                     r["net_pct_t1"], r["outcome_t1"], now,
                     None if r["outcome"] == "open" else now))
            conn.commit()
        finally:
            conn.close()
        print(f"paper.record: {len(rows)} setup(s) for {target}")
        return len(rows)
    except Exception as e:  # noqa: BLE001 — research must never break the pipeline
        print(f"paper.record FAILED: {e!r}")
        return 0


def settle_pending(db_path=None, lookback: int = 10) -> int:
    """Fill in T+1 legs that could not settle on the day they were recorded.

    A T+1 exit happens at the *next* session's open, which does not exist when
    the setup fires. This revisits recently recorded rows and settles the ones
    whose next open has since arrived.

    It **only ever updates rows that already exist**, and that restriction is
    load-bearing. The forward ledger's whole value is that entries were written
    before their outcomes were known; a settle pass that could also insert would
    be a backfill in disguise, which is the exact failure this module was
    rewritten once already to prevent. Rows are never created here.

    Never raises — a settling pass must not break the daily pipeline.
    """
    try:
        db.init_db(db_path)
        conn = db.connect(db_path)
        try:
            pending = [r["trade_date"] for r in conn.execute(
                """SELECT DISTINCT trade_date FROM paper_trades
                   WHERE strategy = ? AND (outcome_t1 IS NULL OR outcome_t1 = 'open')
                   ORDER BY trade_date DESC LIMIT ?""", (STRATEGY, lookback))]
        finally:
            conn.close()
        if not pending:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        settled = 0
        conn = db.connect(db_path)
        try:
            for d in pending:
                for r in scan(d, db_path):
                    if r["outcome_t1"] == "open":
                        continue
                    cur = conn.execute(
                        """UPDATE paper_trades
                              SET exit_price_t1 = ?, net_pct_t1 = ?,
                                  outcome_t1 = ?, settled_at = COALESCE(settled_at, ?)
                            WHERE trade_date = ? AND sector = ? AND strategy = ?""",
                        (r["exit_price_t1"], r["net_pct_t1"], r["outcome_t1"],
                         now, r["trade_date"], r["sector"], r["strategy"]))
                    settled += cur.rowcount
            conn.commit()
        finally:
            conn.close()
        if settled:
            print(f"paper.settle_pending: settled {settled} T+1 leg(s)")
        return settled
    except Exception as e:  # noqa: BLE001 — research must never break the pipeline
        print(f"paper.settle_pending FAILED: {e!r}")
        return 0


def summary(db_path=None, since: str | None = None,
            leg: str = "t0") -> dict:
    """Aggregate the settled forward record for one settlement leg.

    ``leg='t0'`` is the same-session exit the rule was validated on;
    ``leg='t1'`` is the same trade sold at the next open, which is what the rule
    becomes if these ETFs settle T+1.
    """
    from math import sqrt
    from statistics import pstdev

    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        col = "net_pct_t1" if leg == "t1" else "net_pct"
        oc = "outcome_t1" if leg == "t1" else "outcome"
        q = (f"SELECT trade_date, sector, {col} AS net_pct, {oc} AS outcome "
             f"FROM paper_trades "
             f"WHERE strategy = ? AND {oc} IS NOT NULL AND {oc} != 'open'")
        args = [STRATEGY]
        if since:
            q += " AND trade_date >= ?"
            args.append(since)
        rows = conn.execute(q + " ORDER BY trade_date", args).fetchall()
    finally:
        conn.close()

    nets = [r["net_pct"] for r in rows if r["net_pct"] is not None]
    if not nets:
        return {"n": 0, "since": since, "leg": leg}
    mean = sum(nets) / len(nets)
    sd = pstdev(nets)
    wins = sum(1 for n in nets if n > 0)
    return {
        "n": len(nets), "leg": leg,
        "since": rows[0]["trade_date"],
        "through": rows[-1]["trade_date"],
        "hit_rate": round(wins / len(nets), 4),
        "mean_net_pct": round(mean, 4),
        "t": round(mean / (sd / sqrt(len(nets))), 3) if sd > 0 else None,
        "best": round(max(nets), 4),
        "worst": round(min(nets), 4),
    }


# The retrospective holdout result this forward record exists to confirm or
# refute. Stated here so the report always shows what is being tested against,
# rather than letting the forward number be read on its own.
HOLDOUT_REFERENCE = {"n": 332, "hit_rate": 0.596, "mean_net_pct": 0.312, "t": 2.40}

# The same rule on the same holdout, sold at the next open instead — what it
# becomes if these ETFs settle T+1 and the same-session exit is not placeable.
# Measured in oracle.research.execution.
HOLDOUT_REFERENCE_T1 = {"n": 330, "hit_rate": 0.539, "mean_net_pct": 0.304, "t": 2.22}


def _leg_rows(label: str, ref: dict, s: dict) -> list[str]:
    """Retrospective and forward rows for one settlement leg."""
    L = [f"  {label}",
         f"  {'record':22}{'n':>7}{'hit':>9}{'net':>11}{'t':>8}",
         f"  {'-' * 57}",
         f"  {'retrospective holdout':22}{ref['n']:>7}{ref['hit_rate']:>8.1%}"
         f"{ref['mean_net_pct']:>+10.3f}%{ref['t']:>+8.2f}"]
    if not s.get("n"):
        L.append(f"  {'forward (live)':22}{0:>7}{'—':>9}{'—':>11}{'—':>8}")
    else:
        t = "n/a" if s["t"] is None else f"{s['t']:+.2f}"
        L.append(f"  {'forward (live)':22}{s['n']:>7}{s['hit_rate']:>8.1%}"
                 f"{s['mean_net_pct']:>+10.3f}%{t:>8}")
        L.append(f"  window {s['since']} → {s['through']}   "
                 f"best {s['best']:+.2f}%  worst {s['worst']:+.2f}%")
        if s["n"] < 60:
            L.append(f"  Still thin — {60 - s['n']} more settled trade(s) before this "
                     "row carries weight.")
    return L


def format_report(s: dict, s_t1: dict | None = None) -> str:
    L = [f"Paper strategy — {STRATEGY}", "",
         f"  rule: enter at the open when prior body <= {PRIOR_BODY_MAX}% "
         f"AND gap <= {GAP_MAX}%",
         f"  costs: {COST_PCT}% round trip", ""]
    L += _leg_rows("T+0 — exit at the same session's close (as validated)",
                   HOLDOUT_REFERENCE, s)
    L += [""]
    L += _leg_rows("T+1 — sold at the next open (if same-session selling is barred)",
                   HOLDOUT_REFERENCE_T1, s_t1 or {"n": 0})
    L += ["",
          "  Both legs are recorded because the settlement question is unresolved.",
          "  Mainland equities cannot be sold on the session they were bought; if",
          "  that applies to these ETFs, the T+0 rule is not placeable and the T+1",
          "  row is the real one. Keeping only T+0 would mean discovering months",
          "  from now that the wait had recorded the wrong number.",
          "  The T+1 leg settles a session later than T+0, so it always lags."]
    if not s.get("n"):
        L += ["",
              "  No settled forward trades yet. The setup fires on about 5% of",
              "  sessions, so across ten sectors expect roughly one every other day.",
              "  Nothing here is evidence until these rows fill."]
    L += ["",
          "  Every retrospective test reuses the same ten years. This row is the",
          "  only evidence that could not have been fitted, which is why it is the",
          "  one worth waiting for.",
          "",
          "  Records what the rule would have done. No order is placed or advised.",
          "  Not investment advice."]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    record()
    settle_pending()
    print(format_report(summary(), summary(leg="t1")))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
