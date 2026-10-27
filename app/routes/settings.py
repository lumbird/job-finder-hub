"""Profile → Services (connect job sites, see health) and Profile → Criteria (a form, or the YAML file)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.criteria import Area, Criteria, CriteriaError, Profile, to_yaml
from app.deps import get_db, require_user, templates
from app.ingest import (
    LIMIT_KEYS,
    calls_left,
    check_service,
    credentials_for,
    disconnect,
    get_service,
    limits_for,
    rescore_all,
    save_credentials,
    save_limits,
    usage_for,
)
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
        groups[plugin.kind].append(
            {
                "key": key,
                "label": plugin.label,
                "summary": plugin.summary,
                "row": rows.get(key),
                "no_keys": not plugin.credential_fields,
            }
        )
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
            "limits": limits_for(row) if row else dict(plugin.default_limits),
            "usage": usage_for(row) if row else None,
            "calls_left": calls_left(row) if row else None,
            "limits_saved": request.query_params.get("limits"),
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


@router.post("/profile/services/{key}/limits")
async def service_limits(request: Request, key: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    plugin = _plugin_or_404(key)
    row = get_service(db, key) or Service(kind=plugin.kind, name=key)
    form = await request.form()
    try:
        limits = {k: int(str(form.get(k) or "0").strip() or 0) for k in LIMIT_KEYS}
    except ValueError:
        return _service_page(request, db, key, error="Limits must be whole numbers. Use 0 for no limit.", status=400)
    save_limits(db, row, limits)
    return _see_other(f"/profile/services/{key}?limits=1")


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


def _criteria_saved(request: Request, criteria: Criteria) -> RedirectResponse:
    count = rescore_all(request.app.state.engine, criteria)
    reschedule_fetch(request.app, criteria.fetch_every_minutes)
    return _see_other(f"/profile/criteria?saved={count}")


def _form_page(request: Request, criteria: Criteria, error: str | None = None, status: int = 200):
    return templates.TemplateResponse(
        request,
        "pages/criteria_form.html",
        {
            "tab": "profile",
            "c": criteria,
            "error": error,
            "saved": request.query_params.get("saved"),
            "area_blanks": AREA_BLANKS,
        },
        status_code=status,
    )


AREA_BLANKS = 2  # empty area rows on the form for adding new ones


def _lines(value) -> list[str]:
    """Textarea → list: one item per line, commas also split."""
    return [part.strip() for line in str(value or "").splitlines() for part in line.split(",") if part.strip()]


def _form_int(form, key: str, default: int) -> int:
    value = str(form.get(key) or "").strip()
    try:
        return int(value) if value else default
    except ValueError:
        label = "Miles" if key.endswith("_miles") else key.replace("_", " ").capitalize()
        raise CriteriaError(f"{label} should be a whole number.") from None


def criteria_from_form(form) -> Criteria:
    profiles = []
    for i in range(int(form.get("profile_count") or 0) + 1):  # the extra one is the blank "add a profile"
        name = str(form.get(f"p{i}_name") or "").strip()
        if form.get(f"p{i}_remove") or not name:
            continue
        profiles.append(
            Profile(
                name=name,
                searches=_lines(form.get(f"p{i}_searches")),
                keywords=_lines(form.get(f"p{i}_keywords")),
                must_have=_lines(form.get(f"p{i}_must_have")),
                dealbreakers=_lines(form.get(f"p{i}_dealbreakers")),
            )
        )
    areas = []
    for i in range(int(form.get("area_count") or 0) + AREA_BLANKS):
        name = str(form.get(f"a{i}_name") or "").strip()
        if name and not form.get(f"a{i}_remove"):
            miles = _form_int(form, f"a{i}_miles", 20)
            if miles < 1:
                raise CriteriaError(f"{name}: miles should be at least 1.")
            areas.append(Area(name, miles))
    return Criteria(
        areas=areas,
        salary_floor=_form_int(form, "salary_floor", 0),
        fetch_every_minutes=_form_int(form, "fetch_every_minutes", 60),
        calm_mode_on=bool(form.get("calm_mode_on")),
        daily_cap=_form_int(form, "daily_cap", 3),
        profiles=profiles,
    )


@router.get("/profile/criteria")
def criteria_page(request: Request, user: User = Depends(require_user)):
    return _form_page(request, request.app.state.criteria_file.load())


@router.post("/profile/criteria/form")
async def criteria_form_save(request: Request, user: User = Depends(require_user)):
    form = await request.form()
    submitted = None
    try:
        submitted = criteria_from_form(form)
        criteria = request.app.state.criteria_file.save(to_yaml(submitted))
    except CriteriaError as exc:
        return _form_page(request, submitted or request.app.state.criteria_file.load(), error=str(exc), status=400)
    return _criteria_saved(request, criteria)


@router.get("/profile/criteria/yaml")
def criteria_yaml_page(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(
        request,
        "pages/criteria.html",
        {"tab": "profile", "text": request.app.state.criteria_file.text(), "error": None, "saved": None},
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
    return _criteria_saved(request, criteria)
