"""Business orchestration for standing wants — event layer E1 / E2 / E4 / E5.

Design constraints:
  * C1 buyer search costs zero LLM tokens — this module contains no LLM calls;
  * C2 the seller's synchronous listing path adds no LLM calls — same here;
  * C8 buyer demand must never be exposed to sellers in individually
    identifiable form — this module only produces notifications for the
    **buyer themself**.

Every failure is swallowed and logged: wants are a value-add feature and must
never drag down the search or listing main flows.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    Item,
    ItemStatus,
    Notification,
    NotificationType,
    StandingWant,
    User,
    WantSource,
    WantStatus,
    utcnow,
)
from ..repositories import ItemRepository, NotificationRepository
from ..repositories.want_repo import ItemTrafficRepository, StandingWantRepository
from .item_index import ItemIndexService
from .text import words
from .views import photo_url
from .want_index import WantIndexService, get_want_index

logger = logging.getLogger(__name__)

#: Search outcomes considered "there was demand but it wasn't satisfied"
UNSATISFIED_DECISIONS = ("empty", "low_confidence", "clarify")


# =============================================================================
# E2: unsatisfied search → record the want + reverse-scan the inventory
# =============================================================================

def sediment_want(
    db: Session,
    index: ItemIndexService,
    user: User | None,
    *,
    decision: str,
    raw_query: str,
    filters: dict[str, Any],
    semantic_text: str,
    want_index: WantIndexService | None = None,
) -> tuple[StandingWant | None, str | None]:
    """Turn one unsatisfied search into a standing want and reverse-scan the
    inventory once.

    Returns (want, extra copy). The extra copy is **template-generated**, no
    LLM — having the model restate an already-decided result is a path
    README §5 explicitly rejected.

    Incremental LLM calls: 0. filters / semantic_text come from
    outcome.parsed; whether or not call site B fired inside the retrieval
    pipeline, they're already in hand.
    """
    if not settings.want_enabled or user is None:
        return None, None
    if decision not in UNSATISFIED_DECISIONS:
        return None, None
    # A sentence with no constraints at all ("got anything good?") is
    # meaningless to record.
    if not (filters or semantic_text.strip()):
        return None, None

    try:
        repo = StandingWantRepository(db)
        want = repo.find_duplicate(user.id, semantic_text, filters)
        want = repo.touch(want) if want else repo.create(
            buyer_id=user.id,
            raw_query=raw_query,
            filters=filters,
            semantic_text=semantic_text,
            origin=(user.lat, user.lng) if user.lat is not None else None,
            max_distance_km=(
                user.pickup_range_km if user.pickup_range_enabled else None
            ),
            source=WantSource.IMPLICIT,
        )
        wi = want_index or get_want_index()
        wi.index_want(want)

        # Reverse-scan the inventory: no recall just now doesn't mean nothing
        # is in stock — it might have missed by a few dollars, or the semantic
        # path's relative window kept it out of the top 8.
        hint = _stock_hint(db, index, wi, want)
        db.commit()
        return want, hint
    except Exception as exc:  # noqa: BLE001  must never affect the search main flow
        db.rollback()
        logger.warning("[wants] failed to record want (search unaffected): %s", exc)
        return None, None


def _stock_hint(
    db: Session, index: ItemIndexService, want_index: WantIndexService, want: StandingWant
) -> str | None:
    """Extra copy after the reverse inventory scan. Pure template, zero tokens."""
    matches = want_index.match_items_for_want(index, want, limit=5)
    if not matches:
        return None
    # The index has no seller_id (banned by the C3 whitelist), so filtering out
    # the buyer's own listings has to go back to the business DB.
    own = {
        i.id
        for i in ItemRepository(db).get_many([m.item_id for m in matches])
        if i.seller_id == want.buyer_id
    }
    matches = [m for m in matches if m.item_id not in own]
    if not matches:
        return None
    cheapest = min(matches, key=lambda m: m.price)
    rec = index.get_record(cheapest.item_id)
    if rec is None:
        return None
    if want.max_price is not None and cheapest.price > want.max_price:
        gap = round(cheapest.price - want.max_price)
        return f'顺便一提，有一件「{rec.name}」，只比你的预算高 ${gap}，要不要看看？'
    return f'顺便一提，现在有一件「{rec.name}」可能正合适，要不要看看？'


# =============================================================================
# E1: seller hand-picks a candidate card → push this item to that one buyer
# =============================================================================
#
# There used to be an automatic push here: every new listing fanned out to
# every matching want. It was removed on purpose. Automatic matching pushes on
# a *score*, and a score cannot tell "this is the thing you wanted" from "this
# shares vocabulary with the thing you wanted" — the buyer absorbs every false
# positive as spam. A seller pressing a button on one anonymised card is a
# stronger signal than any threshold, and it also means there is exactly one
# push path, so nothing can double-notify the same buyer for the same item.


class PushError(Exception):
    """A push was refused. The message is safe to show the seller — it must
    never reveal anything about the buyer beyond "this candidate is no longer
    reachable"."""


def push_item_to_want(
    db: Session,
    item: Item,
    want: StandingWant,
    want_index: WantIndexService | None = None,
) -> None:
    """Seller-initiated push of one item to the buyer behind one want.

    The card the seller clicked is a **snapshot**, possibly minutes old. Between
    rendering it and pressing the button the want may have been dismissed, the
    buyer may have reserved something else, or the seller may have raised the
    price out of that buyer's budget. So every condition is re-checked here
    against the business DB — the card is a display artefact and is never
    trusted as authorisation.
    """
    if not settings.want_enabled:
        raise PushError("长期求购功能已关闭")
    if item.status not in ItemStatus.SEARCHABLE:
        raise PushError("这件商品已经下架，无法推送")
    if want.buyer_id == item.seller_id:
        raise PushError("不能把商品推送给你自己")

    wi = want_index or get_want_index()
    # respect_notify_cap=False: the lifetime cap is automated-nagging control.
    # A hand-picked push is exempt from it (see want_max_notify_total in
    # config.py); the per-day cap below still applies.
    if not wi.passes_hard_for_push(item, want):
        raise PushError(
            "这位买家已经不再匹配了（他的求购可能已经关闭，"
            "或者你的价格现在超出了他的预算）。请重新获取一次候选名单。"
        )

    repo = StandingWantRepository(db)
    notifications = NotificationRepository(db)

    if _already_pushed(db, want.buyer_id, item.id):
        raise PushError("你已经把这件商品推送给这位买家了")
    if repo.notified_today(want.buyer_id) >= settings.want_max_notify_per_day:
        raise PushError(
            "这位买家今天收到的通知已达上限，明天再试"
        )
    if _pushed_today_by_seller(db, item.seller_id) >= settings.want_max_push_per_seller_per_day:
        raise PushError(
            f"你今天的推送已达上限 {settings.want_max_push_per_seller_per_day} 次。"
            "候选名单是用来挑人的，不是用来群发的。"
        )

    notifications.create(
        user_id=want.buyer_id,
        type_=NotificationType.WANT_MATCHED,
        payload={
            "want_id": want.id,
            # The buyer's own search text — theirs, so C8 does not apply here.
            "want_text": want.semantic_text,
            "item_id": item.id,
            "item_name": item.name,
            "price": item.price,
            "photo_url": photo_url(item),
            "pickup_location_name": item.pickup_location_name,
            "seller_id": item.seller_id,   # internal only; never rendered to the buyer
        },
    )
    repo.mark_notified(want)
    ItemTrafficRepository(db).bump([item.id], "pushed")
    db.commit()
    logger.info("[wants] seller=%s pushed item=%s to want=%s (buyer=%s)",
                item.seller_id, item.id, want.id, want.buyer_id)


#: Words that say something about price or condition rather than naming the
#: thing wanted. "cheap textbooks" is a request for textbooks with a budget
#: attached, and the budget is enforced by max_price — requiring the word
#: "cheap" to appear in a listing's title would only ever reject good matches.
#: Words that qualify the ask rather than name the thing. They are stripped
#: before the coverage test, because requiring them in a listing title would
#: reject good matches for saying the same thing differently: a buyer asking for
#: 便宜的教材 is asking for a 教材, and max_price already enforces the 便宜.
#: Condition words are here for the same reason — CONDITION_DICT extracts those
#: into a filter, so a title that omits 二手 is not a mismatch.
_QUALIFIERS = {
    # Chinese
    "便宜", "实惠", "划算", "平价", "低价", "预算",
    "二手", "闲置", "全新", "九成新", "成色",
    "好", "好用", "不错", "任意", "随便", "一个", "一点",
    "大", "小", "大号", "小号", "新", "旧",
    # English — kept for the mixed-script queries the campus actually types
    "cheap", "affordable", "budget", "inexpensive", "bargain",
    "use", "used", "second", "hand", "preloved", "new", "brand",
    "good", "nice", "decent", "any", "some", "small", "big", "large",
}


def _covers_the_want(want: StandingWant, item: Item) -> bool:
    """Does the listing actually name the thing the buyer asked for?

    This is the gate the similarity score could not be. Every content word of
    the want — minus price/condition qualifiers — has to appear in the
    listing's own words. "psychology textbook" does not match a physics
    textbook; "mechanical keyboard" does not match a microphone, however close
    their embeddings sit.

    Not sufficient on its own, which is why the semantic floor stays: word
    containment cannot tell a head noun from a modifier, so a want for "bike"
    is covered by "bike lock". The project already knows this failure —
    see MODIFIER_PENALTY in services/text.py — and the embedding is what
    separates those two.
    """
    wanted = {w for w in words(want.semantic_text or want.raw_query or "") if w not in _QUALIFIERS}
    if not wanted:
        # Nothing specific was asked for. That is not licence to send anything.
        return False
    listed = set(words(item.name or "")) | set(words(item.summary or ""))
    return wanted <= listed


def notify_optin_buyers(db: Session, item: Item) -> int:
    """Tell opted-in buyers that something they asked for has been listed.

    This is the automatic fan-out, brought back on 2026-09-14 after being
    removed on 2026-08-22. Two things are different, and both have to hold or
    it should be removed again.

    **Consent.** The buyer turned it on themselves (`User.auto_notify`, off by
    default), and it only ever fires on wants they wrote deliberately —
    `source="explicit"`. Wants captured from a search (`implicit`, and two
    thirds of the table) are excluded: "I searched for a printer once" is not
    "tell me about printers from now on", and treating it as consent is how the
    first version generated junk.

    **Relevance.** The toggle answers "may we send you things", not "is this
    thing worth sending", and the original failure was entirely the second
    question. Four narrowings answer it, and the load-bearing one is not the
    score: the listing has to actually **name** what the buyer asked for
    (`_covers_the_want`), the categories have to agree, the similarity has to
    clear a floor, and every ordinary hard condition still applies. Measuring
    the corpus showed a threshold alone cannot do this — wrong answers outscore
    right ones — so the score is a backstop and the words are the gate. See the
    note on want_autonotify_min_similarity in config.py for the numbers.

    The seller's candidate list can afford a loose match because a person reads
    it before anything is sent. Nobody reads this one.

    Nagging caps apply in full here, unlike the hand-picked push: the lifetime
    cap exists precisely to stop an automated sender, and this is one.

    Returns how many buyers were notified. Never raises — a listing must not
    fail to be created because a notification could not be sent.
    """
    if not settings.want_enabled or not settings.want_autonotify_enabled:
        return 0
    if item.status not in ItemStatus.SEARCHABLE:
        return 0

    item_category = (item.attributes or {}).get("category")
    try:
        matches = get_want_index().match_wants_for_item(db, item, limit=50)
    except Exception:  # noqa: BLE001
        logger.exception("[wants] auto-notify match failed for item=%s", item.id)
        return 0

    repo = StandingWantRepository(db)
    notifications = NotificationRepository(db)
    sent = 0
    for m in matches:
        if m.similarity < settings.want_autonotify_min_similarity:
            continue
        want = db.get(StandingWant, m.want_id)
        if want is None or want.source != WantSource.EXPLICIT:
            continue
        # A want with no category is not "matches every category" — it is a
        # want we know less about, which is the wrong thing to act on
        # unprompted.
        if not item_category or want.category != item_category:
            continue
        if not _covers_the_want(want, item):
            continue

        buyer = db.get(User, want.buyer_id)
        if buyer is None or not buyer.auto_notify:
            continue
        if want.buyer_id == item.seller_id:
            continue
        # Deduped against the hand-picked path too: a buyer who is about to be
        # auto-notified and then hand-picked would otherwise hear about the same
        # listing twice from two features that do not know about each other.
        if _already_pushed(db, want.buyer_id, item.id):
            continue
        if (want.notify_count or 0) >= settings.want_max_notify_total:
            continue
        if repo.notified_today(want.buyer_id) >= settings.want_max_notify_per_day:
            continue

        notifications.create(
            user_id=want.buyer_id,
            type_=NotificationType.WANT_MATCHED,
            payload={
                "want_id": want.id,
                "want_text": want.semantic_text,
                "item_id": item.id,
                "item_name": item.name,
                "price": item.price,
                "photo_url": photo_url(item),
                "pickup_location_name": item.pickup_location_name,
                # Internal only, never rendered. Present so _already_pushed
                # counts this row: both paths have to see each other's sends,
                # and rows from the deleted 2026-08 feature carry neither key
                # and stay invisible, which is still what we want.
                "seller_id": item.seller_id,
                "auto": True,
            },
        )
        repo.mark_notified(want)
        sent += 1

    if sent:
        ItemTrafficRepository(db).bump([item.id], "pushed", sent)
        db.commit()
        logger.info("[wants] auto-notified %s opted-in buyer(s) about item=%s", sent, item.id)
    return sent


def _already_pushed(db: Session, buyer_id: int, item_id: int) -> bool:
    """Has a **seller** already pushed this item to this buyer?

    Read straight off the notification history rather than from a dedicated
    log table: the volume per buyer is tiny (both writers are capped per day),
    so a scan of their recent notifications is cheaper than another table to
    keep consistent. Revisit this if a bulk push path is ever added.

    Despite the name it answers "has this buyer already heard about this item",
    counting the opt-in fan-out as well as a seller pressing the button — two
    senders that do not know about each other would otherwise tell one person
    the same thing twice.

    The `seller_id` test is what keeps rows left behind by the *2026-08* automatic
    push invisible here. Those came from a feature that was deleted, and counting
    them would let history from a removed code path veto a push a human is asking
    for right now — which is exactly what happened the first time a seller tried
    this on an item that had already been auto-pushed. Both current writers set
    the key; the old ones did not.
    """
    rows = NotificationRepository(db).list_for_user(buyer_id, limit=200)
    return any(
        n.type == NotificationType.WANT_MATCHED
        and (n.payload or {}).get("seller_id") is not None
        and int((n.payload or {}).get("item_id") or 0) == int(item_id)
        for n in rows
    )


def _pushed_today_by_seller(db: Session, seller_id: int) -> int:
    """Pushes this seller sent in the last 24h, counted off the notification
    payload for the same reason as above."""
    since = utcnow() - timedelta(days=1)
    rows = (
        db.query(Notification)
        .filter(
            Notification.type == NotificationType.WANT_MATCHED,
            Notification.created_at >= since,
        )
        .all()
    )
    return sum(
        1 for n in rows if int((n.payload or {}).get("seller_id") or 0) == int(seller_id)
    )


# =============================================================================
# E4: buyer reserved successfully → close their similar wants
# =============================================================================

def fulfill_on_reserve(
    db: Session,
    buyer: User,
    item: Item,
    want_index: WantIndexService | None = None,
) -> int:
    """The buyer got one — stop pushing them more of the same.

    "The same" is decided by **semantic matching**, not category-field
    equality — rule parsing often fails to extract a category ("a used mini
    fridge under $50" only parses out max_price), leaving that want's category
    as None; field equality would never close it.
    """
    if not settings.want_enabled:
        return 0
    try:
        wi = want_index or get_want_index()
        matches = wi.match_wants_for_item(
            db, item, buyer_id=buyer.id, respect_notify_cap=False
        )
        repo = StandingWantRepository(db)
        closed: list[int] = []
        for m in matches:
            want = repo.get(m.want_id)
            if want is None or want.status != WantStatus.ACTIVE:
                continue
            repo.set_status(want, WantStatus.FULFILLED, fulfilled_by_item_id=item.id)
            wi.remove_want(want.id)
            closed.append(want.id)

        # Second pass: wants with an explicit, exactly matching category are
        # closed too, even if their semantic score missed the threshold.
        category = (item.attributes or {}).get("category")
        if category:
            for want in repo.list_for_buyer(buyer.id):
                if want.category == category and want.id not in closed:
                    repo.set_status(want, WantStatus.FULFILLED, fulfilled_by_item_id=item.id)
                    wi.remove_want(want.id)
                    closed.append(want.id)

        if closed:
            logger.info("[wants] buyer=%s reserved successfully, closed %d wants %s",
                        buyer.id, len(closed), closed)
        return len(closed)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[wants] failed to close wants (reservation unaffected): %s", exc)
        return 0


# =============================================================================
# E5: item reaches a final state (sold / cancelled / delisted / expired) → wants reset
# =============================================================================

def release_wants_for_item(
    db: Session,
    item_id: int,
    buyer_id: int | None = None,
    want_index: WantIndexService | None = None,
) -> int:
    """Restore to active any wants that were closed by this item but where the
    buyer ultimately didn't get it.

    Without this step the want lifecycle is a one-way street: E4 marks the want
    fulfilled at **reservation** time, but a reservation isn't a deal — a
    cancelled reservation, the item going to someone else, the seller missing
    the confirmation window, or the seller delisting all leave the buyer with
    no item AND no future pushes for similar listings.

    Restoration keys off `fulfilled_by_item_id` for an exact match instead of
    re-running semantic matching: the thresholds float, and one wrong guess
    permanently swallows a want.
    """
    if not settings.want_enabled:
        return 0
    try:
        repo = StandingWantRepository(db)
        wi = want_index or get_want_index()
        restored: list[int] = []
        for want in repo.list_fulfilled_by_item(item_id, buyer_id=buyer_id):
            if repo.reactivate(want):
                wi.index_want(want)
                restored.append(want.id)
        if restored:
            logger.info("[wants] item=%s did not close the deal, restored %d wants %s",
                        item_id, len(restored), restored)
        return len(restored)
    except Exception as exc:  # noqa: BLE001  must never affect the state-machine main flow
        logger.warning("[wants] failed to restore wants (main flow unaffected): %s", exc)
        return 0


def settle_on_deal(
    db: Session,
    item: Item,
    winner_buyer_id: int,
    loser_buyer_ids: list[int],
    want_index: WantIndexService | None = None,
) -> int:
    """E5: seller confirms the deal → restore the wants of every losing backup.

    The winning buyer's wants stay fulfilled (set by E4); the losers are still
    waiting for this thing, and the next similar listing must still reach them.
    """
    if not settings.want_enabled:
        return 0
    restored = 0
    for buyer_id in dict.fromkeys(loser_buyer_ids):
        if buyer_id == winner_buyer_id:
            continue
        restored += release_wants_for_item(db, item.id, buyer_id=buyer_id,
                                           want_index=want_index)
    return restored


# =============================================================================
# Explicit wants: the buyer taps "notify me when it's in"
# =============================================================================

def _index_and_commit(db: Session, want: StandingWant,
                      want_index: WantIndexService | None) -> StandingWant:
    (want_index or get_want_index()).index_want(want)
    db.commit()
    return want


def create_explicit_want(
    db: Session,
    user: User,
    text: str,
    want_index: WantIndexService | None = None,
) -> StandingWant:
    """The buyer writes a want by hand.

    Parsing goes through `parse_query` (pure regex + dictionaries), **no
    LLM** — this path sits on the buyer side and is bound by C1; explicit
    wants and E2-recorded wants share the same parser so their
    filters / semantic_text speak the same language — otherwise the seller-side
    aggregate stats would get muddled.
    """
    from .query_parser import parse_query  # local import to dodge the retrieval-layer module cycle

    parsed = parse_query(text)
    repo = StandingWantRepository(db)
    filters = parsed.filters_dict()

    existing = repo.find_duplicate(user.id, parsed.semantic_text, filters)
    if existing is not None:
        repo.touch(existing)
        existing.source = WantSource.EXPLICIT
        existing.fulfilled_by_item_id = None
        db.add(existing)
        db.flush()
        return _index_and_commit(db, existing, want_index)

    want = repo.create(
        buyer_id=user.id,
        raw_query=text,
        filters=filters,
        semantic_text=parsed.semantic_text,
        origin=(user.lat, user.lng) if user.lat is not None else None,
        max_distance_km=user.pickup_range_km if user.pickup_range_enabled else None,
        source=WantSource.EXPLICIT,
    )
    return _index_and_commit(db, want, want_index)


def promote_to_explicit(
    db: Session,
    user: User,
    want_id: int,
    want_index: WantIndexService | None = None,
) -> StandingWant | None:
    """Upgrade an E2 auto-recorded want to an explicit subscription (the search
    page's "notify me when it's in").

    Also renews it: the user just confirmed they're still waiting, so the TTL
    restarts from now.
    """
    repo = StandingWantRepository(db)
    want = repo.get(want_id)
    if want is None or want.buyer_id != user.id:
        return None
    want.source = WantSource.EXPLICIT
    want.fulfilled_by_item_id = None
    repo.touch(want)          # touch sets status=active + renews the TTL
    return _index_and_commit(db, want, want_index)


def dismiss_want(
    db: Session,
    user: User,
    want_id: int,
    want_index: WantIndexService | None = None,
) -> bool:
    """Buyer manually closes a want (WantStatus.DISMISSED). Leaves the index
    immediately; no more pushes."""
    repo = StandingWantRepository(db)
    want = repo.get(want_id)
    if want is None or want.buyer_id != user.id:
        return False
    repo.set_status(want, WantStatus.DISMISSED)
    (want_index or get_want_index()).remove_want(want.id)
    db.commit()
    return True


def list_wants(db: Session, user: User) -> list[StandingWant]:
    return StandingWantRepository(db).list_all_for_buyer(user.id)


# =============================================================================
# Expiry cleanup (piggybacks on the existing 60s poll)
# =============================================================================

def expire_due_wants(db: Session, want_index: WantIndexService | None = None) -> int:
    try:
        repo = StandingWantRepository(db)
        expired = repo.expire_due()
        wi = want_index or get_want_index()
        for want_id in expired:
            wi.remove_want(want_id)
        db.commit()
        if expired:
            logger.info("[wants] expired %d wants this round", len(expired))
        return len(expired)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("[wants] expiry cleanup failed: %s", exc)
        return 0
