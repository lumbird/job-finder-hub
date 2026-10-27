"""CSRF protection: a random cookie plus an HMAC of it sent back as a header or form field.

HTMX sends the token as the `X-CSRF-Token` header (set via `hx-headers` on <body>);
plain forms include a hidden `csrf_token` field.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request

CSRF_COOKIE = "hub_csrf"
CSRF_HEADER = "x-csrf-token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _sign(secret: str, value: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def csrf_cookie_value(request: Request) -> str:
    """The cookie value for this request, minting one if the browser has none yet."""
    value = getattr(request.state, "csrf_cookie", None)
    if value is None:
        value = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
        request.state.csrf_cookie = value
    return value


def csrf_token(request: Request) -> str:
    return _sign(request.app.state.settings.csrf_secret, csrf_cookie_value(request))


async def verify_csrf(request: Request) -> None:
    """FastAPI dependency: reject unsafe requests without a valid token."""
    if request.method in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE)
    sent = request.headers.get(CSRF_HEADER)
    if sent is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        sent = (await request.form()).get(CSRF_FIELD)
    expected = _sign(request.app.state.settings.csrf_secret, cookie) if cookie else None
    if not expected or not isinstance(sent, str) or not hmac.compare_digest(sent, expected):
        raise HTTPException(status_code=403, detail="Your session form expired. Reload the page and try again.")
