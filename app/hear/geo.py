"""Addresses → coordinates (server side, at sign-up) and the distance maths for "what is live near me".

Geocoder: Nominatim (OpenStreetMap) by default, Mapbox when HEARCHURCH_MAPBOX_TOKEN is set. Nominatim's usage
policy is followed: a User-Agent naming this service, at most one request per second, results cached. A failure
never raises: the caller gets None and asks the app for a map pin instead.

Listener coordinates are never stored or logged: see hide_coordinates_in_access_log().
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from urllib.parse import quote
from collections import OrderedDict

import httpx

log = logging.getLogger("hear.geo")

USER_AGENT = "HearChurch/1.0 (hearchurch.com)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
MAPBOX_URL = "https://api.mapbox.com/geocoding/v5/mapbox.places/{q}.json"
MILES_PER_DEG_LAT = 69.0


class Geocoder:
    """geocode(address) → (lat, lng) or None. Override _lookup (or pass a different provider) in tests."""

    def __init__(self, mapbox_token: str | None = None, min_interval_s: float = 1.1, cache_size: int = 2000,
                 timeout_s: float = 6.0):
        self.mapbox_token = os.environ.get("HEARCHURCH_MAPBOX_TOKEN", "") if mapbox_token is None else mapbox_token
        self.provider = "mapbox" if self.mapbox_token else "nominatim"
        self.min_interval_s, self.timeout_s = min_interval_s, timeout_s
        self._cache: OrderedDict[str, tuple[float, float] | None] = OrderedDict()
        self._cache_size = cache_size
        self._lock = asyncio.Lock()      # one request at a time, spaced by min_interval_s (Nominatim: max 1/s)
        self._last = 0.0

    async def geocode(self, address: str) -> tuple[float, float] | None:
        key = " ".join(address.lower().split())
        if len(key) < 4:
            return None
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        async with self._lock:
            if key in self._cache:   # another request for the same address finished while we waited
                return self._cache[key]
            wait = self._last + self.min_interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                found, final = await self._lookup(address), True
            except Exception as e:   # network, quota, bad JSON: not cached, so a retry can succeed
                log.warning("geocoding failed (%s): %s", self.provider, e)
                found, final = None, False
            self._last = time.monotonic()
        if final:
            self._cache[key] = found
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return found

    async def town(self, lat: float, lng: float) -> str:
        """The town at a building's pin, for search results: "New York, NY", "Dakar, Senegal"; "" if unknown.
        Shares geocode()'s spacing (Nominatim: max 1 request a second)."""
        async with self._lock:
            wait = self._last + self.min_interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                return await self._town(lat, lng)
            except Exception as e:
                log.warning("reverse geocoding failed (%s): %s", self.provider, e)
                return ""
            finally:
                self._last = time.monotonic()

    async def _town(self, lat: float, lng: float) -> str:
        async with httpx.AsyncClient(timeout=self.timeout_s, headers={"User-Agent": USER_AGENT}) as http:
            if self.mapbox_token:
                r = await http.get(MAPBOX_URL.format(q=f"{lng},{lat}"),
                                   params={"access_token": self.mapbox_token, "types": "place", "limit": 1})
                r.raise_for_status()
                feats = r.json().get("features") or []
                if not feats:
                    return ""
                ctx = {c["id"].split(".")[0]: c for c in feats[0].get("context", [])}
                region, country = ctx.get("region", {}), ctx.get("country", {})
                if country.get("short_code") == "us" and region.get("short_code"):
                    return f"{feats[0]['text']}, {region['short_code'].split('-')[-1]}"
                return ", ".join(x for x in (feats[0]["text"], country.get("text", "")) if x)
            r = await http.get(NOMINATIM_REVERSE_URL, params={"lat": lat, "lon": lng, "format": "jsonv2", "zoom": 10,
                                                              "accept-language": "en"})
            r.raise_for_status()
            return town_from_nominatim(r.json().get("address") or {})

    async def _lookup(self, address: str) -> tuple[float, float] | None:
        async with httpx.AsyncClient(timeout=self.timeout_s, headers={"User-Agent": USER_AGENT}) as http:
            if self.mapbox_token:
                r = await http.get(MAPBOX_URL.format(q=quote(address, safe="")),
                                   params={"access_token": self.mapbox_token, "limit": 1})
                r.raise_for_status()
                feats = r.json().get("features") or []
                if not feats:
                    return None
                lng, lat = feats[0]["center"]
            else:
                r = await http.get(NOMINATIM_URL, params={"q": address, "format": "jsonv2", "limit": 1})
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    return None
                lat, lng = float(rows[0]["lat"]), float(rows[0]["lon"])
        return (lat, lng) if -90 <= lat <= 90 and -180 <= lng <= 180 else None


def town_from_nominatim(a: dict) -> str:
    """Nominatim's address parts → "New York, NY" in the US, "Dakar, Senegal" elsewhere."""
    town = a.get("city") or a.get("town") or a.get("village") or a.get("municipality") or a.get("county") or ""
    if not town:
        return ""
    if a.get("country_code") == "us":
        state = (a.get("ISO3166-2-lvl4") or "").removeprefix("US-") or a.get("state", "")
        return f"{town}, {state}" if state else town
    return f"{town}, {a['country']}" if a.get("country") and a.get("country") != town else town


def bounding_box(lat: float, lng: float, miles: float) -> tuple[float, float, float, float]:
    """(south, north, west, east) of the box ±miles around a point (longitude widened with latitude)."""
    dlat = miles / MILES_PER_DEG_LAT
    dlng = miles / (MILES_PER_DEG_LAT * max(0.01, math.cos(math.radians(lat))))
    return lat - dlat, lat + dlat, lng - dlng, lng + dlng


def in_box(lat: float, lng: float, box: tuple[float, float, float, float]) -> bool:
    s, n, w, e = box
    if not s <= lat <= n:
        return False
    if e - w >= 360:
        return True
    # compare longitudes modulo 360 so a box across the date line works
    return ((lng - w) % 360) <= ((e - w) % 360)


class _StripLiveQuery(logging.Filter):
    """uvicorn's access log line carries the query string; for /api/live that is the listener's position."""

    def filter(self, record: logging.LogRecord) -> bool:
        a = record.args
        if isinstance(a, tuple) and len(a) >= 3 and isinstance(a[2], str) and a[2].startswith("/api/live"):
            record.args = (*a[:2], "/api/live", *a[3:])
        return True


def hide_coordinates_in_access_log() -> None:
    """Idempotent. Access logging is off by default (log_level=warning); this keeps it safe if someone turns it on."""
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _StripLiveQuery) for f in lg.filters):
        lg.addFilter(_StripLiveQuery())
