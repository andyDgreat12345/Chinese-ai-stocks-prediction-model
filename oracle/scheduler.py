"""APScheduler wiring (spec §2, §6.6).

Cron triggers pinned to CST. A persistent SQLite job store lets the schedule
survive restarts (spec §2). Jobs are registered from the config table so the
schedule has a single source of truth.
"""
from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.triggers.cron import CronTrigger

from . import config
from .jobs import REGISTRY


def build_scheduler() -> BlockingScheduler:
    jobstores = {
        "default": SQLAlchemyJobStore(url=f"sqlite:///{config.DB_PATH}")
    }
    sched = BlockingScheduler(jobstores=jobstores, timezone=config.TIMEZONE)

    for name, (hour, minute) in config.JOB_SCHEDULE.items():
        func = REGISTRY[name]
        sched.add_job(
            func,
            trigger=CronTrigger(hour=hour, minute=minute, timezone=config.TIMEZONE),
            id=name,
            replace_existing=True,
            misfire_grace_time=3600,  # market-data jobs can flake; tolerate lateness
        )
    return sched


def main() -> None:
    from .db import init_db

    init_db()
    sched = build_scheduler()
    print("China Market Oracle scheduler starting. Registered jobs (CST):")
    for name, (h, m) in config.JOB_SCHEDULE.items():
        print(f"  {h:02d}:{m:02d}  {name}")
    print(config.DISCLAIMER)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("Scheduler stopped.")


if __name__ == "__main__":
    main()
