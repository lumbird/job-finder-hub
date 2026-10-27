from __future__ import annotations

import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import applications as apps
from app.main import create_app
from app.models import Application, Cluster, ClusterJob, CvVersion, Job, Service, utcnow
from tests.conftest import csrf, do_setup, make_settings

PDF = b"%PDF-1.4\n% test cv\n"


@pytest.fixture
def hub(tmp_path, sleeps) -> TestClient:
    app = create_app(make_settings(tmp_path), sleep=sleeps, scheduler=False)
    client = TestClient(app, follow_redirects=False)
    assert do_setup(client).status_code == 303
    return client


def add_job(client, title, company, location="Leeds", profile="Junior developer", state="saved", score=80) -> Job:
    with Session(client.app.state.engine) as db:
        service = db.exec(select(Service)).first()
        if service is None:
            service = Service(kind="job_site", name="reed", credentials="connected")
            db.add(service)
            db.flush()
        job = Job(
            source_service_id=service.id,
            external_id=f"{title}-{company}",
            title=title,
            company=company,
            location=location,
            url=f"https://example.test/{len(title)}",
            fingerprint=f"{title}|{company}",
            profile=profile,
            state=state,
            score=score,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


def post(client, path, page="/applications", **data):
    return client.post(path, data={"csrf_token": csrf(client, page), **data})


def db_rows(client, model):
    with Session(client.app.state.engine) as db:
        return list(db.exec(select(model)).all())


# ---------- the already-applied guard ----------


def test_guard_spots_same_role_same_company(hub):
    first = add_job(hub, "Junior Python Developer", "Acme Ltd")
    post(hub, f"/jobs/{first.id}/apply")
    again = add_job(hub, "Junior Python Developer (Hybrid)", "ACME LIMITED", location="Leeds, West Yorkshire")
    r = post(hub, f"/jobs/{again.id}/apply")
    assert r.status_code == 409
    assert "You started an application for Junior Python Developer at Acme Ltd" in r.text
    assert len(db_rows(hub, Application)) == 1

    r = post(hub, f"/jobs/{again.id}/apply", force="1")
    assert r.status_code == 303
    assert len(db_rows(hub, Application)) == 2


def test_guard_spots_agency_reposts(hub):
    first = add_job(hub, "Junior Python Developer", "Acme Ltd")
    post(hub, f"/jobs/{first.id}/apply")
    repost = add_job(hub, "Junior Python Developer", "Hays Recruitment", location="Leeds")
    r = post(hub, f"/jobs/{repost.id}/apply")
    assert r.status_code == 409 and "posted again by an agency" in r.text


def test_guard_ignores_different_roles_and_old_applications(hub):
    first = add_job(hub, "Junior Python Developer", "Acme Ltd")
    post(hub, f"/jobs/{first.id}/apply")
    other = add_job(hub, "Electronics Technician", "Acme Ltd")
    assert post(hub, f"/jobs/{other.id}/apply").status_code == 303

    # A matching application from over six months ago doesn't count.
    with Session(hub.app.state.engine) as db:
        for app in db.exec(select(Application)).all():
            app.created_at = utcnow() - timedelta(days=200)
            db.add(app)
        db.commit()
    again = add_job(hub, "Junior Python Developer", "Acme")
    assert post(hub, f"/jobs/{again.id}/apply").status_code == 303


def test_different_agency_role_elsewhere_is_fine(hub):
    first = add_job(hub, "Junior Python Developer", "Acme Ltd", location="Leeds")
    post(hub, f"/jobs/{first.id}/apply")
    elsewhere = add_job(hub, "Junior Python Developer", "Hays Recruitment", location="Bristol")
    assert post(hub, f"/jobs/{elsewhere.id}/apply").status_code == 303


def test_looks_like_agency():
    assert apps.looks_like_agency("Hays Recruitment")
    assert apps.looks_like_agency("Reed Talent Solutions")
    assert not apps.looks_like_agency("Acme Software Ltd")


# ---------- cover notes ----------


def test_fill_note():
    job = Job(source_service_id=1, external_id="x", title="Repair Technician", company="Fixit", location="York", fingerprint="f")
    note = apps.fill_note("Dear {company}, re {role} in {location}. {unknown} stays.", job)
    assert note == "Dear Fixit, re Repair Technician in York. {unknown} stays."


# ---------- single applications ----------


def test_apply_from_a_saved_job_and_update(hub):
    job = add_job(hub, "Junior Python Developer", "Acme")
    assert "Apply</button>" in hub.get("/jobs?view=saved").text

    r = post(hub, f"/jobs/{job.id}/apply", page="/jobs")
    app_url = r.headers["location"].split("?")[0]
    assert "Application started" in hub.get(r.headers["location"]).text
    # Applying again goes to the same application.
    assert post(hub, f"/jobs/{job.id}/apply").headers["location"] == app_url
    assert "Application · Found" in hub.get("/jobs?view=saved").text

    follow = (date.today() + timedelta(days=7)).isoformat()
    r = post(hub, app_url, page=app_url, status="applied", follow_up_on=follow, cover_note="Hi Acme", notes="Spoke to Sam")
    assert r.status_code == 303
    app = db_rows(hub, Application)[0]
    assert app.status == "applied" and app.applied_at is not None
    assert app.follow_up_on.isoformat() == follow and app.notes == "Spoke to Sam"


def test_follow_ups_due_show_first(hub):
    job = add_job(hub, "Junior Python Developer", "Acme")
    post(hub, f"/jobs/{job.id}/apply")
    app = db_rows(hub, Application)[0]
    post(hub, f"/applications/{app.id}", status="applied", follow_up_on=date.today().isoformat())
    html = hub.get("/applications").text
    assert "Follow-ups due" in html and "due today" in html


def test_status_filters(hub):
    a = add_job(hub, "Junior Python Developer", "Acme")
    b = add_job(hub, "Electronics Technician", "Volt")
    post(hub, f"/jobs/{a.id}/apply")
    post(hub, f"/jobs/{b.id}/apply")
    app_a, app_b = db_rows(hub, Application)
    post(hub, f"/applications/{app_b.id}", status="closed")
    active = hub.get("/applications").text
    assert "Junior Python Developer" in active and "Electronics Technician" not in active
    assert "Electronics Technician" in hub.get("/applications?status=closed").text


def test_bad_inputs(hub):
    job = add_job(hub, "Junior Python Developer", "Acme")
    post(hub, f"/jobs/{job.id}/apply")
    app = db_rows(hub, Application)[0]
    assert post(hub, f"/applications/{app.id}", status="nonsense").status_code == 400
    assert post(hub, f"/applications/{app.id}", status="found", follow_up_on="31/02").status_code == 400
    assert hub.get("/applications/999").status_code == 404


# ---------- clusters ----------


def make_cluster(hub) -> Cluster:
    for i, company in enumerate(("Acme", "Beta", "Gamma")):
        add_job(hub, f"Junior Developer {i}", company, score=70 + i)
    add_job(hub, "Repair Technician", "Fixit", profile="Electronics or repair technician")
    html = hub.get("/applications").text
    assert "Group 3 saved jobs" in html and "Group 1 saved job " in html
    r = post(hub, "/clusters", profile="Junior developer")
    assert re.fullmatch(r"/clusters/\d+", r.headers["location"])
    return db_rows(hub, Cluster)[0]


def test_cluster_review_preview_and_remove(hub):
    cluster = make_cluster(hub)
    page = hub.get(f"/clusters/{cluster.id}").text
    assert "Jobs in this batch (3)" in page
    assert "Dear Gamma hiring team" in page  # preview uses the best match first

    gamma = next(j for j in db_rows(hub, Job) if j.company == "Gamma")
    post(hub, f"/clusters/{cluster.id}/remove/{gamma.id}", page=f"/clusters/{cluster.id}")
    assert "Jobs in this batch (2)" in hub.get(f"/clusters/{cluster.id}").text
    # Jobs already in a batch aren't offered for another.
    assert "Group 2 saved jobs" not in hub.get("/applications").text


def test_cluster_approve_and_work_through_queue(hub):
    with Session(hub.app.state.engine) as db:
        db.add(CvVersion(label="Dev CV", file_path="x.pdf", original_name="cv.pdf", is_default=True))
        db.commit()
    cluster = make_cluster(hub)
    page = f"/clusters/{cluster.id}"
    template = "Hello {company}, I'd like the {role} job."
    r = post(hub, page, page=page, name="Junior dev batch", cover_note_template=template, cv_version_id="1", approve="1")
    assert r.headers["location"] == f"/clusters/{cluster.id}/queue"

    applications = db_rows(hub, Application)
    assert len(applications) == 3
    assert {a.status for a in applications} == {"found"}
    assert all(a.cv_version_id == 1 and a.cluster_id == cluster.id for a in applications)
    assert any(a.cover_note == "Hello Gamma, I'd like the Junior Developer 2 job." for a in applications)

    queue = hub.get(f"/clusters/{cluster.id}/queue").text
    assert "0 of 3 done" in queue and "Hello Gamma" in queue and "Open advert" in queue

    jobs = {j.company: j for j in db_rows(hub, Job)}
    post(hub, f"/clusters/{cluster.id}/queue/{jobs['Gamma'].id}", page=page, outcome="applied")
    post(hub, f"/clusters/{cluster.id}/queue/{jobs['Beta'].id}", page=page, outcome="skipped")
    assert "2 of 3 done" in hub.get(f"/clusters/{cluster.id}/queue").text
    post(hub, f"/clusters/{cluster.id}/queue/{jobs['Acme'].id}", page=page, outcome="applied")

    done = hub.get(f"/clusters/{cluster.id}/queue").text
    assert "Batch finished" in done and "You applied for 2 jobs" in done
    assert db_rows(hub, Cluster)[0].state == "done"
    by_job = {a.job_id: a.status for a in db_rows(hub, Application)}
    assert by_job[jobs["Gamma"].id] == "applied" and by_job[jobs["Beta"].id] == "found"


def test_cluster_approval_keeps_existing_single_applications(hub):
    cluster = make_cluster(hub)
    acme = next(j for j in db_rows(hub, Job) if j.company == "Acme")
    # Started on its own before approval (e.g. from the Jobs tab).
    with Session(hub.app.state.engine) as db:
        db.add(Application(job_id=acme.id, cover_note="My own note"))
        db.commit()
    post(hub, f"/clusters/{cluster.id}", name="x", cover_note_template="T {company}", approve="1")
    notes = {a.job_id: a.cover_note for a in db_rows(hub, Application)}
    assert notes[acme.id] == "My own note"


def test_delete_draft_cluster(hub):
    cluster = make_cluster(hub)
    post(hub, f"/clusters/{cluster.id}/delete")
    assert db_rows(hub, Cluster) == [] and db_rows(hub, ClusterJob) == []
    assert all(j.state == "saved" for j in db_rows(hub, Job))


# ---------- CV versions ----------


def upload(hub, name="cv.pdf", data=PDF, label="Main CV"):
    token = csrf(hub, "/profile/cvs")
    return hub.post("/profile/cvs", data={"csrf_token": token, "label": label}, files={"file": (name, data, "application/pdf")})


def test_cv_upload_default_download_delete(hub):
    assert upload(hub).headers["location"] == "/profile/cvs?done=added"
    assert upload(hub, label="Electronics CV").status_code == 303
    first, second = sorted(db_rows(hub, CvVersion), key=lambda c: c.id)
    assert first.is_default and not second.is_default
    stored = hub.app.state.settings.data_dir / "cvs" / first.file_path
    assert stored.read_bytes() == PDF and oct(stored.stat().st_mode)[-3:] == "600"

    r = hub.get(f"/profile/cvs/{first.id}/file")
    assert r.content == PDF and "cv.pdf" in r.headers["content-disposition"]

    post(hub, f"/profile/cvs/{second.id}/default", page="/profile/cvs")
    assert {c.label: c.is_default for c in db_rows(hub, CvVersion)} == {"Main CV": False, "Electronics CV": True}

    post(hub, f"/profile/cvs/{first.id}/delete", page="/profile/cvs")
    assert [c.label for c in db_rows(hub, CvVersion)] == ["Electronics CV"]
    assert not stored.exists()


@pytest.mark.parametrize(
    "name, data, message",
    [
        ("cv.exe", b"MZ", "Upload a PDF"),
        ("cv.pdf", b"not a pdf", "real PDF"),
        ("cv.docx", b"nope", "real .docx"),
        ("cv.pdf", b"", "empty"),
        ("cv.pdf", b"%PDF" + b"x" * (5 * 1024 * 1024), "over 5 MB"),
    ],
)
def test_cv_upload_rejects_bad_files(hub, name, data, message):
    r = upload(hub, name=name, data=data)
    assert r.status_code == 400 and message in r.text
    assert db_rows(hub, CvVersion) == []


def test_cv_sent_with_an_application_is_kept(hub):
    upload(hub)
    job = add_job(hub, "Junior Python Developer", "Acme")
    post(hub, f"/jobs/{job.id}/apply")  # picks the default CV
    assert db_rows(hub, Application)[0].cv_version_id is not None
    cv = db_rows(hub, CvVersion)[0]
    r = post(hub, f"/profile/cvs/{cv.id}/delete", page="/profile/cvs")
    assert r.status_code == 400 and "kept as a record" in r.text
    assert "sent 1 time" in hub.get("/profile/cvs").text
