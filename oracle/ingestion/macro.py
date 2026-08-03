"""Macro-event calendar ingestion (spec §3, §4.3).

Scheduled macro events (Fed decisions, CPI, PMI, trade data) often dominate over
pure correlation, so they get a manual dominance weight and flag the day.

v1 is a **hand-maintained JSON calendar** — reliable, offline, and matches the
spec's "manual weight" framing. Copy `examples/macro_events.sample.json` to
`data/macro_events.json` and keep it current. A live akshare / Trading Economics
RSS feed is a future upgrade; the parse/normalize split below keeps that a
drop-in swap for `load_from_file`.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import config, db


def normalize_macro(events: list[dict], default_weight: float = 1.0) -> list[dict]:
    """Coerce raw calendar entries into macro_events rows. Pure/testable."""
    rows = []
    for e in events:
        date = e.get("event_date") or e.get("date")
        if not date:
            continue
        try:
            weight = float(e.get("weight", default_weight))
        except (TypeError, ValueError):
            weight = default_weight
        rows.append({
            "event_date": str(date),
            "category": e.get("category"),
            "description": e.get("description") or e.get("event") or "",
            "weight": weight,
            "notes": e.get("notes"),
        })
    return rows


def load_from_file(path=None) -> list[dict]:
    """Read + normalize the JSON calendar file (list, or {"events": [...]})."""
    path = Path(path or config.MACRO_CALENDAR_FILE)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"fetch_macro_calendar: unreadable calendar file: {e!r}")
        return []
    if isinstance(data, dict):
        data = data.get("events", [])
    return normalize_macro(data)


def fetch_macro_calendar() -> int:
    """Job step (runs within fetch_world_news): load the macro calendar into the
    DB, deduped. Returns the number of new events written. Never raises."""
    try:
        rows = load_from_file()
        n = db.insert_macro_events(rows)
        print(f"fetch_macro_calendar: loaded {n} new macro event(s)")
        return n
    except Exception as e:  # noqa: BLE001
        print(f"fetch_macro_calendar FAILED: {e!r}")
        return 0
