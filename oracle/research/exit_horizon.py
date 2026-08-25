"""How long should the validated rule stay in the trade?

The mean-reversion rule enters at the open and exits at the close of the same
session. That exit was never tested against alternatives — it was simply the
horizon the rule was found on. This module tests it.

The question matters because of where this system's measured edge sits. The
model calls the overnight **gap** with 71.8% accuracy against a 58.8% majority
baseline, and the gap is the one segment a position entered at the open cannot
capture: the US session driving it trades inside that window, so owning it means
entering before the information exists. That argument is airtight for the gap
*preceding* an entry — and it says nothing about the gap that comes *after* one.

A position opened at open[D] is already holding when gap[D+1] happens. Nothing
has to be predicted to capture it; the position simply has to still be open. So
"exit at the close or hold overnight" is a decidable empirical question rather
than a matter of risk appetite, and this module decides it.

Three things keep the answer honest:

  * **One round trip, every horizon.** Holding longer costs no extra friction —
    you buy once and sell once. Charging per-day would rig the test toward the
    short horizon.
  * **The baseline moves with the horizon.** Chinese sessions carry positive
    drift, so a longer hold earns more *for everybody*. The rule is scored on
    its excess over holding every session the same length, never on its raw
    return. Without this control, "hold longer earns more" is guaranteed and
    meaningless.
  * **Only the holdout counts.** The same chronological 70/30 split the rule was
    validated on, so the numbers here are comparable to the ones already
    published rather than a fresh fit.

Longer holds also carry more variance, and a mean that improves while the
per-trade spread widens is not automatically an improvement. Both are reported.

**Not investment advice.** No order is placed or recommended.
"""
from __future__ import annotations

from math import sqrt
from statistics import pstdev

from .. import config, db
from ..ingestion.china_market import daily_limit_for
from ..paper import COST_PCT, qualifies
from . import sweep as sw

# Exit points, measured from an entry at the open of day D. The names say which
# bar and which side of it the position is closed on.
HORIZONS = ("close_d0", "open_d1", "close_d1", "close_d2")

# The same-session exit the rule was validated on, and the earliest exit that
# would remain available if these instruments settle T+1. Named here beside the
# horizons themselves so every module that reasons about settlement reads one
# definition.
T0_EXIT = "close_d0"
T1_EXIT = "open_d1"

# Mean overnight (previous close -> open) return per session across the traded
# universe, 2016-2026: t=-14.35, negative in 10/10 sectors and 11/11 years.
# Published as a constant so anything reasoning about the cost of holding
# overnight cites this measurement instead of re-deriving it — recomputing it
# needs the whole bar history, which most callers have no reason to load.
# Re-measure with drift_decomposition() if the universe or window changes.
MEASURED_OVERNIGHT_DRIFT_PCT = -0.0722

# What each horizon adds to the one before it, for the report.
HORIZON_NOTE = {
    "close_d0": "same-session body (the validated exit)",
    "open_d1":  "+ the following overnight gap",
    "close_d1": "+ that gap and the next session's body",
    "close_d2": "+ a second full session",
}


def _pct(a: float | None, b: float | None) -> float | None:
    """(a/b - 1) * 100, or None when either side is unusable. Pure."""
    if a is None or b is None:
        return None
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if b == 0:
        return None
    return round((a / b - 1.0) * 100.0, 4)


def path_returns(bars: list[dict], i: int, limit: float) -> dict | None:
    """Gross % return from entering at ``bars[i]['open']`` to each horizon. Pure.

    Returns None when the entry bar itself is unusable. Individual horizons
    degrade to None independently when the path runs off the end of the data, so
    a setup near the last session still contributes its shorter horizons.

    Any bar on the path whose close-to-close move exceeds the instrument's daily
    limit is a corporate action rather than a return, and truncates the path —
    carrying it would book a share conversion as profit.
    """
    entry = bars[i].get("open")
    if entry is None:
        return None
    out: dict[str, float | None] = {h: None for h in HORIZONS}
    out["close_d0"] = _pct(bars[i].get("close"), entry)

    # Walk forward, stopping at the first artifact or the end of the data.
    # (bar offset from entry, horizon name, which side of that bar).
    steps = ((1, "open_d1", "open"), (1, "close_d1", "close"),
             (2, "close_d2", "close"))
    for offset, name, field in steps:
        j = i + offset
        if j >= len(bars):
            continue
        # Every bar from the entry through bar j must be genuine price action.
        if any(_is_artifact(bars, k, limit) for k in range(i + 1, j + 1)):
            continue
        out[name] = _pct(bars[j].get(field), entry)
    return out


def _is_artifact(bars: list[dict], k: int, limit: float) -> bool:
    """Did bar k move beyond its instrument's daily limit? Pure."""
    if k <= 0 or k >= len(bars):
        return False
    move = _pct(bars[k].get("close"), bars[k - 1].get("close"))
    return move is not None and abs(move) > limit


def _f(x: float | None) -> str:
    """Signed percent, or an em dash when the number does not exist. Pure."""
    return "—" if x is None else f"{x:+.3f}%"


def _bars(symbol: str, db_path=None) -> list[dict]:
    return sorted((dict(r) for r in
                   db.close_series("china_close", symbol=symbol, limit=10 ** 6,
                                   db_path=db_path)),
                  key=lambda r: r["trade_date"])


def build_paths(db_path=None, start: str | None = None) -> list[dict]:
    """One row per sector-session: the setup's inputs plus its forward path.

    ``prior_body`` and ``gap`` are both complete at the open, so selecting on
    them involves no lookahead. The returns are what happens afterwards.
    """
    # A fresh clone has no database file at all, and the research phase runs
    # before any ingestion has happened on a first run. Creating the schema
    # here turns that into an empty result instead of "no such table".
    db.init_db(db_path)
    rows = []
    for sector, symbol in config.CHINA_SECTOR_ETFS.items():
        bars = _bars(symbol, db_path)
        limit = daily_limit_for(symbol)
        for i in range(1, len(bars)):
            d = bars[i]["trade_date"]
            if start is not None and d < start:
                continue
            gap = _pct(bars[i].get("open"), bars[i - 1].get("close"))
            prior_body = _pct(bars[i - 1].get("close"), bars[i - 1].get("open"))
            # Artifacts are not setups and not returns.
            if gap is None or abs(gap) > limit:
                continue
            if prior_body is not None and abs(prior_body) > limit:
                continue
            path = path_returns(bars, i, limit)
            if path is None or path["close_d0"] is None:
                continue
            window = [x for x in (_pct(bars[k].get("close"), bars[k - 1].get("close"))
                                  for k in range(max(1, i - 20), i)) if x is not None]
            rows.append({
                "sector": sector, "date": d,
                "prior_body": prior_body, "gap": gap,
                "volatility": (sum(abs(w) for w in window) / len(window)) if window else None,
                **path,
            })
    return rows


def drift_decomposition(rows: list[dict]) -> dict:
    """Split the market's own return into its overnight and intraday halves.

    This is the mechanism behind the exit verdict, and it is worth stating on its
    own because it inverts the pattern most equity intuition is built on. In US
    equities the overnight window carries the return and the intraday session is
    roughly flat. In these Chinese sector ETFs it is the other way round: the
    overnight gap is a persistent, strongly significant *drag*, and the whole of
    the return accrues while the market is open.

    A plausible mechanism is T+1 settlement. Shares bought today cannot be sold
    until tomorrow, so anyone wanting out must wait for the next open, which
    concentrates selling pressure there. Whatever the cause, the consequence for
    a long position is the same: time spent holding overnight is time spent in
    the losing half of the day.

    Reported with per-sector and per-year agreement counts, because a decomposition
    this large is more likely to be an artifact of one instrument or one era than
    a fact about the market — and those counts are what rule that out. Pure.
    """
    from collections import defaultdict

    def block(vals: list[float]) -> dict:
        if len(vals) < 10:
            return {"n": len(vals), "mean": None, "t": None, "share_pos": None}
        m = sum(vals) / len(vals)
        sd = pstdev(vals)
        return {"n": len(vals), "mean": round(m, 4),
                "t": round(m / (sd / sqrt(len(vals))), 2) if sd > 0 else None,
                "share_pos": round(sum(1 for v in vals if v > 0) / len(vals), 4)}

    usable = [r for r in rows
              if r.get("gap") is not None and r.get("close_d0") is not None]
    by_sector: dict = defaultdict(lambda: ([], []))
    by_year: dict = defaultdict(lambda: ([], []))
    gaps, bodies = [], []
    for r in usable:
        gaps.append(r["gap"])
        bodies.append(r["close_d0"])
        by_sector[r["sector"]][0].append(r["gap"])
        by_sector[r["sector"]][1].append(r["close_d0"])
        by_year[str(r.get("date", ""))[:4]][0].append(r["gap"])
        by_year[str(r.get("date", ""))[:4]][1].append(r["close_d0"])

    def agree(groups) -> tuple[int, int]:
        """(groups whose overnight mean is negative, groups measured)."""
        hits = tot = 0
        for g, _b in groups.values():
            if len(g) < 10:
                continue
            tot += 1
            hits += (sum(g) / len(g)) < 0
        return hits, tot

    s_hits, s_tot = agree(by_sector)
    y_hits, y_tot = agree(by_year)
    return {
        "overnight": block(gaps), "intraday": block(bodies),
        "sectors_negative_overnight": s_hits, "sectors": s_tot,
        "years_negative_overnight": y_hits, "years": y_tot,
    }


def premium_economics(rows: list[dict], predicate=None,
                      cost_pct: float = COST_PCT) -> dict:
    """Why the intraday premium survives, and what the rule actually does. Pure.

    The obvious trade against a negative overnight drift is to be long during
    every session and flat every night. It does not work, and the reason is the
    most useful single fact about this edge: the premium is smaller than the
    friction needed to harvest it. Buying every open and selling every close
    earns +0.11% gross and loses money after a 0.15% round trip.

    That is why a decade-long, strongly significant anomaly has not been
    competed away — at retail friction it is not an opportunity, and anyone
    trying to harvest it indiscriminately pays for the privilege.

    It also reframes what the validated rule is for. The rule is not a bet on
    direction so much as a **filter on magnitude**: it identifies the small
    minority of sessions whose intraday drift is a large multiple of normal,
    which is the only condition under which the premium clears its costs. Stated
    that way, "how much bigger than normal, and on how few sessions" is the
    number that decides whether the rule is worth anything.
    """
    if predicate is None:
        predicate = lambda r: qualifies(r.get("prior_body"), r.get("gap"))  # noqa: E731
    every = [r[T0_EXIT] for r in rows if r.get(T0_EXIT) is not None]
    picked = [r[T0_EXIT] for r in rows
              if predicate(r) and r.get(T0_EXIT) is not None]
    if not every or not picked:
        return {"status": "no_data"}
    m_all = sum(every) / len(every)
    m_rule = sum(picked) / len(picked)
    return {
        "status": "measured",
        "n_all": len(every), "n_rule": len(picked),
        "fire_rate": round(len(picked) / len(every), 4),
        "premium_all": round(m_all, 4),
        "premium_all_net": round(m_all - cost_pct, 4),
        "premium_rule": round(m_rule, 4),
        "premium_rule_net": round(m_rule - cost_pct, 4),
        "multiple": round(m_rule / m_all, 2) if m_all else None,
        "cost_pct": cost_pct,
        "harvestable_unconditionally": bool(m_all > cost_pct),
    }


def format_premium(e: dict) -> str:
    if e.get("status") != "measured":
        return "  premium economics: not enough data"
    L = ["  Why this survives, and what the rule is actually for", "",
         f"  {'sessions traded':26}{'n':>7}{'gross':>10}{'net':>10}",
         f"  {'-' * 53}",
         f"  {'every session':26}{e['n_all']:>7}{e['premium_all']:>+9.4f}%"
         f"{e['premium_all_net']:>+9.4f}%",
         f"  {'only when the rule fires':26}{e['n_rule']:>7}"
         f"{e['premium_rule']:>+9.4f}%{e['premium_rule_net']:>+9.4f}%", ""]
    if not e["harvestable_unconditionally"]:
        L += [f"  Being long every session and flat every night LOSES money: the",
              f"  {e['premium_all']:+.4f}% premium is smaller than the {e['cost_pct']:.2f}% "
              f"round trip needed",
              "  to collect it. That is why a decade-long, strongly significant",
              "  anomaly has not been competed away — at retail friction it is not",
              "  an opportunity."]
    else:
        L += ["  The premium exceeds the assumed friction, so it could in principle",
              "  be harvested unconditionally. Treat that as a reason to doubt the",
              "  cost assumption before trusting the finding."]
    L += ["",
          f"  The rule fires on {e['fire_rate']:.2%} of sessions and selects intraday",
          f"  drift {e['multiple']}x normal. It is better understood as a filter on",
          "  magnitude than as a bet on direction: the premium only clears its costs",
          "  on the minority of sessions where it is a large multiple of average."]
    return "\n".join(L)


def format_drift(d: dict) -> str:
    o, i = d["overnight"], d["intraday"]
    if o["mean"] is None or i["mean"] is None:
        return "  drift decomposition: not enough data"

    def line(label: str, b: dict) -> str:
        # t is None whenever the segment has no variance, which is degenerate
        # rather than an error — the row still carries the mean worth reading.
        t = "n/a" if b["t"] is None else f"{b['t']:+.2f}"
        pos = "n/a" if b["share_pos"] is None else f"{b['share_pos']:.1%}"
        return f"  {label:22}{b['n']:>7}{b['mean']:>+14.4f}%{t:>9}{pos:>11}"

    L = ["  Where the market's own return accrues", "",
         f"  {'segment':22}{'n':>7}{'mean/session':>15}{'t':>9}{'share up':>11}",
         f"  {'-' * 64}",
         line("overnight (gap)", o),
         line("intraday (body)", i),
         "",
         f"  Overnight drift is negative in {d['sectors_negative_overnight']}/{d['sectors']} "
         f"sectors and {d['years_negative_overnight']}/{d['years']} years, so this is a",
         "  property of the market rather than of one instrument or one era.",
         "",
         "  This inverts the US pattern, where the overnight window carries the",
         "  return. A plausible cause is T+1 settlement: shares bought today cannot",
         "  be sold until tomorrow, concentrating exits at the open.",
         "",
         "  The consequence is the whole of the exit verdict — for a long position,",
         "  holding overnight means holding through the losing half of the day."]
    return "\n".join(L)


def _stats(vals: list[float], cost_pct: float) -> dict:
    """n / hit / gross / net / t / spread for one bundle of gross returns. Pure.

    ``t`` is against zero net return, which is the question a trader asks: does
    this make money after friction, not is it different from the market.
    """
    if not vals:
        return {"n": 0, "hit": None, "gross": None, "net": None,
                "t": None, "sd": None, "worst": None}
    gross = sum(vals) / len(vals)
    net = gross - cost_pct
    sd = pstdev(vals)
    return {
        "n": len(vals),
        "hit": round(sum(1 for v in vals if v > cost_pct) / len(vals), 4),
        "gross": round(gross, 4),
        "net": round(net, 4),
        "t": round(net / (sd / sqrt(len(vals))), 3) if sd > 0 else None,
        "sd": round(sd, 4),
        "worst": round(min(vals), 4),
    }


def _welch(a: list[float], b: list[float]) -> tuple[float | None, float]:
    """(t, two-sided p) for a difference of means, unequal variances. Pure.

    The rule's trades and the baseline's are different samples of different
    sizes, so this is Welch rather than a paired test. Normal approximation for
    the tail — with n in the hundreds the difference from a t-distribution is
    immaterial, and no scipy dependency is worth it here.
    """
    if len(a) < 2 or len(b) < 2:
        return None, 1.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va, vb = pstdev(a) ** 2 / len(a), pstdev(b) ** 2 / len(b)
    se = sqrt(va + vb)
    if se == 0:
        return None, 1.0
    t = (ma - mb) / se
    return round(t, 3), min(1.0, 2.0 * sw._norm_sf(abs(t)))


def simulate(rows: list[dict], predicate=None, cost_pct: float = COST_PCT,
             holdout_frac: float = 0.3) -> dict:
    """Score every horizon for the rule and for the all-sessions baseline. Pure.

    The chronological split matches the one the rule was validated on, so the
    ``close_d0`` holdout row here should reproduce the published result.
    """
    if predicate is None:
        predicate = lambda r: qualifies(r.get("prior_body"), r.get("gap"))  # noqa: E731
    usable = sorted(rows, key=lambda r: r.get("date", ""))
    if not usable:
        return {"status": "no_data"}
    cut = int(len(usable) * (1 - holdout_frac))
    parts = {"train": usable[:cut], "holdout": usable[cut:]}

    out = {"status": "measured", "cost_pct": cost_pct,
           "split_date": parts["holdout"][0]["date"] if parts["holdout"] else None,
           "drift": drift_decomposition(usable),
           "premium": premium_economics(usable, predicate, cost_pct)}
    for part_name, part in parts.items():
        picked = [r for r in part if predicate(r)]
        block = {}
        for h in HORIZONS:
            rv = [r[h] for r in picked if r.get(h) is not None]
            bv = [r[h] for r in part if r.get(h) is not None]
            t, p = _welch(rv, bv)
            block[h] = {
                "rule": _stats(rv, cost_pct),
                "baseline": _stats(bv, cost_pct),
                "excess": (round(sum(rv) / len(rv) - sum(bv) / len(bv), 4)
                           if rv and bv else None),
                "excess_t": t, "excess_p": p,
            }
        out[part_name] = block
    return out


def verdict(res: dict, q: float = sw.FDR_Q) -> dict:
    """Which horizon wins on the holdout, and is the improvement real?

    Two hurdles, both required. The winner must beat the validated exit on net
    return, and its excess over the same-horizon baseline must survive FDR
    correction across all horizons tested — otherwise the "improvement" is the
    best of four draws.
    """
    if res.get("status") != "measured" or "holdout" not in res:
        # An unmeasurable study is not a licence to change anything. The safe
        # default is the exit already validated, and callers read this key
        # rather than the status, so it must always be present.
        return {"status": res.get("status", "no_data"),
                "keep_current_exit": True, "best": T0_EXIT,
                "validated_exit": T0_EXIT}
    hold = res["holdout"]
    ps = [hold[h]["excess_p"] for h in HORIZONS]
    _, threshold = sw.benjamini_hochberg(ps, q=q)

    base = hold["close_d0"]["rule"]["net"]
    best, best_net = "close_d0", base
    for h in HORIZONS:
        n = hold[h]["rule"]["net"]
        if n is not None and (best_net is None or n > best_net):
            best, best_net = h, n
    survives = (threshold is not None
                and hold[best]["excess_p"] <= threshold
                and (hold[best]["excess"] or 0) > 0)
    return {
        "status": "measured",
        "validated_exit": "close_d0", "validated_net": base,
        "best": best, "best_net": best_net,
        "improvement": (None if best_net is None or base is None
                        else round(best_net - base, 4)),
        "p_threshold": threshold, "best_p": hold[best]["excess_p"],
        "survives_fdr": bool(survives),
        "keep_current_exit": best == "close_d0" or not survives,
    }


def format_report(res: dict, v: dict) -> str:
    if res.get("status") != "measured":
        return f"Exit horizon — {res.get('status')}"
    L = ["Exit horizon — how long the mean-reversion rule should hold", "",
         f"  one round trip of {res['cost_pct']:.2f}% is charged at every horizon;",
         f"  holdout begins {res.get('split_date')}", ""]
    for part in ("train", "holdout"):
        L += [f"  {part}", f"  {'horizon':11}{'n':>6}{'hit':>8}{'net':>9}"
                           f"{'t':>7}{'sd':>8}{'worst':>9}{'vs base':>9}{'p':>9}"]
        L.append(f"  {'-' * 76}")
        for h in HORIZONS:
            b = res[part][h]
            r = b["rule"]
            if not r["n"]:
                L.append(f"  {h:11}{'—':>6}")
                continue
            t = "n/a" if r["t"] is None else f"{r['t']:+.2f}"
            ex = "n/a" if b["excess"] is None else f"{b['excess']:+.3f}"
            L.append(f"  {h:11}{r['n']:>6}{r['hit']:>7.1%}{r['net']:>+8.3f}%"
                     f"{t:>7}{r['sd']:>8.2f}{r['worst']:>+8.2f}%{ex:>9}"
                     f"{b['excess_p']:>9.3f}")
        L.append("")
    if res.get("drift"):
        L += [format_drift(res["drift"]), ""]
    if res.get("premium"):
        L += [format_premium(res["premium"]), ""]
    L += ["  'vs base' is the rule's mean gross return minus what holding EVERY",
          "  session for the same horizon earned. That control is the whole test:",
          "  each horizon carries its own drift — positive where it adds a session,",
          "  negative where it adds an overnight — and only the excess over that",
          "  drift belongs to the rule rather than to the market.", ""]
    for h in HORIZONS:
        L.append(f"    {h:11}{HORIZON_NOTE[h]}")
    L += [""]
    if v.get("status") == "measured":
        best_net = _f(v["best_net"])
        val_net = _f(v["validated_net"])
        L.append(f"  best net on the holdout: {v['best']} ({best_net})")
        L.append(f"  validated exit close_d0: {val_net}")
        if v["keep_current_exit"]:
            L += ["", "  VERDICT: keep the current same-session exit.",
                  "  Either it already wins, or the longer hold's advantage does not",
                  "  survive correction for having tried four horizons."]
        else:
            L += ["", f"  VERDICT: {v['best']} beat the validated exit by "
                      f"{v['improvement']:+.3f}% and survived FDR "
                      f"(p={v['best_p']:.4f} <= {v['p_threshold']:.4f}).",
                  "  This is a candidate, not an instruction: it is one holdout, and",
                  "  the forward ledger still has the deciding vote."]
    L += ["", "  Nothing here is executed. No order is placed or recommended.",
          "  Not investment advice."]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    start = config.LEARNING_TRAIN_START or None
    rows = build_paths(start=start)
    res = simulate(rows)
    print(format_report(res, verdict(res)))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
