"""Re-sync stored coordinate snapshots after CAMPUS_LOCATIONS was re-geocoded.

`users`, `items` and `wants` copy lat/lng at creation time rather than joining
on the location table, so correcting `CAMPUS_LOCATIONS` alone leaves existing
rows pointing at the old, hand-estimated positions — map pins would move while
`pickup_range_km` / `max_distance_km` distances kept using stale values.

Users and items are matched by location name. Wants have no location column,
so they are matched against the *old* coordinate pair (see OLD_COORDS below);
a want whose origin matches nothing is left untouched and reported.

Run from anywhere:  python scripts/fix_location_coords.py [--dry-run]
Restart the backend afterwards so the vector / BM25 indexes rebuild from the
corrected rows.
"""

from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401  path setup

from app.config import LOCATION_BY_NAME
from app.db import SessionLocal
from app.models import Item, StandingWant, User

#: The rounded values that shipped before the 2026-09-07 re-geocoding. Kept
#: verbatim so want origins (which store no location name) can be remapped.
OLD_COORDS: dict[str, tuple[float, float]] = {
    "The Marylander (3501 St Paul St)": (39.33240, -76.61570),
    "The Carlyle (500 W University Pkwy)": (39.33920, -76.62530),
    "The Broadview (105 W 39th St)": (39.33790, -76.62270),
    "Wyman Park Apartments (W 40th St)": (39.33840, -76.62930),
    "ICON Residences (Hampden)": (39.33620, -76.63180),
    "Nine East 33rd": (39.32860, -76.61480),
    "Jefferson House (N Calvert St)": (39.32820, -76.61360),
    "The Allston (St Paul & 32nd)": (39.32730, -76.61620),
    "The Baltimorean (3121 St Paul St)": (39.32450, -76.61530),
    "HH Cresmont (Cresmont Ave)": (39.32380, -76.62120),
    "Campus Square Apartments (E 29th St)": (39.32280, -76.61280),
    "Fox Building Apartments (Hampden)": (39.32700, -76.63320),
    "E 33rd St townhouses (at Abell Ave)": (39.32880, -76.61060),
    "Guilford Ave 3200 block townhouses": (39.32680, -76.61180),
    "St Paul St 2900 block townhouses": (39.32260, -76.61680),
    "N Charles St 2800 block townhouses": (39.32160, -76.61760),
    "Remington Ave townhouses (at W 29th St)": (39.32320, -76.62540),
    "Hampden Elm Ave townhouses (at W 36th St)": (39.33120, -76.63380),
}

#: ~1 m — coordinates were stored as exact copies, so this only absorbs float noise.
EPS = 1e-5


def _new(name: str) -> tuple[float, float] | None:
    loc = LOCATION_BY_NAME.get(name)
    return (loc["lat"], loc["lng"]) if loc else None


def _match_old(lat: float, lng: float) -> str | None:
    for name, (olat, olng) in OLD_COORDS.items():
        if abs(lat - olat) < EPS and abs(lng - olng) < EPS:
            return name
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report without writing")
    args = ap.parse_args()

    moved = {"users": 0, "items": 0, "wants": 0}
    skipped: list[str] = []

    with SessionLocal() as db:
        for user in db.query(User).all():
            if not user.location_name or user.lat is None:
                continue
            new = _new(user.location_name)
            if not new or (abs(user.lat - new[0]) < EPS and abs(user.lng - new[1]) < EPS):
                if not new:
                    skipped.append(f"user {user.id}: unknown location {user.location_name!r}")
                continue
            user.lat, user.lng = new
            moved["users"] += 1

        for item in db.query(Item).all():
            new = _new(item.pickup_location_name)
            if not new:
                skipped.append(f"item {item.id}: unknown location {item.pickup_location_name!r}")
                continue
            if abs(item.lat - new[0]) < EPS and abs(item.lng - new[1]) < EPS:
                continue
            item.lat, item.lng = new
            moved["items"] += 1

        for want in db.query(StandingWant).all():
            if want.origin_lat is None or want.origin_lng is None:
                continue
            name = _match_old(want.origin_lat, want.origin_lng)
            if not name:
                # already current, or a free-form origin that never came from the table
                if not _match_old_new(want.origin_lat, want.origin_lng):
                    skipped.append(
                        f"want {want.id}: origin ({want.origin_lat:.5f}, {want.origin_lng:.5f}) "
                        "matches no known location"
                    )
                continue
            want.origin_lat, want.origin_lng = _new(name)
            moved["wants"] += 1

        if args.dry_run:
            db.rollback()
        else:
            db.commit()

    verb = "would move" if args.dry_run else "moved"
    print(f"{verb}: users={moved['users']} items={moved['items']} wants={moved['wants']}")
    for line in skipped:
        print(f"  skipped: {line}")
    if not args.dry_run:
        print("restart the backend so the vector / BM25 indexes rebuild from the corrected rows")


def _match_old_new(lat: float, lng: float) -> str | None:
    """True when the origin already sits on a *current* location (nothing to do)."""
    for name, loc in LOCATION_BY_NAME.items():
        if abs(lat - loc["lat"]) < EPS and abs(lng - loc["lng"]) < EPS:
            return name
    return None


if __name__ == "__main__":
    main()
