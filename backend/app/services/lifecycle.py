"""Item state machine and reservation flow (spec §3).

    selling → reserved → sold_out / expired      (the last two are final)
    reserved → selling                            (when every reservation is cancelled)

Every status change is written to status_log; every index create/update/delete
goes through ItemIndexService.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Item,
    ItemStatus,
    NotificationType,
    Reservation,
    ReservationStatus,
    User,
    utcnow,
)
from ..repositories import (
    ItemRepository,
    NotificationRepository,
    ReservationRepository,
    StatusLogRepository,
)
from .item_index import ItemIndexService
from .wants import release_wants_for_item, settle_on_deal

logger = logging.getLogger(__name__)


class LifecycleError(Exception):
    """Business-rule conflict (converted to a 400 by the routes)."""


@dataclass
class ReserveResult:
    reservation: Reservation
    rank: int
    reserved_count: int
    confirm_deadline: datetime | None


def _item_snapshot(item: Item) -> dict:
    """Notifications keep an item snapshot — the item gets physically deleted,
    and the notification must still render on its own.

    Note: the snapshot contains **no** seller email or other contact info
    (§7: only the cart endpoint returns that).
    """
    return {
        "item_id": item.id,
        "item_name": item.name,
        "price": item.price,
        "pickup_location_name": item.pickup_location_name,
        "pickup_time_range": item.pickup_time_range,
    }


class LifecycleService:
    def __init__(self, db: Session, index: ItemIndexService):
        self.db = db
        self.index = index
        self.items = ItemRepository(db)
        self.reservations = ReservationRepository(db)
        self.notifications = NotificationRepository(db)
        self.status_log = StatusLogRepository(db)

    # ---------------- §3.1 Reserve ----------------
    def reserve(self, item: Item, buyer: User) -> ReserveResult:
        if item.seller_id == buyer.id:
            raise LifecycleError("不能预订自己发布的商品")
        if item.status not in ItemStatus.SEARCHABLE:
            raise LifecycleError("这件商品已经不能预订了")
        if self.reservations.get_active(item.id, buyer.id):
            raise LifecycleError("你已经预订过这件商品了")

        reservation = self.reservations.create(item.id, buyer.id)

        # Only the first active reservation flips the status and starts the
        # countdown; later reservations don't reset it.
        if item.status == ItemStatus.SELLING:
            from_status = item.status
            item.status = ItemStatus.RESERVED
            item.confirm_deadline = utcnow() + timedelta(hours=settings.confirm_window_hours)
            self.items.save(item)
            self.status_log.append(item.id, from_status, item.status, "First buyer reserved")
            self.index.update_status(item)

        self.db.commit()
        count = self.reservations.count_active_for_item(item.id)
        rank = self.reservations.rank_of(reservation)
        return ReserveResult(reservation, rank, count, item.confirm_deadline)

    # ---------------- §3.2 Cancel a reservation ----------------
    def cancel(self, item: Item, buyer: User) -> int:
        reservation = self.reservations.get_active(item.id, buyer.id)
        if not reservation:
            raise LifecycleError("你没有这件商品的有效预订")
        if item.status == ItemStatus.SOLD_OUT:
            raise LifecycleError("这件商品已经成交，无法取消")

        reservation.status = ReservationStatus.CANCELLED
        self.reservations.save(reservation)

        # E5: reservation cancelled → wants closed by this item must be
        # restored, or the buyer never hears about similar listings again.
        release_wants_for_item(self.db, item.id, buyer_id=buyer.id)

        remaining = self.reservations.count_active_for_item(item.id)
        if remaining == 0 and item.status == ItemStatus.RESERVED:
            from_status = item.status
            item.status = ItemStatus.SELLING
            item.confirm_deadline = None
            self.items.save(item)
            self.status_log.append(item.id, from_status, item.status, "All reservations cancelled")
            self.index.update_status(item)

        self.db.commit()
        return remaining

    # ---------------- §3.3 Seller confirms the deal ----------------
    def confirm(self, item: Item, seller: User, buyer_id: int) -> Reservation:
        if item.seller_id != seller.id:
            raise LifecycleError("只有卖家可以确认交易")
        if item.status != ItemStatus.RESERVED:
            raise LifecycleError("只有处于已预订状态的商品才能确认交易")

        winner = self.reservations.get_active(item.id, buyer_id)
        if not winner:
            raise LifecycleError("这位买家没有有效的预订")

        from_status = item.status
        item.status = ItemStatus.SOLD_OUT
        item.confirm_deadline = None
        # E5: persist the deal snapshot — these two columns are the categorized
        # price-evidence pool read by §7.2 similar_sold_items. The platform has
        # no haggling channel, so the sale price is the listing price at this
        # moment; storing a separate copy keeps later price edits from
        # overwriting it.
        item.sold_at = utcnow()
        item.sold_price = item.price
        self.items.save(item)
        self.status_log.append(item.id, from_status, item.status, f"Seller confirmed deal with buyer {buyer_id}")
        # sold_out is final: remove from the retrieval index immediately
        self.index.update_status(item)

        snapshot = _item_snapshot(item)
        self.notifications.create(winner.buyer_id, NotificationType.DEAL_CONFIRMED, snapshot)

        # Remaining active reservations become closed and get a "didn't get it"
        # notification (the item disappears from their carts automatically)
        losers: list[int] = []
        for other in self.reservations.list_active_for_item(item.id):
            if other.id == winner.id:
                continue
            other.status = ReservationStatus.CLOSED
            self.reservations.save(other)
            self.notifications.create(other.buyer_id, NotificationType.DEAL_LOST, snapshot)
            losers.append(other.buyer_id)

        # E5: the losing backups didn't get the item — their wants must be restored
        settle_on_deal(self.db, item, winner.buyer_id, losers)

        self.db.commit()
        return winner

    # ---------------- §3.4b Seller pauses / resumes (reversible) ----------------
    def pause(self, item: Item, seller: User) -> None:
        """Take a listing off sale without destroying it.

        The reversible half of "下架". Everything that makes an item findable
        keys off `ItemStatus.SEARCHABLE`, so dropping out of that tuple is the
        whole mechanism: the search index removes it, want matching skips it,
        the advisor's tools refuse it, and nothing else had to learn a new
        status.

        Refused while a reservation is live. A buyer holding an active
        reservation has a countdown running and an expectation attached to it;
        silently pulling the item out from under them is a worse outcome than
        telling the seller to settle the reservation first.
        """
        if item.seller_id != seller.id:
            raise LifecycleError("只有卖家可以下架这件商品")
        if item.status == ItemStatus.PAUSED:
            raise LifecycleError("这件商品已经是暂停状态了")
        if item.status not in ItemStatus.PAUSABLE:
            if item.status == ItemStatus.RESERVED:
                raise LifecycleError(
                    "这件商品已经有买家预订了，先处理完预订（确认或等它到期）再下架"
                )
            raise LifecycleError(f"这件商品当前状态是 {item.status}，不能下架")

        from_status = item.status
        item.status = ItemStatus.PAUSED
        self.items.save(item)
        self.status_log.append(item.id, from_status, ItemStatus.PAUSED, "Paused by seller")
        # Leaves the index here; the row stays in the database.
        self.index.update_status(item)
        release_wants_for_item(self.db, item.id)
        self.db.commit()

    def resume(self, item: Item, seller: User) -> int:
        """Put a paused listing back on sale. Returns how many sale plans it cancelled.

        恢复上架会**作废挂在这件商品上的出售计划**。

        理由是这两件事互相矛盾：一条 `hold_item` 计划的意思是「先收起来，等需求起来
        再卖」，而恢复上架的意思是「我现在就要卖」。把计划留着，几周后系统还会发一封
        「可以上架了」的邮件——而它早就已经上架了。

        挂在这里而不是挂在某个端点上，是因为恢复上架有**两条入口**：界面上的按钮和
        对话里的「重新上架」。两条都汇到这个方法，所以这里是唯一一个不会漏的地方。
        """
        if item.seller_id != seller.id:
            raise LifecycleError("只有卖家可以重新上架这件商品")
        if item.status not in ItemStatus.RESUMABLE:
            raise LifecycleError(
                f"这件商品当前状态是 {item.status}，只有暂停中的商品才能恢复上架"
            )

        item.status = ItemStatus.SELLING
        item.confirm_deadline = None
        self.items.save(item)
        self.status_log.append(item.id, ItemStatus.PAUSED, ItemStatus.SELLING, "Resumed by seller")
        self.index.update_status(item)
        cancelled = self._cancel_plans_for(item)
        self.db.commit()
        return cancelled

    def _cancel_plans_for(self, item: Item) -> int:
        """作废绑在这件商品上、且是「收起来等」的那些计划。不 commit，由调用方一起提交。

        只动 `hold_item` 为真的那些。卖家可能对一件商品既排了「12 月再卖」的计划、
        又因为别的原因手动暂停过——只有前者是「因为这条计划才收起来的」，也只有它
        该随着恢复上架一起作废。
        """
        from ..models import SalePlan, SalePlanStatus

        rows = list(self.db.scalars(
            select(SalePlan).where(
                SalePlan.item_id == int(item.id),
                SalePlan.status == SalePlanStatus.ACTIVE,
                SalePlan.hold_item.is_(True),
            )
        ))
        for plan in rows:
            plan.status = SalePlanStatus.CANCELLED
        if rows:
            logger.info(
                "[lifecycle] item %s resumed -> cancelled %d sale plan(s)", item.id, len(rows)
            )
        return len(rows)

    # ---------------- §3.5 Seller delists (permanent delete) ----------------
    def delist(self, item: Item, seller: User) -> None:
        if item.seller_id != seller.id:
            raise LifecycleError("只有卖家可以下架这件商品")

        snapshot = _item_snapshot(item)
        self.status_log.append(item.id, item.status, "deleted", "Delisted by seller")
        for reservation in self.reservations.list_active_for_item(item.id):
            self.notifications.create(reservation.buyer_id, NotificationType.ITEM_DELISTED, snapshot)
        # E5: must run before delete_item (which commits), or the restoration
        # would be thrown away
        release_wants_for_item(self.db, item.id)

        # Physically delete the item and its reservations (cascade);
        # notifications are kept
        self.index.delete_item(self.db, item)

    # ---------------- §3.4 Expiry on timeout ----------------
    def expire_due_items(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        due = self.items.list_expired_candidates(now)
        for item in due:
            item_id = item.id
            snapshot = _item_snapshot(item)
            self.status_log.append(item_id, item.status, ItemStatus.EXPIRED, "Seller did not confirm within the window")
            for reservation in self.reservations.list_active_for_item(item_id):
                self.notifications.create(
                    reservation.buyer_id, NotificationType.ITEM_EXPIRED, snapshot
                )
            release_wants_for_item(self.db, item_id)   # as above, must run before delete_item
            self.index.delete_item(self.db, item)
            logger.info("[lifecycle] item %s(%s) expired on timeout, deleted", item_id, snapshot["item_name"])
        return len(due)
