"""The criteria file (`data/criteria.yaml`): what to search for and how to score it.

Edited as YAML in Profile → Criteria. Each profile is searched and scored
separately; a job takes the score of whichever profile fits it best.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_YAML = """\
# Job Finder Hub criteria. Saving re-scores every job.

# Where you want to work. Leave blank to search the whole UK.
location: ""
distance_miles: 20
# Lowest yearly salary you'd consider (0 = no floor). Jobs with no salary listed still show.
salary_floor: 0
# Check for new jobs every this many minutes.
fetch_every_minutes: 60

calm_mode:
  enabled: true
  # How many of the best new matches to show on the Today list.
  daily_cap: 3

# Each profile is searched and scored separately.
#   searches:     what to type into the job sites
#   keywords:     words that make a job a better fit
#   must_have:    jobs missing these score lower
#   dealbreakers: a job whose title contains one of these scores zero
profiles:
  - name: Software apprenticeships
    searches: ["software developer apprenticeship", "software engineering apprentice"]
    keywords: [apprenticeship, apprentice, software, developer, python, javascript, coding]
    must_have: [apprentice]
    dealbreakers: [senior, lead, principal, manager]

  - name: Junior developer
    searches: ["junior python developer", "junior software developer", "graduate developer"]
    keywords: [python, junior, graduate, git, sql, linux, javascript, api, testing]
    must_have: []
    dealbreakers: [senior, lead, principal, manager]

  - name: Electronics or repair technician
    searches: ["electronics technician", "repair technician", "electronics test technician"]
    keywords: [electronics, soldering, repair, pcb, test, fault finding, technician, multimeter]
    must_have: []
    dealbreakers: [senior, manager, chartered]
"""


class CriteriaError(ValueError):
    pass


@dataclass
class Profile:
    name: str
    searches: list[str]
    keywords: list[str] = field(default_factory=list)
    must_have: list[str] = field(default_factory=list)
    dealbreakers: list[str] = field(default_factory=list)


@dataclass
class Criteria:
    location: str = ""
    distance_miles: int = 20
    salary_floor: int = 0
    fetch_every_minutes: int = 60
    calm_mode_on: bool = True
    daily_cap: int = 3
    profiles: list[Profile] = field(default_factory=list)


def _str_list(value, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
        raise CriteriaError(f"{where} should be a list, like [python, sql].")
    return [str(v).strip() for v in value if str(v).strip()]


def _int(value, where: str, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise CriteriaError(f"{where} should be a whole number.") from None
    if number < minimum:
        raise CriteriaError(f"{where} should be at least {minimum}.")
    return number


def parse(text: str) -> Criteria:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise CriteriaError(f"The YAML couldn't be read{where}. Check indentation and colons.") from None
    if not isinstance(raw, dict):
        raise CriteriaError("The file should be a set of 'name: value' settings.")

    calm = raw.get("calm_mode") or {}
    if not isinstance(calm, dict):
        raise CriteriaError("calm_mode should contain 'enabled' and 'daily_cap'.")

    profiles_raw = raw.get("profiles") or []
    if not isinstance(profiles_raw, list) or not profiles_raw:
        raise CriteriaError("Add at least one profile under 'profiles:'.")
    profiles = []
    for i, p in enumerate(profiles_raw, 1):
        if not isinstance(p, dict) or not str(p.get("name") or "").strip():
            raise CriteriaError(f"Profile {i} needs a name.")
        name = str(p["name"]).strip()
        searches = _str_list(p.get("searches"), f"{name}: searches")
        if not searches:
            raise CriteriaError(f"{name}: add at least one search, like [\"junior developer\"].")
        profiles.append(
            Profile(
                name=name,
                searches=searches,
                keywords=_str_list(p.get("keywords"), f"{name}: keywords"),
                must_have=_str_list(p.get("must_have"), f"{name}: must_have"),
                dealbreakers=_str_list(p.get("dealbreakers"), f"{name}: dealbreakers"),
            )
        )

    return Criteria(
        location=str(raw.get("location") or "").strip(),
        distance_miles=_int(raw.get("distance_miles", 20), "distance_miles", 1),
        salary_floor=_int(raw.get("salary_floor", 0), "salary_floor"),
        fetch_every_minutes=_int(raw.get("fetch_every_minutes", 60), "fetch_every_minutes", 15),
        # YAML reads a bare `on:` key as True, so older files written that way still work.
        calm_mode_on=bool(calm.get("enabled", calm.get(True, True))),
        daily_cap=_int(calm.get("daily_cap", 3), "calm_mode: daily_cap", 1),
        profiles=profiles,
    )


class CriteriaFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def text(self) -> str:
        return self.path.read_text() if self.path.exists() else DEFAULT_YAML

    def load(self) -> Criteria:
        try:
            return parse(self.text())
        except CriteriaError:
            # A broken file on disk shouldn't take the hub down; fall back to defaults.
            return parse(DEFAULT_YAML)

    def save(self, text: str) -> Criteria:
        criteria = parse(text)  # raises CriteriaError with a friendly message
        self.path.write_text(text if text.endswith("\n") else text + "\n")
        return criteria
