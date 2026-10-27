"""Applications, clusters and CV versions: the rules behind phase 3's pages."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from rapidfuzz import fuzz
from sqlmodel import Session, select

from app.matching import normalise_company, normalise_title
from app.models import Application, Cluster, ClusterJob, CvVersion, Job, utcnow

STATUSES = {
    "found": "Found",
    "applied": "Applied",
    "replied": "Replied",
    "interview": "Interview",
    "offer": "Offer",
    "closed": "Closed",
}
GUARD_WINDOW = timedelta(days=183)  # about 6 months
SAME_ROLE = 85  # title similarity (0-100) that counts as the same role at the same company
AGENCY_REPOST = 92  # stricter similarity when the employer name differs

_AGENCY_WORDS = re.compile(
    r"\b(recruit\w*|staffing|personnel|resourcing|talent|search|selection|appointments|employment|agency|consult\w*)\b"
)

DEFAULT_COVER_NOTE = """\
Dear {company} hiring team,

I'm applying for the {role} role. [One or two sentences on why this kind of role suits you.]

[One sentence on relevant experience or a project you're proud of.]

I'd welcome the chance to talk. My CV is attached.

Kind regards,
[Your name]
"""


# ---------- already-applied guard ----------


@dataclass
class GuardWarning:
    application: Application
    job: Job
    reason: str


def looks_like_agency(company: str) -> bool:
    return bool(_AGENCY_WORDS.search(company.lower()))


def _town(location: str) -> str:
    return location.lower().split(",")[0].strip()


def already_applied(db: Session, job: Job) -> list[GuardWarning]:
    """Applications from the last 6 months that look like this same job, including agency reposts."""
    since = utcnow() - GUARD_WINDOW
    rows = db.exec(
        select(Application, Job)
        .join(Job, Application.job_id == Job.id)
        .where(Application.job_id != job.id, Application.created_at >= since)
    ).all()
    company = normalise_company(job.company)
    title = normalise_title(job.title)
    warnings = []
    for app, other in rows:
        similarity = fuzz.token_sort_ratio(title, normalise_title(other.title))
        other_company = normalise_company(other.company)
        when = (app.applied_at or app.created_at).strftime("%-d %b")
        if company and company == other_company and similarity >= SAME_ROLE:
            warnings.append(GuardWarning(app, other, f"You started an application for {other.title} at {other.company} on {when}."))
        elif (
            company != other_company
            and (looks_like_agency(job.company) or looks_like_agency(other.company))
            and similarity >= AGENCY_REPOST
            and _town(job.location) == _town(other.location)
        ):
            warnings.append(
                GuardWarning(
                    app,
                    other,
                    f"This may be the same role as {other.title} at {other.company} ({when}), posted again by an agency.",
                )
            )
    return warnings


# ---------- cover notes ----------


def fill_note(template: str, job: Job) -> str:
    """Fill {company}, {role} and {location}; anything else in braces is left alone."""
    values = {"company": job.company or "your company", "role": job.title, "location": job.location}
    return re.sub(r"\{(company|role|location)\}", lambda m: values[m.group(1)], template)


# ---------- applications ----------


def application_for(db: Session, job_id: int) -> Application | None:
    return db.exec(select(Application).where(Application.job_id == job_id)).first()


def default_cv(db: Session) -> CvVersion | None:
    return db.exec(select(CvVersion).where(CvVersion.is_default)).first() or db.exec(
        select(CvVersion).order_by(CvVersion.created_at.desc())  # type: ignore[attr-defined]
    ).first()


def start_application(db: Session, job: Job, cluster: Cluster | None = None) -> Application:
    existing = application_for(db, job.id)
    if existing:
        return existing
    cv = db.get(CvVersion, cluster.cv_version_id) if cluster and cluster.cv_version_id else default_cv(db)
    template = cluster.cover_note_template if cluster else ""
    app = Application(
        job_id=job.id,
        cluster_id=cluster.id if cluster else None,
        cv_version_id=cv.id if cv else None,
        cover_note=fill_note(template, job) if template else "",
    )
    if job.state in ("new", "dismissed"):
        job.state = "saved"
        db.add(job)
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def set_status(db: Session, app: Application, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(status)
    app.status = status
    if status == "applied" and app.applied_at is None:
        app.applied_at = utcnow()
    app.updated_at = utcnow()
    db.add(app)
    db.commit()


# ---------- clusters ----------


def cluster_candidates(db: Session) -> dict[str, list[Job]]:
    """Saved jobs not yet applied for or in a cluster, grouped by their best-fit profile."""
    taken = set(db.exec(select(Application.job_id)).all()) | set(db.exec(select(ClusterJob.job_id)).all())
    jobs = db.exec(select(Job).where(Job.state == "saved").order_by(Job.score.desc())).all()  # type: ignore[attr-defined]
    groups: dict[str, list[Job]] = {}
    for job in jobs:
        if job.id not in taken:
            groups.setdefault(job.profile or "Other", []).append(job)
    return groups


def create_cluster(db: Session, profile: str) -> Cluster | None:
    jobs = cluster_candidates(db).get(profile, [])
    if not jobs:
        return None
    cv = default_cv(db)
    cluster = Cluster(
        name=profile, profile=profile, cover_note_template=DEFAULT_COVER_NOTE, cv_version_id=cv.id if cv else None
    )
    db.add(cluster)
    db.flush()
    for job in jobs:
        db.add(ClusterJob(cluster_id=cluster.id, job_id=job.id))
    db.commit()
    db.refresh(cluster)
    return cluster


def cluster_jobs(db: Session, cluster: Cluster) -> list[tuple[ClusterJob, Job]]:
    return list(
        db.exec(
            select(ClusterJob, Job)
            .join(Job, ClusterJob.job_id == Job.id)
            .where(ClusterJob.cluster_id == cluster.id)
            .order_by(Job.score.desc())  # type: ignore[attr-defined]
        ).all()
    )


def approve_cluster(db: Session, cluster: Cluster) -> None:
    """Approving turns every job into an Application (status Found) and opens the submit queue."""
    for _, job in cluster_jobs(db, cluster):
        app = start_application(db, job, cluster)
        if app.cluster_id is None:  # started on its own earlier; leave its note alone
            continue
        app.cv_version_id = cluster.cv_version_id
        app.cover_note = fill_note(cluster.cover_note_template, job)
        db.add(app)
    cluster.state = "approved"
    db.add(cluster)
    db.commit()


def next_in_queue(db: Session, cluster: Cluster) -> tuple[ClusterJob, Job] | None:
    for link, job in cluster_jobs(db, cluster):
        if link.queue_state == "waiting":
            return link, job
    return None


def mark_queue(db: Session, cluster: Cluster, job_id: int, outcome: str) -> None:
    link = db.get(ClusterJob, (cluster.id, job_id))
    if link is None or outcome not in ("applied", "skipped"):
        raise ValueError(outcome)
    link.queue_state = outcome
    db.add(link)
    if outcome == "applied":
        app = application_for(db, job_id)
        if app:
            set_status(db, app, "applied")
    db.commit()
    if next_in_queue(db, cluster) is None:
        cluster.state = "done"
        db.add(cluster)
        db.commit()


# ---------- CV files ----------

CV_EXTENSIONS = {".pdf", ".docx", ".doc", ".odt", ".txt"}
CV_MAX_BYTES = 5 * 1024 * 1024


class CvUploadError(ValueError):
    pass


def store_cv(cv_dir: Path, filename: str, data: bytes) -> str:
    """Validate and save an uploaded CV; returns the stored file name."""
    ext = Path(filename).suffix.lower()
    if ext not in CV_EXTENSIONS:
        raise CvUploadError("Upload a PDF, Word, OpenDocument or text file.")
    if not data:
        raise CvUploadError("That file is empty.")
    if len(data) > CV_MAX_BYTES:
        raise CvUploadError("That file is over 5 MB.")
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        raise CvUploadError("That doesn't look like a real PDF.")
    if ext in (".docx", ".odt") and not data.startswith(b"PK"):
        raise CvUploadError(f"That doesn't look like a real {ext} file.")
    cv_dir.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(8) + ext
    path = cv_dir / name
    path.write_bytes(data)
    path.chmod(0o600)
    return name
