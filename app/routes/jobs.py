"""Jobs: the daily list (calm mode), all matches, saved, details and actions."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlmodel import Session, select

from app.deps import get_db, require_user, safe_next, templates
from app.matching import STRONG_MATCH
from app.models import Job, JobSource, Service, User
from app.security.csrf import verify_csrf
from app.services import PLUGINS

router = APIRouter(dependencies=[Depends(verify_csrf)])

VIEWS = {
    "today": "Today",
    "new": "All new",
    "saved": "Saved",
    "dismissed": "Dismissed",
    "closed": "Closed",
}
SORTS = {"score": "Best match", "closing": "Closing soonest", "newest": "Newest"}
STATES = ("new", "saved", "dismissed", "closed")
CLOSING_SOON_DAYS = 3


def _order(sort: str):
    if sort == "closing":
        # Jobs without a closing date go last.
        return [Job.closing_date.is_(None), Job.closing_date, Job.score.desc()]  # type: ignore[union-attr]
    if sort == "newest":
        return [Job.first_seen_at.desc()]
    return [Job.score.desc(), Job.first_seen_at.desc()]


def _sources(db: Session, jobs: list[Job]) -> dict[int, list[tuple[str, str]]]:
    """job id → [(board label, link)] for every board the job was seen on."""
    if not jobs:
        return {}
    labels = {s.id: PLUGINS[s.name].label if s.name in PLUGINS else s.name for s in db.exec(select(Service)).all()}
    out: dict[int, list[tuple[str, str]]] = {}
    for src in db.exec(select(JobSource).where(JobSource.job_id.in_([j.id for j in jobs]))).all():  # type: ignore[union-attr]
        out.setdefault(src.job_id, []).append((labels.get(src.service_id, "?"), src.url))
    return out


def card_context(db: Session, job: Job) -> dict:
    return {"job": job, "sources": _sources(db, [job]).get(job.id, []), "today": date.today(), **_helpers()}


def _helpers() -> dict:
    return {"STRONG_MATCH": STRONG_MATCH, "CLOSING_SOON_DAYS": CLOSING_SOON_DAYS}


@router.get("/jobs")
def jobs_page(
    request: Request,
    view: str | None = None,
    sort: str = "score",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    criteria = request.app.state.criteria_file.load()
    if view not in VIEWS:
        view = "today" if criteria.calm_mode_on else "new"
    if sort not in SORTS:
        sort = "score"

    counts = dict(db.exec(select(Job.state, func.count()).group_by(Job.state)).all())
    state = "new" if view == "today" else view
    query = select(Job).where(Job.state == state).order_by(*_order("score" if view == "today" else sort))
    if view == "today":
        query = query.where(Job.score > 0).limit(criteria.daily_cap)
    jobs = list(db.exec(query).all())
    waiting = max(0, counts.get("new", 0) - len(jobs)) if view == "today" else 0

    connected = db.exec(
        select(func.count()).select_from(Service).where(Service.kind == "job_site", Service.credentials.is_not(None))  # type: ignore[union-attr]
    ).one()
    fetcher = request.app.state.fetcher
    return templates.TemplateResponse(
        request,
        "pages/jobs.html",
        {
            "tab": "jobs",
            "jobs": jobs,
            "sources": _sources(db, jobs),
            "view": view,
            "views": VIEWS,
            "sort": sort,
            "sorts": SORTS,
            "counts": counts,
            "waiting": waiting,
            "criteria": criteria,
            "connected": connected,
            "fetch_message": fetcher.last.message() if fetcher.last and request.query_params.get("fetched") else None,
            "fetching": fetcher.running,
            "today": date.today(),
            **_helpers(),
        },
    )


@router.post("/jobs/fetch")
async def fetch_now(request: Request, user: User = Depends(require_user)):
    fetcher = request.app.state.fetcher
    if not fetcher.running:
        await fetcher.run()
    return RedirectResponse("/jobs?fetched=1", status_code=303)


def _get_job(db: Session, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="That job isn't here any more.")
    return job


@router.get("/jobs/{job_id}")
def job_detail(request: Request, job_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    job = _get_job(db, job_id)
    return templates.TemplateResponse(request, "pages/job_detail.html", {"tab": "jobs", **card_context(db, job)})


@router.post("/jobs/{job_id}/state")
def set_state(
    request: Request,
    job_id: int,
    state: str = Form(...),
    next: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if state not in STATES:
        raise HTTPException(status_code=400, detail="Unknown state.")
    job = _get_job(db, job_id)
    previous = job.state
    job.state = state
    db.add(job)
    db.commit()
    if request.headers.get("hx-request"):
        return templates.TemplateResponse(
            request, "_job_card.html", {**card_context(db, job), "previous": previous, "just_changed": True}
        )
    return RedirectResponse(safe_next(next, f"/jobs/{job_id}"), status_code=303)
