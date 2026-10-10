"""SQL convergence point for standing wants (StandingWant) and item traffic
(ItemTraffic).

Vector search does not live here — the want vector in/out paths are unified in
services/want_index.py, for the same reason as the item index: the primary DB
and the vector store must have their consistency guaranteed by a single service.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import settings
from ..models import ItemTraffic, StandingWant, WantSource, WantStatus, utcnow


class StandingWantRepository:
    def __init__(self, db: Session):
        self.db = db

    # ---------------- Reads ----------------
    def get(self, want_id: int) -> StandingWant | None:
        return self.db.get(StandingWant, want_id)

    def get_many(self, want_ids: Sequence[int]) -> list[StandingWant]:
        if not want_ids:
            return []
        return list(
            self.db.scalars(select(StandingWant).where(StandingWant.id.in_(list(want_ids))))
        )

    def list_active(self, limit: int = 1000) -> list[StandingWant]:
        return list(
            self.db.scalars(
                select(StandingWant)
                .where(StandingWant.status == WantStatus.ACTIVE)
                .order_by(StandingWant.created_at.desc())
                .limit(limit)
            )
        )

    def list_for_buyer(self, buyer_id: int, active_only: bool = True) -> list[StandingWant]:
        stmt = select(StandingWant).where(StandingWant.buyer_id == buyer_id)
        if active_only:
            stmt = stmt.where(StandingWant.status == WantStatus.ACTIVE)
        return list(self.db.scalars(stmt.order_by(StandingWant.created_at.desc())))

    def find_duplicate(self, buyer_id: int, semantic_text: str,
                       filters: dict[str, Any]) -> StandingWant | None:
        """A duplicate want from the same buyer is not re-created — only its TTL
        is extended.

        Duplicate detection requires "semantic text + filters" to match exactly;
        no fuzzy matching — fuzzy dedup would merge "bike under $300" and
        "bike under $500" into one, while for seller-side price distribution
        those are two different signals.
        """
        for w in self.list_for_buyer(buyer_id):
            if w.semantic_text == semantic_text and (w.filters or {}) == (filters or {}):
                return w
        return None

    def notified_today(self, buyer_id: int) -> int:
        since = utcnow() - timedelta(days=1)
        return int(
            self.db.scalar(
                select(func.count(StandingWant.id)).where(
                    StandingWant.buyer_id == buyer_id,
                    StandingWant.last_notified_at.is_not(None),
                    StandingWant.last_notified_at >= since,
                )
            )
            or 0
        )

    # ---------------- Writes ----------------
    def create(
        self,
        *,
        buyer_id: int,
        raw_query: str,
        filters: dict[str, Any],
        semantic_text: str,
        origin: tuple[float, float] | None = None,
        max_distance_km: float | None = None,
        source: str = WantSource.IMPLICIT,
    ) -> StandingWant:
        w = StandingWant(
            buyer_id=buyer_id,
            raw_query=raw_query[:300],
            filters=filters or {},
            semantic_text=semantic_text[:300],
            category=filters.get("category"),
            max_price=filters.get("max_price"),
            min_price=filters.get("min_price"),
            origin_lat=origin[0] if origin else None,
            origin_lng=origin[1] if origin else None,
            max_distance_km=max_distance_km,
            source=source,
            status=WantStatus.ACTIVE,
            expires_at=utcnow() + timedelta(days=settings.want_ttl_days),
        )
        self.db.add(w)
        self.db.flush()
        return w

    def touch(self, want: StandingWant) -> StandingWant:
        """A need expressed again: renew it instead of creating a new row."""
        want.expires_at = utcnow() + timedelta(days=settings.want_ttl_days)
        want.status = WantStatus.ACTIVE
        self.db.add(want)
        self.db.flush()
        return want

    def mark_notified(self, want: StandingWant) -> None:
        want.notify_count = (want.notify_count or 0) + 1
        want.last_notified_at = utcnow()
        self.db.add(want)
        self.db.flush()

    def set_status(self, want: StandingWant, status: str,
                   fulfilled_by_item_id: int | None = None) -> None:
        want.status = status
        if status == WantStatus.FULFILLED:
            want.fulfilled_by_item_id = fulfilled_by_item_id
        else:
            # Leaving fulfilled must clear the source, otherwise reserving and
            # cancelling the same item again would hit a stale
            # fulfilled_by_item_id and restore a duplicate want
            want.fulfilled_by_item_id = None
        self.db.add(want)
        self.db.flush()

    def list_fulfilled_by_item(self, item_id: int,
                               buyer_id: int | None = None) -> list[StandingWant]:
        """Wants closed by a given item. Used to restore them precisely when the
        reservation is cancelled / falls through (E5)."""
        stmt = select(StandingWant).where(
            StandingWant.status == WantStatus.FULFILLED,
            StandingWant.fulfilled_by_item_id == item_id,
        )
        if buyer_id is not None:
            stmt = stmt.where(StandingWant.buyer_id == buyer_id)
        return list(self.db.scalars(stmt))

    def reactivate(self, want: StandingWant) -> bool:
        """Restore a want that was closed by mistake. Ones past their TTL are not
        restored — they were due to expire anyway."""
        if want.expires_at <= utcnow():
            self.set_status(want, WantStatus.EXPIRED)
            return False
        self.set_status(want, WantStatus.ACTIVE)
        return True

    def fulfill_by_category(self, buyer_id: int, category: str | None) -> list[int]:
        """Buyer reserved successfully (E4) → close their active wants in the
        same category. Returns the closed ids."""
        stmt = select(StandingWant).where(
            StandingWant.buyer_id == buyer_id,
            StandingWant.status == WantStatus.ACTIVE,
        )
        if category:
            stmt = stmt.where(StandingWant.category == category)
        rows = list(self.db.scalars(stmt))
        for w in rows:
            w.status = WantStatus.FULFILLED
            self.db.add(w)
        self.db.flush()
        return [w.id for w in rows]

    def list_all_for_buyer(self, buyer_id: int, limit: int = 50) -> list[StandingWant]:
        """The buyer's "things I'm waiting for" list: active first, the rest in
        reverse chronological order."""
        rows = list(
            self.db.scalars(
                select(StandingWant)
                .where(StandingWant.buyer_id == buyer_id)
                .order_by(StandingWant.created_at.desc())
                .limit(limit)
            )
        )
        rows.sort(key=lambda w: (w.status != WantStatus.ACTIVE, -w.id))
        return rows

    def expire_due(self) -> list[int]:
        rows = list(
            self.db.scalars(
                select(StandingWant).where(
                    StandingWant.status == WantStatus.ACTIVE,
                    StandingWant.expires_at <= utcnow(),
                )
            )
        )
        for w in rows:
            w.status = WantStatus.EXPIRED
            self.db.add(w)
        self.db.flush()
        return [w.id for w in rows]


class ItemTrafficRepository:
    """Per-item traffic counters. Writes sit on the retrieval hot path: use bulk
    upserts, never per-row ORM writes."""

    def __init__(self, db: Session):
        self.db = db

    def stats(self, item_id: int) -> ItemTraffic:
        row = self.db.get(ItemTraffic, item_id)
        return row or ItemTraffic(item_id=item_id)

    def stats_many(self, item_ids: Sequence[int]) -> dict[int, ItemTraffic]:
        if not item_ids:
            return {}
        rows = self.db.scalars(
            select(ItemTraffic).where(ItemTraffic.item_id.in_(list(item_ids)))
        )
        return {r.item_id: r for r in rows}

    def bump(self, item_ids: Sequence[int], field: str, delta: int = 1) -> None:
        """Increment a counter in place. field ∈ impressions | entered_candidates | pushed | clicks."""
        ids = [int(i) for i in dict.fromkeys(item_ids)]
        if not ids or delta <= 0:
            return
        if field not in ("impressions", "entered_candidates", "pushed", "clicks"):
            raise ValueError(f"Unknown traffic field: {field}")

        now = utcnow()
        stmt = sqlite_insert(ItemTraffic).values(
            [{"item_id": i, field: delta, "updated_at": now} for i in ids]
        )
        # ON CONFLICT DO UPDATE: one statement handles both "insert" and "increment"
        stmt = stmt.on_conflict_do_update(
            index_elements=[ItemTraffic.item_id],
            set_={
                field: getattr(ItemTraffic, field) + delta,
                "updated_at": now,
            },
        )
        self.db.execute(stmt)
