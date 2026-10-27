from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import csrf, do_setup, login, make_settings


def test_security_headers(client):
    r = client.get("/setup")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_cookie_flags_when_secure(tmp_path, sleeps):
    app = create_app(make_settings(tmp_path, cookie_secure=True, public_origin="https://hub.example.ts.net"), sleep=sleeps)
    client = TestClient(app, base_url="https://hub.example.ts.net", follow_redirects=False)
    r = do_setup(client)
    cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("hub_session="))
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=strict" in cookie.lower()
    assert "Max-Age=604800" in cookie  # 7 days
    assert r.headers["strict-transport-security"]


def test_theme_defaults_to_system(account):
    login(account)
    html = account.get("/profile").text
    assert "<html lang=\"en-GB\">" in html
    assert 'media="(prefers-color-scheme: dark)" content="#1d2026"' in html


def test_theme_toggle_is_rendered_server_side(account):
    login(account)
    token = csrf(account, "/profile")
    r = account.post("/theme", data={"csrf_token": token, "theme": "dark", "next": "/profile"})
    assert r.headers["location"] == "/profile"
    html = account.get("/profile").text
    assert 'data-theme="dark"' in html
    assert '<meta name="theme-color" content="#1d2026">' in html
    assert re.search(r'value="dark"\s+checked', html)

    account.post("/theme", data={"csrf_token": token, "theme": "light"})
    assert 'data-theme="light"' in account.get("/jobs").text

    account.post("/theme", data={"csrf_token": token, "theme": "neon"})  # ignored
    assert 'data-theme="light"' in account.get("/jobs").text


def test_theme_works_before_sign_in(account):
    token = csrf(account, "/login")
    account.post("/theme", data={"csrf_token": token, "theme": "dark", "next": "/login"})
    assert 'data-theme="dark"' in account.get("/login").text


def test_tab_bar_marks_current_tab(account):
    login(account)
    html = account.get("/applications").text
    assert '<a href="/applications" aria-current="page">' in html
    for tab in ("jobs", "reporting", "profile"):
        assert f'href="/{tab}"' in html
    assert 'class="tabbar"' not in account.get("/login").text


def test_manifest_and_static(client):
    r = client.get("/manifest.webmanifest")
    assert r.headers["content-type"].startswith("application/manifest+json")
    assert r.json()["display"] == "standalone"
    for icon in r.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert client.get("/static/vendor/htmx.min.js").status_code == 200


def test_passkey_options(account):
    login(account)
    token = csrf(account, "/profile/security")
    r = account.post("/webauthn/register/options", headers={"X-CSRF-Token": token})
    body = r.json()
    assert body["rp"]["id"] == "testserver"
    assert body["authenticatorSelection"]["userVerification"] == "required"
    assert body["authenticatorSelection"]["residentKey"] == "required"
    assert "hub_webauthn" in r.cookies

    r = account.post("/webauthn/login/options", headers={"X-CSRF-Token": token})
    assert r.json()["rpId"] == "testserver"


def test_passkey_endpoints_reject_junk(account):
    token = csrf(account)
    account.post("/webauthn/login/options", headers={"X-CSRF-Token": token})
    r = account.post("/webauthn/login/verify", json={"credential": {"id": "nope"}}, headers={"X-CSRF-Token": token})
    assert r.status_code == 400

    login(account)
    account.post("/webauthn/register/options", headers={"X-CSRF-Token": token})
    r = account.post("/webauthn/register/verify", json={"credential": {"id": "x"}}, headers={"X-CSRF-Token": token})
    assert r.status_code == 400


def test_passkey_registration_needs_sign_in(account):
    token = csrf(account)
    r = account.post(
        "/webauthn/register/options", headers={"X-CSRF-Token": token, "Content-Type": "application/json"}, content="{}"
    )
    assert r.status_code == 401


def test_secrets_generated_into_env_file(tmp_path, monkeypatch):
    from app.config import load_settings

    for key in ("HUB_FERNET_KEY", "HUB_CSRF_SECRET", "HUB_DATA_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HUB_DATA_DIR", str(tmp_path / "data"))
    env = tmp_path / ".env"
    settings = load_settings(env)
    text = env.read_text()
    assert f"HUB_FERNET_KEY={settings.fernet_key}" in text
    assert oct(env.stat().st_mode)[-3:] == "600"
    # Second load keeps the same key.
    monkeypatch.delenv("HUB_FERNET_KEY")
    monkeypatch.delenv("HUB_CSRF_SECRET")
    assert load_settings(env).fernet_key == settings.fernet_key
