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
        self.status = {"www.reed.co.uk": 200, "api.adzuna.com": 200, "api.postcodes.io": 200}
        # name → [(local_type, county, lat, lon)] for postcodes.io /places
        self.places = {
            "leeds": [("City", "West Yorkshire", 53.7965, -1.5478)],
            "london": [("City", "Greater London", 51.5072, -0.1276)],
            "bradford": [("City", "West Yorkshire", 53.7938, -1.7564)],
            "farnborough": [("Village", "Warwickshire", 52.14, -1.36), ("Town", "Hampshire", 51.2869, -0.7526)],
        }
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
        if host == "api.postcodes.io" and request.url.path == "/places":
            q = request.url.params["q"].lower()
            result = [
                {"name_1": q.title(), "local_type": t, "county_unitary": county, "latitude": lat, "longitude": lon}
                for t, county, lat, lon in self.places.get(q, [])
            ]
            return httpx.Response(200, json={"status": 200, "result": result})
        return httpx.Response(404)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def hits(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]
