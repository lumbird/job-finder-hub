"""Reporting rules: queue entries the candidate chose, copy, mark reported, undo, weekly summary."""

from __future__ import annotations

from datetime import date, timedelta

from sqlmodel import Session, select

from app.models import Application, CvVersion, Job, JobSource, ReportEntry, Service, utcnow
from app.services import PLUGINS
from app.services.base import Activity, ReportingPlugin

ACTIVITY_TYPES = {
    "applied": "Applied for a job",
    "interview": "Interview",
    "follow_up": "Followed up",
    "cv_update": "Updated CV",
    "course": "Training or course",
    "employer_research": "Researched employers",
    "interview_prep": "Interview preparation",
    "job_search": "Searched for jobs",
    "other": "Other",
}
APPLICATION_ACTIVITIES = ("applied", "interview", "follow_up")
OTHER_ACTIVITIES = tuple(k for k in ACTIVITY_TYPES if k not in APPLICATION_ACTIVITIES)


def reporting_plugins() -> dict[str, type[ReportingPlugin]]:
    return {k: p for k, p in PLUGINS.items() if issubclass(p, ReportingPlugin)}


def reporting_service(db: Session, key: str) -> Service:
    """Reporting targets need no keys, so their Service row is made on first use."""
    if key not in reporting_plugins():
        raise KeyError(key)
    service = db.exec(select(Service).where(Service.name == key)).first()
    if service is None:
        service = Service(kind="reporting", name=key, health="ok")
        db.add(service)
        db.commit()
        db.refresh(service)
    return service


def _plugin(key: str) -> ReportingPlugin:
    return reporting_plugins()[key]({}, None)  # type: ignore[arg-type]


def _activity_for(db: Session, app: Application, activity_type: str) -> Activity:
    job = db.get(Job, app.job_id)
    cv = db.get(CvVersion, app.cv_version_id) if app.cv_version_id else None
    source = db.exec(select(JobSource).where(JobSource.job_id == job.id)).first()
    source_label = ""
    if source:
        service = db.get(Service, source.service_id)
        source_label = PLUGINS[service.name].label if service and service.name in PLUGINS else ""
    when = (app.applied_at.date() if activity_type == "applied" and app.applied_at else date.today())
    return Activity(
        activity_type=activity_type,
        when=when,
        role=job.title,
        company=job.company,
        location=job.location,
        source=source_label,
        cv_label=cv.label if cv else "",
    )


def report_application(db: Session, app: Application, activity_type: str, service_key: str) -> ReportEntry:
    """Queue an application activity for reporting. Choosing it twice doesn't make a duplicate."""
    if activity_type not in APPLICATION_ACTIVITIES:
        raise ValueError(activity_type)
    service = reporting_service(db, service_key)
    existing = db.exec(
        select(ReportEntry).where(
            ReportEntry.application_id == app.id,
            ReportEntry.activity_type == activity_type,
            ReportEntry.reporting_service_id == service.id,
        )
    ).first()
    if existing:
        return existing
    activity = _activity_for(db, app, activity_type)
    entry = ReportEntry(
        reporting_service_id=service.id,
        application_id=app.id,
        activity_type=activity_type,
        activity_date=activity.when,
        text=_plugin(service_key).format_entry(activity),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def log_activity(db: Session, service_key: str, activity_type: str, when: date, detail: str) -> ReportEntry:
    """Work search that isn't an application: CV updates, courses, research, interview prep."""
    if activity_type not in OTHER_ACTIVITIES:
        raise ValueError(activity_type)
    service = reporting_service(db, service_key)
    text = _plugin(service_key).format_entry(Activity(activity_type=activity_type, when=when, detail=detail))
    entry = ReportEntry(
        reporting_service_id=service.id, activity_type=activity_type, activity_date=when, text=text
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def entries_for_application(db: Session, app_id: int) -> dict[tuple[str, str], ReportEntry]:
    """(service key, activity type) → entry, for showing what's already been chosen."""
    rows = db.exec(
        select(ReportEntry, Service)
        .join(Service, ReportEntry.reporting_service_id == Service.id)
        .where(ReportEntry.application_id == app_id)
    ).all()
    return {(service.name, entry.activity_type): entry for entry, service in rows}


def mark_reported(db: Session, entry: ReportEntry) -> None:
    entry.state = "reported"
    entry.reported_at = utcnow()
    db.add(entry)
    db.commit()


def unmark(db: Session, entry: ReportEntry) -> None:
    entry.state = "ready"
    entry.reported_at = None
    db.add(entry)
    db.commit()


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_entries(db: Session, service: Service, monday: date) -> list[ReportEntry]:
    return list(
        db.exec(
            select(ReportEntry)
            .where(
                ReportEntry.reporting_service_id == service.id,
                ReportEntry.activity_date >= monday,
                ReportEntry.activity_date < monday + timedelta(days=7),
            )
            .order_by(ReportEntry.activity_date, ReportEntry.created_at)
        ).all()
    )


def weekly_summary(db: Session, service_key: str, monday: date) -> tuple[str, list[ReportEntry]]:
    service = reporting_service(db, service_key)
    entries = week_entries(db, service, monday)
    text = _plugin(service_key).weekly_text(
        monday, [(e.activity_date, ACTIVITY_TYPES.get(e.activity_type, ""), e.text) for e in entries]
    )
    return text, entries
