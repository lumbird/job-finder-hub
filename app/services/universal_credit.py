"""Universal Credit: entries formatted for pasting into the work search journal.

There's no public API, and scripting a login to a government account is fragile
and risky (spec: Constraints), so the hub only prepares text for you to paste.
Entries share as little as possible: the job title and the date. No employer, place, board or CV.
"""

from __future__ import annotations

from datetime import date

from app.services.base import Activity, ReportingPlugin


def _day(d: date) -> str:
    return d.strftime("%a %-d %b %Y")


class UniversalCredit(ReportingPlugin):
    key = "universal_credit"
    label = "Universal Credit"
    summary = "Work search entries ready to copy into your journal."

    def format_entry(self, a: Activity) -> str:
        job = a.role
        detail = a.detail.strip()
        if a.activity_type == "applied":
            body = f"Applied for {job}."
        elif a.activity_type == "interview":
            body = f"Interview arranged for {job}."
        elif a.activity_type == "follow_up":
            body = f"Followed up on my application for {job}."
        else:
            lead = {
                "cv_update": "Updated my CV",
                "course": "Training or course",
                "employer_research": "Researched employers",
                "interview_prep": "Prepared for an interview",
                "job_search": "Searched for jobs",
            }.get(a.activity_type)
            body = f"{lead}: {detail}" if lead and detail else (lead or detail or "Work search activity")
            if not body.endswith("."):
                body += "."
            return f"{_day(a.when)}: {body}"
        if detail:
            body += f" {detail}" if detail.endswith(".") else f" {detail}."
        return f"{_day(a.when)}: {body}"

    def weekly_text(self, week_start: date, entries: list[tuple[date, str, str]]) -> str:
        """entries: (date, activity type label, text). Returns paste-ready weekly text."""
        if not entries:
            return f"Week of {_day(week_start)}: no activities logged."
        lines = [f"Work search, week of {_day(week_start)} ({len(entries)} activit{'y' if len(entries) == 1 else 'ies'})", ""]
        lines += [f"- {text}" for _, _, text in sorted(entries, key=lambda e: e[0])]
        return "\n".join(lines)
