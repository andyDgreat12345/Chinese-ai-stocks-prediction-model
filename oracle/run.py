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
    "fetch_us_close", "fetch_world_news", "run_analysis",
    "pre_open_refresh", "fetch_china_close", "reflect_and_update",
]


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("usage: python -m oracle.run <job|all>")
        print("jobs :", ", ".join(ORDER))
        return 0

    name = argv[0]
    if name != "all" and name not in REGISTRY:
        print(f"unknown job: {name}")
        print("jobs :", ", ".join(ORDER))
        return 1

    from .db import init_db
    init_db()

    for job in (ORDER if name == "all" else [name]):
        REGISTRY[job]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
