from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..models import Item, ItemStatus


class ItemRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, item_id: int) -> Item | None:
        return self.db.get(Item, item_id)

    def get_with_seller(self, item_id: int) -> Item | None:
        return self.db.scalar(
            select(Item).options(joinedload(Item.seller)).where(Item.id == item_id)
        )

    def get_many(self, item_ids: Sequence[int]) -> list[Item]:
        if not item_ids:
            return []
        return list(self.db.scalars(select(Item).where(Item.id.in_(list(item_ids)))))

    def list_all(self) -> list[Item]:
        return list(self.db.scalars(select(Item).order_by(Item.id)))

    def list_searchable(self) -> list[Item]:
        """selling + reserved: both the hard filter set for search and the data
        source for index rebuilds."""
        return list(
            self.db.scalars(
                select(Item).where(Item.status.in_(ItemStatus.SEARCHABLE)).order_by(Item.id)
            )
        )

    def list_by_seller(self, seller_id: int) -> list[Item]:
        return list(
            self.db.scalars(
                select(Item).where(Item.seller_id == seller_id).order_by(Item.created_at.desc())
            )
        )

    def list_expired_candidates(self, now: datetime) -> list[Item]:
        return list(
            self.db.scalars(
                select(Item).where(
                    Item.status == ItemStatus.RESERVED,
                    Item.confirm_deadline.is_not(None),
                    Item.confirm_deadline <= now,
                )
            )
        )

    def list_sold_evidence(self, category: str | None = None, limit: int = 50) -> list[Item]:
        """Per-category sold-price evidence pool (the read outlet for E5).

        Only sold_out items with a deal snapshot are taken — historical sales
        that predate E5 have no sold_price, and including them would pollute the
        stats; better a small sample than a dirty one.
        M1's similar_sold_items tool reads from here — don't build a second query.
        """
        stmt = select(Item).where(
            Item.status == ItemStatus.SOLD_OUT,
            Item.sold_at.is_not(None),
            Item.sold_price.is_not(None),
        )
        if category:
            # Filtered in SQL, not after the LIMIT. Filtering afterwards means
            # the category has to appear inside the most recent N sales
            # platform-wide to be seen at all, so a category that sells rarely
            # reports "no evidence" while its sales sit in the table — and it
            # reports it as an empty result, which reads like a quiet fact
            # rather than a dropped row. (A single Musical Instruments sale
            # vanished exactly this way once the sale count reached 141.)
            stmt = stmt.where(Item.attributes["category"].as_string() == category)
        return list(self.db.scalars(stmt.order_by(Item.sold_at.desc()).limit(limit)))

    def create(self, **kwargs) -> Item:
        item = Item(**kwargs)
        self.db.add(item)
        self.db.flush()
        return item

    def save(self, item: Item) -> Item:
        self.db.add(item)
        self.db.flush()
        return item

    def delete(self, item: Item) -> None:
        self.db.delete(item)
        self.db.flush()
