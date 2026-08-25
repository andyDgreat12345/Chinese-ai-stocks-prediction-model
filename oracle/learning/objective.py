"""Is the learner optimizing something a trade can actually earn?

The learning loop scores every prediction against ``pct_change``, which is
close-to-close. A close-to-close return is two different things added together:
the overnight **gap**, which a position entered at the open cannot capture, and
the **body**, which is what such a position actually earns.

That distinction has been measured before at the level of the whole model — the
gap is called 71.8% right and the body 49.4%. What this module adds is the
consequence for the *learner*: every weight ever adopted was selected for its
ability to predict close-to-close, so signals have been rewarded for predicting
the half of the day no trade can reach.

The result is stark. The dominant signal, ``us_spillover`` — the thesis this
whole system was built on — carries t=+5.8 against the scored objective and
t=+0.1 against the tradeable one. It is a real signal about the gap and close to
nothing about the body.

**This is a diagnostic, not a proposal to reweight.** Two honest reasons to stop
short of switching the objective:

  * Nothing here predicts the body. Every signal sits within a hair of 50% on
    it, so realigning the objective would most likely reveal that the tradeable
    segment is unpredictable rather than unlock a better model. "The objective
    is wrong" and "there is a better objective available" are different claims,
    and only the first is supported.
  * The gap edge is genuinely informative even though it is not capturable by
    entering at the open. Throwing it away would discard the one thing the model
    demonstrably does well.

What it does change is how the headline accuracy should be read. A scored 52%
is not 52% of anything a position could have earned, and the learning report now
says so rather than leaving the two to be conflated.

**Not investment advice.**
"""
from __future__ import annotations

from math import sqrt

# What the learner scores against today, and what a position entered at the
# open would actually have earned. The whole module is the distance between
# these two columns.
SCORED_SEGMENT = "close_to_close"
TRADEABLE_SEGMENT = "body"

# Signals the composite is built from, in the order the learner carries them.
SIGNALS = ("us_spillover", "sentiment", "macro", "rsi_signal",
           "momentum_signal", "trend_signal")

# Below this a per-signal edge is noise, not a disagreement worth reporting.
MIN_N = 50


def _edge(hits: list[bool], min_n: int = MIN_N) -> dict:
    """Hit rate and edge_t for one bundle of directional calls. Pure."""
    n = len(hits)
    if n < min_n:
        return {"n": n, "hit": None, "t": None}
    h = sum(hits) / n
    return {"n": n, "hit": round(h, 4), "t": round((h - 0.5) * sqrt(n), 2)}


def signal_edges(records: list[dict], segments: dict,
                 signals=SIGNALS, min_n: int = MIN_N) -> list[dict]:
    """Each signal's directional edge against both segments. Pure.

    A signal's "call" is the sign of its own value, which is how the composite
    uses it. Zero values are abstentions and are excluded rather than counted as
    a coin flip — a signal that is silent 95% of the time should be measured on
    the 5% where it speaks.
    """
    from ..analysis.segments import direction

    out = []
    for name in signals:
        scored, tradeable = [], []
        for r in records:
            v = r.get(name)
            if v is None or abs(v) < 1e-9:
                continue
            seg = (segments.get(r["sector"]) or {}).get(r["date"])
            if seg is None:
                continue
            call = "bullish" if v > 0 else "bearish"
            a_scored = direction(getattr(seg, SCORED_SEGMENT, None))
            a_trade = direction(getattr(seg, TRADEABLE_SEGMENT, None))
            if a_scored:
                scored.append(a_scored == call)
            if a_trade:
                tradeable.append(a_trade == call)
        s, t = _edge(scored, min_n), _edge(tradeable, min_n)
        if s["hit"] is None:
            continue
        out.append({
            "signal": name, "scored": s, "tradeable": t,
            "divergence": (None if s["t"] is None or t["t"] is None
                           else round(s["t"] - t["t"], 2)),
        })
    return out


def _spearman(a: list[float], b: list[float]) -> float | None:
    """Rank correlation between two orderings. Pure.

    Written out rather than imported because scipy is not a dependency and this
    is six lines. Ties are averaged, which matters when several signals sit at
    the same near-zero edge.
    """
    n = len(a)
    if n < 3:
        return None

    def ranks(xs):
        order = sorted(range(n), key=lambda i: xs[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / n, sum(rb) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    da = sqrt(sum((x - ma) ** 2 for x in ra))
    db = sqrt(sum((y - mb) ** 2 for y in rb))
    return None if da == 0 or db == 0 else round(num / (da * db), 3)


def alignment(edges: list[dict]) -> dict:
    """Do the two objectives agree about which signals are worth weight? Pure."""
    usable = [e for e in edges
              if e["scored"]["t"] is not None and e["tradeable"]["t"] is not None]
    if len(usable) < 3:
        return {"status": "too few signals"}
    rho = _spearman([e["scored"]["t"] for e in usable],
                    [e["tradeable"]["t"] for e in usable])
    best_scored = max(usable, key=lambda e: e["scored"]["t"])
    best_trade = max(usable, key=lambda e: e["tradeable"]["t"])
    any_tradeable = [e for e in usable if e["tradeable"]["t"] >= 2.0]
    return {
        "status": "measured",
        "rank_correlation": rho,
        "top_by_scored": best_scored["signal"],
        "top_by_scored_tradeable_t": best_scored["tradeable"]["t"],
        "top_by_tradeable": best_trade["signal"],
        "same_winner": best_scored["signal"] == best_trade["signal"],
        "signals_with_tradeable_edge": [e["signal"] for e in any_tradeable],
        "nothing_predicts_the_tradeable_segment": not any_tradeable,
    }


def format_report(edges: list[dict], a: dict) -> str:
    L = ["Learning objective — is the scored target the tradeable one?", "",
         f"  the learner scores against {SCORED_SEGMENT}; a position entered at",
         f"  the open earns {TRADEABLE_SEGMENT}", "",
         f"  {'signal':18}{'n':>7}{'scored hit':>12}{'t':>8}"
         f"{'tradeable hit':>15}{'t':>8}",
         f"  {'-' * 68}"]
    for e in edges:
        s, t = e["scored"], e["tradeable"]
        th = "n/a" if t["hit"] is None else f"{t['hit']:.2%}"
        tt = "n/a" if t["t"] is None else f"{t['t']:+.2f}"
        L.append(f"  {e['signal']:18}{s['n']:>7}{s['hit']:>11.2%}{s['t']:>+8.2f}"
                 f"{th:>15}{tt:>8}")
    L.append("")
    if a.get("status") != "measured":
        L.append(f"  alignment: {a.get('status')}")
        return "\n".join(L)
    L += [f"  Rank agreement between the two objectives: rho={a['rank_correlation']}",
          f"  Best signal by the scored objective: {a['top_by_scored']} "
          f"(tradeable t={a['top_by_scored_tradeable_t']:+.2f})",
          f"  Best signal by the tradeable objective: {a['top_by_tradeable']}", ""]
    if not a["same_winner"]:
        L += ["  The two objectives do not even agree on which signal is best, so",
              "  fitting weights on the scored one is not a harmless approximation",
              "  of fitting them on what a trade earns.", ""]
    if a["nothing_predicts_the_tradeable_segment"]:
        L += ["  READ THIS BEFORE CONCLUDING THE FIX IS TO REWEIGHT.",
              "  No signal reaches t>=2 against the tradeable segment. Realigning the",
              "  objective would therefore most likely reveal that the body is",
              "  unpredictable rather than produce a better model. 'The objective is",
              "  wrong' and 'a better objective is available' are different claims,",
              "  and only the first is supported here.", ""]
    L += ["  The consequence is for how the headline is read, not for the weights:",
          "  a scored accuracy is not the accuracy of anything a position could",
          "  have earned, and the two must not be quoted interchangeably.",
          "",
          "  The gap edge is real and worth keeping even though entering at the",
          "  open cannot capture it — it is the one thing this model does well.",
          "",
          "  Not investment advice."]
    return "\n".join(L)


def measure(db_path=None, start: str | None = None) -> tuple[list[dict], dict]:
    """Build both edge columns from stored history. Network-free."""
    from .. import config, db
    from ..analysis.segments import build_segments
    from ..backtest import collect_records

    # See build_paths: the research phase can run before any ingestion has.
    db.init_db(db_path)
    start = start or config.LEARNING_TRAIN_START or None
    recs = collect_records(start=start, db_path=db_path)
    segs = build_segments(db_path=db_path, start=start)
    edges = signal_edges(recs, segs)
    return edges, alignment(edges)


def main(argv: list[str]) -> int:
    edges, a = measure()
    print(format_report(edges, a))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
