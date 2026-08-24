"""Decompose a daily K-line into the segments a trader actually acts on.

A close-to-close return is a blend of two different things, and the pipeline has
been predicting the blend:

  * the **gap** — previous close to today's open. China opens ~14h after the US
    closed, so this is the window in which US spillover can physically land. It
    is the purest expression of the thesis this whole system is built on.
  * the **body** — open to close, the domestic session itself, set by mainland
    flow while the US is shut.

Predicting close-to-close asks one model to call both at once. If the US-spillover
edge is real it should be concentrated in the gap, and predicting the body with a
US signal is close to asking a closed market to explain an open one.

The wicks are carried too, as the intraday excursion beyond the body — they say
how far a position would have been dragged before the bar settled, which is what
determines whether a stop was touched.

All returns are percentages of the reference price, matching ``pct_change``
elsewhere. Pure functions over bar dicts — no DB, no clock.

**Not investment advice.**
"""
from __future__ import annotations

from dataclasses import dataclass

# Segment names in the order they occur in a session.
SEGMENTS = ("gap", "body", "close_to_close")


@dataclass(frozen=True)
class Segments:
    gap: float | None            # prev_close -> open  (overnight)
    body: float | None           # open -> close       (the session)
    close_to_close: float | None # prev_close -> close (what we predicted before)
    upper_wick: float | None     # high above the body top
    lower_wick: float | None     # low below the body bottom
    range_pct: float | None      # high -> low, as a share of the open


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


def decompose(prev_close: float | None, bar: dict | None) -> Segments:
    """Split one session's bar into its segments. Pure.

    Every field degrades to None independently, so a source that publishes only
    closes still yields a usable ``close_to_close`` instead of nothing.
    """
    bar = bar or {}
    o = bar.get("open", bar.get("o"))
    h = bar.get("high", bar.get("h"))
    low = bar.get("low", bar.get("l"))
    c = bar.get("close", bar.get("c"))

    gap = _pct(o, prev_close)
    body = _pct(c, o)
    ctc = _pct(c, prev_close)

    upper = lower = rng = None
    if o is not None and c is not None:
        top, bottom = (max(o, c), min(o, c))
        upper = _pct(h, top) if h is not None else None
        # Signed negative: how far below the body the bar traded.
        lower = _pct(low, bottom) if low is not None else None
    if h is not None and low is not None and o:
        rng = _pct(h, low)

    return Segments(gap=gap, body=body, close_to_close=ctc,
                    upper_wick=upper, lower_wick=lower, range_pct=rng)


def direction(value: float | None, deadband: float = 0.0) -> str | None:
    """'bullish'/'bearish' for a segment return, or None inside the deadband.

    A deadband matters more here than for close-to-close: gaps cluster tightly
    around zero, and scoring a 0.01% gap as a directional "hit" would inflate
    every accuracy number with coin flips on noise.
    """
    if value is None:
        return None
    if abs(value) <= deadband:
        return None
    return "bullish" if value > 0 else "bearish"


# ── measurement ───────────────────────────────────────────────────────────
# Which segments a position entered at the OPEN can actually capture.
#
# gap runs from the previous session's close to this one's open, and the US
# session that drives it trades INSIDE that window: China closes 07:00 UTC on
# d-1, the US session of d-1 runs 13:30-21:00 UTC, China opens 01:30 UTC on d.
# Capturing the gap therefore means entering at 15:00 CST on d-1 — before the US
# session the signal comes from. The information is real and arrives too late.
TRADEABLE_FROM_OPEN = {"gap": False, "body": True, "close_to_close": False}


def score_segments(records: list[dict], segments_by_sector_date: dict,
                   deadband: float = 0.0) -> list[dict]:
    """Hit rate of each model call against each K-line segment. Pure.

    ``segments_by_sector_date``: {sector: {date: Segments}}.
    Returns one row per segment with n, hit rate, and whether a position entered
    at the open could have captured it.
    """
    from math import sqrt

    out = []
    for name in SEGMENTS:
        hits = n = 0
        for r in records:
            if r.get("model_dir") not in ("bullish", "bearish"):
                continue
            seg = (segments_by_sector_date.get(r["sector"]) or {}).get(r["date"])
            if seg is None:
                continue
            actual = direction(getattr(seg, name, None), deadband)
            if actual is None:
                continue
            n += 1
            hits += (actual == r["model_dir"])
        hit_rate = hits / n if n else None
        out.append({
            "segment": name,
            "n": n,
            "hit_rate": None if hit_rate is None else round(hit_rate, 4),
            "edge_t": None if not n else round((hit_rate - 0.5) * sqrt(n), 3),
            "tradeable_from_open": TRADEABLE_FROM_OPEN.get(name, False),
        })
    return out


def format_segment_report(rows: list[dict]) -> str:
    L = ["K-line segment accuracy — where the edge actually is", "",
         f"  {'segment':16}{'n':>8}{'hit':>9}{'edge_t':>9}  capturable entering at the open",
         f"  {'-' * 62}"]
    for r in rows:
        hit = "n/a" if r["hit_rate"] is None else f"{r['hit_rate']:.2%}"
        et = "n/a" if r["edge_t"] is None else f"{r['edge_t']:+.2f}"
        mark = "yes" if r["tradeable_from_open"] else "NO"
        L.append(f"  {r['segment']:16}{r['n']:>8}{hit:>9}{et:>9}  {mark}")
    L += ["",
          "  Read the 'capturable' column before the hit rate. The gap runs from the",
          "  previous close to this open, and the US session driving it trades inside",
          "  that window — owning the gap means entering before the US session the",
          "  signal comes from. A high gap hit rate is information that arrives too",
          "  late to act on, not an edge.",
          "",
          "  The body is what a position entered at the open actually earns.",
          "",
          "  That is less of a loss than it sounds, and this report used to imply",
          "  otherwise. Over ten years the gap carries a mean of -0.072% per session",
          "  (t=-14.4) while the body carries +0.110% (t=+10.2): all of the market's",
          "  return accrues while it is open, and the overnight window is a",
          "  persistent drag, in 10/10 sectors and 11/11 years. Entering at the open",
          "  is not forfeiting the profitable segment — it is sitting out the losing",
          "  one. See `oracle.research.exit_horizon` for the decomposition and for",
          "  what happens when the position is held overnight anyway.",
          "",
          "  Not investment advice."]
    return "\n".join(L)


def build_segments(db_path=None, start: str | None = None) -> dict:
    """{sector: {date: Segments}} from stored China bars. Network-free."""
    from .. import config, db

    out: dict[str, dict] = {}
    for sector, symbol in config.CHINA_SECTOR_ETFS.items():
        rows = db.close_series("china_close", symbol=symbol, limit=1000000,
                               db_path=db_path)
        rows = sorted(rows, key=lambda r: r["trade_date"])
        prev, m = None, {}
        for r in rows:
            if start is None or r["trade_date"] >= start:
                m[r["trade_date"]] = decompose(prev, dict(r))
            prev = r["close"]          # advance even when filtered out
        out[sector] = m
    return out


def main(argv: list[str]) -> int:
    from .. import config
    from ..backtest import collect_records

    start = config.LEARNING_TRAIN_START or None
    records = collect_records(start=start)
    segs = build_segments(start=start)
    print(format_segment_report(score_segments(records, segs)))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
