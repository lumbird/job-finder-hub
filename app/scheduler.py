"""In-process schedule: fetch jobs every N minutes and back up the database nightly."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func
from sqlmodel import Session, select

from app.models import Service

log = logging.getLogger("hub.scheduler")

KEEP_BACKUPS = 14


def backup_database(db_path: Path, backup_dir: Path, keep: int = KEEP_BACKUPS) -> Path:
    """Copy the live SQLite file safely (online backup API) and keep the newest `keep` copies."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"hub-{datetime.now():%Y-%m-%d}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    target.chmod(0o600)
    for old in sorted(backup_dir.glob("hub-*.db"))[:-keep]:
        old.unlink()
    log.info("Backed up database to %s", target)
    return target


def _first_fetch_at(engine, every: timedelta) -> datetime:
    """Pick up the schedule where it left off, so restarts don't trigger a fetch every time."""
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        oldest = db.exec(
            select(func.min(Service.last_checked)).where(Service.kind == "job_site", Service.credentials.is_not(None))  # type: ignore[union-attr]
        ).one()
    if oldest is None:
        return now + timedelta(seconds=30)
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    return max(now + timedelta(seconds=30), oldest + every)


def start_scheduler(app) -> AsyncIOScheduler:
    state = app.state
    every = timedelta(minutes=state.criteria_file.load().fetch_every_minutes)
    scheduler = AsyncIOScheduler(timezone="Europe/London")
    scheduler.add_job(
        state.fetcher.run,
        "interval",
        minutes=every.total_seconds() / 60,
        next_run_time=_first_fetch_at(state.engine, every),
        id="fetch_jobs",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        backup_database,
        "cron",
        hour=3,
        minute=30,
        args=[state.settings.db_path, state.settings.data_dir / "backups"],
        id="nightly_backup",
    )
    scheduler.start()
    log.info("Scheduler started: fetching every %d minutes, backup nightly at 03:30.", every.total_seconds() // 60)
    return scheduler


def reschedule_fetch(app, minutes: int) -> None:
    scheduler: AsyncIOScheduler | None = getattr(app.state, "scheduler", None)
    if scheduler is not None and scheduler.get_job("fetch_jobs"):
        scheduler.reschedule_job("fetch_jobs", trigger="interval", minutes=minutes)
