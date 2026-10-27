"""A fake internet for job-site plug-ins, serving recorded-style sample responses."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeBoards:
    """httpx transport that answers Reed and Adzuna, recording every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = {"www.reed.co.uk": 200, "api.adzuna.com": 200}
        self.reed = fixture("reed_search.json")
        self.adzuna = fixture("adzuna_search.json")

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        status = self.status.get(host, 404)
        if status != 200:
            return httpx.Response(status, json={"error": "nope"})
        if host == "www.reed.co.uk":
            return httpx.Response(200, json=self.reed)
        if host == "api.adzuna.com":
            return httpx.Response(200, json=self.adzuna)
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def hits(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]
