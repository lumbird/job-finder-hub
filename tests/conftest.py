from __future__ import annotations

import re

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PASSWORD = "correct horse battery"


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        data_dir=tmp_path,
        public_origin="http://testserver",
        cookie_secure=False,  # TestClient talks plain HTTP to "testserver"
        fernet_key=Fernet.generate_key().decode(),
        csrf_secret="test-secret",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def app(tmp_path, sleeps):
    return create_app(make_settings(tmp_path), sleep=sleeps)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, follow_redirects=False)


def csrf(client: TestClient, path: str = "/login") -> str:
    html = client.get(path, follow_redirects=True).text
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def do_setup(client: TestClient, password: str = PASSWORD):
    code = client.app.state.setup_code.issue()
    token = csrf(client, "/setup")
    return client.post(
        "/setup", data={"csrf_token": token, "code": code, "password": password, "confirm": password}
    )


def login(client: TestClient, password: str = PASSWORD, totp: str = ""):
    token = csrf(client, "/login")
    return client.post("/login", data={"csrf_token": token, "password": password, "totp": totp})


@pytest.fixture
def account(client):
    """An app with the account created and the client signed out."""
    assert do_setup(client).status_code == 303
    client.post("/logout", data={"csrf_token": csrf(client, "/jobs")})
    return client
