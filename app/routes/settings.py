"""Profile → Services (connect job sites, see health) and Profile → Criteria (the YAML file)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.criteria import CriteriaError
from app.deps import get_db, require_user, templates
from app.ingest import check_service, credentials_for, disconnect, get_service, rescore_all, save_credentials
from app.models import Service, User
from app.scheduler import reschedule_fetch
from app.security.csrf import verify_csrf
from app.services import KIND_LABELS, PLANNED, PLUGINS

router = APIRouter(dependencies=[Depends(verify_csrf)])

HEALTH_LABELS = {
    "ok": "Working",
    "broken": "Broken",
    "needs_login": "Needs new key",
    "rate_limited": "Slowed down",
    "unchecked": "Not checked yet",
    "not_connected": "Not connected",
}


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _plugin_or_404(key: str):
    plugin = PLUGINS.get(key)
    if plugin is None:
        raise HTTPException(status_code=404, detail="No such service.")
    return plugin


@router.get("/profile/services")
def services_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = {s.name: s for s in db.exec(select(Service)).all()}
    groups: dict[str, list[dict]] = {k: [] for k in KIND_LABELS}
    for key, plugin in PLUGINS.items():
        groups[plugin.kind].append({"key": key, "label": plugin.label, "summary": plugin.summary, "row": rows.get(key)})
    for planned in PLANNED:
        groups[planned.kind].append(
            {"key": planned.key, "label": planned.label, "summary": planned.summary, "phase": planned.phase}
        )
    return templates.TemplateResponse(
        request,
        "pages/services.html",
        {"tab": "profile", "groups": groups, "kind_labels": KIND_LABELS, "health_labels": HEALTH_LABELS},
    )


def _service_page(request: Request, db: Session, key: str, error: str | None = None, status: int = 200):
    plugin = _plugin_or_404(key)
    row = get_service(db, key)
    saved = credentials_for(row, request.app.state.secret_box) if row and row.credentials else {}
    return templates.TemplateResponse(
        request,
        "pages/service.html",
        {
            "tab": "profile",
            "plugin": plugin,
            "row": row,
            "saved": saved,
            "error": error,
            "health_labels": HEALTH_LABELS,
            "checked": request.query_params.get("checked"),
        },
        status_code=status,
    )


@router.get("/profile/services/{key}")
def service_page(request: Request, key: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _service_page(request, db, key)


@router.post("/profile/services/{key}")
async def service_save(request: Request, key: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    plugin = _plugin_or_404(key)
    form = await request.form()
    row = get_service(db, key)
    box = request.app.state.secret_box
    existing = credentials_for(row, box) if row and row.credentials else {}
    creds = {}
    for f in plugin.credential_fields:
        value = str(form.get(f.key) or "").strip()
        creds[f.key] = value or existing.get(f.key, "")  # blank secret field = keep the saved one
        if not creds[f.key]:
            return _service_page(request, db, key, error=f"{f.label} is needed.", status=400)
    save_credentials(db, box, key, creds)
    await check_service(request.app.state.engine, box, key, request.app.state.http_transport)
    return _see_other(f"/profile/services/{key}?checked=1")


@router.post("/profile/services/{key}/check")
async def service_check(request: Request, key: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _plugin_or_404(key)
    row = get_service(db, key)
    if row and row.credentials:
        await check_service(request.app.state.engine, request.app.state.secret_box, key, request.app.state.http_transport)
    return _see_other(f"/profile/services/{key}?checked=1")


@router.post("/profile/services/{key}/disconnect")
def service_disconnect(key: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    _plugin_or_404(key)
    disconnect(db, key)
    return _see_other(f"/profile/services/{key}")


# ---------- criteria ----------


@router.get("/profile/criteria")
def criteria_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "pages/criteria.html",
        {
            "tab": "profile",
            "text": request.app.state.criteria_file.text(),
            "error": None,
            "saved": request.query_params.get("saved"),
        },
    )


@router.post("/profile/criteria")
async def criteria_save(request: Request, user: User = Depends(require_user)):
    text = str((await request.form()).get("criteria") or "")
    try:
        criteria = request.app.state.criteria_file.save(text)
    except CriteriaError as exc:
        return templates.TemplateResponse(
            request,
            "pages/criteria.html",
            {"tab": "profile", "text": text, "error": str(exc), "saved": None},
            status_code=400,
        )
    count = rescore_all(request.app.state.engine, criteria)
    reschedule_fetch(request.app, criteria.fetch_every_minutes)
    return _see_other(f"/profile/criteria?saved={count}")
