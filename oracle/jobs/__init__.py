"""Scheduled jobs (spec §2). Phase-1 stubs — real logic lands in Phase 2+.

Each job is a plain callable with no arguments so it can be registered with
APScheduler and also invoked manually for time-shifted mock runs (spec §6.6).
"""
from __future__ import annotations

from datetime import datetime, timezone


def _stamp(name: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] job '{name}' fired (stub)")


def fetch_us_close() -> None:
    """04:15 CST — pull S&P/Nasdaq/Dow closes + sector ETF performance (§3)."""
    _stamp("fetch_us_close")


def fetch_world_news() -> None:
    """04:30 CST — pull overnight RSS headlines + first paragraph (§3)."""
    _stamp("fetch_world_news")


def run_analysis() -> None:
    """05:00 CST — combine signals into a prediction (§4). Retrieves recent
    reflection-log entries first (§4b-iii) before producing the day's call."""
    _stamp("run_analysis")


def pre_open_refresh() -> None:
    """09:15 CST — re-check 05:00–09:15 breaking news, adjust confidence."""
    _stamp("pre_open_refresh")


def fetch_china_close() -> None:
    """15:05 CST — log actual China close, compare vs morning prediction."""
    _stamp("fetch_china_close")


def reflect_and_update() -> None:
    """15:15 CST — self-improvement pass (§4b, TOP PRIORITY): score prediction,
    update correlation leaderboard + news-impact table, write reflection log."""
    _stamp("reflect_and_update")


REGISTRY = {
    "fetch_us_close": fetch_us_close,
    "fetch_world_news": fetch_world_news,
    "run_analysis": run_analysis,
    "pre_open_refresh": pre_open_refresh,
    "fetch_china_close": fetch_china_close,
    "reflect_and_update": reflect_and_update,
}
