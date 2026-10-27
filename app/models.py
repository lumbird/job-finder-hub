"""Database tables. Phase 1 covers login only; later phases add the rest of the data model."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    password_hash: str
    totp_secret: str | None = None  # Fernet-encrypted
    totp_enabled: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class PasskeyCredential(SQLModel, table=True):
    __tablename__ = "passkey_credential"

    id: str = Field(primary_key=True)  # base64url credential ID
    user_id: int = Field(foreign_key="user.id", index=True)
    public_key: bytes
    sign_count: int = 0
    name: str
    created_at: datetime = Field(default_factory=utcnow)


class AuthSession(SQLModel, table=True):
    __tablename__ = "auth_session"

    token_hash: str = Field(primary_key=True)  # sha256 of the cookie value
    user_id: int = Field(foreign_key="user.id", index=True)
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime


class LoginThrottle(SQLModel, table=True):
    __tablename__ = "login_throttle"

    key: str = Field(primary_key=True)  # client address
    failures: int = 0
    last_failure_at: datetime | None = None
    locked_until: datetime | None = None
