"""Rank contested position slots by each sector's measured, out-of-sample edge.

The simulator holds 2–3 positions but now has 10 candidates, and it filled slots
in ``CHINA_SECTORS`` order — so a slot could go to ``consumer`` (40.8% hit rate,
worse than a coin flip) while ``brokers`` (56.7%) was passed over for want of
room. Order in a config list is not a trading thesis.

**The whole difficulty is doing this without cheating.** Ranking sectors by their
hit rate over the full history and then replaying that history is circular: it
uses the answer to pick the question. So the edge for session *d* is computed
**only from records strictly before d** — the same walk-forward discipline the
signal weights and exit rules already use. Early in the history the ranking is
therefore ignorant, which is correct: a real trader on day 30 did not know which
sectors would work.

Small samples are handled by **shrinkage toward a coin flip** rather than by a
hard minimum. A sector with 6 observations and 4 hits has a 67% raw hit rate and
essentially no evidence; ``(hits + k/2) / (n + k)`` pulls it back to ~0.52, so
a newly added sector is neither promoted on noise nor punished for being new. The
prior strength ``k`` is the number of imaginary coin-flip observations mixed in.

**Not investment advice.** A sector ranking is a research artifact.
"""
from __future__ import annotations

from bisect import bisect_left

# Imaginary coin-flip observations blended into every sector's estimate. At k=40
# a sector needs a few months of real history before its own record outweighs
# the prior — deliberately slow, because the cost of chasing a hot sector early
# is real money and the cost of waiting is a slightly worse slot assignment.
PRIOR_STRENGTH = 40


class EdgeBook:
    """Per-sector hit history, queryable as of any date without lookahead.

    Built once from scored records and reused across simulations — the tuner runs
    hundreds of them, so recomputing per call would dominate its runtime.
    """

    def __init__(self, records: list[dict], prior_strength: int = PRIOR_STRENGTH):
        self.prior = prior_strength
        # sector -> (sorted dates, cumulative hits aligned to those dates)
        self._dates: dict[str, list[str]] = {}
        self._cum_hits: dict[str, list[int]] = {}

        by_sector: dict[str, list[tuple[str, int]]] = {}
        for r in records:
            if r.get("model_dir") not in ("bullish", "bearish"):
                continue          # abstentions carry no information about edge
            hit = 1 if r["model_dir"] == r.get("actual_dir") else 0
            by_sector.setdefault(r["sector"], []).append((r["date"], hit))

        for sector, rows in by_sector.items():
            rows.sort(key=lambda x: x[0])
            dates, cum, total = [], [], 0
            for d, hit in rows:
                total += hit
                dates.append(d)
                cum.append(total)
            self._dates[sector] = dates
            self._cum_hits[sector] = cum

    def edge(self, sector: str, before_date: str) -> float:
        """Shrunk hit rate from records STRICTLY BEFORE ``before_date``.

        Returns 0.5 for a sector with no prior record — ignorance reads as a coin
        flip, not as a reason to prefer or avoid it.
        """
        dates = self._dates.get(sector)
        if not dates:
            return 0.5
        i = bisect_left(dates, before_date)      # first index >= before_date
        if i == 0:
            return 0.5
        hits = self._cum_hits[sector][i - 1]
        n = i
        k = self.prior
        return (hits + k / 2.0) / (n + k)

    def sample_size(self, sector: str, before_date: str) -> int:
        dates = self._dates.get(sector)
        if not dates:
            return 0
        return bisect_left(dates, before_date)


def rank_calls(calls: list[dict], book: EdgeBook | None, date: str) -> list[dict]:
    """Order today's entry candidates best-first. Pure w.r.t. ``calls``.

    Without a book the original order is preserved, so ranking is opt-in and its
    effect is measurable against the unranked baseline.

    Ties break on sector name so a run is reproducible — an arbitrary but *stable*
    order beats one that depends on dict iteration.
    """
    if book is None:
        return list(calls)
    return sorted(calls, key=lambda c: (-book.edge(c["sector"], date), c["sector"]))


def edge_floor_filter(calls: list[dict], book: EdgeBook | None, date: str,
                      floor: float) -> list[dict]:
    """Drop candidates whose measured edge is below ``floor``.

    Separate from ranking on purpose: ranking only reorders and can never reduce
    the trade count, while a floor refuses trades outright. That is a stronger
    claim and is therefore off unless a caller asks for it.
    """
    if book is None or floor <= 0:
        return list(calls)
    return [c for c in calls if book.edge(c["sector"], date) >= floor]


# ── order sensitivity ─────────────────────────────────────────────────────
def order_sensitivity(calls_by_date: dict, bars: dict, dates: list[str],
                      rules, cash: float = 100_000.0, seeds: int = 12) -> dict:
    """How much of the result is just the order candidates happen to be listed in?

    This exists because it caught a real problem. With more candidates than
    slots, the config order silently decides who gets filled — and
    ``CHINA_SECTORS`` happens to list the original five sectors first, which are
    also the higher-edge ones. That made the headline return sit ABOVE every
    random ordering, flattered by an ordering chosen (by me, knowing the sector
    hit rates) rather than earned.

    Shuffling the daily candidate lists and re-running gives the honest
    distribution. If the reported figure sits at the top of it, the figure is
    about the ordering, not the model.
    """
    import random
    import statistics

    from . import engine

    actual = engine.simulate(calls_by_date, bars, dates, rules, cash)["return_pct"]
    rets = []
    for seed in range(seeds):
        rng = random.Random(seed)
        shuffled = {d: rng.sample(v, len(v)) for d, v in calls_by_date.items()}
        rets.append(engine.simulate(shuffled, bars, dates, rules, cash)["return_pct"])
    rets.sort()
    return {
        "actual_return_pct": round(actual, 4),
        "random_mean_pct": round(statistics.mean(rets), 4),
        "random_median_pct": round(statistics.median(rets), 4),
        "random_min_pct": round(min(rets), 4),
        "random_max_pct": round(max(rets), 4),
        "percentile": round(sum(1 for r in rets if r < actual) / len(rets), 3),
        "seeds": seeds,
    }


def format_order_sensitivity(s: dict) -> str:
    pct = s["percentile"]
    verdict = ("the reported figure is an ordering artifact, not an edge — read the "
               "random-order mean as the honest number"
               if pct >= 0.9 else
               "ordering is not carrying the result")
    return "\n".join([
        "  order sensitivity (same rules, candidate order shuffled):",
        f"    reported     {s['actual_return_pct']:+8.2f}%",
        f"    random order {s['random_mean_pct']:+8.2f}% mean, "
        f"{s['random_min_pct']:+.2f}% … {s['random_max_pct']:+.2f}% over {s['seeds']} seeds",
        f"    reported sits at the {pct:.0%} percentile of random — {verdict}",
    ])
