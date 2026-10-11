"""Board browsing — the non-conversational way into the same inventory.

The chat search (`/api/search/chat`) parses a sentence and ranks by meaning;
this one takes explicit filters and orders deterministically. Both read the same
`ItemRepository.list_searchable()` set and both leave through `build_card`, so a
listing can never be visible on one and hidden on the other.

Nothing here calls an LLM or touches the vector index: every predicate is a
column or a JSON attribute comparison, which is why the home board can afford to
re-query on every keystroke.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from ..config import CONDITION_DICT
from ..models import Item, User
from .geo import haversine_km

#: Best-first. Used both by the `condition` sort and to rank the filter chips.
#: Derived from CONDITION_DICT rather than spelled out again: this list and
#: agents.publishing.schemas.CONDITIONS must agree exactly, and the 2026-09-15
#: localization is a reminder of what happens when one canonical vocabulary is
#: written down in three places.
CONDITION_ORDER: tuple[str, ...] = tuple(CONDITION_DICT.keys())

SORTS: tuple[str, ...] = ("newest", "price_asc", "price_desc", "distance", "condition")


@dataclass
class BrowseFilters:
    q: str = ""
    category: str | None = None
    min_price: float | None = None
    max_price: float | None = None
    #: Empty = every condition, including listings that predate the required-field
    #: rule and carry none at all.
    conditions: tuple[str, ...] = field(default_factory=tuple)
    max_distance_km: float | None = None
    sort: str = "newest"


def _haystack(item: Item) -> str:
    attrs = item.attributes or {}
    parts = [
        item.name,
        item.summary,
        item.description,
        attrs.get("category"),
        attrs.get("brand"),
        attrs.get("color"),
        attrs.get("condition"),
    ]
    return " ".join(p for p in parts if isinstance(p, str)).lower()


def _distance(viewer: User | None, item: Item) -> float | None:
    if not viewer or viewer.lat is None or viewer.lng is None:
        return None
    return haversine_km(viewer.lat, viewer.lng, item.lat, item.lng)


def filter_and_sort(
    items: Sequence[Item],
    viewer: User | None,
    f: BrowseFilters,
) -> list[Item]:
    """Apply the board's filters and ordering. Returns every match, unpaged —
    the caller slices, because the item count is what the board displays."""

    terms = [t for t in f.q.lower().split() if t]
    wanted_conditions = set(f.conditions)
    rows: list[tuple[Item, float | None]] = []

    for item in items:
        if f.category and (item.attributes or {}).get("category") != f.category:
            continue
        if f.min_price is not None and item.price < f.min_price:
            continue
        if f.max_price is not None and item.price > f.max_price:
            continue
        if wanted_conditions and (item.attributes or {}).get("condition") not in wanted_conditions:
            continue
        if terms:
            hay = _haystack(item)
            if not all(t in hay for t in terms):
                continue
        dist = _distance(viewer, item)
        # A distance filter the viewer's location cannot answer (no location on
        # file) is ignored rather than emptying the board; the endpoint reports
        # distance_available=False so the UI can say why.
        if f.max_distance_km is not None and dist is not None and dist > f.max_distance_km:
            continue
        rows.append((item, dist))

    far = float("inf")
    if f.sort == "price_asc":
        rows.sort(key=lambda r: (r[0].price, -r[0].id))
    elif f.sort == "price_desc":
        rows.sort(key=lambda r: (-r[0].price, -r[0].id))
    elif f.sort == "distance":
        # Items with no distance (viewer has no location) sink to the bottom
        # instead of pretending to be nearby.
        rows.sort(key=lambda r: (r[1] if r[1] is not None else far, -r[0].id))
    elif f.sort == "condition":
        rows.sort(key=lambda r: (_condition_rank(r[0]), -r[0].id))
    else:  # "newest" — the default, and what a board is for
        rows.sort(key=lambda r: (r[0].created_at, r[0].id), reverse=True)

    return [item for item, _ in rows]


def _condition_rank(item: Item) -> int:
    cond = (item.attributes or {}).get("condition")
    try:
        return CONDITION_ORDER.index(cond)
    except ValueError:
        return len(CONDITION_ORDER)  # unknown condition ranks last, not first
