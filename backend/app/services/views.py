"""View assembly: DB entities → public schemas.

Buyer-side cards only go through the `ItemCard` whitelist; the seller's contact
info appears only in `build_cart_item`.
"""

from __future__ import annotations

from datetime import datetime

from ..config import CAMPUS_CENTER
from ..models import (
    Item,
    ItemStatus,
    Notification,
    NotificationType,
    Reservation,
    StandingWant,
    User,
    utcnow,
)
from ..schemas import BuyerRow, CartItem, ItemCard, NotificationOut, SellerItem, WantOut
from ..security import mask_email
from .geo import distance_or_none


def photo_url(item: Item) -> str | None:
    return f"/static/{item.photo_path}" if item.photo_path else None


def seconds_left(deadline: datetime | None) -> int | None:
    if not deadline:
        return None
    return max(0, int((deadline - utcnow()).total_seconds()))


def build_card(
    item: Item,
    viewer: User | None,
    reserved_count: int,
    my_reservation_rank: int | None = None,
) -> ItemCard:
    return ItemCard(
        item_id=item.id,
        name=item.name,
        summary=item.summary or "",
        price=item.price,
        photo_url=photo_url(item),
        # Signed out, distances are measured from the campus centre rather than
        # omitted — see CAMPUS_CENTER. `distance_from` tells the frontend which
        # of the two it is, so a card can say "from campus" instead of implying
        # the visitor is standing there.
        distance_km=distance_or_none(
            viewer.lat if viewer else CAMPUS_CENTER["lat"],
            viewer.lng if viewer else CAMPUS_CENTER["lng"],
            item.lat, item.lng,
        ),
        distance_from="you" if viewer else "campus",
        category=(item.attributes or {}).get("category"),
        condition=(item.attributes or {}).get("condition"),
        pickup_location_name=item.pickup_location_name,
        status=item.status,
        reserved_count=reserved_count,
        my_rank=my_reservation_rank,
        reserved_by_me=my_reservation_rank is not None,
    )


def build_cart_item(
    reservation: Reservation,
    item: Item,
    seller: User,
    viewer: User,
    rank: int,
    reserved_count: int,
) -> CartItem:
    return CartItem(
        item_id=item.id,
        name=item.name,
        summary=item.summary or "",
        category=(item.attributes or {}).get("category"),
        price=item.price,
        photo_url=photo_url(item),
        distance_km=distance_or_none(viewer.lat, viewer.lng, item.lat, item.lng),
        pickup_location_name=item.pickup_location_name,
        pickup_time_range=item.pickup_time_range,
        seller_contact=seller.email,  # §7: returned here and nowhere else
        status=item.status,
        confirm_deadline=item.confirm_deadline,
        seconds_left=seconds_left(item.confirm_deadline),
        my_rank=rank,
        reserved_count=reserved_count,
        deal_confirmed=item.status == ItemStatus.SOLD_OUT,
    )


def build_seller_item(item: Item, reserved_count: int) -> SellerItem:
    return SellerItem(
        item_id=item.id,
        name=item.name,
        price=item.price,
        photo_url=photo_url(item),
        status=item.status,
        reserved_count=reserved_count,
        confirm_deadline=item.confirm_deadline,
        seconds_left=seconds_left(item.confirm_deadline),
        created_at=item.created_at,
    )


def build_buyer_row(reservation: Reservation, rank: int, reveal_contact: bool) -> BuyerRow:
    return BuyerRow(
        buyer_id=reservation.buyer_id,
        display_name=f"买家 #{rank}（{mask_email(reservation.buyer.email)}）",
        contact=reservation.buyer.email if reveal_contact else None,
        rank=rank,
        reserved_at=reservation.created_at,
    )


_NOTIFICATION_TEXT = {
    NotificationType.DEAL_CONFIRMED: (
        "交易已确认",
        "卖家已经和你确认了这笔交易。请按约定的时间和地点自提，自提信息在「我的购物车」里。",
    ),
    NotificationType.DEAL_LOST: (
        "很遗憾，这件被别人先拿走了",
        "卖家把这件商品确认给了另一位买家，它已经从你的购物车里移除。",
    ),
    NotificationType.ITEM_EXPIRED: (
        "预订已过期",
        "抱歉，卖家没有在确认时限内确认这笔交易，商品已下架，你的预订也随之失效。",
    ),
    NotificationType.ITEM_DELISTED: (
        "商品已下架",
        "卖家把这件商品下架了，你的预订随之失效。",
    ),
    NotificationType.WANT_MATCHED: (
        "有卖家给你推荐了一件商品",
        "有卖家认为他的某件在售商品正好是你在找的。",
    ),
}


def build_notification(n: Notification) -> NotificationOut:
    title, body = _NOTIFICATION_TEXT.get(n.type, ("系统通知", ""))
    payload = n.payload or {}
    item_name = payload.get("item_name")
    if n.type == NotificationType.WANT_MATCHED:
        # The item name alone won't remind the buyer what they were after —
        # include the want text too.
        want_text = payload.get("want_text") or ""
        price = payload.get("price")
        price_part = f"，售价 ${float(price):.0f}" if price is not None else ""
        # Worded as "a seller sent this", not "we found this": a human chose to
        # send it, and saying so is both accurate and the reason the buyer
        # should give it more attention than an algorithmic match.
        body = (
            f'有卖家认为这件商品正好符合你在找的东西'
            f'（「{want_text}」）：「{item_name}」{price_part}。要不要预订完全由你决定。'
        )
        return NotificationOut(
            id=n.id, type=n.type, title=title, body=body,
            payload=payload, read=n.read, created_at=n.created_at,
        )
    if item_name:
        body = f'「{item_name}」：{body}'
    return NotificationOut(
        id=n.id,
        type=n.type,
        title=title,
        body=body,
        payload=n.payload or {},
        read=n.read,
        created_at=n.created_at,
    )


def build_want(want: StandingWant) -> WantOut:
    """Buyer-side view of a want. Sellers never see this structure (C8)."""
    days_left = max(0, (want.expires_at - utcnow()).days)
    return WantOut(
        want_id=want.id,
        text=want.semantic_text or want.raw_query,
        raw_query=want.raw_query,
        filters=want.filters or {},
        status=want.status,
        source=want.source,
        notify_count=want.notify_count or 0,
        created_at=want.created_at,
        expires_at=want.expires_at,
        days_left=days_left,
    )
