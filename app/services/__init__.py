"""Service plug-ins. Adding a board or reporting target means adding one class here."""

from __future__ import annotations

from app.services.adzuna import Adzuna
from app.services.base import PlannedService, Plugin
from app.services.reed import Reed

PLUGINS: dict[str, type[Plugin]] = {p.key: p for p in (Reed, Adzuna)}

PLANNED: tuple[PlannedService, ...] = (
    PlannedService("find_an_apprenticeship", "Find an Apprenticeship", "job_site", "GOV.UK apprenticeship vacancies.", 6),
    PlannedService("indeed_alerts", "Indeed alerts", "job_site", "Read from Indeed alert emails.", 4),
    PlannedService("linkedin_alerts", "LinkedIn alerts", "job_site", "Read from LinkedIn alert emails.", 4),
    PlannedService("cv_library_alerts", "CV-Library alerts", "job_site", "Read from CV-Library alert emails.", 4),
    PlannedService("careers_feeds", "Careers page feeds", "job_site", "RSS feeds from employers' careers pages.", 6),
    PlannedService("gmail", "Gmail", "email", "Read job emails and save reply drafts.", 4),
    PlannedService("outlook", "Outlook", "email", "Read job emails and save reply drafts.", 4),
    PlannedService("universal_credit", "Universal Credit", "reporting", "Entries formatted for copy and paste.", 5),
)

KIND_LABELS = {"job_site": "Job sites", "email": "Emails", "reporting": "Reporting"}
