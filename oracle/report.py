"""Daily action report — the plain-language "what to lean toward, what to avoid".

Everything upstream already produces the raw signal: ``run_analysis`` writes the
rule-based per-sector direction/confidence, and (when enabled) ``run_llm_analysis``
writes the DeepSeek analyst's per-sector call with conviction, key drivers, and
the foreign-tradeable ETF each maps to. This module is the *last mile*: right
after the US 4 pm close, it merges those two reads into a single human-readable
outlook for the coming China session and sorts each sector into:

  * **lean constructive** — the read points up; a candidate to research/consider,
  * **lean cautious**     — the read points down; a reason to hold off / avoid adding,
  * **mixed / flat**      — the two engines disagree, or the call is neutral.

Where the rule-based model and the AI analyst AGREE, that's flagged as higher
conviction; where they DISAGREE, the sector is demoted to "watch" rather than
pretending an edge exists. This is deliberately honest: a confident-looking call
that only one engine supports is weaker than one both support.

The merge/format functions are pure (operate on record dicts) so they unit-test
without a DB. ``run_report`` does the DB read around them, and the workflow pipes
the markdown into a GitHub Issue that GitHub emails to the user.

**Not investment advice.** Every line is a probabilistic lean for the reader to
weigh and size to their own risk — never a buy/sell instruction, never a
guarantee, and nothing here is auto-executed (see ``config.DISCLAIMER``).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from . import config, db
from .analysis.pipeline import CHINA_SECTORS

# Pretty sector labels for the report.
_SECTOR_LABEL = {
    "broad": "Broad market",
    "growth": "Growth / internet",
    "semis": "Semiconductors",
    "energy": "Energy",
    "financials": "Financials",
}

_DIR_ARROW = {"bullish": "▲ up", "bearish": "▼ down", "neutral": "► flat"}
_CONV_RANK = {"high": 3, "med": 2, "low": 1}


# ── pure merge ────────────────────────────────────────────────────────────
def _drivers_from(call: dict | None) -> list[str]:
    if not call:
        return []
    raw = call.get("key_drivers")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raw = [raw]
    return [str(d) for d in (raw or [])][:4]


def merge_sector(sector: str, pred: dict | None, call: dict | None) -> dict | None:
    """Merge one sector's rule-based prediction and AI-analyst call into a single
    stance. Returns None when neither engine has anything to say. Pure.

    ``stance`` ∈ {consider, avoid, watch}; ``agree`` is True only when both
    engines ran and pointed the same way."""
    rule_dir = pred.get("direction") if pred else None
    rule_conf = pred.get("confidence") if pred else None
    llm_dir = call.get("direction") if call else None
    llm_conv = call.get("conviction") if call else None
    if rule_dir is None and llm_dir is None:
        return None

    both = rule_dir is not None and llm_dir is not None
    agree = both and rule_dir == llm_dir

    if both and not agree:
        consensus = None                      # genuine disagreement → no edge
    else:
        consensus = rule_dir or llm_dir       # agreement, or whichever ran

    # conviction: stronger of the two when they agree; else the lone source's.
    if agree:
        conv = max(rule_conf, llm_conv, key=lambda c: _CONV_RANK.get(c, 0))
        source = "rule + AI agree"
    elif consensus is not None and llm_dir is not None and rule_dir is None:
        conv, source = llm_conv, "AI analyst"
    elif consensus is not None and rule_dir is not None and llm_dir is None:
        conv, source = rule_conf, "rule-based"
    elif consensus is not None:               # both ran but only via agreement path
        conv, source = rule_conf or llm_conv, "rule-based"
    else:                                     # mixed
        conv, source = "low", "rule vs AI split"

    stance = ("consider" if consensus == "bullish"
              else "avoid" if consensus == "bearish"
              else "watch")

    # A small conviction bump when both engines independently agree.
    rank = _CONV_RANK.get(conv, 1) + (1 if agree else 0)

    drivers = _drivers_from(call)
    rationale = (call or {}).get("rationale") or (pred or {}).get("rationale") or ""
    return {
        "sector": sector,
        "label": _SECTOR_LABEL.get(sector, sector),
        "etf": config.SECTOR_TRADEABLE_ETF.get(sector),
        "consensus": consensus,          # bullish | bearish | neutral | None(=mixed)
        "rule_dir": rule_dir,
        "llm_dir": llm_dir,
        "conviction": conv,
        "source": source,
        "agree": agree,
        "stance": stance,
        "rank": rank,
        "drivers": drivers,
        "rationale": str(rationale)[:400],
        "pick": _pick_from(call),
    }


def _pick_from(call: dict | None):
    """Parse the AI analyst's single-name pick (stored as a JSON string)."""
    raw = (call or {}).get("top_pick")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    return raw if isinstance(raw, dict) and raw.get("ticker") else None


def _us_summary(us_rows: list[dict]) -> str:
    moves = [r["pct_change"] for r in us_rows if r.get("pct_change") is not None]
    if not moves:
        return "US tape: no US closes ingested for this date yet."
    up = sum(1 for m in moves if m > 0)
    avg = sum(moves) / len(moves)
    return (f"US tape: {up} of {len(moves)} tracked US gauges closed higher "
            f"(avg {avg:+.2f}%).")


def build_report(trade_date: str, predictions: list[dict], llm_calls: list[dict],
                 us_rows: list[dict] | None = None,
                 technicals: dict | None = None, divergence: dict | None = None) -> dict:
    """Assemble the full daily outlook from the day's rule-based predictions and
    AI-analyst calls, plus the per-sector technical snapshot and US-link label. Pure."""
    from .analysis.divergence import summary_line

    preds = {p["sector"]: p for p in predictions}
    calls = {c["sector"]: c for c in llm_calls}
    techs = technicals or {}
    divg = divergence or {}
    merged = []
    for sector in CHINA_SECTORS:
        m = merge_sector(sector, preds.get(sector), calls.get(sector))
        if m:
            t = techs.get(sector) or {}
            m["tech"] = t.get("technical_note")
            m["us_link"] = summary_line(divg[sector]) if sector in divg else None
            merged.append(m)

    def _sorted(items):
        return sorted(items, key=lambda m: (-m["rank"], not m["agree"], m["label"]))

    return {
        "trade_date": trade_date,
        "us_summary": _us_summary(us_rows or []),
        "consider": _sorted([m for m in merged if m["stance"] == "consider"]),
        "avoid": _sorted([m for m in merged if m["stance"] == "avoid"]),
        "watch": _sorted([m for m in merged if m["stance"] == "watch"]),
        "analyst_enabled": bool(llm_calls),
        "n_sectors": len(merged),
    }


# ── rendering ─────────────────────────────────────────────────────────────
def _line(m: dict) -> list[str]:
    if m["consensus"] is None:
        head = (f"- **{m['label']}** (proxy {m['etf']}) — mixed: "
                f"rule {_DIR_ARROW.get(m['rule_dir'], '—')} / "
                f"AI {_DIR_ARROW.get(m['llm_dir'], '—')} → no clear edge")
    else:
        head = (f"- **{m['label']}** (proxy {m['etf']}) — "
                f"{_DIR_ARROW.get(m['consensus'], m['consensus'])}, "
                f"{m['conviction']} conviction ({m['source']})")
    out = [head]
    if m.get("pick"):
        p = m["pick"]
        trad = f", tradeable {p['tradeable']}" if p.get("tradeable") else ""
        note = f" — {p['note']}" if p.get("note") else ""
        out.append(f"  - name to watch: {p.get('name', p['ticker'])} ({p['ticker']}{trad}){note}")
    if m.get("tech"):
        out.append(f"  - technicals: {m['tech']}")
    if m.get("us_link"):
        out.append(f"  - US link: {m['us_link']}")
    if m["drivers"]:
        out.append(f"  - drivers: {', '.join(m['drivers'])}")
    if m["rationale"]:
        out.append(f"  - {m['rationale']}")
    return out


def format_markdown(report: dict) -> str:
    r = report
    lines = [
        f"# China Market Oracle — daily outlook ({r['trade_date']})",
        "",
        "_For the upcoming China A-share session, from tonight's US close + "
        "overnight world news._",
        "",
        f"> {r['us_summary']}",
        "",
    ]
    lines += ["## ✅ Leaning constructive — candidates to research / consider"]
    if r["consider"]:
        for m in r["consider"]:
            lines += _line(m)
    else:
        lines.append("- (nothing leans clearly constructive today)")
    lines += ["", "## ⛔ Leaning cautious — reasons to hold off / avoid adding"]
    if r["avoid"]:
        for m in r["avoid"]:
            lines += _line(m)
    else:
        lines.append("- (nothing leans clearly cautious today)")
    if r["watch"]:
        lines += ["", "## 👀 Mixed or flat — watch, no clear edge"]
        for m in r["watch"]:
            lines += _line(m)
    lines += [
        "",
        "---",
        "**How to read this.** *Leaning constructive* = the combined analysis "
        "points to that sector rising in the coming session — a candidate to "
        "research and size to your own risk, not a purchase order. *Leaning "
        "cautious* = the read points down — a reason to hold off, not a short "
        "instruction. Conviction is highest when the rule-based model and the AI "
        "analyst independently agree; a *mixed* sector means they split, so "
        "there's no dependable edge. The proxy (ASHR / KWEB / FXI) is the "
        "foreign-tradeable ETF each A-share sector call approximates.",
    ]
    if not r["analyst_enabled"]:
        lines += [
            "",
            "> ℹ️ The AI analyst is **not enabled**, so this is the rule-based read "
            "alone. Turn it on for the buy/avoid conviction to reflect both "
            "engines: set repo variable `ORACLE_ANALYST_PROVIDER=deepseek` + "
            "secret `DEEPSEEK_API_KEY`.",
        ]
    lines += ["", f"_{config.DISCLAIMER}_"]
    return "\n".join(lines)


def format_text(report: dict) -> str:
    """Plain-text (console) rendering — markdown with the decoration stripped."""
    md = format_markdown(report)
    return md.replace("# ", "").replace("**", "").replace("_", "").replace("> ", "")


# ── DB entrypoint ─────────────────────────────────────────────────────────
def run_report(trade_date: str | None = None, db_path=None) -> dict:
    """Read the day's predictions + AI calls and build the outlook report."""
    db.init_db(db_path)   # self-heal schema on a restored older state DB
    trade_date = trade_date or datetime.now(timezone.utc).date().isoformat()
    preds = db.predictions_for_date(trade_date, db_path)
    calls = db.llm_calls_for_date(trade_date, db_path)
    # Fall back to the most recent predicted date if today has nothing yet
    # (e.g. a manual run before the morning jobs fire).
    if not preds and not calls:
        preds = db.latest_predictions(db_path)
        calls = db.latest_llm_calls(db_path)
        if preds:
            trade_date = preds[0]["trade_date"]
        elif calls:
            trade_date = calls[0]["trade_date"]
    us_rows = db.get_rows_for_date("us_close", trade_date, db_path)
    # Per-sector technical snapshot from each sector's own price history.
    from .analysis import technicals as ta
    techs = {}
    for sector in CHINA_SECTORS:
        symbol = config.CHINA_SECTOR_ETFS.get(sector)
        if not symbol:
            continue
        series = [r["close"] for r in
                  db.close_series("china_close", symbol=symbol, limit=120, end=trade_date,
                                  db_path=db_path)
                  if r["close"] is not None]
        if len(series) >= 2:
            techs[sector] = ta.compute_indicators(series)
    # US-follows-vs-diverges label per sector (measured over history).
    from .analysis.divergence import classify_sectors
    try:
        divg = classify_sectors(end=trade_date, db_path=db_path)
    except Exception:
        divg = {}
    return build_report(trade_date, preds, calls, us_rows, technicals=techs, divergence=divg)


def main(argv: list[str]) -> int:
    # Usage: python -m oracle.report [trade_date] [--md]
    as_md = "--md" in argv
    date_args = [a for a in argv if not a.startswith("-")]
    report = run_report(date_args[0] if date_args else None)
    print(format_markdown(report) if as_md else format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
