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
            out.append({
                "trade_date": row["trade_date"], "sector": sector,
                "strategy": STRATEGY,
                "prior_body": prev_seg.body, "gap": seg.gap,
                "entry_price": row.get("open"), "exit_price": row.get("close"),
                "body_pct": seg.body, "net_pct": net, "outcome": outcome,
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
                            recorded_at, settled_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(trade_date, sector, strategy) DO UPDATE SET
                            exit_price=excluded.exit_price,
                            body_pct=excluded.body_pct,
                            net_pct=excluded.net_pct,
                            outcome=excluded.outcome,
                            settled_at=excluded.settled_at""",
                    (r["trade_date"], r["sector"], r["strategy"], r["prior_body"],
                     r["gap"], r["entry_price"], r["exit_price"], r["body_pct"],
                     r["net_pct"], r["outcome"], now,
                     None if r["outcome"] == "open" else now))
            conn.commit()
        finally:
            conn.close()
        print(f"paper.record: {len(rows)} setup(s) for {target}")
        return len(rows)
    except Exception as e:  # noqa: BLE001 — research must never break the pipeline
        print(f"paper.record FAILED: {e!r}")
        return 0


def summary(db_path=None, since: str | None = None) -> dict:
    """Aggregate the settled forward record."""
    from math import sqrt
    from statistics import pstdev

    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        q = ("SELECT trade_date, sector, net_pct, outcome FROM paper_trades "
             "WHERE strategy = ? AND outcome != 'open'")
        args = [STRATEGY]
        if since:
            q += " AND trade_date >= ?"
            args.append(since)
        rows = conn.execute(q + " ORDER BY trade_date", args).fetchall()
    finally:
        conn.close()

    nets = [r["net_pct"] for r in rows if r["net_pct"] is not None]
    if not nets:
        return {"n": 0, "since": since}
    mean = sum(nets) / len(nets)
    sd = pstdev(nets)
    wins = sum(1 for n in nets if n > 0)
    return {
        "n": len(nets),
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


def format_report(s: dict) -> str:
    L = [f"Paper strategy — {STRATEGY}", "",
         f"  rule: enter at the open when prior body <= {PRIOR_BODY_MAX}% "
         f"AND gap <= {GAP_MAX}%; exit at the close",
         f"  costs: {COST_PCT}% round trip", ""]
    ref = HOLDOUT_REFERENCE
    L.append(f"  {'record':22}{'n':>7}{'hit':>9}{'net':>11}{'t':>8}")
    L.append(f"  {'-' * 57}")
    L.append(f"  {'retrospective holdout':22}{ref['n']:>7}{ref['hit_rate']:>8.1%}"
             f"{ref['mean_net_pct']:>+10.3f}%{ref['t']:>+8.2f}")
    if not s.get("n"):
        L.append(f"  {'forward (live)':22}{0:>7}{'—':>9}{'—':>11}{'—':>8}")
        L += ["",
              "  No settled forward trades yet. The setup fires on about 5% of",
              "  sessions, so across ten sectors expect roughly one every other day.",
              "  Nothing here is evidence until this row fills."]
    else:
        t = "n/a" if s["t"] is None else f"{s['t']:+.2f}"
        L.append(f"  {'forward (live)':22}{s['n']:>7}{s['hit_rate']:>8.1%}"
                 f"{s['mean_net_pct']:>+10.3f}%{t:>8}")
        L += ["", f"  forward window: {s['since']} → {s['through']}   "
                  f"best {s['best']:+.2f}%  worst {s['worst']:+.2f}%"]
        if s["n"] < 60:
            L.append(f"  Still thin — {60 - s['n']} more settled trade(s) before the "
                     "forward number carries weight.")
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
    print(format_report(summary()))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
