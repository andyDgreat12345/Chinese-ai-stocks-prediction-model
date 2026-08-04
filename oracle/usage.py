"""AI research spend meter — read out the DeepSeek/Claude token + cost tracker.

The analyst (and future deep-research passes) record every API call's tokens and
an estimated cost in the ``llm_usage`` table. This prints a compact summary —
today, the last 7 days, and all time, with a per-model breakdown — so cost is
visible before deepening the research. Stdlib-only, so the weekly digest workflow
can run it with no pip install.

    python -m oracle.usage
"""
from __future__ import annotations

import sys

from . import config, db


def format_usage(summary: dict) -> str:
    def line(label: str, s: dict) -> str:
        return (f"  {label:<10} {s['calls']:>5} calls   {s['tokens']:>10,} tokens   "
                f"~${s['cost_usd']:.4f}")

    lines = [
        "AI research spend (DeepSeek/Claude token meter — estimated):",
        line("today", summary["today"]),
        line("last 7d", summary["last_7d"]),
        line("all-time", summary["all_time"]),
    ]
    if summary["by_model"]:
        lines.append("  by model:")
        for m in summary["by_model"]:
            lines.append(
                f"    {m['provider']}/{m['model']:<22} {m['calls']:>5} calls   "
                f"~${m['cost_usd']:.4f}")
    if not summary["all_time"]["calls"]:
        lines.append("  (no AI calls recorded yet — the analyst logs usage once "
                     "ORACLE_ANALYST_PROVIDER + its API key are set)")
    lines.append("  Estimate only; the provider invoice is the source of truth. "
                 + config.DISCLAIMER)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    db.init_db()
    print(format_usage(db.llm_usage_summary()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
