from __future__ import annotations

from datetime import timedelta

import pyotp
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.deps import safe_next
from app.models import LoginThrottle, User, utcnow
from app.security import throttle
from tests.conftest import PASSWORD, csrf, do_setup, login


# ---------- first run ----------


def test_fresh_install_sends_everything_to_setup(client):
    assert client.get("/").headers["location"] == "/setup"
    assert client.get("/login").headers["location"] == "/setup"
    assert client.get("/jobs").headers["location"] == "/setup"


def test_setup_needs_the_right_code(client):
    client.app.state.setup_code.issue()
    token = csrf(client, "/setup")
    r = client.post("/setup", data={"csrf_token": token, "code": "AAAA-BBBB-CCCC", "password": PASSWORD, "confirm": PASSWORD})
    assert r.status_code == 400
    assert "setup code" in r.text


def test_setup_rejects_short_or_mismatched_password(client):
    assert do_setup(client, password="short").status_code == 400
    code = client.app.state.setup_code.issue()
    r = client.post(
        "/setup",
        data={"csrf_token": csrf(client, "/setup"), "code": code, "password": PASSWORD, "confirm": PASSWORD + "x"},
    )
    assert r.status_code == 400 and "match" in r.text


def test_setup_creates_account_signs_in_and_uses_up_code(client):
    code = client.app.state.setup_code.issue()
    r = client.post(
        "/setup", data={"csrf_token": csrf(client, "/setup"), "code": code, "password": PASSWORD, "confirm": PASSWORD}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/profile/security?done=welcome"
    assert client.get("/jobs").status_code == 200
    assert not client.app.state.setup_code.active
    assert client.get("/setup").headers["location"] == "/login"
    # The code can't be replayed once used.
    r = client.post(
        "/setup", data={"csrf_token": csrf(client, "/jobs"), "code": code, "password": PASSWORD, "confirm": PASSWORD}
    )
    assert r.headers["location"] == "/login"
    with Session(client.app.state.engine) as db:
        assert len(db.exec(select(User)).all()) == 1


def test_setup_code_survives_restart_until_used(tmp_path, sleeps):
    from app.main import create_app
    from tests.conftest import make_settings

    settings = make_settings(tmp_path)
    first = create_app(settings, sleep=sleeps).state.setup_code.issue()
    second_app = create_app(settings, sleep=sleeps)
    assert second_app.state.setup_code.issue() == first
    second_app.state.setup_code.consume()
    assert not (tmp_path / ".setup-code").exists()


def test_setup_code_tolerates_case_and_spacing(client):
    code = client.app.state.setup_code.issue()
    assert client.app.state.setup_code.check(code.lower().replace("-", " "))


# ---------- password sign-in ----------


def test_login_and_logout(account):
    assert account.get("/jobs").headers["location"] == "/login?next=/jobs"
    r = login(account)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert account.get("/jobs").status_code == 200
    r = account.post("/logout", data={"csrf_token": csrf(account, "/jobs")})
    assert r.headers["location"] == "/login"
    assert account.get("/jobs").status_code == 303


def test_wrong_password(account):
    r = login(account, password="nope nope nope")
    assert r.status_code == 400
    assert "not recognised" in r.text


def test_login_returns_to_next_but_not_offsite(account):
    token = csrf(account)
    r = account.post("/login", data={"csrf_token": token, "password": PASSWORD, "next": "/reporting"})
    assert r.headers["location"] == "/reporting"
    account.cookies.delete("hub_session")
    r = account.post("/login", data={"csrf_token": token, "password": PASSWORD, "next": "//evil.example"})
    assert r.headers["location"] == "/"


def test_safe_next():
    assert safe_next("/applications") == "/applications"
    for bad in ("https://evil.example/", "//evil.example", "evil", "", None):
        assert safe_next(bad) == "/"


# ---------- CSRF ----------


def test_post_without_csrf_token_is_refused(account):
    csrf(account)  # gets the cookie
    assert account.post("/login", data={"password": PASSWORD}).status_code == 403
    assert account.post("/login", data={"password": PASSWORD, "csrf_token": "forged"}).status_code == 403
    assert account.post("/login", data={"password": PASSWORD}, headers={"X-CSRF-Token": "forged"}).status_code == 403


def test_csrf_token_accepted_as_header(account):
    token = csrf(account)
    r = account.post("/login", data={"password": PASSWORD}, headers={"X-CSRF-Token": token})
    assert r.status_code == 303


def test_csrf_token_from_another_browser_is_refused(app, account):
    stolen = csrf(account)
    other = TestClient(app, follow_redirects=False)
    csrf(other)  # different cookie
    assert other.post("/login", data={"csrf_token": stolen, "password": PASSWORD}).status_code == 403


# ---------- throttling ----------


def test_slows_after_five_failures_and_locks_after_ten(account, sleeps):
    for _ in range(5):
        login(account, password="wrong password!")
    assert sleeps.calls == []
    login(account, password="wrong password!")
    assert sleeps.calls == [1]
    for _ in range(4):
        login(account, password="wrong password!")
    assert sleeps.calls == [1, 2, 4, 8, 16]

    r = login(account)  # right password, but locked
    assert r.status_code == 429
    assert "15 minutes" in r.text


def test_lock_expires(account):
    for _ in range(10):
        login(account, password="wrong password!")
    with Session(account.app.state.engine) as db:
        row = db.exec(select(LoginThrottle)).one()
        row.locked_until = utcnow() - timedelta(seconds=1)
        db.add(row)
        db.commit()
    assert login(account).status_code == 303


def test_success_clears_failures(account, sleeps):
    for _ in range(4):
        login(account, password="wrong password!")
    assert login(account).status_code == 303
    with Session(account.app.state.engine) as db:
        assert db.exec(select(LoginThrottle)).first() is None


def test_old_failures_are_forgotten(account):
    for _ in range(4):
        login(account, password="wrong password!")
    with Session(account.app.state.engine) as db:
        row = db.exec(select(LoginThrottle)).one()
        row.last_failure_at = utcnow() - throttle.FORGET_AFTER - timedelta(seconds=1)
        db.add(row)
        db.commit()
    login(account, password="wrong password!")
    with Session(account.app.state.engine) as db:
        assert db.exec(select(LoginThrottle)).one().failures == 1


def test_wrong_setup_codes_are_throttled(client, sleeps):
    client.app.state.setup_code.issue()
    token = csrf(client, "/setup")
    for _ in range(10):
        client.post("/setup", data={"csrf_token": token, "code": "nope", "password": PASSWORD, "confirm": PASSWORD})
    code = client.app.state.setup_code.issue()
    r = client.post("/setup", data={"csrf_token": token, "code": code, "password": PASSWORD, "confirm": PASSWORD})
    assert r.status_code == 429


# ---------- TOTP ----------


def _enable_totp(client) -> str:
    login(client)
    token = csrf(client, "/profile/security")
    r = client.post("/profile/totp/start", data={"csrf_token": token})
    assert r.status_code == 200 and "<svg" in r.text
    with Session(client.app.state.engine) as db:
        user = db.exec(select(User)).one()
        secret = client.app.state.secret_box.decrypt(user.totp_secret)
        assert user.totp_secret != secret  # stored encrypted
    r = client.post("/profile/totp/confirm", data={"csrf_token": token, "code": "000000"})
    assert r.status_code == 400
    r = client.post("/profile/totp/confirm", data={"csrf_token": token, "code": pyotp.TOTP(secret).now()})
    assert r.headers["location"] == "/profile/security?done=totp-on"
    return secret


def test_totp_required_once_enabled(account):
    secret = _enable_totp(account)
    account.cookies.delete("hub_session")
    assert "Authenticator code" in account.get("/login").text
    assert login(account).status_code == 400
    assert login(account, totp="123456").status_code == 400
    assert login(account, totp=pyotp.TOTP(secret).now()).status_code == 303


def test_totp_can_be_turned_off(account):
    secret = _enable_totp(account)
    token = csrf(account, "/profile/security")
    r = account.post("/profile/totp/disable", data={"csrf_token": token, "code": pyotp.TOTP(secret).now()})
    assert r.headers["location"] == "/profile/security?done=totp-off"
    account.cookies.delete("hub_session")
    assert login(account).status_code == 303


# ---------- sessions ----------


def test_sign_out_everywhere(app, account):
    phone = account
    laptop = TestClient(app, follow_redirects=False)
    login(phone)
    login(laptop)
    assert "1 other device" in phone.get("/profile/security").text
    r = phone.post("/logout-everywhere", data={"csrf_token": csrf(phone, "/jobs")})
    assert r.headers["location"] == "/login"
    assert laptop.get("/jobs").status_code == 303
    assert phone.get("/jobs").status_code == 303


def test_change_password(account):
    login(account)
    token = csrf(account, "/profile/security")
    new = "a different passphrase"
    r = account.post("/profile/password", data={"csrf_token": token, "current": "wrong", "password": new, "confirm": new})
    assert r.status_code == 400
    r = account.post("/profile/password", data={"csrf_token": token, "current": PASSWORD, "password": new, "confirm": new})
    assert r.status_code == 303
    account.cookies.delete("hub_session")
    assert login(account).status_code == 400
    assert login(account, password=new).status_code == 303


def test_expired_session_is_rejected(account):
    from app.models import AuthSession

    login(account)
    with Session(account.app.state.engine) as db:
        row = db.exec(select(AuthSession)).one()
        row.expires_at = utcnow() - timedelta(seconds=1)
        db.add(row)
        db.commit()
    assert account.get("/jobs").status_code == 303


def test_htmx_requests_get_hx_redirect_when_signed_out(account):
    r = account.get("/jobs", headers={"HX-Request": "true"})
    assert r.status_code == 204
    assert r.headers["HX-Redirect"].startswith("/login")
