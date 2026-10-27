"""Server-side login sessions. The cookie holds a random token; the database stores only its hash."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from fastapi import Request, Response
from sqlmodel import Session, delete, select

from app.config import Settings
from app.models import AuthSession, User, utcnow

SESSION_COOKIE = "hub_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db: Session, settings: Settings, response: Response, user: User) -> None:
    token = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(days=settings.session_days)
    db.add(AuthSession(token_hash=_hash(token), user_id=user.id, expires_at=expires))
    db.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def current_user(db: Session, request: Request) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = db.get(AuthSession, _hash(token))
    if row is None:
        return None
    if row.expires_at <= utcnow():
        db.delete(row)
        db.commit()
        return None
    return db.get(User, row.user_id)


def end_session(db: Session, request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        row = db.get(AuthSession, _hash(token))
        if row is not None:
            db.delete(row)
            db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


def end_all_sessions(db: Session, user: User, response: Response) -> None:
    db.exec(delete(AuthSession).where(AuthSession.user_id == user.id))
    db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


def other_session_count(db: Session, request: Request, user: User) -> int:
    token = request.cookies.get(SESSION_COOKIE, "")
    rows = db.exec(
        select(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at > utcnow())
    ).all()
    return sum(1 for r in rows if r.token_hash != _hash(token))
