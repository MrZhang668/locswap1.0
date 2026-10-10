"""Seller side (messages page · my listings): item list / buyer list /
confirm deal / delist / deferred-sale plans."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user, get_index
from ..models import User
from ..repositories import ItemRepository, ReservationRepository
from ..repositories.want_repo import StandingWantRepository
from ..schemas import (
    BuyerRow,
    ConfirmIn,
    ItemStatusIn,
    PushToBuyerIn,
    PushToBuyerOut,
    SellerItem,
)
from ..services.item_index import ItemIndexService
from ..services.lifecycle import LifecycleError, LifecycleService
from ..services import sale_plans as sale_plan_service
from ..services.seller_advisor.tools import resolve_candidate
from ..services.views import build_buyer_row, build_seller_item
from ..services.wants import PushError, push_item_to_want

router = APIRouter(prefix="/api/seller", tags=["seller"])


def _owned_item(db: Session, item_id: int, user: User):
    item = ItemRepository(db).get(item_id)
    if not item:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "商品不存在")
    if item.seller_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "这不是你发布的商品")
    return item


@router.get("/items", response_model=list[SellerItem])
def my_items(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SellerItem]:
    items = ItemRepository(db).list_by_seller(user.id)
    counts = ReservationRepository(db).count_active_bulk([i.id for i in items])
    return [build_seller_item(i, counts.get(i.id, 0)) for i in items]


@router.get("/items/{item_id}/buyers", response_model=list[BuyerRow])
def buyers(
    item_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BuyerRow]:
    """Buyer list ordered by reservation time.

    Contact info is revealed as soon as a buyer holds an active reservation —
    the same moment the buyer starts seeing the seller's. Reservation is the
    point where both sides have committed enough to need to talk (about pickup
    time, condition questions, whether it is still worth confirming), and a
    one-way reveal left the seller unable to answer a buyer who could already
    reach them. Buyers with no active reservation are still masked.
    """
    item = _owned_item(db, item_id, user)
    reservations = ReservationRepository(db).list_active_for_item(item.id)
    return [
        build_buyer_row(r, rank, reveal_contact=True)
        for rank, r in enumerate(reservations, start=1)
    ]


@router.post("/items/{item_id}/push", response_model=PushToBuyerOut)
def push_to_buyer(
    item_id: int,
    payload: PushToBuyerIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PushToBuyerOut:
    """Send this listing to the buyer behind one anonymised candidate card.

    The seller supplies only the ordinal they can see; `resolve_candidate`
    turns it back into a want using the map recorded when that candidate list
    was generated. An unknown or stale ordinal is refused rather than guessed —
    guessing here means notifying the wrong person.
    """
    item = _owned_item(db, item_id, user)

    want_id = resolve_candidate(user.id, item.id, payload.card_id.strip())
    if want_id is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "这份候选名单已经过期。请让顾问重新找一次潜在买家，"
            "再从新名单里推送。",
        )
    want = StandingWantRepository(db).get(want_id)
    if want is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这条买家求购已经不存在了")

    try:
        push_item_to_want(db, item, want)
    except PushError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return PushToBuyerOut(
        ok=True,
        message="已发送。买家会在消息里看到你的商品——要不要预订由他自己决定。",
    )


@router.post("/items/{item_id}/confirm", response_model=SellerItem)
def confirm(
    item_id: int,
    payload: ConfirmIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    index: ItemIndexService = Depends(get_index),
) -> SellerItem:
    item = _owned_item(db, item_id, user)
    try:
        LifecycleService(db, index).confirm(item, user, payload.buyer_id)
    except LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return build_seller_item(item, ReservationRepository(db).count_active_for_item(item.id))


@router.post("/items/{item_id}/status", response_model=SellerItem)
def change_status(
    item_id: int,
    payload: ItemStatusIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    index: ItemIndexService = Depends(get_index),
) -> SellerItem:
    """Pause or resume a listing — the reversible counterpart to delisting.

    Same service call the advisor's confirmation gate ends in, so the two
    paths cannot drift: whichever way a seller asks for it, the status change,
    the status-log entry, the index update and the want restoration are the
    same code.
    """
    item = _owned_item(db, item_id, user)
    service = LifecycleService(db, index)
    cancelled = 0
    try:
        if payload.target == "pause":
            service.pause(item, user)
        else:
            # 恢复上架顺带作废「收起来等」的出售计划。条数回传给界面，因为
            # 一次静默的撤销和一次静默的隐身是同一类问题——卖家会以为提醒还排着。
            cancelled = service.resume(item, user)
    except LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    row = build_seller_item(item, ReservationRepository(db).count_active_for_item(item.id))
    if cancelled:
        row.cancelled_sale_plans = cancelled
    return row


@router.delete("/items/{item_id}")
def delist(
    item_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    index: ItemIndexService = Depends(get_index),
) -> dict:
    """Permanent deletion (the frontend shows a confirmation dialog)."""
    item = _owned_item(db, item_id, user)
    try:
        LifecycleService(db, index).delist(item, user)
    except LifecycleError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True, "message": "商品已永久删除"}


class SaleWatchState(BaseModel):
    """出售计划监控的总开关，以及它现在管着几条计划。

    `active_plans` 和开关一起返回，是因为界面上那个按钮需要说清楚**关掉会影响
    什么**：一个孤零零的开关，卖家不知道关了它会失去什么，也就不敢开。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    active_plans: int = 0
    #: 其中有几条已经进入观察窗口、正在被每周扫描。
    watching_now: int = 0


class SaleWatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class SalePlanPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 只停这一条 / 只开这一条。撤销走 DELETE，是另一件事。
    watch_enabled: bool


class SalePlanRow(BaseModel):
    """一条延后出售计划，卖家视角。

    `date_basis` 一起返回，是因为这个日期有可能是系统从「12 月放假前」推断出来的。
    一个推断出来的日期和一个卖家亲口说的日期在界面上长得一模一样，把依据摆出来，
    错的那个才有机会被改掉。
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: int
    item_name: str
    category: str | None = None
    target_date: str
    watch_from: str
    date_basis: str = ""
    status: str
    #: 这一条自己的开关。和总开关是两层——界面要能分辨「我把这条停了」和
    #: 「我把整个功能停了」，否则卖家停掉一条之后会以为全部都停了。
    watch_enabled: bool = True
    #: 商品是不是为这条计划被收进了隐身（在库中、买家搜不到）。
    hold_item: bool = False
    #: 这一条现在是不是真的在被扫描（活着 + 自己开着 + 总开关开着 + 进了窗口）。
    watching_now: bool = False
    notified_count: int = 0
    last_checked_at: str = ""


def _plan_row(plan, master_enabled: bool) -> SalePlanRow:
    # 视图由 plan_agent 的工具层算，不在这里重算一遍：`watching_now` 是四个条件
    # 的合成，两处各写一份迟早会给出不同的答案，而卖家看到的正是这一个布尔值。
    from ..services.plan_agent.tools import _view

    v = _view(plan, master_enabled)
    return SalePlanRow(
        plan_id=v.plan_id,
        item_name=v.item_name,
        category=v.category,
        target_date=v.target_date,
        watch_from=v.watch_from,
        date_basis=v.date_basis,
        status=v.status,
        watch_enabled=v.watch_enabled,
        hold_item=v.hold_item,
        watching_now=v.watching_now,
        notified_count=v.notified_count,
        last_checked_at=v.last_checked_at,
    )


@router.get("/sale-plans", response_model=list[SalePlanRow])
def list_sale_plans(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SalePlanRow]:
    """卖家排着的出售计划。

    存在的理由和「我在卖的」一样：一个只能通过对话查看的东西，卖家没法确认它还
    在不在。周扫描会按这张表发通知和邮件，所以这张表必须是可见、可撤的。
    """
    master = bool(getattr(user, "sale_watch_enabled", True))
    return [_plan_row(p, master) for p in sale_plan_service.list_plans(db, user.id)]


@router.delete("/sale-plans/{plan_id}")
def cancel_sale_plan(
    plan_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """撤掉一条计划。撤掉之后周扫描不再看它，也不再发任何提醒。"""
    if not sale_plan_service.cancel_plan(db, user.id, plan_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "计划不存在")
    return {"ok": True, "message": "出售计划已撤销，不会再提醒你了"}


@router.get("/sale-watch", response_model=SaleWatchState)
def get_sale_watch(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SaleWatchState:
    """总开关的当前状态。界面上那个按钮的亮灭读的就是它。"""
    plans = sale_plan_service.list_plans(db, user.id)
    master = bool(getattr(user, "sale_watch_enabled", True))
    rows = [_plan_row(p, master) for p in plans]
    return SaleWatchState(
        enabled=master,
        active_plans=len(rows),
        watching_now=sum(1 for r in rows if r.watching_now),
    )


@router.patch("/sale-watch", response_model=SaleWatchState)
def set_sale_watch(
    payload: SaleWatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SaleWatchState:
    """开 / 关总开关。

    这个端点和 `plan_agent` 的 `set_plan_watch` 工具**改的是同一个字段**，不是
    各记各的。两边各存一份状态，就会出现卖家用嘴关掉之后界面上的开关还亮着——
    而那个亮着的开关在说假话，比没有这个开关更糟。

    关掉不删任何计划：周扫描在 `due_plans` 里跳过这位卖家，仅此而已。所以这是
    一个随时可逆的动作，不需要二次确认。
    """
    user.sale_watch_enabled = bool(payload.enabled)
    db.commit()
    return get_sale_watch(user=user, db=db)


@router.patch("/sale-plans/{plan_id}", response_model=SalePlanRow)
def patch_sale_plan(
    plan_id: int,
    payload: SalePlanPatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SalePlanRow:
    """单条计划的开关。停掉 ≠ 撤掉：停掉随时能开，撤掉（DELETE）要重排。"""
    from ..services.plan_agent.tools import resolve_owned_plan
    from ..services.seller_advisor.tools import ToolError

    try:
        plan = resolve_owned_plan(db, user.id, plan_id)
    except ToolError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "计划不存在") from exc
    plan.watch_enabled = bool(payload.watch_enabled)
    db.commit()
    return _plan_row(plan, bool(getattr(user, "sale_watch_enabled", True)))
