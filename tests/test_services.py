from __future__ import annotations

import asyncio
import base64
from datetime import date

import httpx
import pytest

from app.criteria import DEFAULT_YAML, Area, parse
from app.services.adzuna import Adzuna
from app.services.base import NeedsLogin, Plugin, RateLimited, ServiceError
from app.services.reed import Reed
from tests.fakes import FakeBoards

CRITERIA = parse(DEFAULT_YAML.replace("salary_floor: 0", "salary_floor: 20000"))
LEEDS = Area("Leeds", 20)


@pytest.fixture(autouse=True)
def no_spacing(monkeypatch):
    monkeypatch.setattr(Plugin, "min_interval", 0)


def run(coro):
    return asyncio.run(coro)


async def _search(plugin_cls, creds, boards, query="junior python developer"):
    async with httpx.AsyncClient(transport=boards.transport) as http:
        return await plugin_cls(creds, http).search(query, CRITERIA, LEEDS)


def test_reed_search_parses_results_and_sends_criteria():
    boards = FakeBoards()
    listings = run(_search(Reed, {"api_key": "k123"}, boards))
    first = listings[0]
    assert first.external_id == "51234567"
    assert first.title == "Junior Python Developer"
    assert first.company == "Acme Software Ltd"
    assert (first.salary_min, first.salary_max) == (26000, 30000)
    assert first.closing_date == date(2099, 12, 30)
    assert first.url.startswith("https://www.reed.co.uk/jobs/")

    req = boards.hits("www.reed.co.uk")[0]
    assert req.url.params["keywords"] == "junior python developer"
    assert req.url.params["locationName"] == "Leeds"
    assert req.url.params["distanceFromLocation"] == "20"
    assert req.url.params["minimumSalary"] == "20000"
    # Reed uses the key as the Basic auth username with an empty password.
    assert req.headers["authorization"] == "Basic " + base64.b64encode(b"k123:").decode()


def test_adzuna_search_cleans_html_and_ignores_guessed_salaries():
    boards = FakeBoards()
    listings = run(_search(Adzuna, {"app_id": "id1", "app_key": "key1"}, boards))
    junior, tech = listings
    assert junior.title == "Junior Python Developer (Hybrid)"
    assert "<strong>" not in junior.description and "…" in junior.description
    assert junior.company == "ACME SOFTWARE LIMITED"
    assert junior.salary_max == 30000
    assert tech.salary_min is None and tech.salary_max is None  # salary_is_predicted = "1"

    req = boards.hits("api.adzuna.com")[0]
    assert req.url.params["app_id"] == "id1"
    assert req.url.params["where"] == "Leeds"
    assert req.url.params["distance"] == "32"  # 20 miles in km


@pytest.mark.parametrize(
    "status, error, health",
    [(401, NeedsLogin, "needs_login"), (403, NeedsLogin, "needs_login"), (429, RateLimited, "rate_limited"), (500, ServiceError, "broken")],
)
def test_http_errors_map_to_health(status, error, health):
    boards = FakeBoards()
    boards.status["www.reed.co.uk"] = status
    with pytest.raises(error) as exc:
        run(_search(Reed, {"api_key": "bad"}, boards))
    assert exc.value.health == health


def test_network_failure_is_a_service_error():
    def boom(request):
        raise httpx.ConnectError("no route")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
            await Reed({"api_key": "k"}, http).check()

    with pytest.raises(ServiceError, match="Couldn't reach Reed"):
        run(go())


def test_requests_are_spaced_out(monkeypatch):
    monkeypatch.setattr(Plugin, "min_interval", 0.2)
    boards = FakeBoards()

    async def go():
        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(transport=boards.transport) as http:
            plugin = Reed({"api_key": "k"}, http)
            start = loop.time()
            await plugin.check()
            await plugin.check()
            return loop.time() - start

    assert run(go()) >= 0.2
