"""Build a static dashboard for GitHub Pages (zero-server deployment).

Reuses the FastAPI endpoint functions directly (same serialization) to write
`site/api/<name>.json` snapshots, then copies the dashboard front-end with
relative asset paths and a static-mode flag so it fetches those snapshots
instead of a live backend.

    python -m oracle.site_build [out_dir]     # default out_dir: site
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from .api import server
from .db import init_db

# name -> callable returning the JSON-able payload (defaults match the API).
ENDPOINTS = {
    "prediction": server.prediction,
    "report": server.report,
    "llm-calls": server.llm_calls,
    "llm-usage": server.llm_usage,
    "learning": server.learning,
    "charts": lambda: server.charts(days=90),
    "pairs": lambda: server.pairs(limit=6, days=90),
    "correlation-accumulated": server.correlation_accumulated,
    "proven-hubs": lambda: server.proven_hubs(days=90, limit=6),
    "simulation": server.simulation,
    # Forward evidence + where the edge sits in the bar — the two research
    # results a reader most needs beside the headline accuracy.
    "paper": server.paper_strategy,
    "segments": server.segments,
    # Can the one validated rule be executed, does it hold across regimes, and
    # is its exit the right one — the three questions that decide whether the
    # headline number means anything.
    "execution": server.execution_realism,
    "exit-horizon": server.exit_horizon,
    "regimes": server.regimes,
    "heatmap": server.heatmap,
    "accuracy": server.accuracy,
    "leaderboard": server.leaderboard,
    "weights": server.weights,
    "reflections": lambda: server.reflections(limit=30),
    "markets": server.markets,
    "history": server.history,
    "news-impact": server.news_impact,
    "health": server.health,
}

_DASH = Path(__file__).resolve().parent / "dashboard"


def build(out_dir: str | Path = "site") -> Path:
    init_db()  # ensure tables exist even if the state DB is fresh
    out = Path(out_dir)
    (out / "api").mkdir(parents=True, exist_ok=True)
    (out / "static").mkdir(parents=True, exist_ok=True)

    for name, fn in ENDPOINTS.items():
        (out / "api" / f"{name}.json").write_text(json.dumps(fn(), default=str))

    for asset in ("styles.css", "app.js"):
        shutil.copy(_DASH / "static" / asset, out / "static" / asset)

    # Switch the front-end into static mode (fetch api/<name>.json snapshots).
    (out / "static" / "config.js").write_text("window.CMO_STATIC = true;\n")

    # Rewrite absolute /static/ links to relative so it works under the Pages
    # subpath (https://<user>.github.io/<repo>/).
    html = (_DASH / "index.html").read_text().replace('href="/static/', 'href="static/') \
                                             .replace('src="/static/', 'src="static/')
    (out / "index.html").write_text(html)

    # Tell Pages to serve files verbatim (no Jekyll processing of /static etc.).
    (out / ".nojekyll").write_text("")

    print(f"site_build: wrote static dashboard -> {out}")
    return out


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else "site")
