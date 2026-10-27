"""Settings loaded from the environment and `.env`.

On first run, missing secrets are generated and appended to `.env` so the
encryption key lives outside the database, as the spec requires.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _ensure_env_secrets(env_path: Path) -> None:
    """Append any missing secrets to `.env` (mode 600)."""
    generated = {
        "HUB_FERNET_KEY": lambda: Fernet.generate_key().decode(),
        "HUB_CSRF_SECRET": lambda: secrets.token_urlsafe(32),
    }
    missing = {k: make() for k, make in generated.items() if not os.environ.get(k)}
    if not missing:
        return
    existing = env_path.read_text() if env_path.exists() else ""
    lines = [f"{k}={v}" for k, v in missing.items() if f"{k}=" not in existing]
    if lines:
        with env_path.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("# Generated on first run. Keep secret; losing HUB_FERNET_KEY loses stored credentials.\n")
            f.write("\n".join(lines) + "\n")
        env_path.chmod(0o600)
    os.environ.update(missing)


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    public_origin: str
    cookie_secure: bool
    fernet_key: str
    csrf_secret: str
    session_days: int = 7

    @property
    def db_path(self) -> Path:
        return self.data_dir / "hub.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def rp_id(self) -> str:
        """WebAuthn relying party ID: the hostname of the public origin."""
        return urlparse(self.public_origin).hostname or "localhost"


def load_settings(env_file: Path | None = None) -> Settings:
    env_path = env_file or PROJECT_ROOT / ".env"
    load_dotenv(env_path, override=False)
    _ensure_env_secrets(env_path)

    data_dir = Path(os.environ.get("HUB_DATA_DIR", PROJECT_ROOT / "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    public_origin = os.environ.get("HUB_PUBLIC_ORIGIN", "http://localhost:8000").rstrip("/")
    return Settings(
        data_dir=data_dir,
        public_origin=public_origin,
        # Browsers accept Secure cookies on http://localhost, so this stays on
        # unless you reach the hub over plain HTTP on another hostname.
        cookie_secure=_bool(os.environ.get("HUB_COOKIE_SECURE"), True),
        fernet_key=os.environ["HUB_FERNET_KEY"],
        csrf_secret=os.environ["HUB_CSRF_SECRET"],
    )
