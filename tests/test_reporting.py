from __future__ import annotations

from datetime import date, datetime, timezone

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
        "Mon 26 Oct 2026: Applied for Junior Python Developer at Acme Ltd (Leeds), found on Reed. "
        "Sent my CV (Software CV) and a cover note."
    )


def test_interview_and_follow_up_entries():
    assert UC.format_entry(Activity("interview", MON, role="Tech", company="Volt")) == "Mon 26 Oct 2026: Interview arranged for Tech at Volt."
    assert UC.format_entry(Activity("follow_up", MON, role="Tech", company="Volt")) == (
        "Mon 26 Oct 2026: Followed up on my application for Tech at Volt."
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
    assert entry.text.startswith("Tue 27 Oct 2026: Applied for Junior Developer at Acme (Leeds), found on Reed.")
    assert "Main CV" in entry.text


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
