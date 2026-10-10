"""Distance computation (haversine)."""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def distance_or_none(
    lat1: float | None, lng1: float | None, lat2: float | None, lng2: float | None
) -> float | None:
    if None in (lat1, lng1, lat2, lng2):
        return None
    return round(haversine_km(lat1, lng1, lat2, lng2), 2)
