"""The plug-in contract every service follows (spec: Service operations)."""

from __future__ import annotations

import asyncio
import html
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import ClassVar

import httpx

from app.criteria import Criteria

USER_AGENT = "JobFinderHub/0.1 (personal job search; single user)"


class ServiceError(Exception):
    """Something went wrong talking to a service. `health` is what the Services page shows."""

    health = "broken"


class NeedsLogin(ServiceError):
    health = "needs_login"


class RateLimited(ServiceError):
    health = "rate_limited"


@dataclass(frozen=True)
class CredentialField:
    key: str
    label: str
    secret: bool = True
    help: str = ""


@dataclass
class Listing:
    """A job as one board describes it, before de-duplication."""

    external_id: str
    title: str
    company: str = ""
    location: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    url: str = ""
    description: str = ""
    closing_date: date | None = None
    posted_at: datetime | None = None


@dataclass(frozen=True)
class PlannedService:
    """Shown on the Services page so the full picture is visible before it's built."""

    key: str
    label: str
    kind: str
    summary: str
    phase: int


class Plugin:
    key: ClassVar[str]
    label: ClassVar[str]
    kind: ClassVar[str]
    summary: ClassVar[str]
    signup_url: ClassVar[str] = ""
    credential_fields: ClassVar[tuple[CredentialField, ...]] = ()
    min_interval: ClassVar[float] = 1.0  # seconds between requests to the same service

    _last_request: ClassVar[dict[str, float]] = {}
    _locks: ClassVar[dict[str, asyncio.Lock]] = {}

    def __init__(self, credentials: dict[str, str], http: httpx.AsyncClient) -> None:
        self.credentials = credentials
        self.http = http

    async def check(self) -> None:
        """Make one cheap call; raise ServiceError if it doesn't work."""
        raise NotImplementedError

    async def _get(self, url: str, **kwargs) -> httpx.Response:
        """GET with polite spacing between requests and errors mapped to health states."""
        lock = self._locks.setdefault(self.key, asyncio.Lock())
        async with lock:
            wait = self.min_interval - (time.monotonic() - self._last_request.get(self.key, 0))
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                response = await self.http.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, **kwargs)
            except httpx.HTTPError as exc:
                raise ServiceError(f"Couldn't reach {self.label}: {exc.__class__.__name__}.") from exc
            finally:
                self._last_request[self.key] = time.monotonic()
        if response.status_code in (401, 403):
            raise NeedsLogin(f"{self.label} didn't accept the key. Check it and save again.")
        if response.status_code == 429:
            raise RateLimited(f"{self.label} asked us to slow down. Will try again next round.")
        if response.status_code >= 400:
            raise ServiceError(f"{self.label} replied with error {response.status_code}.")
        return response


class JobSitePlugin(Plugin):
    kind = "job_site"

    async def search(self, query: str, criteria: Criteria) -> list[Listing]:
        raise NotImplementedError


_TAG = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str:
    """Strip HTML tags and entities that boards put in titles and snippets."""
    if not value:
        return ""
    text = html.unescape(_TAG.sub(" ", value))
    return " ".join(text.split())


def money(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


@dataclass
class Activity:
    activity_type: str
    when: date
    role: str = ""
    company: str = ""
    location: str = ""
    source: str = ""  # board it was found on
    cv_label: str = ""
    detail: str = ""  # free text for activities that aren't applications


class ReportingPlugin(Plugin):
    """A place work search gets reported. Formats text only; never logs in anywhere."""

    kind = "reporting"

    async def check(self) -> None:
        return None  # nothing to connect

    def format_entry(self, activity: Activity) -> str:
        raise NotImplementedError

    def weekly_text(self, week_start: date, entries: list[tuple[date, str, str]]) -> str:
        raise NotImplementedError
