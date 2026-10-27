"""Login throttling: slow down after 5 failures, lock for 15 minutes after 10."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlmodel import Session

from app.models import LoginThrottle, utcnow

SLOW_AFTER = 5
LOCK_AFTER = 10
LOCK_FOR = timedelta(minutes=15)
FORGET_AFTER = timedelta(minutes=15)  # failures older than this no longer count
MAX_DELAY_SECONDS = 16


class LockedOut(Exception):
    def __init__(self, minutes: int) -> None:
        super().__init__(f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.")
        self.minutes = minutes


def _row(db: Session, key: str) -> LoginThrottle | None:
    row = db.get(LoginThrottle, key)
    if row is None:
        return None
    now = utcnow()
    expired_lock = row.locked_until is not None and row.locked_until <= now
    stale = row.locked_until is None and row.last_failure_at and now - row.last_failure_at > FORGET_AFTER
    if expired_lock or stale:
        db.delete(row)
        db.commit()
        return None
    return row


def delay_for(failures: int) -> float:
    if failures < SLOW_AFTER:
        return 0
    return min(2 ** (failures - SLOW_AFTER), MAX_DELAY_SECONDS)


async def before_attempt(db: Session, key: str, sleep=asyncio.sleep) -> None:
    """Raise LockedOut if locked; otherwise wait out any slow-down delay."""
    row = _row(db, key)
    if row is None:
        return
    if row.locked_until is not None:
        remaining = row.locked_until - utcnow()
        raise LockedOut(max(1, -(-int(remaining.total_seconds()) // 60)))
    delay = delay_for(row.failures)
    if delay:
        await sleep(delay)


def record_failure(db: Session, key: str) -> None:
    row = _row(db, key) or LoginThrottle(key=key)
    row.failures += 1
    row.last_failure_at = utcnow()
    if row.failures >= LOCK_AFTER:
        row.locked_until = utcnow() + LOCK_FOR
    db.add(row)
    db.commit()


def record_success(db: Session, key: str) -> None:
    row = db.get(LoginThrottle, key)
    if row is not None:
        db.delete(row)
        db.commit()
