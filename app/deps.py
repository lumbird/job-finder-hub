"""Shared FastAPI dependencies and template setup."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.models import User
from app.security.csrf import csrf_token
from app.security.sessions import current_user

THEMES = ("system", "light", "dark")
THEME_COOKIE = "hub_theme"
# Browser/status-bar colours; keep in step with --hub-bg in static/app.css.
THEME_COLORS = {"light": "#ffffff", "dark": "#1d2026"}

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["THEME_COLORS"] = THEME_COLORS


def theme_for(request: Request) -> str:
    theme = request.cookies.get(THEME_COOKIE, "system")
    return theme if theme in THEMES else "system"


def request_origin(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


templates.env.globals["theme_for"] = theme_for
templates.env.globals["request_origin"] = request_origin


def salary_text(low: float | None, high: float | None) -> str:
    def fmt(n: float) -> str:
        # Hourly and daily rates are small numbers; show pence only when there are some.
        return f"£{n:,.2f}" if n < 1000 and n != int(n) else f"£{n:,.0f}"

    if low and high and low != high:
        return f"{fmt(low)}–{fmt(high)}"
    if low or high:
        return fmt(low or high)
    return ""


def short_date(value) -> str:
    return value.strftime("%-d %b") if value else ""


templates.env.filters["short_date"] = short_date
templates.env.globals["salary_text"] = salary_text


def get_db(request: Request) -> Iterator[Session]:
    with Session(request.app.state.engine) as session:
        yield session


def user_exists(db: Session) -> bool:
    return db.exec(select(User.id).limit(1)).first() is not None


class NeedsLogin(Exception):
    """Raised by require_user; turned into a redirect to /login or /setup."""


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(db, request)
    if user is None:
        raise NeedsLogin()
    request.state.user = user
    return user


def safe_next(target: str | None, default: str = "/") -> str:
    """Only allow same-site relative paths as redirect targets."""
    if not target:
        return default
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return default
    return target
