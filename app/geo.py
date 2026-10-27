"""Where places are: free UK geocoding via postcodes.io (no key), cached on disk.

Used to show how far each job is from each search area. Lookups happen during a fetch,
never while a page is rendering; pages only read the cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from pathlib import Path

import httpx

log = logging.getLogger("hub.geo")

API = "https://api.postcodes.io"
MAX_LOOKUPS_PER_RUN = 150  # polite to a free service; the rest wait for the next fetch
_POSTCODE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})$")
_OUTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]?$")
# Prefer the bigger place when several share a name (Farnborough the town, not the village).
_TYPE_RANK = {"City": 0, "Town": 1, "Suburban Area": 2, "Village": 3, "Hamlet": 4}


def key(place: str) -> str:
    return " ".join(place.lower().split())


def miles_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


class GeoCache:
    """place text → (lat, lon), or None when postcodes.io doesn't know it (so we don't ask again)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, list[float] | None] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except ValueError:
                self._data = {}

    def get(self, place: str) -> tuple[float, float] | None:
        hit = self._data.get(key(place))
        return (hit[0], hit[1]) if hit else None

    def known(self, place: str) -> bool:
        return key(place) in self._data

    def put(self, place: str, coords: tuple[float, float] | None) -> None:
        self._data[key(place)] = list(coords) if coords else None

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, sort_keys=True))
        tmp.replace(self.path)

    def distances(self, location: str, areas) -> list[tuple[str, int, int]]:
        """[(area name, whole miles away, area radius)] for every area we can place, nearest first."""
        here = self.get(location) if location else None
        if here is None:
            return []
        out = [(a.name, round(miles_between(here, there)), a.miles) for a in areas if (there := self.get(a.name))]
        return sorted(out, key=lambda pair: pair[1])

    async def locate(self, places: list[str], http: httpx.AsyncClient) -> int:
        """Look up places not yet cached. Returns how many were looked up."""
        todo = list(dict.fromkeys(p for p in places if p and p.strip() and not self.known(p)))[:MAX_LOOKUPS_PER_RUN]
        done = 0
        for place in todo:
            try:
                coords = await _lookup(place, http)
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                log.info("Couldn't place %r: %s", place, exc.__class__.__name__)
                continue  # network trouble: try again next time
            self.put(place, coords)
            done += 1
            await asyncio.sleep(0.05)
        if done:
            self.save()
        return done


def _coords(result: dict) -> tuple[float, float] | None:
    lat, lon = result.get("latitude"), result.get("longitude")
    return (float(lat), float(lon)) if lat is not None and lon is not None else None


async def _lookup(place: str, http: httpx.AsyncClient) -> tuple[float, float] | None:
    first, _, rest = place.partition(",")
    compact = first.strip().upper()
    if m := _POSTCODE.match(compact.replace(" ", "")):
        r = await http.get(f"{API}/postcodes/{m.group(1)}{m.group(2)}", timeout=15)
        return _coords(r.json()["result"]) if r.status_code == 200 else None
    if _OUTCODE.match(compact):
        r = await http.get(f"{API}/outcodes/{compact}", timeout=15)
        return _coords(r.json()["result"]) if r.status_code == 200 else None

    name = first.strip()
    r = await http.get(f"{API}/places", params={"q": name, "limit": 30}, timeout=15)
    if r.status_code != 200:
        return None
    results = [p for p in (r.json().get("result") or []) if key(p.get("name_1") or "") == key(name)]
    if not results:
        return None
    hint = key(rest)

    def rank(p: dict) -> tuple[int, int]:
        where = key(" ".join(str(p.get(f) or "") for f in ("county_unitary", "district_borough", "region")))
        return (0 if hint and hint in where else 1, _TYPE_RANK.get(p.get("local_type"), 9))

    return _coords(min(results, key=rank))
