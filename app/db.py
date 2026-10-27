"""SQLite engine, migrations and sessions."""

from __future__ import annotations

from collections.abc import Iterator

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, event
from sqlmodel import Session, create_engine

from app.config import PROJECT_ROOT, Settings


def make_engine(settings: Settings) -> Engine:
    engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.close()

    return engine


def run_migrations(settings: Settings) -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", settings.db_url)
    command.upgrade(cfg, "head")


def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine) as session:
        yield session
