"""US-follows-vs-diverges classifier — the self-improvement loop's answer to
"which China sectors track Wall Street, and which move on their own?"

Some China sectors mostly follow the US overnight (semis, big-cap financials);
others diverge — moving on domestic catalysts, policy, or their own cycle (the AI
theme is the classic example). Treating them all as "US spilled over, so China
follows" is exactly the shallow, broad read to move past. This measures it: for
each sector it correlates the sector's US-spillover signal against the actual
next China move over all history, and labels the relationship — so both the
analyst and the reader know when the US lead is real and when it's noise.

It reuses the backtest's ``collect_records`` (the same honest replay used to score
the model) for the aligned (us_spillover, actual_move) pairs, and the reflection
loop's ``pearson``. The gate is ``config.MIN_CORRELATION_SAMPLE`` — below it, we
say "insufficient data" rather than pretend a handful of days is a relationship.
"""
from __future__ import annotations

from .. import config
from ..reflection.stats import pearson

# Correlation thresholds for the label (on the sector's US-spillover ↔ actual move).
_FOLLOWS = 0.30      # r at/above this ⇒ the sector tracks the US lead
_DIVERGES = -0.15    # r at/below this ⇒ it tends to move opposite the US read


def classify_pairs(us_spillovers: list[float], moves: list[float]) -> dict:
    """Label one sector's US→China relationship from aligned pairs. Pure."""
    n = len(us_spillovers)
    r = pearson(us_spillovers, moves)
    if n < config.MIN_CORRELATION_SAMPLE or r is None:
        return {"label": "insufficient data", "r": None if r is None else round(r, 3), "n": n}
    if r >= _FOLLOWS:
        label = "follows US"
    elif r <= _DIVERGES:
        label = "diverges from US"
    else:
        label = "weak / independent"
    return {"label": label, "r": round(r, 3), "n": n}


def classify_sectors(start=None, end=None, db_path=None) -> dict:
    """Per-sector US→China relationship over history. Returns
    {sector: {label, r, n}}. Reuses the backtest replay for aligned pairs."""
    from ..backtest import collect_records

    by_sector: dict[str, list[tuple[float, float]]] = {}
    for rec in collect_records(start, end, db_path):
        if rec.get("us_spillover") is None or rec.get("actual_move") is None:
            continue
        by_sector.setdefault(rec["sector"], []).append(
            (rec["us_spillover"], rec["actual_move"]))
    out = {}
    for sector, pairs in by_sector.items():
        xs = [p[0] for p in pairs]
        ys = [p[1] for p in pairs]
        out[sector] = classify_pairs(xs, ys)
    return out


def summary_line(sector_label: dict) -> str:
    """One compact human line, e.g. 'semis follows US (r=0.61, n=90)'."""
    d = sector_label
    if d["r"] is None:
        return f"{d['label']} (n={d['n']})"
    return f"{d['label']} (r={d['r']}, n={d['n']})"
