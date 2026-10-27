"""Reed.co.uk jobseeker API. Free key: https://www.reed.co.uk/developers/jobseeker"""

from __future__ import annotations

from datetime import datetime, timezone

from app.criteria import Criteria
from app.services.base import CredentialField, JobSitePlugin, Listing, ServiceError, clean_text, money

API = "https://www.reed.co.uk/api/1.0/search"


def _uk_date(value: str | None):
    try:
        return datetime.strptime(value or "", "%d/%m/%Y")
    except ValueError:
        return None


class Reed(JobSitePlugin):
    key = "reed"
    label = "Reed"
    summary = "UK job board with an official search API."
    signup_url = "https://www.reed.co.uk/developers/jobseeker"
    credential_fields = (CredentialField("api_key", "API key", help="From reed.co.uk/developers/jobseeker"),)

    @property
    def _auth(self):
        return (self.credentials.get("api_key", ""), "")

    async def check(self) -> None:
        await self._get(API, params={"keywords": "developer", "resultsToTake": 1}, auth=self._auth)

    async def search(self, query: str, criteria: Criteria) -> list[Listing]:
        params: dict = {"keywords": query, "resultsToTake": 100}
        if criteria.location:
            params["locationName"] = criteria.location
            params["distanceFromLocation"] = criteria.distance_miles
        if criteria.salary_floor:
            params["minimumSalary"] = criteria.salary_floor
        response = await self._get(API, params=params, auth=self._auth)
        try:
            results = response.json().get("results") or []
        except ValueError as exc:
            raise ServiceError("Reed sent something that wasn't JSON.") from exc

        listings = []
        for r in results:
            if not r.get("jobId") or not r.get("jobTitle"):
                continue
            expires = _uk_date(r.get("expirationDate"))
            posted = _uk_date(r.get("date"))
            listings.append(
                Listing(
                    external_id=str(r["jobId"]),
                    title=clean_text(r["jobTitle"]),
                    company=clean_text(r.get("employerName")),
                    location=clean_text(r.get("locationName")),
                    salary_min=money(r.get("minimumSalary")),
                    salary_max=money(r.get("maximumSalary")),
                    url=r.get("jobUrl") or f"https://www.reed.co.uk/jobs/{r['jobId']}",
                    description=clean_text(r.get("jobDescription")),
                    closing_date=expires.date() if expires else None,
                    posted_at=posted.replace(tzinfo=timezone.utc) if posted else None,
                )
            )
        return listings
