"""Score analyst variants head-to-head on the sessions they both called.

Phase 01 of the TradingAgents graft ships an adversarial bull/bear analyst
alongside the incumbent single pass. This is the part that decides whether it
stays.

Two rules make the comparison mean something:

  * **Paired sessions only.** A variant is scored only on (date, sector) pairs
    where BOTH variants produced a directional call. If the debate abstains on
    the hard days and the single pass does not, comparing their raw hit rates
    rewards the abstention rather than the reasoning.
  * **The same objective the learner already uses.** ``edge_t = (hit − 0.5)·√n``
    rewards being right *and* betting often enough to matter, so a variant
    cannot win by making three confident calls a month.

The kill gate is deliberately the same bar the parameter learner demands of any
change: beat the incumbent by ``LEARNING_MIN_IMPROVEMENT`` on edge_t, with a
minimum number of paired calls behind it. Below that the debate is a cost with a
story attached, and the honest action is to delete it.

**Not investment advice.**
"""
from __future__ import annotations

from math import sqrt

from .. import config, db
from .debate import VARIANT_DEBATE, VARIANT_SINGLE

# Paired calls required before the comparison is allowed to conclude anything.
# Sized from the same arithmetic used elsewhere in this project: distinguishing a
# few points of hit rate needs hundreds of observations, so this floor is a
# minimum for *reporting*, not a claim of significance.
MIN_PAIRED_CALLS = 60

_DIRECTIONAL = ("bullish", "bearish")


def load_variant_calls(db_path=None) -> dict:
    """{(trade_date, sector): {variant: direction}} for directional calls.

    Self-heals the schema first: the `variant` column arrives by migration, and a
    restored older state DB does not have it until init_db runs. Reading straight
    through would crash on exactly the DBs this report is most useful for.
    """
    db.init_db(db_path)
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT trade_date, sector, direction, variant FROM llm_calls"
        ).fetchall()
    finally:
        conn.close()
    out: dict = {}
    for r in rows:
        if r["direction"] not in _DIRECTIONAL:
            continue
        out.setdefault((r["trade_date"], r["sector"]), {})[
            r["variant"] or VARIANT_SINGLE] = r["direction"]
    return out


def actual_directions(db_path=None) -> dict:
    """{(trade_date, sector): actual direction} from the replayed record set."""
    from ..backtest import collect_records

    return {(r["date"], r["sector"]): r["actual_dir"]
            for r in collect_records(db_path=db_path)
            if r.get("actual_dir") in _DIRECTIONAL}


def compare(calls: dict, actuals: dict,
            variants=(VARIANT_SINGLE, VARIANT_DEBATE)) -> dict:
    """Head-to-head on paired, scored sessions. Pure.

    ``calls``: {(date, sector): {variant: direction}}
    ``actuals``: {(date, sector): direction}
    """
    a, b = variants
    paired = [(k, v) for k, v in calls.items()
              if a in v and b in v and k in actuals]

    stats = {}
    for variant in variants:
        hits = sum(1 for k, v in paired if v[variant] == actuals[k])
        n = len(paired)
        hit = hits / n if n else None
        stats[variant] = {
            "n": n, "hits": hits,
            "hit_rate": None if hit is None else round(hit, 4),
            "edge_t": None if hit is None else round((hit - 0.5) * sqrt(n), 3),
        }

    # Where they disagreed is the only place the debate can have added anything.
    disagreed = [(k, v) for k, v in paired if v[a] != v[b]]
    b_won = sum(1 for k, v in disagreed if v[b] == actuals[k])

    gain = None
    if stats[a]["edge_t"] is not None and stats[b]["edge_t"] is not None:
        gain = round(stats[b]["edge_t"] - stats[a]["edge_t"], 3)

    return {
        "paired": len(paired),
        "incumbent": a, "challenger": b,
        "stats": stats,
        "disagreements": len(disagreed),
        "challenger_won_disagreements": b_won,
        "edge_gain": gain,
        "min_paired": MIN_PAIRED_CALLS,
        "bar": config.LEARNING_MIN_IMPROVEMENT,
        "verdict": _verdict(len(paired), gain),
    }


def _verdict(paired: int, gain: float | None) -> str:
    if paired < MIN_PAIRED_CALLS or gain is None:
        return "insufficient"
    return "adopt" if gain >= config.LEARNING_MIN_IMPROVEMENT else "delete"


def format_report(res: dict) -> str:
    a, b = res["incumbent"], res["challenger"]
    L = ["Analyst variant comparison — debate vs single pass", "",
         f"  paired calls (both variants called the same session): {res['paired']}",
         ""]
    L.append(f"  {'variant':12}{'n':>7}{'hit':>9}{'edge_t':>10}")
    L.append(f"  {'-' * 38}")
    for v in (a, b):
        s = res["stats"][v]
        hit = "n/a" if s["hit_rate"] is None else f"{s['hit_rate']:.1%}"
        et = "n/a" if s["edge_t"] is None else f"{s['edge_t']:+.3f}"
        L.append(f"  {v:12}{s['n']:>7}{hit:>9}{et:>10}")
    L += ["",
          f"  disagreed on {res['disagreements']} call(s); "
          f"{b} was right on {res['challenger_won_disagreements']} of them",
          ""]

    verdict = res["verdict"]
    if verdict == "insufficient":
        need = res["min_paired"] - res["paired"]
        L.append(f"  VERDICT: not yet decidable — {max(need, 0)} more paired call(s) "
                 f"needed (floor {res['min_paired']}).")
    elif verdict == "adopt":
        L.append(f"  VERDICT: adopt — edge_t gain {res['edge_gain']:+.3f} "
                 f"clears the bar of {res['bar']}.")
    else:
        L.append(f"  VERDICT: delete — edge_t gain {res['edge_gain']:+.3f} "
                 f"is below the bar of {res['bar']}. The debate is a cost with a "
                 "story attached.")
    L += ["",
          "  Scored only where both variants called the same session, so a variant",
          "  cannot win by abstaining on the hard days. Same objective the parameter",
          "  learner uses, so 'better' means the same thing here as it does there.",
          "",
          "  Not investment advice."]
    return "\n".join(L)


def run(db_path=None) -> dict:
    return compare(load_variant_calls(db_path), actual_directions(db_path))


def main(argv: list[str]) -> int:
    print(format_report(run()))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
