from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from ..models import Item, Reservation, ReservationStatus


class ReservationRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, reservation_id: int) -> Reservation | None:
        return self.db.get(Reservation, reservation_id)

    def get_active(self, item_id: int, buyer_id: int) -> Reservation | None:
        return self.db.scalar(
            select(Reservation).where(
                Reservation.item_id == item_id,
                Reservation.buyer_id == buyer_id,
                Reservation.status == ReservationStatus.ACTIVE,
            )
        )

    def list_active_for_item(self, item_id: int) -> list[Reservation]:
        """Ascending by reservation time — "you're #N in line" is derived
        directly from this ordering; no redundant column is stored."""
        return list(
            self.db.scalars(
                select(Reservation)
                .options(joinedload(Reservation.buyer))
                .where(
                    Reservation.item_id == item_id,
                    Reservation.status == ReservationStatus.ACTIVE,
                )
                .order_by(Reservation.created_at, Reservation.id)
            )
        )

    def count_active_for_item(self, item_id: int) -> int:
        return int(
            self.db.scalar(
                select(func.count(Reservation.id)).where(
                    Reservation.item_id == item_id,
                    Reservation.status == ReservationStatus.ACTIVE,
                )
            )
            or 0
        )

    def count_active_bulk(self, item_ids: list[int]) -> dict[int, int]:
        if not item_ids:
            return {}
        rows = self.db.execute(
            select(Reservation.item_id, func.count(Reservation.id))
            .where(
                Reservation.item_id.in_(item_ids),
                Reservation.status == ReservationStatus.ACTIVE,
            )
            .group_by(Reservation.item_id)
        ).all()
        return {int(r[0]): int(r[1]) for r in rows}

    def active_by_buyer_bulk(self, buyer_id: int, item_ids: list[int]) -> dict[int, Reservation]:
        if not item_ids:
            return {}
        rows = self.db.scalars(
            select(Reservation).where(
                Reservation.buyer_id == buyer_id,
                Reservation.item_id.in_(item_ids),
                Reservation.status == ReservationStatus.ACTIVE,
            )
        )
        return {r.item_id: r for r in rows}

    def rank_of(self, reservation: Reservation) -> int:
        """This reservation's rank (1-based) among the item's active
        reservations, derived from the created_at ordering."""
        actives = self.list_active_for_item(reservation.item_id)
        for idx, r in enumerate(actives, start=1):
            if r.id == reservation.id:
                return idx
        return 0

    def list_active_by_buyer(self, buyer_id: int) -> list[Reservation]:
        """Cart data source: active only (reservations lost to another buyer are
        set to closed and disappear automatically)."""
        return list(
            self.db.scalars(
                select(Reservation)
                .options(joinedload(Reservation.item).joinedload(Item.seller))
                .where(
                    Reservation.buyer_id == buyer_id,
                    Reservation.status == ReservationStatus.ACTIVE,
                )
                .order_by(Reservation.created_at.desc())
            )
        )

    def create(self, item_id: int, buyer_id: int) -> Reservation:
        r = Reservation(item_id=item_id, buyer_id=buyer_id, status=ReservationStatus.ACTIVE)
        self.db.add(r)
        self.db.flush()
        return r

    def save(self, reservation: Reservation) -> Reservation:
        self.db.add(reservation)
        self.db.flush()
        return reservation
