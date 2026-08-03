"""Local web-app entrypoint (spec §5).

    python app.py   ->   starts the backend + dashboard on http://localhost:8000

Serves the FastAPI app (API + terminal dashboard). Run the scheduler separately
with `python -m oracle.scheduler` to populate data on the daily cron.
"""
from __future__ import annotations

import os
import threading
import webbrowser

import uvicorn

from oracle import db

PORT = int(os.environ.get("ORACLE_PORT", "8000"))


def main() -> None:
    db.init_db()  # idempotent — ensures tables exist before first request
    if os.environ.get("ORACLE_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    print(f"China Market Oracle dashboard → http://localhost:{PORT}")
    uvicorn.run("oracle.api.server:app", host="127.0.0.1", port=PORT)


if __name__ == "__main__":
    main()
