"""Fetch jobs from every connected job site, collapse duplicates and score them."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date

import httpx
from sqlalchemy import Engine
from sqlmodel import Session, select

from app.criteria import Criteria
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
        async with httpx.AsyncClient(transport=transport) as http:
            plugin = PLUGINS[key](credentials_for(service, box), http)
            try:
                await plugin.check()
                record_health(db, service, None)
            except ServiceError as exc:
                record_health(db, service, exc)
        db.refresh(service)
        return service


# ---------- jobs ----------


@dataclass
class FetchSummary:
    new: int = 0
    merged: int = 0  # same job found on another board
    updated: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def message(self) -> str:
        if self.errors and not (self.new or self.updated or self.merged):
            return "Couldn't fetch: " + " ".join(self.errors.values())
        text = f"{self.new} new job{'s' if self.new != 1 else ''}"
        if self.merged:
            text += f", {self.merged} duplicate{'s' if self.merged != 1 else ''} merged"
        text += "."
        if self.errors:
            text += " Problems: " + " ".join(self.errors.values())
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


async def fetch_all(engine: Engine, box: SecretBox, criteria: Criteria, transport=None) -> FetchSummary:
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
                plugin: Plugin = plugin_cls(credentials_for(service, box), http)
                seen: set[str] = set()
                error: ServiceError | None = None
                for profile in criteria.profiles:
                    for query in profile.searches:
                        try:
                            listings = await plugin.search(query, criteria)
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
                    if error:
                        break
                if error:
                    summary.errors[service.name] = str(error)
                    log.warning("%s: %s", service.name, error)
                record_health(db, service, error)
        close_expired(db, today)
    log.info("Fetch done: %s", summary.message())
    return summary


class Fetcher:
    """Runs one fetch at a time, whether from the schedule or the Fetch now button."""

    def __init__(self, engine: Engine, box: SecretBox, criteria_file, transport=None) -> None:
        self.engine = engine
        self.box = box
        self.criteria_file = criteria_file
        self.transport = transport
        self.lock = asyncio.Lock()
        self.last: FetchSummary | None = None

    @property
    def running(self) -> bool:
        return self.lock.locked()

    async def run(self) -> FetchSummary:
        async with self.lock:
            self.last = await fetch_all(self.engine, self.box, self.criteria_file.load(), self.transport)
            return self.last
