from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import create_app
from app.models import Job, JobSource, Service
from app.scheduler import backup_database
from app.services.base import Plugin
from tests.conftest import csrf, do_setup, make_settings
from tests.fakes import FakeBoards


@pytest.fixture(autouse=True)
def no_spacing(monkeypatch):
    monkeypatch.setattr(Plugin, "min_interval", 0)


@pytest.fixture
def boards() -> FakeBoards:
    return FakeBoards()


@pytest.fixture
def hub(tmp_path, sleeps, boards) -> TestClient:
    """Signed in, with no services connected yet."""
    app = create_app(make_settings(tmp_path), sleep=sleeps, http_transport=boards.transport, scheduler=False)
    client = TestClient(app, follow_redirects=False)
    assert do_setup(client).status_code == 303
    return client


def connect(client, key, **fields):
    token = csrf(client, f"/profile/services/{key}")
    return client.post(f"/profile/services/{key}", data={"csrf_token": token, **fields})


@pytest.fixture
def stocked(hub):
    """Both boards connected and one fetch done."""
    assert connect(hub, "reed", api_key="k").status_code == 303
    assert connect(hub, "adzuna", app_id="i", app_key="k").status_code == 303
    r = hub.post("/jobs/fetch", data={"csrf_token": csrf(hub, "/jobs")})
    assert r.headers["location"] == "/jobs?fetched=1"
    return hub


def jobs(client) -> list[Job]:
    with Session(client.app.state.engine) as db:
        return list(db.exec(select(Job)).all())


def job_named(client, title: str) -> Job:
    return next(j for j in jobs(client) if j.title.startswith(title))


# ---------- services ----------


def test_empty_jobs_page_points_to_services(hub):
    html = hub.get("/jobs").text
    assert "Connect a job site to start" in html
    assert 'href="/profile/services"' in html


def test_connecting_a_service_stores_key_encrypted_and_checks_it(hub, boards):
    r = connect(hub, "reed", api_key="secret-key-123")
    assert r.headers["location"] == "/profile/services/reed?checked=1"
    assert "Connected and working" in hub.get(r.headers["location"]).text
    with Session(hub.app.state.engine) as db:
        row = db.exec(select(Service).where(Service.name == "reed")).one()
        assert "secret-key-123" not in row.credentials
        assert row.health == "ok" and row.last_checked is not None
    assert len(boards.hits("www.reed.co.uk")) == 1
    assert "1 connected, all working" in hub.get("/profile").text


def test_rejected_key_shows_needs_new_key(hub, boards):
    boards.status["www.reed.co.uk"] = 401
    r = connect(hub, "reed", api_key="wrong")
    page = hub.get(r.headers["location"]).text
    assert "Needs new key" in page and "accept the key" in page
    assert "1 needs attention" in hub.get("/profile").text


def test_blank_secret_keeps_saved_one(hub, boards):
    connect(hub, "adzuna", app_id="id1", app_key="key1")
    connect(hub, "adzuna", app_id="id2", app_key="")
    assert boards.hits("api.adzuna.com")[-1].url.params["app_key"] == "key1"
    assert boards.hits("api.adzuna.com")[-1].url.params["app_id"] == "id2"


def test_disconnect(hub):
    connect(hub, "reed", api_key="k")
    hub.post("/profile/services/reed/disconnect", data={"csrf_token": csrf(hub, "/profile/services/reed")})
    assert "Not connected" in hub.get("/profile/services").text
    assert "Connect a job site to start" in hub.get("/jobs").text


def test_services_page_lists_planned_services(hub):
    html = hub.get("/profile/services").text
    for name in ("Reed", "Adzuna", "Gmail", "Outlook", "Universal Credit", "LinkedIn alerts"):
        assert name in html


def test_unknown_service_404(hub):
    assert hub.get("/profile/services/nope").status_code == 404


# ---------- fetching ----------


def test_fetch_merges_duplicates_and_skips_expired(stocked):
    titles = sorted(j.title for j in jobs(stocked))
    # Reed's junior job and Adzuna's "(Hybrid)" repost collapse into one; the 2020 advert is skipped.
    assert titles == ["Electronics Test Technician", "Junior Python Developer", "Senior Python Engineer"]
    junior = job_named(stocked, "Junior Python")
    with Session(stocked.app.state.engine) as db:
        sources = db.exec(select(JobSource).where(JobSource.job_id == junior.id)).all()
    assert len(sources) == 2
    # The longer Adzuna description wins.
    assert "Full training given" in junior.description
    assert "1 duplicate merged" in stocked.get("/jobs?fetched=1").text


def test_repeat_fetch_adds_nothing(stocked):
    before = len(jobs(stocked))
    stocked.post("/jobs/fetch", data={"csrf_token": csrf(stocked, "/jobs")})
    assert len(jobs(stocked)) == before
    assert "0 new jobs" in stocked.get("/jobs?fetched=1").text


def test_scores_and_reasons(stocked):
    assert job_named(stocked, "Senior").score == 0
    junior = job_named(stocked, "Junior Python")
    assert junior.score >= 70 and junior.profile == "Junior developer"
    tech = job_named(stocked, "Electronics")
    assert tech.profile == "Electronics or repair technician"


def test_a_broken_board_doesnt_stop_the_others(hub, boards):
    connect(hub, "reed", api_key="k")
    connect(hub, "adzuna", app_id="i", app_key="k")
    boards.status["api.adzuna.com"] = 429
    hub.post("/jobs/fetch", data={"csrf_token": csrf(hub, "/jobs")})
    assert len(jobs(hub)) == 2  # Reed's two live jobs
    assert "asked us to slow down" in hub.get("/jobs?fetched=1").text
    assert "Slowed down" in hub.get("/profile/services").text


# ---------- lists and calm mode ----------


def test_today_view_respects_daily_cap(stocked):
    token = csrf(stocked, "/profile/criteria")
    text = stocked.app.state.criteria_file.text().replace("daily_cap: 3", "daily_cap: 1")
    stocked.post("/profile/criteria", data={"csrf_token": token, "criteria": text})
    html = stocked.get("/jobs").text
    assert html.count('class="job"') == 1
    assert "Electronics Test Technician" in html  # the best match in the sample data
    assert "more are waiting" in html or "more is waiting" in html


def test_today_hides_zero_scores_but_all_new_shows_them(stocked):
    assert "Senior Python Engineer" not in stocked.get("/jobs?view=today").text
    assert "Senior Python Engineer" in stocked.get("/jobs?view=new").text


def test_calm_mode_off_defaults_to_all_new(stocked):
    text = stocked.app.state.criteria_file.text().replace("enabled: true", "enabled: false")
    stocked.post("/profile/criteria", data={"csrf_token": csrf(stocked, "/profile/criteria"), "criteria": text})
    assert stocked.get("/jobs").text.count('class="job"') == 3


def test_save_dismiss_and_undo_with_htmx(stocked):
    job = job_named(stocked, "Junior Python")
    token = csrf(stocked, "/jobs")
    hx = {"HX-Request": "true", "X-CSRF-Token": token}

    r = stocked.post(f"/jobs/{job.id}/state", data={"state": "saved"}, headers=hx)
    assert r.status_code == 200 and "Unsave" in r.text
    assert "Junior Python Developer" in stocked.get("/jobs?view=saved").text

    r = stocked.post(f"/jobs/{job.id}/state", data={"state": "dismissed"}, headers=hx)
    assert "Dismissed" in r.text and 'value="saved"' in r.text  # Undo goes back to Saved
    assert job_named(stocked, "Junior Python").state == "dismissed"

    stocked.post(f"/jobs/{job.id}/state", data={"state": "saved"}, headers=hx)
    assert job_named(stocked, "Junior Python").state == "saved"


def test_state_change_without_js_redirects_back(stocked):
    job = job_named(stocked, "Electronics")
    token = csrf(stocked, "/jobs")
    r = stocked.post(f"/jobs/{job.id}/state", data={"csrf_token": token, "state": "dismissed", "next": "/jobs?view=new"})
    assert r.headers["location"] == "/jobs?view=new"
    r = stocked.post(f"/jobs/{job.id}/state", data={"csrf_token": token, "state": "bogus"})
    assert r.status_code == 400


def test_closing_date_sort_and_warning(stocked):
    with Session(stocked.app.state.engine) as db:
        tech = db.exec(select(Job).where(Job.title.startswith("Electronics"))).one()
        tech.closing_date = date.today() + timedelta(days=1)
        db.add(tech)
        db.commit()
    html = stocked.get("/jobs?view=new&sort=closing").text
    titles = re.findall(r'class="job-title">([^<]+)<', html)
    assert titles[0] == "Electronics Test Technician"
    assert "Closes tomorrow" in html and "closing-soon" in html


def test_expired_jobs_close_on_next_fetch(stocked):
    with Session(stocked.app.state.engine) as db:
        junior = db.exec(select(Job).where(Job.title.startswith("Junior"))).one()
        junior.closing_date = date.today() - timedelta(days=1)
        db.add(junior)
        db.commit()
    stocked.app.state.fetcher.transport  # same fake boards
    with Session(stocked.app.state.engine) as db:
        from app.ingest import close_expired

        assert close_expired(db) == 1
    assert job_named(stocked, "Junior Python").state == "closed"


def test_job_detail_page(stocked):
    job = job_named(stocked, "Junior Python")
    html = stocked.get(f"/jobs/{job.id}").text
    assert "Full training given" in html
    assert html.count("Open ↗") == 2  # listed on both boards
    assert stocked.get("/jobs/99999").status_code == 404


# ---------- criteria ----------


def test_criteria_page_shows_defaults(hub):
    html = hub.get("/profile/criteria").text
    assert "Software apprenticeships" in html and "calm_mode" in html


def test_bad_criteria_not_saved(stocked):
    r = stocked.post("/profile/criteria", data={"csrf_token": csrf(stocked, "/profile/criteria"), "criteria": "profiles: ["})
    assert r.status_code == 400 and "Nothing was saved" in r.text
    assert not (stocked.app.state.settings.data_dir / "criteria.yaml").exists()


def test_saving_criteria_rescores(stocked):
    text = stocked.app.state.criteria_file.text().replace(
        "dealbreakers: [senior, lead, principal, manager]\n\n  - name: Electronics",
        "dealbreakers: [lead, principal, manager]\n\n  - name: Electronics",
    )
    r = stocked.post("/profile/criteria", data={"csrf_token": csrf(stocked, "/profile/criteria"), "criteria": text})
    assert r.headers["location"] == "/profile/criteria?saved=3"
    assert job_named(stocked, "Senior").score > 0


# ---------- backups ----------


def test_backup_keeps_last_fourteen(tmp_path, stocked):
    backups = tmp_path / "backups"
    backups.mkdir()
    for day in range(1, 21):
        (backups / f"hub-2020-01-{day:02d}.db").write_text("old")
    target = backup_database(stocked.app.state.settings.db_path, backups)
    kept = sorted(p.name for p in backups.glob("hub-*.db"))
    assert len(kept) == 14 and target.name in kept
    assert target.stat().st_size > 0
