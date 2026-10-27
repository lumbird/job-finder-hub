"""Adzuna jobs API (UK). Free key: https://developer.adzuna.com/"""

from __future__ import annotations

from datetime import datetime

from app.criteria import Criteria
from app.services.base import CredentialField, JobSitePlugin, Listing, ServiceError, clean_text, money

API = "https://api.adzuna.com/v1/api/jobs/gb/search/1"
KM_PER_MILE = 1.609


class Adzuna(JobSitePlugin):
    key = "adzuna"
    label = "Adzuna"
    summary = "Job search engine that gathers listings from many UK boards."
    signup_url = "https://developer.adzuna.com/"
    credential_fields = (
        CredentialField("app_id", "App ID", secret=False, help="From developer.adzuna.com → Dashboard"),
        CredentialField("app_key", "App key"),
    )

    def _params(self, **extra) -> dict:
        return {
            "app_id": self.credentials.get("app_id", ""),
            "app_key": self.credentials.get("app_key", ""),
            "content-type": "application/json",
            **extra,
        }

    async def check(self) -> None:
        await self._get(API, params=self._params(what="developer", results_per_page=1))

    async def search(self, query: str, criteria: Criteria) -> list[Listing]:
        extra: dict = {"what": query, "results_per_page": 50, "max_days_old": 30, "sort_by": "date"}
        if criteria.location:
            extra["where"] = criteria.location
            extra["distance"] = round(criteria.distance_miles * KM_PER_MILE)
        if criteria.salary_floor:
            extra["salary_min"] = criteria.salary_floor
        response = await self._get(API, params=self._params(**extra))
        try:
            results = response.json().get("results") or []
        except ValueError as exc:
            raise ServiceError("Adzuna sent something that wasn't JSON.") from exc

        listings = []
        for r in results:
            if not r.get("id") or not r.get("title"):
                continue
            # Adzuna estimates salaries when the advert has none; don't present guesses as fact.
            predicted = str(r.get("salary_is_predicted", "0")) == "1"
            try:
                posted = datetime.fromisoformat(str(r.get("created", "")).replace("Z", "+00:00"))
            except ValueError:
                posted = None
            listings.append(
                Listing(
                    external_id=str(r["id"]),
                    title=clean_text(r["title"]),
                    company=clean_text((r.get("company") or {}).get("display_name")),
                    location=clean_text((r.get("location") or {}).get("display_name")),
                    salary_min=None if predicted else money(r.get("salary_min")),
                    salary_max=None if predicted else money(r.get("salary_max")),
                    url=r.get("redirect_url") or "",
                    description=clean_text(r.get("description")),
                    posted_at=posted,
                )
            )
        return listings
