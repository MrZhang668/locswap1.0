"""Reserve / cancel reservation / cart."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user, get_index
from ..models import User
from ..repositories import ItemRepository, ReservationRepository, UserRepository
from ..schemas import CartItem, ItemCard
from ..services.item_index import ItemIndexService
from ..services.lifecycle import LifecycleError, LifecycleService
from ..services.views import build_card, build_cart_item
from ..services.wants import fulfill_on_reserve

router = APIRouter(prefix="/api", tags=["reservations"])


@router.post("/reservations/{item_id}", response_model=ItemCard)
def reserve(
    item_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    index: ItemIndexService = Depends(get_index),
) -> ItemCard:
    item = ItemRepository(db).get(item_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "商品不存在或已下架")
    try:
        result = LifecycleService(db, index).reserve(item, user)
    except LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    # ---- E4: the buyer got one — close their matching standing wants so we
    # stop pushing this category to them ----
    fulfill_on_reserve(db, user, item)
    db.commit()

    return build_card(item, user, result.reserved_count, result.rank)


@router.delete("/reservations/{item_id}", response_model=ItemCard)
def cancel(
    item_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    index: ItemIndexService = Depends(get_index),
) -> ItemCard:
    item = ItemRepository(db).get(item_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "商品不存在或已下架")
    try:
        remaining = LifecycleService(db, index).cancel(item, user)
    except LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return build_card(item, user, remaining, None)


@router.get("/cart", response_model=list[CartItem])
def cart(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CartItem]:
    """The post-reservation view: seller contact info is returned here (and only here)."""
    res_repo = ReservationRepository(db)
    user_repo = UserRepository(db)
    rows: list[CartItem] = []
    for reservation in res_repo.list_active_by_buyer(user.id):
        item = reservation.item
        if item is None:  # the item was deleted (expired/delisted), so the reservation goes with it
            continue
        seller = user_repo.get(item.seller_id)
        if seller is None:
            continue
        rows.append(
            build_cart_item(
                reservation,
                item,
                seller,
                user,
                rank=res_repo.rank_of(reservation),
                reserved_count=res_repo.count_active_for_item(item.id),
            )
        )
    return rows
