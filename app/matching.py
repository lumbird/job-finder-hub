"""Matching v1: keyword scoring against the criteria file, and de-duplication fingerprints."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.criteria import Criteria, Profile

STRONG_MATCH = 70  # score at or above this counts as a strong match

_COMPANY_SUFFIXES = {"ltd", "limited", "plc", "llp", "llc", "inc", "uk", "group", "the", "co"}
_NON_WORD = re.compile(r"[^a-z0-9+#]+")
_BRACKETS = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def normalise_title(title: str) -> str:
    text = _BRACKETS.sub(" ", title.lower())
    return " ".join(_NON_WORD.sub(" ", text).split())


def normalise_company(company: str) -> str:
    words = _NON_WORD.sub(" ", company.lower()).split()
    return " ".join(w for w in words if w not in _COMPANY_SUFFIXES)


def fingerprint(title: str, company: str, location: str) -> str:
    """Same title, employer and town on different boards → same fingerprint."""
    town = _NON_WORD.sub(" ", location.lower().split(",")[0]).strip()
    key = "|".join((normalise_company(company), normalise_title(title), town))
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _has(text: str, phrase: str) -> bool:
    """Match at the start of a word, so "apprentice" finds "apprenticeship" but "git" skips "digital"."""
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase.lower()), text) is not None


@dataclass
class Match:
    score: int
    profile: str
    reason: str


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _money(amount: float) -> str:
    return f"£{amount:,.0f}"


def score_profile(
    profile: Profile,
    criteria: Criteria,
    title: str,
    description: str,
    salary_min: float | None,
    salary_max: float | None,
) -> Match:
    title_norm = normalise_title(title)
    text = f"{title} {description}".lower()

    for word in profile.dealbreakers:
        if _has(title.lower(), word):
            return Match(0, profile.name, f'The title says "{word}", one of your dealbreakers.')

    best_search, title_sim = max(
        ((s, fuzz.token_set_ratio(title_norm, normalise_title(s))) for s in profile.searches), key=lambda t: t[1]
    )
    hits = [k for k in profile.keywords if _has(text, k)]
    wanted = max(3, min(len(profile.keywords), 6))
    kw_part = min(len(hits) / wanted, 1.0) if profile.keywords else 0.5

    # Titles under 40% similar get no credit, so unrelated jobs don't pick up points.
    title_part = max(0.0, (title_sim - 40) / 60)

    top_pay = salary_max or salary_min
    if criteria.salary_floor <= 0 or (top_pay is not None and top_pay >= criteria.salary_floor):
        pay_factor = 1.0
    elif top_pay is None:
        pay_factor = 0.85
    else:
        pay_factor = 0.4

    score = (55 * title_part + 45 * kw_part) * pay_factor
    missing = [m for m in profile.must_have if not _has(text, m)]
    if missing:
        score *= 0.5

    parts = []
    if title_sim >= 60:
        parts.append(f'close to "{best_search}"')
    if hits:
        parts.append("mentions " + _join(hits[:3]))
    reason = f"Fits {profile.name}"
    reason += (": " + " and ".join(parts) if parts else ", loosely") + "."
    if top_pay is not None and criteria.salary_floor > 0 and top_pay < criteria.salary_floor:
        reason += f" Pays up to {_money(top_pay)}, under your {_money(criteria.salary_floor)} floor."
    elif top_pay is not None:
        reason += f" Pays up to {_money(top_pay)}."
    if missing:
        reason += f' Doesn\'t mention "{missing[0]}".'
    return Match(round(score), profile.name, reason)


def score_job(
    criteria: Criteria, title: str, description: str, salary_min: float | None, salary_max: float | None
) -> Match:
    matches = [score_profile(p, criteria, title, description, salary_min, salary_max) for p in criteria.profiles]
    return max(matches, key=lambda m: m.score)
