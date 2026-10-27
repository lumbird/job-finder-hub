"""Tab pages, Profile, security settings and the theme toggle."""

from __future__ import annotations

import pyotp
import segno
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlmodel import Session, select

from app.deps import THEME_COOKIE, THEMES, get_db, require_user, safe_next, templates
from app.models import PasskeyCredential, Service, User
from app.security.csrf import verify_csrf
from app.security.passwords import hash_password, password_problem, verify_password
from app.security.sessions import other_session_count

router = APIRouter(dependencies=[Depends(verify_csrf)])

DONE_MESSAGES = {
    "welcome": "Your login is set up. Add a passkey so you can sign in with your fingerprint.",
    "passkey-added": "Passkey added.",
    "passkey-removed": "Passkey removed.",
    "password-changed": "Password changed.",
    "totp-on": "Authenticator codes are on. You'll need one with your password.",
    "totp-off": "Authenticator codes are off.",
}


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("/")
def home(user: User = Depends(require_user)):
    return _see_other("/jobs")


@router.get("/reporting")
def reporting(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request, "pages/reporting.html", {"tab": "reporting"})


@router.get("/profile")
def profile(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    rows = db.exec(select(Service).where(Service.credentials.is_not(None))).all()  # type: ignore[union-attr]
    broken = [r for r in rows if r.health not in ("ok", "unchecked")]
    if not rows:
        summary = "Nothing connected yet"
    elif broken:
        summary = f"{len(broken)} need{'s' if len(broken) == 1 else ''} attention"
    else:
        summary = f"{len(rows)} connected, all working"
    return templates.TemplateResponse(request, "pages/profile.html", {"tab": "profile", "services_summary": summary})


# ---------- security ----------


def _security_page(
    request: Request,
    db: Session,
    user: User,
    *,
    error: str | None = None,
    totp_setup: dict | None = None,
    status: int = 200,
):
    creds = db.exec(
        select(PasskeyCredential).where(PasskeyCredential.user_id == user.id).order_by(PasskeyCredential.created_at)
    ).all()
    settings = request.app.state.settings
    return templates.TemplateResponse(
        request,
        "pages/security.html",
        {
            "tab": "profile",
            "user": user,
            "passkeys": creds,
            "done": DONE_MESSAGES.get(request.query_params.get("done", "")),
            "error": error,
            "totp_setup": totp_setup,
            "other_sessions": other_session_count(db, request, user),
            "public_origin": settings.public_origin,
        },
        status_code=status,
    )


@router.get("/profile/security")
def security(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return _security_page(request, db, user)


@router.post("/profile/passkeys/{cred_id}/delete")
def delete_passkey(cred_id: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    cred = db.get(PasskeyCredential, cred_id)
    if cred is not None and cred.user_id == user.id:
        db.delete(cred)
        db.commit()
    return _see_other("/profile/security?done=passkey-removed")


@router.post("/profile/password")
def change_password(
    request: Request,
    current: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not verify_password(user.password_hash, current):
        return _security_page(request, db, user, error="Current password not recognised.", status=400)
    if problem := password_problem(password, confirm):
        return _security_page(request, db, user, error=problem, status=400)
    user.password_hash = hash_password(password)
    db.add(user)
    db.commit()
    return _see_other("/profile/security?done=password-changed")


def _totp_setup_context(secret: str) -> dict:
    uri = pyotp.TOTP(secret).provisioning_uri(name="me", issuer_name="Job Finder Hub")
    qr_svg = segno.make(uri, error="m").svg_inline(scale=5, dark="#000", light="#fff", border=2)
    grouped = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
    return {"qr_svg": qr_svg, "secret": grouped, "uri": uri}


@router.post("/profile/totp/start")
def totp_start(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if user.totp_enabled:
        return _see_other("/profile/security")
    secret = pyotp.random_base32()
    user.totp_secret = request.app.state.secret_box.encrypt(secret)
    db.add(user)
    db.commit()
    return _security_page(request, db, user, totp_setup=_totp_setup_context(secret))


@router.post("/profile/totp/confirm")
def totp_confirm(
    request: Request, code: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)
):
    if user.totp_enabled or not user.totp_secret:
        return _see_other("/profile/security")
    secret = request.app.state.secret_box.decrypt(user.totp_secret)
    if not pyotp.TOTP(secret).verify(code.replace(" ", ""), valid_window=1):
        return _security_page(
            request,
            db,
            user,
            error="That code didn't match. Check your phone's clock and try the newest code.",
            totp_setup=_totp_setup_context(secret),
            status=400,
        )
    user.totp_enabled = True
    db.add(user)
    db.commit()
    return _see_other("/profile/security?done=totp-on")


@router.post("/profile/totp/disable")
def totp_disable(
    request: Request, code: str = Form(...), user: User = Depends(require_user), db: Session = Depends(get_db)
):
    if not user.totp_enabled:
        return _see_other("/profile/security")
    secret = request.app.state.secret_box.decrypt(user.totp_secret)
    if not pyotp.TOTP(secret).verify(code.replace(" ", ""), valid_window=1):
        return _security_page(request, db, user, error="That code didn't match, so codes are still on.", status=400)
    user.totp_enabled = False
    user.totp_secret = None
    db.add(user)
    db.commit()
    return _see_other("/profile/security?done=totp-off")


# ---------- theme ----------


@router.post("/theme")
def set_theme(request: Request, theme: str = Form(...), next: str = Form("/")):
    response = _see_other(safe_next(next))
    if theme in THEMES:
        response.set_cookie(
            THEME_COOKIE,
            theme,
            max_age=365 * 86400,
            httponly=True,
            secure=request.app.state.settings.cookie_secure,
            samesite="strict",
            path="/",
        )
    return response
