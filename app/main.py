"""Job Finder Hub web app. Run with `uvicorn --factory app.main:create_app`."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.config import Settings, load_settings
from app.criteria import CriteriaFile
from app.db import make_engine, run_migrations
from app.deps import NeedsLogin, templates, user_exists
from app.geo import GeoCache
from app.ingest import Fetcher
from app.routes import applications, auth, jobs, pages, reporting, settings as settings_routes
from app.scheduler import start_scheduler
from app.security.crypto import SecretBox
from app.security.csrf import CSRF_COOKIE
from app.security.passkeys import ChallengeStore
from app.security.setup_code import SetupCode

STATIC_DIR = Path(__file__).parent / "static"

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"
)


def create_app(
    settings: Settings | None = None, sleep=asyncio.sleep, http_transport=None, scheduler: bool = True
) -> FastAPI:
    """`http_transport` lets tests fake the job sites; `scheduler=False` skips background jobs."""
    settings = settings or load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    run_migrations(settings)
    engine = make_engine(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.scheduler = start_scheduler(app) if scheduler else None
        yield
        if app.state.scheduler:
            app.state.scheduler.shutdown(wait=False)

    app = FastAPI(title="Job Finder Hub", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.secret_box = SecretBox(settings.fernet_key)
    app.state.challenges = ChallengeStore()
    app.state.setup_code = SetupCode(settings.data_dir / ".setup-code")
    app.state.sleep = sleep  # swapped out in tests so throttling doesn't really wait
    app.state.http_transport = http_transport
    app.state.criteria_file = CriteriaFile(settings.data_dir / "criteria.yaml")
    app.state.geo = GeoCache(settings.data_dir / "geocache.json")
    app.state.fetcher = Fetcher(engine, app.state.secret_box, app.state.criteria_file, http_transport, app.state.geo)
    app.state.scheduler = None

    with Session(engine) as db:
        if not user_exists(db):
            app.state.setup_code.issue()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        minted = getattr(request.state, "csrf_cookie", None)
        if minted and request.cookies.get(CSRF_COOKIE) != minted:
            response.set_cookie(
                CSRF_COOKIE, minted, httponly=True, secure=settings.cookie_secure, samesite="strict", path="/"
            )
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if settings.public_origin.startswith("https://"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if not request.url.path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(NeedsLogin)
    async def needs_login(request: Request, _exc: NeedsLogin):
        with Session(engine) as db:
            target = "/login" if user_exists(db) else "/setup"
        if target == "/login" and request.method == "GET" and request.url.path not in ("/", "/login"):
            target += f"?next={request.url.path}"
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        if request.method != "GET" and "application/json" in request.headers.get("content-type", ""):
            return JSONResponse({"error": "Signed out. Reload the page."}, status_code=401)
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(
                request, "error.html", {"status": exc.status_code, "detail": exc.detail}, status_code=exc.status_code
            )
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return Response(
            (STATIC_DIR / "manifest.webmanifest").read_text(), media_type="application/manifest+json"
        )

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(auth.router)
    app.include_router(pages.router)
    app.include_router(jobs.router)
    app.include_router(applications.router)
    app.include_router(reporting.router)
    app.include_router(settings_routes.router)
    return app

