"""Applications (status board), clusters (bulk applying) and CV versions."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import func
from sqlmodel import Session, select

from app import applications as apps
from app.deps import get_db, require_user, templates
from app.models import Application, Cluster, ClusterJob, CvVersion, Job, User, utcnow
from app.security.csrf import verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _get(db: Session, model, key, what: str):
    row = db.get(model, key)
    if row is None:
        raise HTTPException(status_code=404, detail=f"That {what} isn't here any more.")
    return row


def _cvs(db: Session) -> list[CvVersion]:
    return list(db.exec(select(CvVersion).order_by(CvVersion.created_at.desc())).all())  # type: ignore[attr-defined]


# ---------- applications ----------


@router.get("/applications")
def board(request: Request, status: str = "active", user: User = Depends(require_user), db: Session = Depends(get_db)):
    counts = dict(db.exec(select(Application.status, func.count()).group_by(Application.status)).all())
    query = select(Application, Job).join(Job, Application.job_id == Job.id)
    if status == "active":
        query = query.where(Application.status != "closed")
    elif status in apps.STATUSES:
        query = query.where(Application.status == status)
    else:
        status = "active"
    rows = db.exec(query.order_by(Application.updated_at.desc())).all()  # type: ignore[attr-defined]

    today = date.today()
    due = db.exec(
        select(Application, Job)
        .join(Job, Application.job_id == Job.id)
        .where(Application.follow_up_on <= today, Application.status != "closed")  # type: ignore[operator]
        .order_by(Application.follow_up_on)
    ).all()
    open_clusters = db.exec(select(Cluster).where(Cluster.state != "done").order_by(Cluster.created_at.desc())).all()  # type: ignore[attr-defined]
    return templates.TemplateResponse(
        request,
        "pages/applications.html",
        {
            "tab": "applications",
            "rows": rows,
            "status": status,
            "statuses": apps.STATUSES,
            "counts": counts,
            "active_count": sum(n for s, n in counts.items() if s != "closed"),
            "due": due,
            "open_clusters": open_clusters,
            "candidates": apps.cluster_candidates(db),
            "today": today,
        },
    )


@router.post("/jobs/{job_id}/apply")
def start(request: Request, job_id: int, force: str = Form(""), user: User = Depends(require_user), db: Session = Depends(get_db)):
    job = _get(db, Job, job_id, "job")
    existing = apps.application_for(db, job.id)
    if existing:
        return _see_other(f"/applications/{existing.id}")
    warnings = apps.already_applied(db, job)
    if warnings and not force:
        return templates.TemplateResponse(
            request, "pages/guard.html", {"tab": "jobs", "job": job, "warnings": warnings}, status_code=409
        )
    app = apps.start_application(db, job)
    return _see_other(f"/applications/{app.id}?done=started")


DONE = {
    "started": "Application started. Fill in the cover note, then open the advert to apply.",
    "saved": "Saved.",
    "applied": "Marked as applied. Nice one.",
}


@router.get("/applications/{app_id}")
def detail(request: Request, app_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    app = _get(db, Application, app_id, "application")
    job = db.get(Job, app.job_id)
    return templates.TemplateResponse(
        request,
        "pages/application.html",
        {
            "tab": "applications",
            "app": app,
            "job": job,
            "cvs": _cvs(db),
            "cluster": db.get(Cluster, app.cluster_id) if app.cluster_id else None,
            "statuses": apps.STATUSES,
            "done": DONE.get(request.query_params.get("done", "")),
            "today": date.today(),
        },
    )


@router.post("/applications/{app_id}")
def update(
    app_id: int,
    status: str = Form(...),
    cv_version_id: str = Form(""),
    follow_up_on: str = Form(""),
    cover_note: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    app = _get(db, Application, app_id, "application")
    if status not in apps.STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status.")
    app.cv_version_id = int(cv_version_id) if cv_version_id.isdigit() and db.get(CvVersion, int(cv_version_id)) else None
    try:
        app.follow_up_on = date.fromisoformat(follow_up_on) if follow_up_on else None
    except ValueError:
        raise HTTPException(status_code=400, detail="That follow-up date isn't valid.") from None
    app.cover_note = cover_note
    app.notes = notes
    db.add(app)
    apps.set_status(db, app, status)  # commits
    return _see_other(f"/applications/{app_id}?done=saved")


@router.post("/applications/{app_id}/applied")
def mark_applied(app_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    app = _get(db, Application, app_id, "application")
    apps.set_status(db, app, "applied")
    return _see_other(f"/applications/{app_id}?done=applied")


# ---------- clusters ----------


@router.post("/clusters")
def new_cluster(profile: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    cluster = apps.create_cluster(db, profile)
    if cluster is None:
        return _see_other("/applications")
    return _see_other(f"/clusters/{cluster.id}")


@router.get("/clusters/{cluster_id}")
def cluster_page(request: Request, cluster_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    cluster = _get(db, Cluster, cluster_id, "cluster")
    if cluster.state == "approved":
        return _see_other(f"/clusters/{cluster.id}/queue")
    rows = apps.cluster_jobs(db, cluster)
    warnings = {job.id: apps.already_applied(db, job) for _, job in rows}
    return templates.TemplateResponse(
        request,
        "pages/cluster.html",
        {
            "tab": "applications",
            "cluster": cluster,
            "rows": rows,
            "warnings": warnings,
            "cvs": _cvs(db),
            "preview": apps.fill_note(cluster.cover_note_template, rows[0][1]) if rows else "",
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/clusters/{cluster_id}")
def cluster_save(
    cluster_id: int,
    name: str = Form(...),
    cover_note_template: str = Form(""),
    cv_version_id: str = Form(""),
    approve: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    cluster = _get(db, Cluster, cluster_id, "cluster")
    if cluster.state != "draft":
        return _see_other(f"/clusters/{cluster.id}")
    cluster.name = name.strip()[:80] or cluster.name
    cluster.cover_note_template = cover_note_template
    cluster.cv_version_id = int(cv_version_id) if cv_version_id.isdigit() and db.get(CvVersion, int(cv_version_id)) else None
    db.add(cluster)
    db.commit()
    if approve and apps.cluster_jobs(db, cluster):
        apps.approve_cluster(db, cluster)
        return _see_other(f"/clusters/{cluster.id}/queue")
    return _see_other(f"/clusters/{cluster.id}?saved=1")


@router.post("/clusters/{cluster_id}/remove/{job_id}")
def cluster_remove(cluster_id: int, job_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    cluster = _get(db, Cluster, cluster_id, "cluster")
    link = db.get(ClusterJob, (cluster_id, job_id))
    if link and cluster.state == "draft":
        db.delete(link)
        db.commit()
    if not apps.cluster_jobs(db, cluster):
        db.delete(cluster)
        db.commit()
        return _see_other("/applications")
    return _see_other(f"/clusters/{cluster_id}")


@router.post("/clusters/{cluster_id}/delete")
def cluster_delete(cluster_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    cluster = _get(db, Cluster, cluster_id, "cluster")
    if cluster.state == "draft":
        for link, _ in apps.cluster_jobs(db, cluster):
            db.delete(link)
        db.flush()  # links first, or the foreign key on cluster_job blocks the delete
        db.delete(cluster)
        db.commit()
    return _see_other("/applications")


@router.get("/clusters/{cluster_id}/queue")
def queue(request: Request, cluster_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    cluster = _get(db, Cluster, cluster_id, "cluster")
    if cluster.state == "draft":
        return _see_other(f"/clusters/{cluster.id}")
    rows = apps.cluster_jobs(db, cluster)
    current = apps.next_in_queue(db, cluster)
    app = apps.application_for(db, current[1].id) if current else None
    return templates.TemplateResponse(
        request,
        "pages/queue.html",
        {
            "tab": "applications",
            "cluster": cluster,
            "rows": rows,
            "current": current,
            "app": app,
            "done_count": sum(1 for link, _ in rows if link.queue_state != "waiting"),
            "applied_count": sum(1 for link, _ in rows if link.queue_state == "applied"),
            "cv": db.get(CvVersion, cluster.cv_version_id) if cluster.cv_version_id else None,
        },
    )


@router.post("/clusters/{cluster_id}/queue/{job_id}")
def queue_mark(
    cluster_id: int, job_id: int, outcome: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)
):
    cluster = _get(db, Cluster, cluster_id, "cluster")
    try:
        apps.mark_queue(db, cluster, job_id, outcome)
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown queue action.") from None
    return _see_other(f"/clusters/{cluster_id}/queue")


# ---------- CV versions ----------


def _cv_dir(request: Request):
    return request.app.state.settings.data_dir / "cvs"


def _cv_page(request: Request, db: Session, error: str | None = None, status: int = 200):
    used = dict(
        db.exec(
            select(Application.cv_version_id, func.count()).where(Application.cv_version_id.is_not(None)).group_by(Application.cv_version_id)  # type: ignore[union-attr]
        ).all()
    )
    return templates.TemplateResponse(
        request,
        "pages/cvs.html",
        {"tab": "profile", "cvs": _cvs(db), "used": used, "error": error, "done": request.query_params.get("done")},
        status_code=status,
    )


@router.get("/profile/cvs")
def cv_list(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _cv_page(request, db)


@router.post("/profile/cvs")
async def cv_upload(
    request: Request,
    file: UploadFile,
    label: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    data = await file.read(apps.CV_MAX_BYTES + 1)
    try:
        stored = apps.store_cv(_cv_dir(request), file.filename or "", data)
    except apps.CvUploadError as exc:
        return _cv_page(request, db, str(exc), 400)
    first = db.exec(select(CvVersion.id).limit(1)).first() is None
    label = label.strip()[:60] or f"CV {utcnow():%-d %b %Y}"
    db.add(CvVersion(label=label, file_path=stored, original_name=(file.filename or stored)[:120], is_default=first))
    db.commit()
    return _see_other("/profile/cvs?done=added")


@router.post("/profile/cvs/{cv_id}/default")
def cv_default(cv_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _get(db, CvVersion, cv_id, "CV")
    for cv in _cvs(db):
        cv.is_default = cv.id == cv_id
        db.add(cv)
    db.commit()
    return _see_other("/profile/cvs")


@router.post("/profile/cvs/{cv_id}/delete")
def cv_delete(request: Request, cv_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    cv = _get(db, CvVersion, cv_id, "CV")
    in_use = db.exec(select(Application.id).where(Application.cv_version_id == cv_id).limit(1)).first()
    in_cluster = db.exec(select(Cluster.id).where(Cluster.cv_version_id == cv_id).limit(1)).first()
    if in_use or in_cluster:
        return _cv_page(request, db, "That CV was sent with an application, so it's kept as a record.", 400)
    (_cv_dir(request) / cv.file_path).unlink(missing_ok=True)
    db.delete(cv)
    db.commit()
    return _see_other("/profile/cvs?done=deleted")


@router.get("/profile/cvs/{cv_id}/file")
def cv_file(request: Request, cv_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    cv = _get(db, CvVersion, cv_id, "CV")
    path = _cv_dir(request) / cv.file_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="That CV file is missing.")
    return FileResponse(path, filename=cv.original_name)
