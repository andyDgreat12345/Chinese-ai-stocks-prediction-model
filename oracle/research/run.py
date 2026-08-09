"""Run the wide US↔China correlation sweep and write the research report.

    python -m oracle.research.run [days] [--no-fetch] [--out FILE]

Steps: backfill the research universe (skippable with ``--no-fetch``), load daily
returns for every symbol, sweep all US×China×lag combinations with FDR control and
split-half stability, then render a markdown report.

The report deliberately leads with *how many tests were run*, because that number
is what makes the q-values meaningful — and it reports the control groups (EEM /
EFA) alongside the findings, since a China link no stronger than generic EM beta
is not a China finding at all.

**Not investment advice.** Surviving pairs are hypotheses to study.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

from .. import config, db
from . import sweep as sw
from . import universe as uni


def backfill_research_universe(days: int = 0) -> int:
    """Load history for every research US symbol (fail-soft per symbol).

    ``days=0`` (the default) pulls full available history. Every US<->China
    pairing is bounded by whichever leg is shorter, so leaving this at 400 while
    the China side carries thousands of bars would cap the sweep at 400 no matter
    how much China history exists.
    """
    from ..backfill import (
        _download_us, _trim_days, _us_records, extract_dated_ohlc, ohlc_to_rows,
    )
    from ..ingestion.us_market import SECTOR_TAGS as PIPELINE_TAGS

    fetched_at = datetime.now(timezone.utc).isoformat()
    total, failed = 0, []
    for sym in uni.us_symbols():
        try:
            series = _trim_days(extract_dated_ohlc(_us_records(_download_us(sym, days))), days)
            if not series:
                failed.append(sym)
                continue
            # CRITICAL: never clobber the sector tag the prediction pipeline maps
            # on (build_signals matches China sectors to US tags like "semis" /
            # "energy"). Research groups ("us_sector", "china_proxy") are only for
            # symbols the pipeline doesn't know; overwriting a pipeline symbol's
            # tag silently zeroes every US spillover signal and turns every daily
            # call neutral.
            sector_tag = PIPELINE_TAGS.get(sym) or uni.group_of(sym)
            rows = ohlc_to_rows(sym, sector_tag, series, fetched_at)
            total += db.upsert_market_close("us_close", rows)
        except Exception as e:  # noqa: BLE001
            failed.append(sym)
            print(f"  {sym}: failed ({e!r})", file=sys.stderr)
    print(f"backfill_research_universe: {total} rows, "
          f"{len(uni.us_symbols()) - len(failed)}/{len(uni.us_symbols())} symbols"
          + (f" (missing: {', '.join(failed)})" if failed else ""))
    return total


def repair_sector_tags(db_path=None) -> int:
    """Restore the pipeline's US sector tags if an earlier research backfill
    overwrote them. Idempotent and safe to run any time."""
    from ..ingestion.us_market import SECTOR_TAGS

    conn = db.connect(db_path)
    try:
        fixed = 0
        for sym, tag in SECTOR_TAGS.items():
            cur = conn.execute(
                "UPDATE us_close SET sector = ? WHERE symbol = ? AND sector IS NOT ?",
                (tag, sym, tag))
            fixed += cur.rowcount or 0
        conn.commit()
        print(f"repair_sector_tags: restored {fixed} row(s) to pipeline tags")
        return fixed
    finally:
        conn.close()


def _returns(table: str, symbol: str, db_path=None) -> dict[str, float]:
    return {r["trade_date"]: r["pct_change"]
            for r in db.close_series(table, symbol=symbol, limit=100000, db_path=db_path)
            if r["pct_change"] is not None}


def load_series(db_path=None) -> tuple[dict, dict]:
    """Daily %-return series for the research US universe and every China symbol."""
    us = {}
    for sym in uni.us_symbols():
        s = _returns("us_close", sym, db_path)
        if len(s) >= sw.MIN_PAIRS:
            us[sym] = s
    cn = {}
    for sym in db.distinct_symbols("china_close", db_path):
        s = _returns("china_close", sym, db_path)
        if len(s) >= sw.MIN_PAIRS:
            cn[sym] = s
    return us, cn


def run(days: int = 400, fetch: bool = True, db_path=None) -> dict:
    db.init_db(db_path)
    if fetch:
        backfill_research_universe(days)
    repair_sector_tags(db_path)   # heal any tags a previous run overwrote
    us, cn = load_series(db_path)
    print(f"sweep universe: {len(us)} US × {len(cn)} China symbols "
          f"× {len(sw.DEFAULT_LAGS)} lags")
    result = sw.sweep(us, cn)
    result["us_symbols"] = sorted(us)
    result["china_symbols"] = sorted(cn)
    register_survivors(result, db_path)
    return result


def register_survivors(result: dict, db_path=None) -> int:
    """Promote sweep survivors to `proven_pairs`, so the reflection round
    re-measures each one every day instead of trusting one discovery run."""
    today = datetime.now(timezone.utc).date().isoformat()
    n = 0
    for r in sw.survivors(result):
        db.upsert_proven_pair({
            "us_symbol": r["us_symbol"], "china_symbol": r["china_symbol"],
            "lag": r["lag"], "r_discovered": r["r"], "q_value": r["q_value"],
            "n_discovered": r["n"], "discovered_on": today}, db_path)
        n += 1
    print(f"register_survivors: registered {n} proven pair(s) for daily refresh")
    return n


# ── report ────────────────────────────────────────────────────────────────
def _sector_of(symbol: str) -> str:
    inv = {c: s for s, c in config.CHINA_SECTOR_ETFS.items()}
    return inv.get(symbol, {"sh000001": "SSE Composite", "sz399001": "SZSE Component",
                            "sz399006": "ChiNext"}.get(symbol, symbol))


def format_report(result: dict) -> str:
    surv = sw.survivors(result)
    groups = sw.group_summary(result, uni.group_of)
    naive = [r for r in result["results"] if (r["p_value"] or 1) < 0.05]
    tradeable_surv = [r for r in surv if r["tradeable"]]

    L = [
        "# US → China correlation sweep — research report",
        "",
        f"_Generated {datetime.now(timezone.utc).date().isoformat()}. "
        "Not investment advice._",
        "",
        "## Method",
        "",
        f"- **{result['tests']:,} correlation tests** run: "
        f"{len(result['us_symbols'])} US symbols × {len(result['china_symbols'])} "
        f"China symbols × lags {list(sw.DEFAULT_LAGS)}.",
        f"- Minimum {sw.MIN_PAIRS} paired sessions per test.",
        "- Two-sided p-value via the Fisher z-transform.",
        f"- **Benjamini–Hochberg false-discovery control at q ≤ {result['fdr_q']}**, "
        "applied across the whole sweep.",
        "- **Split-half stability**: history halved, both halves must agree in sign.",
        "- **Tradeability**: lag 0 is reported but never counted as a finding — "
        "China closes ~14h before the US on the same date, so a same-day "
        "correlation cannot be acted on.",
        "",
        "### Why the correction matters",
        "",
        f"At an uncorrected p < 0.05, **{len(naive):,} of {result['tests']:,} tests "
        f"look significant** — but on pure noise a sweep this wide would produce "
        f"roughly {int(result['tests'] * 0.05):,} by chance alone. After FDR control, "
        f"sign-stability and the tradeability filter, **{len(tradeable_surv)} survive**. "
        "That gap is the difference between a screen and a finding.",
        "",
        "## Findings — pairs that survived every filter",
        "",
    ]
    if not tradeable_surv:
        L += ["**None.** No US→China lead survived multiplicity control, sign "
              "stability and the tradeability filter on this history. That is a "
              "real result, not a failed run: it says the same-day co-movement "
              "everyone sees is not accompanied by a dependable *next-day* lead in "
              "this universe.", ""]
    else:
        L += ["| US | → China | lag | r | n | q-value | 1st half | 2nd half | channel |",
              "|---|---|---:|---:|---:|---:|---:|---:|---|"]
        for r in tradeable_surv[:25]:
            L.append(
                f"| {r['us_symbol']} | {_sector_of(r['china_symbol'])} "
                f"({r['china_symbol']}) | {r['lag']} | {r['r']:+.3f} | {r['n']} | "
                f"{r['q_value']:.4f} | {r['split_r_first']:+.2f} | "
                f"{r['split_r_second']:+.2f} | {uni.group_of(r['us_symbol'])} |")
        L.append("")

    L += ["## Hypothesis groups — is it China, or just beta?", "",
          "| group | tests | survivors | rate | best r |", "|---|---:|---:|---:|---:|"]
    for g in groups:
        mark = " *(control)*" if g["group"] in uni.CONTROL_GROUPS else ""
        L.append(f"| {g['group']}{mark} | {g['tests']} | {g['survivors']} | "
                 f"{g['survival_rate'] * 100:.1f}% | {g['best_r']:+.3f} |")
    L += ["",
          "Read the control rows first: if `control_em` (EEM) survives at a similar "
          "rate to the China-specific groups, the 'China signal' is generic "
          "emerging-market beta wearing a China label.",
          "",
          "## Strongest same-day co-movements (NOT tradeable)", "",
          "| US | → China | r | n |", "|---|---|---:|---:|"]
    same_day = sorted([r for r in result["results"] if r["lag"] == 0 and r["significant"]],
                      key=lambda r: -abs(r["r"]))[:8]
    for r in same_day:
        L.append(f"| {r['us_symbol']} | {_sector_of(r['china_symbol'])} | "
                 f"{r['r']:+.3f} | {r['n']} |")
    L += ["",
          "These are the biggest numbers in the sweep and the least useful: the US "
          "session they describe had not happened yet when China closed. They are "
          "listed to be explicit about what the headline correlations actually are.",
          "",
          "## Universe and stated hypotheses", "",
          "| symbol | group | why it might lead China |", "|---|---|---|"]
    for sym in result["us_symbols"]:
        L.append(f"| {sym} | {uni.group_of(sym)} | {uni.rationale_of(sym)} |")
    L += ["", "---", "",
          f"_{config.DISCLAIMER}_"]
    return "\n".join(L)


def main(argv: list[str]) -> int:
    days = 400
    out = "research_report.md"
    fetch = "--no-fetch" not in argv
    pos = [a for a in argv if not a.startswith("-")]
    if pos:
        try:
            days = int(pos[0])
        except ValueError:
            pass
    if "--out" in argv:
        i = argv.index("--out")
        if i + 1 < len(argv):
            out = argv[i + 1]

    result = run(days=days, fetch=fetch)
    text = format_report(result)
    with open(out, "w") as f:
        f.write(text)
    print(f"\nwrote {out} ({len(text.splitlines())} lines)\n")
    print(text[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
