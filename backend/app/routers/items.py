"""Listing flow (inventory room): photo upload → submit, persist, and index.

There is no AI assistance on this path any more. The quick form is exactly
that — what the seller types is what gets listed — and the photo-plus-a-sentence
route moved into the publishing agent, where the same content also gets checked
against the rule book before it goes live. Autofilling here would have meant
one of the two listing paths quietly skipping that check.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from ..config import LOCATION_BY_NAME, other_subcategory, valid_head_noun, valid_subcategory
from ..db import get_db
from ..deps import get_current_user, get_index, get_optional_user
from ..models import ItemStatus, User
from ..repositories import ItemRepository, ReservationRepository, StatusLogRepository
from ..repositories.want_repo import ItemTrafficRepository
from ..schemas import BrowseOut, ItemCard, ItemCreateIn, ItemCreateOut, PhotoOut
from ..services.browse import SORTS, BrowseFilters, filter_and_sort
from ..services.images import ALLOWED_SUFFIX, compress_and_save
from ..services.item_index import ItemIndexService
from ..services.wants import notify_optin_buyers
from ..services.agents.publishing.schemas import CATEGORIES, CONDITIONS
from ..services.text import clip_sentences
from ..services.views import build_card

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/items", tags=["items"])

MAX_UPLOAD_BYTES = 15 * 1024 * 1024


@router.post("/photo", response_model=PhotoOut)
async def upload_photo(
    file: UploadFile = File(...),
    _: User = Depends(get_current_user),
) -> PhotoOut:
    suffix = "." + (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    if suffix and suffix not in ALLOWED_SUFFIX:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"不支持的图片格式：{suffix}")
    raw = await file.read()
    if not raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "上传的文件是空的")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "图片太大了（超过 15MB）")
    try:
        path = compress_and_save(raw, file.filename or "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"图片无法读取：{exc}") from exc
    return PhotoOut(photo_path=path, photo_url=f"/static/{path}")


@router.post("", response_model=ItemCreateOut)
def create_item(
    payload: ItemCreateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    index: ItemIndexService = Depends(get_index),
) -> ItemCreateOut:
    location = LOCATION_BY_NAME.get(payload.pickup_location_name)
    if not location:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请从预设地点中选择自提点")

    name = (payload.name or "").strip()
    summary = (payload.summary or "").strip()
    attributes = dict(payload.attributes or {})
    description = (payload.description or "").strip()

    # category and condition are required here rather than optional-then-guessed.
    # They drive structured search and the comparable-price lookup, so a listing
    # without them is one buyers cannot filter to and the advisor cannot price —
    # and with no autofill left on this path, nothing would fill them in later.
    if attributes.get("category") not in CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请从列表中选择一个类别")
    if attributes.get("condition") not in CONDITIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请从列表中选择成色")
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请给商品填写标题")

    if not summary:
        summary = clip_sentences(description or name, 2)[:120] or name
    # 子类目不必填：这条路上没有模型来猜，而让一次上架因为一个可选的细分而失败
    # 不值得。没选、或选的不属于这个大类，就落到该大类的「其他…」。
    attributes["subcategory"] = valid_subcategory(
        attributes["category"], attributes.get("subcategory")
    ) or other_subcategory(attributes["category"])
    # 这条路没有抽取步骤，中心词通常是空的；查找时这类商品退回按标题分词比对。
    attributes["head_noun"] = valid_head_noun(name, attributes.get("head_noun"))
    attributes.setdefault("brand", None)
    attributes.setdefault("color", None)

    item = index.create_item(
        db,
        seller_id=user.id,
        name=name[:40],
        description=description,
        summary=summary[:120],
        attributes=attributes,
        price=float(payload.price),
        photo_path=payload.photo_path,
        pickup_location_name=location["name"],
        lat=location["lat"],
        lng=location["lng"],
        pickup_time_start=payload.pickup_time_start.strip(),
        pickup_time_end=payload.pickup_time_end.strip(),
        status=ItemStatus.SELLING,
    )
    StatusLogRepository(db).append(item.id, None, ItemStatus.SELLING, "listed by seller")
    db.commit()

    # Tell buyers who asked to be told. Narrow by construction — opted-in
    # accounts, deliberate wants, a high similarity bar and a matching category
    # (see notify_optin_buyers) — and deliberately after the commit: a listing
    # that is already saved must not be undone by a notification that failed.
    notified = notify_optin_buyers(db, item)

    logger.info("[items] new listing #%s %s (index: %s, auto-notified %s)",
                item.id, item.name, index.stats(), notified)
    return ItemCreateOut(item=build_card(item, user, 0, None))


@router.get("/browse", response_model=BrowseOut)
def browse(
    q: str = Query("", max_length=120),
    category: str | None = Query(None, max_length=60),
    subcategory: str | None = Query(None, max_length=60),
    min_price: float | None = Query(None, ge=0),
    max_price: float | None = Query(None, ge=0),
    condition: list[str] = Query(default_factory=list),
    max_distance_km: float | None = Query(None, gt=0),
    sort: str = Query("newest"),
    limit: int = Query(24, ge=1, le=96),
    offset: int = Query(0, ge=0),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> BrowseOut:
    """The home board: browse the same inventory by explicit filters.

    Declared before `/{item_id}` on purpose — that route takes an int, so a
    `/browse` request would otherwise be rejected as a malformed item id.

    Zero LLM and zero vector search: this is the deterministic sibling of
    `/api/search/chat`, for buyers who would rather narrow with controls than
    describe what they want. Cards go through the same `build_card` whitelist.
    """
    if sort not in SORTS:
        sort = "newest"

    matches = filter_and_sort(
        ItemRepository(db).list_searchable(),
        user,
        BrowseFilters(
            q=(q or "").strip(),
            category=category or None,
            subcategory=subcategory or None,
            min_price=min_price,
            max_price=max_price,
            conditions=tuple(c for c in condition if c),
            max_distance_km=max_distance_km,
            sort=sort,
        ),
    )

    page = matches[offset : offset + limit]
    ids = [i.id for i in page]
    res_repo = ReservationRepository(db)
    counts = res_repo.count_active_bulk(ids) if ids else {}
    mine = res_repo.active_by_buyer_bulk(user.id, ids) if (ids and user) else {}

    cards = []
    for item in page:
        my_reservation = mine.get(item.id)
        cards.append(
            build_card(
                item,
                user,
                counts.get(item.id, 0),
                res_repo.rank_of(my_reservation) if my_reservation else None,
            )
        )

    return BrowseOut(
        items=cards,
        total=len(matches),
        limit=limit,
        offset=offset,
        # True only for a signed-in buyer with a location. A visitor sees
        # distances from the campus centre (see CAMPUS_CENTER), which is useful
        # but is not theirs — so the range filter stays off for them rather than
        # filtering against somewhere they are not.
        distance_available=bool(user and user.lat is not None and user.lng is not None),
    )


@router.get("/{item_id}", response_model=ItemCard)
def get_item(
    item_id: int,
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> ItemCard:
    """One item, through the buyer-facing whitelist.

    Exists so a pushed listing can be rendered as a live card in the buyer's
    messages instead of a frozen copy of the notification payload: by the time
    the buyer opens it the item may already be reserved or sold, and a card
    offering a "Reserve" button on a sold item is worse than no card.
    """
    item = ItemRepository(db).get(item_id)
    if not item or item.status in ItemStatus.TERMINAL:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这件商品已经下架了")
    reservations = ReservationRepository(db).list_active_for_item(item.id)
    rank = next(
        (i for i, r in enumerate(reservations, start=1) if user and r.buyer_id == user.id),
        None,
    )
    return build_card(item, user, len(reservations), rank)


@router.post("/{item_id}/click")
def record_click(
    item_id: int,
    # Open, and it has to be: impressions are counted for signed-out visitors
    # too (browse is open), so requiring an account to count the click would
    # make every click-through rate an undercount — and `analyze_demand` reports
    # that rate to sellers as a reason their listing is or is not working.
    # Nothing here reads the user; it is a counter, not an account action.
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> dict:
    """A search-result card was opened — the other half of an impression.

    The §6.3 "stuck" heuristic filters items by "impressions but no conversion";
    with only impressions and no clicks, "seen but nobody cared" and "never seen
    at all" are indistinguishable.
    Instrumentation failures are always swallowed: they are not important enough
    to break a frontend interaction.
    """
    try:
        ItemTrafficRepository(db).bump([item_id], "clicks")
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("[items] click tracking failed: %s", exc)
    return {"ok": True}
