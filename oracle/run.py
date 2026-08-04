"""Run scheduled jobs on demand — supervised first run / time-shifted testing
(spec §6.6). The scheduler fires these automatically; this is for running them
by hand.

    python -m oracle.run fetch_us_close     # one job
    python -m oracle.run all                # the full daily sequence, in order
    python -m oracle.run --help
"""
from __future__ import annotations

import sys

from .jobs import REGISTRY

# Canonical daily order (matches the CST schedule).
ORDER = [
    "fetch_us_close", "fetch_world_news", "run_analysis", "run_llm_analysis",
    "pre_open_refresh", "fetch_china_close", "reflect_and_update",
]

# Named phases for a two-run automated cadence (e.g. GitHub Actions): the
# morning half produces the day's prediction; the afternoon half (after the
# China close) scores it and reflects.
GROUPS = {
    "morning": ["fetch_us_close", "fetch_world_news", "run_analysis", "run_llm_analysis"],
    "afternoon": ["fetch_china_close", "reflect_and_update"],
    "all": ORDER,
}


def _resolve(name: str) -> list[str] | None:
    if name in GROUPS:
        return GROUPS[name]
    if name in REGISTRY:
        return [name]
    return None


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: python -m oracle.run <job|phase>")
        print("phases:", ", ".join(GROUPS))
        print("jobs  :", ", ".join(ORDER))
        return 0

    jobs = _resolve(argv[0])
    if jobs is None:
        print(f"unknown job/phase: {argv[0]}")
        print("phases:", ", ".join(GROUPS))
        print("jobs  :", ", ".join(ORDER))
        return 1

    from .db import init_db
    init_db()

    for job in jobs:
        REGISTRY[job]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
