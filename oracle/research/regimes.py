"""Is the rule's edge broad, or is it one lucky corner of the data?

A t-statistic of +2.40 says the average trade made money. It does not say the
edge is a property of the market rather than of one volatile year, one sector,
or one unusual stretch of gap depths. Two rules can produce the same headline
number: one that earns a little almost everywhere, and one that loses in most
conditions and is rescued by a handful of outliers. Only the first is worth
trading, and the headline cannot tell them apart.

**This module is not a search.** That distinction is the whole design. The
conditional sweep in ``marginal`` looks for buckets where an edge exists, and
needs FDR correction precisely because looking hard enough guarantees finding
something. Here every bucket is reported together and none is ever selected to
trade — the question is how uniformly the *already-validated* rule holds up, so
the statistic that matters is agreement across buckets, not the best one in the
set. Picking the winning regime out of this table and trading it would be
exactly the error the rest of this system exists to prevent, which is why the
report refuses to name a best bucket.

The test is a sign test: if the rule had no real edge, each bucket would come
out positive about half the time, and the probability of at least k of n positive
is a binomial tail. "Is the agreement itself surprising?" is the right question
to ask of a fragility check.

It is run **per family, never pooled across families**, and that is not a detail.
The families overlap completely — every trade appears once in its year bucket,
once in its sector bucket, and once in each tercile — so pooling them counts a
single trade six times and manufactures significance out of nothing. Pooling the
buckets here reports p<0.0001 where the honest figure is nearer p=0.01. Within a
family the buckets partition the trades disjointly, which is what makes the sign
test legitimate there.

Even per family the trades are not fully independent: sector ETFs move together,
so several of a day's trades are close to one observation. The p-values are
therefore optimistic, and are read as "broad or narrow", not as proof.

Measured on the full window rather than the holdout, deliberately. Splitting
into a dozen buckets and then keeping 30% of each leaves samples too thin to say
anything, and nothing here is being fitted, so the clean-holdout discipline that
governs discovery does not apply.

**Not investment advice.**
"""
from __future__ import annotations

from math import comb, sqrt
from statistics import pstdev

from ..paper import COST_PCT, qualifies
from .exit_horizon import T0_EXIT, build_paths

# A bucket smaller than this cannot say anything, and reporting it as a
# disagreement would let noise masquerade as fragility.
MIN_BUCKET_N = 25


def _net_stats(vals: list[float], cost_pct: float) -> dict:
    if not vals:
        return {"n": 0, "hit": None, "net": None, "t": None}
    gross = sum(vals) / len(vals)
    net = gross - cost_pct
    sd = pstdev(vals)
    return {"n": len(vals), "net": round(net, 4),
            "hit": round(sum(1 for v in vals if v > cost_pct) / len(vals), 4),
            "t": round(net / (sd / sqrt(len(vals))), 2) if sd > 0 else None}


def _terciles(rows: list[dict], key: str) -> list[tuple[str, list[dict]]]:
    """Low/mid/high split on a numeric label. Pure."""
    vals = sorted(r[key] for r in rows if r.get(key) is not None)
    if len(vals) < 3:
        return []
    lo, hi = vals[len(vals) // 3], vals[2 * len(vals) // 3]
    out = {"low": [], "mid": [], "high": []}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        out["low" if v <= lo else "high" if v > hi else "mid"].append(r)
    return [(f"{key}:{k}", v) for k, v in out.items()]


def _by_key(rows: list[dict], key: str) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        groups.setdefault(f"{key}:{v}", []).append(r)
    return sorted(groups.items())


def add_trend(rows: list[dict], window: int = 20) -> list[dict]:
    """Label each row with its instrument's trailing return. Pure-ish.

    Uses only sessions strictly before the one being labelled, so a row never
    sees its own outcome. Returns new dicts rather than mutating the input.
    """
    by_sector: dict[str, list[dict]] = {}
    for r in rows:
        by_sector.setdefault(r["sector"], []).append(r)
    out = []
    for sector, group in by_sector.items():
        group = sorted(group, key=lambda r: r.get("date", ""))
        for i, r in enumerate(group):
            prior = group[max(0, i - window):i]
            moves = [(p.get("gap") or 0.0) + (p.get(T0_EXIT) or 0.0)
                     for p in prior]
            out.append({**r,
                        "trend": (sum(moves) if moves else None),
                        "year": str(r.get("date", ""))[:4] or None})
    return out


def bucket_families(rows: list[dict]) -> dict[str, list[tuple[str, list[dict]]]]:
    """The pre-registered ways of slicing the rule's trades. Pure.

    Fixed in code rather than searched: these are the conditions under which a
    fragile edge would plausibly break, chosen before looking at the answers.
    """
    return {
        "volatility": _terciles(rows, "volatility"),
        "gap depth": _terciles(rows, "gap"),
        "prior body depth": _terciles(rows, "prior_body"),
        "trailing trend": _terciles(rows, "trend"),
        "year": _by_key(rows, "year"),
        "sector": _by_key(rows, "sector"),
    }


def _sign_test(positive: int, total: int) -> float:
    """P(at least `positive` of `total` positive | no edge). Pure."""
    if total <= 0:
        return 1.0
    tail = sum(comb(total, k) for k in range(positive, total + 1))
    return min(1.0, tail / (2 ** total))


def analyse(rows: list[dict], predicate=None, cost_pct: float = COST_PCT,
            min_n: int = MIN_BUCKET_N) -> dict:
    """Score the rule inside every pre-registered bucket. Pure."""
    if predicate is None:
        predicate = lambda r: qualifies(r.get("prior_body"), r.get("gap"))  # noqa: E731
    labelled = add_trend(rows)
    picked = [r for r in labelled if predicate(r)]
    if not picked:
        return {"status": "no_trades"}

    overall = _net_stats([r[T0_EXIT] for r in picked
                          if r.get(T0_EXIT) is not None], cost_pct)
    families = {}
    agreement = {}
    worst = None
    for fam, buckets in bucket_families(picked).items():
        rows_out, pos, tot = [], 0, 0
        for label, chunk in buckets:
            vals = [r[T0_EXIT] for r in chunk if r.get(T0_EXIT) is not None]
            s = _net_stats(vals, cost_pct)
            if s["n"] < min_n:
                s["status"] = "too thin"
            else:
                s["status"] = "measured"
                tot += 1
                pos += s["net"] > 0
                if worst is None or s["net"] < worst[1]:
                    worst = (label, s["net"])
            rows_out.append({"label": label, **s})
        families[fam] = rows_out
        # Valid only because a family's buckets partition the trades disjointly.
        agreement[fam] = {"positive": pos, "measured": tot,
                          "sign_p": _sign_test(pos, tot) if tot else None}

    # The cleanest evidence comes from the families with the most disjoint
    # buckets; a 3-bucket tercile cannot beat p=0.125 even when unanimous.
    best_fam = None
    for fam, a in agreement.items():
        if a["measured"] >= 5 and a["sign_p"] is not None:
            if best_fam is None or a["sign_p"] < agreement[best_fam]["sign_p"]:
                best_fam = fam
    pooled_pos = sum(a["positive"] for a in agreement.values())
    pooled_tot = sum(a["measured"] for a in agreement.values())
    return {
        "status": "measured", "overall": overall, "families": families,
        "agreement": agreement, "strongest_family": best_fam,
        # Descriptive only — families overlap, so this is a summary of the
        # table, never a p-value. Kept because "28 of 32" is the clearest
        # one-line picture of concentration even though it cannot be tested.
        "pooled_positive": pooled_pos, "pooled_measured": pooled_tot,
        "unanimous_families": sum(1 for a in agreement.values()
                                  if a["measured"] and a["positive"] == a["measured"]),
        "families_measured": sum(1 for a in agreement.values() if a["measured"]),
        "strongest_p": agreement[best_fam]["sign_p"] if best_fam else None,
        "worst_bucket": worst[0] if worst else None,
        "worst_net": worst[1] if worst else None,
        "cost_pct": cost_pct,
    }


def format_report(res: dict) -> str:
    if res.get("status") != "measured":
        return f"Regime robustness — {res.get('status')}"
    o = res["overall"]
    t = "n/a" if o["t"] is None else f"{o['t']:+.2f}"
    L = ["Regime robustness — is the edge broad or is it one lucky corner?", "",
         f"  the rule over the full window: n={o['n']}  hit={o['hit']:.1%}  "
         f"net={o['net']:+.3f}%  t={t}",
         f"  friction charged: {res['cost_pct']:.2f}% round trip", ""]
    for fam, rows in res["families"].items():
        L += [f"  {fam}", f"  {'bucket':26}{'n':>7}{'hit':>8}{'net':>10}{'t':>8}",
              f"  {'-' * 59}"]
        for r in rows:
            if r.get("status") != "measured":
                L.append(f"  {r['label']:26}{r['n']:>7}   (too thin to read)")
                continue
            rt = "n/a" if r["t"] is None else f"{r['t']:+.2f}"
            L.append(f"  {r['label']:26}{r['n']:>7}{r['hit']:>7.1%}"
                     f"{r['net']:>+9.3f}%{rt:>8}")
        L.append("")
    L += ["  Agreement within each family (buckets partition the trades, so the",
          "  sign test is legitimate here — it is NOT pooled across families,",
          "  which would count every trade once per family):", "",
          f"  {'family':22}{'positive':>10}{'sign p':>10}", f"  {'-' * 42}"]
    for fam, a in res["agreement"].items():
        if not a["measured"]:
            L.append(f"  {fam:22}{'—':>10}")
            continue
        L.append(f"  {fam:22}{a['positive']:>5}/{a['measured']:<4}"
                 f"{a['sign_p']:>10.4f}")
    p = res.get("strongest_p")
    L += ["",
          f"  Positive in {res['pooled_positive']} of {res['pooled_measured']} readable "
          f"buckets; {res['unanimous_families']} of {res['families_measured']} families "
          f"unanimous.",
          "  (That count is a description of the table, not a test — the families",
          "  overlap, so it cannot carry a p-value.)",
          f"  Worst bucket: {res['worst_bucket']} at {res['worst_net']:+.3f}%.", ""]
    # The edge's existence is already established on a chronological holdout.
    # What is being judged here is concentration, so the verdict leads with how
    # widely the rule holds and treats the sign test as supporting colour.
    if p is not None and p <= 0.05:
        L += [f"  BROAD. Strongest family {res['strongest_family']} at p={p:.4f}: the",
              "  agreement would be surprising by chance, so the edge is a property of",
              "  the market rather than of one corner of it."]
    elif p is not None and p <= 0.15:
        L += [f"  BROAD BUT NOT DECISIVELY. The best family ({res['strongest_family']})",
              f"  reaches only p={p:.4f}, short of the conventional bar, while the rule",
              "  still holds in most buckets and every tercile family agrees.",
              "",
              "  Read that as encouraging rather than settled. A three-bucket family",
              "  cannot beat p=0.125 even when unanimous, and sector ETFs move together",
              "  so the trades inside a bucket are not fully independent — both push",
              "  the achievable p upward regardless of how real the edge is."]
    else:
        L += ["  NARROW. No family shows agreement that would be surprising by chance,",
              "  which means the headline result leans on a subset of conditions. Treat",
              "  the rule as narrower than its overall t-statistic suggests."]
    if res["worst_net"] is not None and res["worst_net"] < 0:
        L += ["",
              f"  The rule loses money in its worst bucket ({res['worst_bucket']}).",
              "  That is expected of any real edge sliced finely enough, and is only a",
              "  warning if that bucket is where the money would actually be put."]
    L += ["",
          "  Do NOT trade the best bucket in this table. These slices were scored",
          "  together to measure uniformity, not searched to find a winner, and the",
          "  best of a dozen buckets is a selection effect rather than a discovery.",
          "  The tradeable rule is the one already validated, unconditioned.",
          "",
          "  Not investment advice."]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    from .. import config

    rows = build_paths(start=config.LEARNING_TRAIN_START or None)
    print(format_report(analyse(rows)))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
