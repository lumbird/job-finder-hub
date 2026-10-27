"""Reporting tab: entries ready to paste, logging other work search, weekly summary."""

from __future__ import annotations

import csv
import io
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlmodel import Session, select

from app import reporting
from app.deps import get_db, require_user, safe_next, templates
from app.models import Application, ReportEntry, User
from app.security.csrf import verify_csrf

router = APIRouter(dependencies=[Depends(verify_csrf)])

SERVICE = "universal_credit"  # the only reporting target for now; the rules take a key for later ones

DONE = {
    "logged": "Added to Ready to paste.",
    "reported": "Marked as reported.",
    "all-reported": "All marked as reported.",
    "undone": "Moved back to Ready to paste.",
    "removed": "Removed. It won't be reported.",
    "saved": "Text saved.",
}


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _entry(db: Session, entry_id: int) -> ReportEntry:
    entry = db.get(ReportEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="That entry isn't here any more.")
    return entry


@router.get("/reporting")
def reporting_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    service = reporting.reporting_service(db, SERVICE)
    ready = db.exec(
        select(ReportEntry)
        .where(ReportEntry.reporting_service_id == service.id, ReportEntry.state == "ready")
        .order_by(ReportEntry.activity_date, ReportEntry.created_at)
    ).all()
    reported = db.exec(
        select(ReportEntry)
        .where(ReportEntry.reporting_service_id == service.id, ReportEntry.state == "reported")
        .order_by(ReportEntry.reported_at.desc())  # type: ignore[union-attr]
        .limit(20)
    ).all()
    return templates.TemplateResponse(
        request,
        "pages/reporting.html",
        {
            "tab": "reporting",
            "ready": ready,
            "reported": reported,
            "all_text": "\n\n".join(e.text for e in ready),
            "types": reporting.ACTIVITY_TYPES,
            "other_types": reporting.OTHER_ACTIVITIES,
            "today": date.today(),
            "done": DONE.get(request.query_params.get("done", "")),
        },
    )


@router.post("/reporting/log")
def log(
    activity_type: str = Form(...),
    when: str = Form(""),
    detail: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    try:
        day = date.fromisoformat(when) if when else date.today()
        reporting.log_activity(db, SERVICE, activity_type, day, detail.strip()[:500])
    except ValueError:
        raise HTTPException(status_code=400, detail="Pick an activity and a valid date.") from None
    return _see_other("/reporting?done=logged")


@router.post("/applications/{app_id}/report")
def report_application(
    app_id: int,
    activity_type: str = Form(...),
    next: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    app = db.get(Application, app_id)
    if app is None:
        raise HTTPException(status_code=404, detail="That application isn't here any more.")
    try:
        reporting.report_application(db, app, activity_type, SERVICE)
    except ValueError:
        raise HTTPException(status_code=400, detail="That can't be reported from here.") from None
    target = safe_next(next, f"/applications/{app_id}")
    return _see_other(target + ("&" if "?" in target else "?") + "done=queued")


@router.post("/reporting/{entry_id}/text")
def save_text(entry_id: int, text: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)):
    entry = _entry(db, entry_id)
    if entry.state == "ready" and text.strip():
        entry.text = text.strip()
        db.add(entry)
        db.commit()
    return _see_other("/reporting?done=saved")


@router.post("/reporting/{entry_id}/reported")
def mark_reported(entry_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    reporting.mark_reported(db, _entry(db, entry_id))
    return _see_other("/reporting?done=reported")


@router.post("/reporting/all-reported")
def mark_all_reported(user: User = Depends(require_user), db: Session = Depends(get_db)):
    service = reporting.reporting_service(db, SERVICE)
    for entry in db.exec(
        select(ReportEntry).where(ReportEntry.reporting_service_id == service.id, ReportEntry.state == "ready")
    ).all():
        reporting.mark_reported(db, entry)
    return _see_other("/reporting?done=all-reported")


@router.post("/reporting/{entry_id}/undo")
def undo(entry_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    reporting.unmark(db, _entry(db, entry_id))
    return _see_other("/reporting?done=undone")


@router.post("/reporting/{entry_id}/remove")
def remove(entry_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Un-report something chosen by mistake. Reported entries are undone first, so history isn't lost by accident."""
    entry = _entry(db, entry_id)
    if entry.state == "ready":
        db.delete(entry)
        db.commit()
    return _see_other("/reporting?done=removed")


@router.get("/reporting/week")
def week(request: Request, start: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    try:
        monday = reporting.week_start(date.fromisoformat(start) if start else date.today())
    except ValueError:
        monday = reporting.week_start(date.today())
    text, entries = reporting.weekly_summary(db, SERVICE, monday)
    counts: dict[str, int] = {}
    for e in entries:
        label = reporting.ACTIVITY_TYPES.get(e.activity_type, "Other")
        counts[label] = counts.get(label, 0) + 1
    this_week = reporting.week_start(date.today())
    return templates.TemplateResponse(
        request,
        "pages/report_week.html",
        {
            "tab": "reporting",
            "monday": monday,
            "sunday": monday + timedelta(days=6),
            "text": text,
            "entries": entries,
            "counts": counts,
            "prev": (monday - timedelta(days=7)).isoformat(),
            "next": (monday + timedelta(days=7)).isoformat() if monday < this_week else None,
        },
    )


def _day_or_none(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("/reporting/export/applied.{fmt}")
def export_applied(
    fmt: str,
    detail: str = "coach",
    since: str = "",
    until: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Download every job applied for: the short work coach list, or the full one for your own records."""
    if fmt not in ("csv", "txt"):
        raise HTTPException(status_code=404, detail="Pick csv or txt.")
    full = detail == "full"
    rows = reporting.export_rows(db, full, _day_or_none(since), _day_or_none(until))
    name = f"jobs-applied-{'full' if full else 'coach'}-{date.today().isoformat()}.{fmt}"
    if fmt == "csv":
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        body, media = "\ufeff" + buf.getvalue(), "text/csv; charset=utf-8"  # BOM so Excel reads £ and accents
    else:
        body = "\n".join(" | ".join(r) for r in rows) + "\n" if full else "\n".join(
            f"{d}  {title}" for title, d, _ in rows[1:]
        ) + "\n"
        body = f"Jobs applied for ({len(rows) - 1})\n\n" + body
        media = "text/plain; charset=utf-8"
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})
