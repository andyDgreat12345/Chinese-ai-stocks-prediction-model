"""Does Chinese-language news sentiment predict Chinese sector returns?

This asks of the news layer exactly what the price sweep (``research/sweep.py``)
asks of the US universe, and holds it to the same four filters: sample floor,
Fisher-z significance, Benjamini–Hochberg control across every test run, and
split-half sign stability. The motivating result is that when those filters were
applied to prices, the "obvious" China correlations turned out to be global risk
beta — the CONTROL groups (EEM/EFA) scored as well as the China-specific ones.
Chinese-language news is a plausible source of genuine China alpha, which is a
reason to *test* it, not a reason to believe it.

**Lag ≥ 1 is mandatory here, and the reason differs from the price sweep.**
For prices, lag 0 is lookahead because the Chinese close precedes the US close by
~14h on the same date. For news it is lookahead because of when the fetch runs:
``fetch_world_news`` stamps ``trade_date`` with the UTC date at fetch time, and
the morning job fires at 21:00 UTC — which is 05:00 CST the *following* day. So
headlines stamped D are gathered on the CST morning of D+1, after china_close[D]
is already on the tape. Pairing news[D] with china[D] would be reading tomorrow's
newspaper. news[D] → china[D+1] is the honest, actually-tradeable alignment, and
it leaves ~4.5h before the 09:30 CST open.

Buckets are (language × category). Language is derived from the source name
rather than re-detecting script per row, because the feed list already knows
which sources are Chinese and a source never changes language.

**Not investment advice.** A surviving bucket is a hypothesis worth studying.
"""
from __future__ import annotations

import math
from statistics import NormalDist

from .. import config, db
from . import sweep as sw
from .run import _returns

# A day's mean sentiment is only meaningful if the day actually carried news.
# Below this, the mean is one or two headlines of noise wearing a day's clothes.
MIN_HEADLINES_PER_DAY = 5

# news[D] is fetched after china_close[D] — see the module docstring. Lag 0 is
# not merely untradeable here, it is lookahead, so it is never tested.
NEWS_LAGS = (1, 2, 3)


def _zh_sources() -> set[str]:
    return set(config.NEWS_FEEDS_ZH)


def language_of(source: str) -> str:
    """'zh' for the Chinese-language feeds, else 'en'. Pure."""
    return "zh" if source in _zh_sources() else "en"


def daily_sentiment(db_path=None,
                    min_headlines: int = MIN_HEADLINES_PER_DAY) -> dict[str, dict[str, float]]:
    """bucket -> {trade_date: mean sentiment}, for buckets with enough coverage.

    Buckets are ``<lang>:ALL`` and ``<lang>:<category>``. A (bucket, date) cell is
    dropped unless it holds at least ``min_headlines`` headlines that day.
    """
    conn = db.connect(db_path)
    rows = conn.execute(
        "SELECT trade_date, source, category, sentiment FROM news "
        "WHERE sentiment IS NOT NULL").fetchall()

    # bucket -> date -> [scores]
    acc: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        lang = language_of(r["source"])
        for bucket in (f"{lang}:ALL", f"{lang}:{r['category']}"):
            acc.setdefault(bucket, {}).setdefault(r["trade_date"], []).append(r["sentiment"])

    series: dict[str, dict[str, float]] = {}
    for bucket, by_date in acc.items():
        kept = {d: sum(v) / len(v) for d, v in by_date.items() if len(v) >= min_headlines}
        if kept:
            series[bucket] = kept
    return series


def china_returns(db_path=None, min_days: int = sw.MIN_PAIRS) -> dict[str, dict[str, float]]:
    """Daily %-return series per China symbol — the outcome side of the test."""
    out = {}
    for sym in db.distinct_symbols("china_close", db_path):
        s = _returns("china_close", sym, db_path)
        if len(s) >= min_days:
            out[sym] = s
    return out


# ── how much history would it take to see something? ─────────────────────
def min_detectable_r(n: int, alpha: float = 0.05) -> float | None:
    """Smallest |r| that would reach two-sided significance at `alpha` with `n`
    paired observations, inverting the Fisher z-transform. Pure.

    This is the question worth asking *before* waiting on data: if the true
    effect is smaller than this, the sweep cannot find it no matter how clean
    the code is."""
    if n is None or n < 4:
        return None
    z_crit = NormalDist().inv_cdf(1.0 - alpha / 2.0)
    return math.tanh(z_crit / math.sqrt(n - 3))


def days_needed_for(r: float, alpha: float = 0.05) -> int | None:
    """Paired observations needed before an effect of size |r| becomes visible."""
    if not r or abs(r) >= 1.0:
        return None
    z_crit = NormalDist().inv_cdf(1.0 - alpha / 2.0)
    return int(math.ceil(3 + (z_crit / math.atanh(abs(r))) ** 2))


def power_table(n_tests: int, q: float = sw.FDR_Q,
                horizons=(20, 40, 60, 90, 120, 250)) -> list[dict]:
    """Detectability at each horizon, raw and corrected for sweep width.

    The corrected column uses the most conservative BH threshold (q/m, the bar
    the *smallest* p-value must clear), so it is a worst case rather than a
    promise. Sweeping wide costs sensitivity — that is the trade this table
    makes visible."""
    alpha_corrected = q / max(n_tests, 1)
    out = []
    for n in horizons:
        out.append({
            "n": n,
            "min_r_raw": min_detectable_r(n, 0.05),
            "min_r_corrected": min_detectable_r(n, alpha_corrected),
        })
    return out


# ── the sweep ─────────────────────────────────────────────────────────────
def run(db_path=None, min_headlines: int = MIN_HEADLINES_PER_DAY) -> dict:
    """Sweep every (news bucket × China symbol × lag) and FDR-correct the lot."""
    db.init_db(db_path)
    news = daily_sentiment(db_path, min_headlines)
    cn = china_returns(db_path)

    dates = sorted({d for s in news.values() for d in s})
    coverage = {
        "buckets": len(news),
        "china_symbols": len(cn),
        "news_days": len(dates),
        "first_day": dates[0] if dates else None,
        "last_day": dates[-1] if dates else None,
        "min_pairs_required": sw.MIN_PAIRS,
    }

    # sweep() enforces the sample floor per pair, but with a handful of news days
    # every test is skipped and the result is an empty set that could be mistaken
    # for "measured, found nothing". Say plainly that it has not been measured.
    if len(dates) < sw.MIN_PAIRS:
        n_hypothetical = max(len(news) * len(cn) * len(NEWS_LAGS), 1)
        return {
            "status": "insufficient_history",
            "coverage": coverage,
            "days_short": sw.MIN_PAIRS - len(dates),
            "tests": 0,
            "results": [],
            "power": power_table(n_hypothetical),
            "planned_tests": n_hypothetical,
        }

    result = sw.sweep(news, cn, lags=NEWS_LAGS)
    result["status"] = "measured"
    result["coverage"] = coverage
    result["power"] = power_table(max(result["tests"], 1))
    return result


def format_report(result: dict) -> str:
    cov = result["coverage"]
    L = ["## Chinese news sentiment — predictive value", ""]

    if result["status"] == "insufficient_history":
        L += [
            f"**Not measured yet** — {cov['news_days']} day(s) of news history, "
            f"{cov['min_pairs_required']} needed. {result['days_short']} to go.",
            "",
            "News history only accumulates forward: RSS serves the current window,",
            "so there is no backfill for this the way there is for prices. The",
            "figure below is what the wait buys.",
            "",
            f"Buckets ready: {cov['buckets']} × {cov['china_symbols']} China symbols "
            f"× {len(NEWS_LAGS)} lags = {result['planned_tests']} planned tests.",
            "",
        ]
    else:
        surv = sw.survivors(result)
        L += [
            f"{result['tests']} tests over {cov['news_days']} news days "
            f"({cov['first_day']} → {cov['last_day']}), FDR q={result['fdr_q']}.",
            f"**{len(surv)} bucket(s) survived** significance + FDR + split-half stability.",
            "",
        ]
        if surv:
            L += ["| bucket | China symbol | lag | r | n | q |",
                  "|---|---|---|---|---|---|"]
            for r in surv[:15]:
                L.append(f"| {r['us_symbol']} | {r['china_symbol']} | {r['lag']} | "
                         f"{r['r']:+.3f} | {r['n']} | {r['q_value']:.4f} |")
        else:
            L.append("No bucket cleared the bar. On this evidence Chinese news "
                     "sentiment does not predict next-session sector returns, and "
                     "should not be wired into predictions.")
        L.append("")

    L += ["### Detectability", "",
          "Smallest |r| a test could resolve, by history length. "
          "'corrected' is the worst-case Benjamini–Hochberg bar for this sweep's width.",
          "", "| days | min |r| raw | min |r| FDR-corrected |", "|---|---|---|"]
    for row in result["power"]:
        L.append(f"| {row['n']} | {row['min_r_raw']:.3f} | {row['min_r_corrected']:.3f} |")
    L += ["",
          "_Not investment advice. A surviving bucket is a hypothesis, not a trade._"]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    print(format_report(run()))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
