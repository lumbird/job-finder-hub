"""Database tables, added phase by phase (see docs/product-spec.md, Data model)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import UniqueConstraint
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


# ---------- Services (phase 2) ----------


class Service(SQLModel, table=True):
    """A connected plug-in: a job site, email account or reporting target."""

    id: int | None = Field(default=None, primary_key=True)
    kind: str  # email, job_site, reporting
    name: str = Field(unique=True)  # plug-in key, e.g. "reed"
    config: str = "{}"  # JSON, non-secret settings
    credentials: str | None = None  # Fernet-encrypted JSON
    enabled: bool = True
    health: str = "not_connected"  # ok, broken, needs_login, rate_limited, not_connected
    health_detail: str | None = None
    last_checked: datetime | None = None


# ---------- Jobs (phase 2) ----------


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    source_service_id: int = Field(foreign_key="service.id", index=True)
    external_id: str
    title: str
    company: str = ""
    location: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    url: str = ""
    description: str = ""
    closing_date: date | None = None
    posted_at: datetime | None = None
    fingerprint: str = Field(index=True)  # same job on several boards collapses to one row
    score: int = 0
    match_reason: str = ""
    profile: str = ""  # which criteria profile it matched best
    state: str = Field(default="new", index=True)  # new, saved, dismissed, closed
    first_seen_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class JobSource(SQLModel, table=True):
    """Every board a job was seen on, including the one in job.source_service_id."""

    __tablename__ = "job_source"
    __table_args__ = (UniqueConstraint("service_id", "external_id"),)

    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", index=True)
    service_id: int = Field(foreign_key="service.id")
    external_id: str
    url: str = ""
