"""First-run setup, sign-in (passkey or password + optional TOTP) and sign-out."""

from __future__ import annotations

import logging

import pyotp
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app.deps import get_db, require_user, safe_next, templates, user_exists
from app.models import PasskeyCredential, User
from app.security import passkeys, throttle
from app.security.csrf import verify_csrf
from app.security.passwords import hash_password, needs_rehash, password_problem, verify_password
from app.security.sessions import end_all_sessions, end_session, start_session

log = logging.getLogger("hub.auth")

router = APIRouter(dependencies=[Depends(verify_csrf)])


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _the_user(db: Session) -> User | None:
    return db.exec(select(User).limit(1)).first()


def _see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ---------- first run ----------


@router.get("/setup")
def setup_page(request: Request, db: Session = Depends(get_db)):
    if user_exists(db):
        return _see_other("/login")
    return templates.TemplateResponse(request, "setup.html", {"error": None})


@router.post("/setup")
async def setup_submit(
    request: Request,
    code: str = Form(...),
    password: str = Form(...),
    confirm: str = Form(...),
    db: Session = Depends(get_db),
):
    if user_exists(db):
        return _see_other("/login")

    def fail(message: str, status: int = 400):
        return templates.TemplateResponse(request, "setup.html", {"error": message}, status_code=status)

    key = _client_key(request)
    try:
        await throttle.before_attempt(db, key, sleep=request.app.state.sleep)
    except throttle.LockedOut as exc:
        return fail(str(exc), 429)

    setup_code = request.app.state.setup_code
    if not setup_code.check(code):
        throttle.record_failure(db, key)
        return fail("That setup code isn't right. Copy it from the hub's logs.")
    if problem := password_problem(password, confirm):
        return fail(problem)

    user = User(password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    setup_code.consume()
    throttle.record_success(db, key)
    log.info("Account created; setup code used up.")

    response = _see_other("/profile/security?done=welcome")
    start_session(db, request.app.state.settings, response, user)
    return response


# ---------- sign in / out ----------


def _login_page(request: Request, db: Session, error: str | None = None, status: int = 200):
    user = _the_user(db)
    has_passkeys = user is not None and db.exec(select(PasskeyCredential.id).limit(1)).first() is not None
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": error,
            "has_passkeys": has_passkeys,
            "totp_enabled": bool(user and user.totp_enabled),
            "public_origin": request.app.state.settings.public_origin,
            "next": safe_next(request.query_params.get("next"), ""),
        },
        status_code=status,
    )


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    if not user_exists(db):
        return _see_other("/setup")
    return _login_page(request, db)


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(...),
    totp: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _the_user(db)
    if user is None:
        return _see_other("/setup")

    key = _client_key(request)
    try:
        await throttle.before_attempt(db, key, sleep=request.app.state.sleep)
    except throttle.LockedOut as exc:
        return _login_page(request, db, str(exc), 429)

    ok = verify_password(user.password_hash, password)
    if ok and user.totp_enabled:
        secret = request.app.state.secret_box.decrypt(user.totp_secret)
        ok = pyotp.TOTP(secret).verify(totp.replace(" ", ""), valid_window=1)
    if not ok:
        throttle.record_failure(db, key)
        message = "Password or code not recognised." if user.totp_enabled else "Password not recognised."
        return _login_page(request, db, message, 400)

    throttle.record_success(db, key)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.add(user)
        db.commit()
    response = _see_other(safe_next(next))
    start_session(db, request.app.state.settings, response, user)
    return response


@router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    response = _see_other("/login")
    end_session(db, request, response)
    return response


@router.post("/logout-everywhere")
def logout_everywhere(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    response = _see_other("/login")
    end_all_sessions(db, user, response)
    return response


# ---------- passkeys ----------


@router.post("/webauthn/login/options")
def passkey_login_options(request: Request, db: Session = Depends(get_db)):
    options = passkeys.authentication_options(request.app.state.settings)
    response = JSONResponse(passkeys.to_json(options))
    request.app.state.challenges.put(response, request.app.state.settings, options.challenge, "login")
    return response


@router.post("/webauthn/login/verify")
async def passkey_login_verify(request: Request, db: Session = Depends(get_db)):
    key = _client_key(request)
    try:
        await throttle.before_attempt(db, key, sleep=request.app.state.sleep)
    except throttle.LockedOut as exc:
        return JSONResponse({"error": str(exc)}, status_code=429)

    body = await request.json()
    credential = body.get("credential") or {}
    challenge = request.app.state.challenges.pop(request, "login")
    stored = db.get(PasskeyCredential, credential.get("id", "")) if isinstance(credential, dict) else None
    if challenge is None or stored is None:
        throttle.record_failure(db, key)
        return JSONResponse({"error": "That passkey isn't registered here. Use your password."}, status_code=400)
    try:
        stored.sign_count = passkeys.verify_authentication(request.app.state.settings, credential, challenge, stored)
    except Exception as exc:  # the library raises several exception types
        log.info("Passkey sign-in failed: %s", exc)
        throttle.record_failure(db, key)
        return JSONResponse({"error": "Passkey check failed. Try again or use your password."}, status_code=400)

    db.add(stored)
    db.commit()
    throttle.record_success(db, key)
    user = db.get(User, stored.user_id)
    response = JSONResponse({"redirect": safe_next(body.get("next"))})
    start_session(db, request.app.state.settings, response, user)
    return response


@router.post("/webauthn/register/options")
def passkey_register_options(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    existing = db.exec(select(PasskeyCredential).where(PasskeyCredential.user_id == user.id)).all()
    options = passkeys.registration_options(request.app.state.settings, user, list(existing))
    response = JSONResponse(passkeys.to_json(options))
    request.app.state.challenges.put(response, request.app.state.settings, options.challenge, "register")
    return response


@router.post("/webauthn/register/verify")
async def passkey_register_verify(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    body = await request.json()
    challenge = request.app.state.challenges.pop(request, "register")
    if challenge is None:
        return JSONResponse({"error": "That took too long. Try adding the passkey again."}, status_code=400)
    try:
        cred_id, public_key, sign_count = passkeys.verify_registration(
            request.app.state.settings, body.get("credential") or {}, challenge
        )
    except Exception as exc:
        log.info("Passkey registration failed: %s", exc)
        return JSONResponse({"error": "Couldn't add that passkey. Try again."}, status_code=400)

    name = str(body.get("name") or "").strip()[:60] or "Passkey"
    db.add(PasskeyCredential(id=cred_id, user_id=user.id, public_key=public_key, sign_count=sign_count, name=name))
    db.commit()
    return JSONResponse({"redirect": "/profile/security?done=passkey-added"})

