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


# ---------- Applications and clusters (phase 3) ----------


class CvVersion(SQLModel, table=True):
    __tablename__ = "cv_version"

    id: int | None = Field(default=None, primary_key=True)
    label: str
    file_path: str  # relative to data/cvs
    original_name: str
    is_default: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Cluster(SQLModel, table=True):
    """Similar saved jobs applied for together with one cover note template and one CV."""

    id: int | None = Field(default=None, primary_key=True)
    name: str
    profile: str = ""  # criteria profile it was built from
    cover_note_template: str = ""
    cv_version_id: int | None = Field(default=None, foreign_key="cv_version.id")
    state: str = "draft"  # draft, approved, done
    created_at: datetime = Field(default_factory=utcnow)


class ClusterJob(SQLModel, table=True):
    __tablename__ = "cluster_job"

    cluster_id: int = Field(foreign_key="cluster.id", primary_key=True)
    job_id: int = Field(foreign_key="job.id", primary_key=True)
    queue_state: str = "waiting"  # waiting, applied, skipped (the submit queue)


class Application(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    job_id: int = Field(foreign_key="job.id", unique=True)
    cluster_id: int | None = Field(default=None, foreign_key="cluster.id", index=True)
    cv_version_id: int | None = Field(default=None, foreign_key="cv_version.id")
    cover_note: str = ""
    status: str = Field(default="found", index=True)  # found, applied, replied, interview, offer, closed
    applied_at: datetime | None = None
    follow_up_on: date | None = None
    notes: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


# ---------- Reporting (phase 5) ----------


class ReportEntry(SQLModel, table=True):
    """One work-search activity the candidate chose to report. Nothing is reported without a tap."""

    __tablename__ = "report_entry"

    id: int | None = Field(default=None, primary_key=True)
    reporting_service_id: int = Field(foreign_key="service.id", index=True)
    application_id: int | None = Field(default=None, foreign_key="application.id", index=True)
    activity_type: str  # applied, interview, follow_up, cv_update, course, employer_research, interview_prep, job_search, other
    activity_date: date
    text: str
    state: str = Field(default="ready", index=True)  # ready, reported
    created_at: datetime = Field(default_factory=utcnow)
    reported_at: datetime | None = None
    evidence_email_id: int | None = None  # becomes a foreign key when emails arrive in phase 4
