from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app import reporting
from app.main import create_app
from app.models import Application, CvVersion, Job, JobSource, ReportEntry, Service
from app.services.base import Activity
from app.services.universal_credit import UniversalCredit
from tests.conftest import make_settings

UC = UniversalCredit({}, None)
MON = date(2026, 10, 26)


# ---------- formatting ----------


def test_applied_entry_reads_like_a_journal_line():
    text = UC.format_entry(
        Activity("applied", MON, role="Junior Python Developer", company="Acme Ltd", location="Leeds", source="Reed", cv_label="Software CV")
    )
    assert text == (
        "Mon 26 Oct 2026: Applied for Junior Python Developer."
    )


def test_interview_and_follow_up_entries():
    assert UC.format_entry(Activity("interview", MON, role="Tech", company="Volt")) == "Mon 26 Oct 2026: Interview arranged for Tech."
    assert UC.format_entry(Activity("follow_up", MON, role="Tech", company="Volt")) == (
        "Mon 26 Oct 2026: Followed up on my application for Tech."
    )


@pytest.mark.parametrize(
    "kind, detail, expected",
    [
        ("cv_update", "added my soldering course", "Updated my CV: added my soldering course."),
        ("course", "Python basics on FutureLearn.", "Training or course: Python basics on FutureLearn."),
        ("employer_research", "", "Researched employers."),
        ("other", "Went to a jobs fair", "Went to a jobs fair."),
    ],
)
def test_other_activities(kind, detail, expected):
    assert UC.format_entry(Activity(kind, MON, detail=detail)) == f"Mon 26 Oct 2026: {expected}"


def test_weekly_text():
    text = UC.weekly_text(MON, [(date(2026, 10, 28), "", "B"), (MON, "", "A")])
    assert text.splitlines() == ["Work search, week of Mon 26 Oct 2026 (2 activities)", "", "- A", "- B"]
    assert "no activities" in UC.weekly_text(MON, [])


# ---------- rules ----------


@pytest.fixture
def db(tmp_path, sleeps):
    app = create_app(make_settings(tmp_path), sleep=sleeps, scheduler=False)
    with Session(app.state.engine) as session:
        yield session


def _application(db) -> Application:
    reed = Service(kind="job_site", name="reed", credentials="x")
    db.add(reed)
    db.flush()
    job = Job(source_service_id=reed.id, external_id="1", title="Junior Developer", company="Acme", location="Leeds", fingerprint="f")
    cv = CvVersion(label="Main CV", file_path="a.pdf", original_name="a.pdf")
    db.add(job)
    db.add(cv)
    db.flush()
    db.add(JobSource(job_id=job.id, service_id=reed.id, external_id="1"))
    app = Application(job_id=job.id, cv_version_id=cv.id, status="applied", applied_at=datetime(2026, 10, 27, 10, tzinfo=timezone.utc))
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def test_reporting_an_application_uses_its_details(db):
    app = _application(db)
    entry = reporting.report_application(db, app, "applied", "universal_credit")
    assert entry.state == "ready" and entry.activity_date == date(2026, 10, 27)
    assert entry.text.startswith("Tue 27 Oct 2026: Applied for Junior Developer.")
    assert "Main CV" not in entry.text and "Acme" not in entry.text  # title and date only


def test_choosing_twice_does_not_duplicate(db):
    app = _application(db)
    first = reporting.report_application(db, app, "applied", "universal_credit")
    second = reporting.report_application(db, app, "applied", "universal_credit")
    assert first.id == second.id
    reporting.report_application(db, app, "interview", "universal_credit")
    assert len(db.exec(select(ReportEntry)).all()) == 2


def test_mark_and_undo(db):
    entry = reporting.log_activity(db, "universal_credit", "course", MON, "First aid")
    reporting.mark_reported(db, entry)
    assert entry.state == "reported" and entry.reported_at
    reporting.unmark(db, entry)
    assert entry.state == "ready" and entry.reported_at is None


def test_bad_activity_types_rejected(db):
    app = _application(db)
    with pytest.raises(ValueError):
        reporting.report_application(db, app, "course", "universal_credit")
    with pytest.raises(ValueError):
        reporting.log_activity(db, "universal_credit", "applied", MON, "")
    with pytest.raises(KeyError):
        reporting.reporting_service(db, "reed")


def test_weekly_summary_covers_monday_to_sunday(db):
    for day in (date(2026, 10, 25), MON, date(2026, 11, 1), date(2026, 11, 2)):
        reporting.log_activity(db, "universal_credit", "job_search", day, f"searched {day.isoformat()}")
    text, entries = reporting.weekly_summary(db, "universal_credit", reporting.week_start(date(2026, 10, 29)))
    assert [e.activity_date for e in entries] == [MON, date(2026, 11, 1)]
    assert "(2 activities)" in text


# ---------- Reporting tab ----------


@pytest.fixture
def hub(tmp_path, sleeps):
    from fastapi.testclient import TestClient

    from tests.conftest import do_setup

    app = create_app(make_settings(tmp_path), sleep=sleeps, scheduler=False)
    client = TestClient(app, follow_redirects=False)
    assert do_setup(client).status_code == 303
    return client


def _post(client, path, **data):
    from tests.conftest import csrf

    return client.post(path, data={"csrf_token": csrf(client, "/reporting"), **data})


def _entries(client) -> list[ReportEntry]:
    with Session(client.app.state.engine) as db:
        return list(db.exec(select(ReportEntry).order_by(ReportEntry.id)).all())


def test_empty_reporting_tab(hub):
    html = hub.get("/reporting").text
    assert "Nothing waiting" in html and "Log other work search" in html


def test_log_copy_edit_and_mark_reported(hub):
    r = _post(hub, "/reporting/log", activity_type="course", when="2026-10-26", detail="Python basics")
    assert r.headers["location"] == "/reporting?done=logged"
    html = hub.get("/reporting").text
    assert "Mon 26 Oct 2026: Training or course: Python basics." in html
    assert 'data-copy="#entry-1"' in html

    _post(hub, "/reporting/1/text", text="Mon 26 Oct 2026: Did the Python basics course (4 hours).")
    assert "(4 hours)" in _entries(hub)[0].text

    _post(hub, "/reporting/1/reported")
    html = hub.get("/reporting").text
    assert "Recently reported" in html and "Nothing waiting" in html

    _post(hub, "/reporting/1/undo")
    assert _entries(hub)[0].state == "ready"


def test_copy_all_and_mark_all(hub):
    _post(hub, "/reporting/log", activity_type="cv_update", detail="new projects section")
    _post(hub, "/reporting/log", activity_type="job_search", detail="Reed and Indeed")
    html = hub.get("/reporting").text
    assert "Copy all" in html and 'id="all-entries"' in html
    _post(hub, "/reporting/all-reported")
    assert {e.state for e in _entries(hub)} == {"reported"}


def test_dont_report_removes_only_ready_entries(hub):
    _post(hub, "/reporting/log", activity_type="other", detail="jobs fair")
    _post(hub, "/reporting/log", activity_type="other", detail="careers talk")
    _post(hub, "/reporting/2/reported")
    _post(hub, "/reporting/1/remove")
    _post(hub, "/reporting/2/remove")  # reported: kept until undone
    assert [e.id for e in _entries(hub)] == [2]


def test_log_rejects_bad_input(hub):
    assert _post(hub, "/reporting/log", activity_type="applied").status_code == 400
    assert _post(hub, "/reporting/log", activity_type="course", when="not-a-date").status_code == 400


def test_week_page(hub):
    from app.reporting import week_start

    _post(hub, "/reporting/log", activity_type="job_search", detail="searched")
    html = hub.get("/reporting/week").text
    assert "Week summary" in html and "(1 activity)" in html and "Copy summary" in html
    assert "Later" not in html  # can't page into the future
    last_week = week_start(date.today()) - timedelta(days=7)
    earlier = hub.get(f"/reporting/week?start={last_week.isoformat()}").text
    assert "Later" in earlier and "no activities logged" in earlier
    assert "no activities logged" in hub.get("/reporting/week?start=2020-01-08").text


def test_services_page_shows_universal_credit_ready(hub):
    html = hub.get("/profile/services").text
    assert '<a href="/reporting">' in html and "Ready" in html


# ---------- Report dropdown ----------


def _hub_application(hub) -> Application:
    with Session(hub.app.state.engine) as db:
        return _application(db)


def test_prompt_after_applying_then_report(hub):
    app = _hub_application(hub)
    page = hub.get(f"/applications/{app.id}?done=applied").text
    assert "Report this to Universal Credit?" in page

    r = _post(hub, f"/applications/{app.id}/report", activity_type="applied")
    assert r.headers["location"] == f"/applications/{app.id}?done=queued"
    page = hub.get(r.headers["location"]).text
    assert "Added to Ready to paste" in page
    assert "Universal Credit: Applied for a job · Ready to paste" in page
    assert "Report this to Universal Credit?" not in hub.get(f"/applications/{app.id}?done=applied").text
    assert "Applied for Junior Developer." in hub.get("/reporting").text


def test_nothing_reported_without_choosing(hub):
    app = _hub_application(hub)
    hub.get(f"/applications/{app.id}")
    assert _entries(hub) == []
    assert "Nothing is reported unless you choose it" in hub.get(f"/applications/{app.id}").text


def test_report_rejects_other_activity_types(hub):
    app = _hub_application(hub)
    assert _post(hub, f"/applications/{app.id}/report", activity_type="course").status_code == 400
    assert _post(hub, "/applications/999/report", activity_type="applied").status_code == 404


def test_report_from_batch_queue(hub):
    from app.models import Cluster, ClusterJob

    app = _hub_application(hub)
    with Session(hub.app.state.engine) as db:
        cluster = Cluster(name="Batch", state="approved")
        db.add(cluster)
        db.flush()
        db.add(ClusterJob(cluster_id=cluster.id, job_id=app.job_id, queue_state="applied"))
        db.commit()
        cid = cluster.id
    page = hub.get(f"/clusters/{cid}/queue").text
    assert "Report Junior Developer to Universal Credit" in page
    r = _post(hub, f"/applications/{app.id}/report", activity_type="applied", next=f"/clusters/{cid}/queue")
    assert r.headers["location"] == f"/clusters/{cid}/queue?done=queued"
    assert "Report Junior Developer to Universal Credit" not in hub.get(f"/clusters/{cid}/queue").text


def test_export_applied_jobs(hub):
    with Session(hub.app.state.engine) as db:
        _application(db)
    r = hub.get("/reporting/export/applied.csv?detail=coach")
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    lines = r.text.lstrip("﻿").splitlines()
    assert lines == ["Job title,Date applied,Applied", "Junior Developer,2026-10-27,Yes"]  # nothing else shared

    full = hub.get("/reporting/export/applied.csv?detail=full").text
    assert "Acme" in full and "Main CV" in full and "Applied" in full

    text = hub.get("/reporting/export/applied.txt?detail=coach").text
    assert "2026-10-27  Junior Developer" in text and "Acme" not in text
    assert "Junior Developer" not in hub.get("/reporting/export/applied.csv?since=2026-10-28").text
    assert hub.get("/reporting/export/applied.pdf").status_code == 404
