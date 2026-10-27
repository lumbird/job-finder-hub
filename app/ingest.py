"""Fetch jobs from every connected job site, collapse duplicates and score them."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.criteria import Criteria
from app.geo import GeoCache
from app.matching import fingerprint, score_job
from app.models import Job, JobSource, Service, utcnow
from app.security.crypto import SecretBox
from app.services import PLUGINS
from app.services.base import JobSitePlugin, Listing, Plugin, ServiceError

log = logging.getLogger("hub.ingest")


# ---------- service rows ----------


def get_service(db: Session, key: str) -> Service | None:
    return db.exec(select(Service).where(Service.name == key)).first()


def credentials_for(service: Service, box: SecretBox) -> dict[str, str]:
    return json.loads(box.decrypt(service.credentials)) if service.credentials else {}


def save_credentials(db: Session, box: SecretBox, key: str, creds: dict[str, str]) -> Service:
    plugin = PLUGINS[key]
    service = get_service(db, key) or Service(kind=plugin.kind, name=key)
    service.credentials = box.encrypt(json.dumps(creds))
    service.enabled = True
    service.health = "unchecked"
    service.health_detail = None
    db.add(service)
    db.commit()
    db.refresh(service)
    return service


def disconnect(db: Session, key: str) -> None:
    service = get_service(db, key)
    if service is None:
        return
    service.credentials = None
    service.health = "not_connected"
    service.health_detail = None
    db.add(service)
    db.commit()


def record_health(db: Session, service: Service, error: ServiceError | None) -> None:
    service.health = error.health if error else "ok"
    service.health_detail = str(error) if error else None
    service.last_checked = utcnow()
    db.add(service)
    db.commit()


async def check_service(engine: Engine, box: SecretBox, key: str, transport=None) -> Service:
    with Session(engine) as db:
        service = get_service(db, key)
        if calls_left(service) == 0:
            service.health_detail = budget_message(service)
            db.add(service)
            db.commit()
            db.refresh(service)
            return service
        async with httpx.AsyncClient(transport=transport) as http:
            plugin = PLUGINS[key](credentials_for(service, box), http)
            try:
                await plugin.check()
                record_health(db, service, None)
            except ServiceError as exc:
                record_health(db, service, exc)
            record_calls(db, service, plugin.calls)
        db.refresh(service)
        return service


# ---------- call budgets ----------

LIMIT_KEYS = ("every_hours", "per_day", "per_month")


def _config(service: Service) -> dict:
    return json.loads(service.config or "{}")


def limits_for(service: Service) -> dict[str, int]:
    plugin = PLUGINS.get(service.name)
    merged = {**getattr(plugin, "default_limits", {}), **_config(service).get("limits", {})}
    return {k: int(merged.get(k) or 0) for k in LIMIT_KEYS}


def save_limits(db: Session, service: Service, limits: dict[str, int]) -> None:
    config = _config(service)
    config["limits"] = {k: max(0, int(limits.get(k) or 0)) for k in LIMIT_KEYS}
    service.config = json.dumps(config)
    db.add(service)
    db.commit()


def usage_for(service: Service, today: date | None = None) -> dict:
    """Calls made today and this month, and when the last fetch ran (ISO string or None)."""
    today = today or date.today()
    u = _config(service).get("usage", {})
    return {
        "today": u.get("day_calls", 0) if u.get("day") == today.isoformat() else 0,
        "month": u.get("month_calls", 0) if u.get("month") == today.strftime("%Y-%m") else 0,
        "last_fetch": u.get("last_fetch"),
    }


def record_calls(db: Session, service: Service, calls: int, fetched: bool = False, today: date | None = None) -> None:
    today = today or date.today()
    used = usage_for(service, today)
    config = _config(service)
    config["usage"] = {
        "day": today.isoformat(),
        "day_calls": used["today"] + calls,
        "month": today.strftime("%Y-%m"),
        "month_calls": used["month"] + calls,
        "last_fetch": utcnow().isoformat() if fetched else used["last_fetch"],
    }
    service.config = json.dumps(config)
    db.add(service)
    db.commit()


def calls_left(service: Service, today: date | None = None) -> int | None:
    """Calls still allowed today and this month, or None if there's no cap."""
    limits, used = limits_for(service), usage_for(service, today)
    left = [limits[k] - used[u] for k, u in (("per_day", "today"), ("per_month", "month")) if limits[k]]
    return max(0, min(left)) if left else None


def budget_message(service: Service, today: date | None = None) -> str:
    limits, used = limits_for(service), usage_for(service, today)
    label = PLUGINS[service.name].label if service.name in PLUGINS else service.name
    if limits["per_month"] and (used["month"] >= limits["per_month"] or not limits["per_day"]):
        return f"{label} has used all {limits['per_month']} calls allowed this month. Paused until next month."
    return f"{label} has used all {limits['per_day']} calls allowed today. Paused until tomorrow."


def fetch_due(service: Service, now: datetime | None = None) -> bool:
    every = limits_for(service)["every_hours"]
    last = usage_for(service)["last_fetch"]
    if not every or not last:
        return True
    return (now or utcnow()) - datetime.fromisoformat(last) >= timedelta(hours=every) - timedelta(minutes=5)


# ---------- jobs ----------


@dataclass
class FetchSummary:
    new: int = 0
    merged: int = 0  # same job found on another board
    updated: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    paused: dict[str, str] = field(default_factory=dict)  # call budget used up

    def message(self) -> str:
        if self.errors and not (self.new or self.updated or self.merged):
            return "Couldn't fetch: " + " ".join(self.errors.values())
        text = f"{self.new} new job{'s' if self.new != 1 else ''}"
        if self.merged:
            text += f", {self.merged} duplicate{'s' if self.merged != 1 else ''} merged"
        text += "."
        if self.errors:
            text += " Problems: " + " ".join(self.errors.values())
        if self.paused:
            text += " " + " ".join(self.paused.values())
        return text


def _apply_score(job: Job, criteria: Criteria) -> None:
    match = score_job(criteria, job.title, job.description, job.salary_min, job.salary_max)
    job.score, job.profile, job.match_reason = match.score, match.profile, match.reason


def upsert_listing(db: Session, service: Service, listing: Listing, criteria: Criteria) -> str:
    """Store one listing. Returns "new", "merged" or "updated"."""
    source = db.exec(
        select(JobSource).where(JobSource.service_id == service.id, JobSource.external_id == listing.external_id)
    ).first()
    if source is not None:
        job = db.get(Job, source.job_id)
        result = "updated"
    else:
        fp = fingerprint(listing.title, listing.company, listing.location)
        job = db.exec(select(Job).where(Job.fingerprint == fp)).first()
        if job is None:
            job = Job(
                source_service_id=service.id,
                external_id=listing.external_id,
                title=listing.title,
                fingerprint=fp,
            )
            result = "new"
        else:
            result = "merged"

    # Keep the fullest details any board has given us.
    job.company = job.company or listing.company
    job.location = job.location or listing.location
    job.url = job.url or listing.url
    job.salary_min = job.salary_min or listing.salary_min
    job.salary_max = job.salary_max or listing.salary_max
    job.closing_date = listing.closing_date or job.closing_date
    job.posted_at = job.posted_at or listing.posted_at
    if len(listing.description) > len(job.description):
        job.description = listing.description
    job.updated_at = utcnow()
    _apply_score(job, criteria)
    db.add(job)
    db.flush()
    if source is None:
        db.add(JobSource(job_id=job.id, service_id=service.id, external_id=listing.external_id, url=listing.url))
    return result


def close_expired(db: Session, today: date | None = None) -> int:
    today = today or date.today()
    jobs = db.exec(
        select(Job).where(Job.closing_date < today, Job.state.in_(("new", "saved")))  # type: ignore[union-attr]
    ).all()
    for job in jobs:
        job.state = "closed"
        db.add(job)
    db.commit()
    return len(jobs)


def rescore_all(engine: Engine, criteria: Criteria) -> int:
    with Session(engine) as db:
        jobs = db.exec(select(Job)).all()
        for job in jobs:
            _apply_score(job, criteria)
            db.add(job)
        db.commit()
        return len(jobs)


async def fetch_all(
    engine: Engine, box: SecretBox, criteria: Criteria, transport=None, force: bool = False, geo: GeoCache | None = None
) -> FetchSummary:
    """Fetch from every connected job site. Scheduled runs skip sites fetched within their every_hours;
    a forced run (Check now) doesn't, but every run stops at the daily and monthly call caps."""
    summary = FetchSummary()
    today = date.today()
    with Session(engine) as db:
        services = db.exec(
            select(Service).where(Service.kind == "job_site", Service.enabled, Service.credentials.is_not(None))  # type: ignore[union-attr]
        ).all()
        async with httpx.AsyncClient(transport=transport) as http:
            for service in services:
                plugin_cls = PLUGINS.get(service.name)
                if plugin_cls is None or not issubclass(plugin_cls, JobSitePlugin):
                    continue
                if not force and not fetch_due(service):
                    continue
                if calls_left(service, today) == 0:
                    summary.paused[service.name] = budget_message(service, today)
                    continue
                plugin: Plugin = plugin_cls(credentials_for(service, box), http)
                seen: set[str] = set()
                error: ServiceError | None = None
                out_of_calls = False
                # Every search phrase in every area: one call each.
                jobs_to_run = [(q, a) for p in criteria.profiles for q in p.searches for a in (criteria.areas or [None])]
                for query, area in jobs_to_run:
                    left = calls_left(service, today)
                    if left is not None and plugin.calls >= left:
                        out_of_calls = True
                        break
                    try:
                        listings = await plugin.search(query, criteria, area)
                    except ServiceError as exc:
                        error = exc
                        break
                    for listing in listings:
                        if listing.external_id in seen:
                            continue
                        seen.add(listing.external_id)
                        if listing.closing_date and listing.closing_date < today:
                            continue
                        outcome = upsert_listing(db, service, listing, criteria)
                        setattr(summary, outcome, getattr(summary, outcome) + 1)
                    db.commit()
                record_calls(db, service, plugin.calls, fetched=True, today=today)
                if out_of_calls:
                    summary.paused[service.name] = budget_message(service, today)
                if error:
                    summary.errors[service.name] = str(error)
                    log.warning("%s: %s", service.name, error)
                record_health(db, service, error)
            if geo is not None:
                # Place the areas and the jobs still in play, for "N miles from <area>" on the cards.
                places = [a.name for a in criteria.areas] + list(
                    db.exec(select(Job.location).where(Job.state.in_(("new", "saved"))).distinct()).all()  # type: ignore[union-attr]
                )
                await geo.locate(places, http)
        close_expired(db, today)
    log.info("Fetch done: %s", summary.message())
    return summary


class Fetcher:
    """Runs one fetch at a time, whether from the schedule or the Fetch now button."""

    def __init__(self, engine: Engine, box: SecretBox, criteria_file, transport=None, geo: GeoCache | None = None) -> None:
        self.engine = engine
        self.geo = geo
        self.box = box
        self.criteria_file = criteria_file
        self.transport = transport
        self.lock = asyncio.Lock()
        self.last: FetchSummary | None = None

    @property
    def running(self) -> bool:
        return self.lock.locked()

    async def run(self, force: bool = False) -> FetchSummary:
        async with self.lock:
            self.last = await fetch_all(self.engine, self.box, self.criteria_file.load(), self.transport, force, self.geo)
            return self.last
