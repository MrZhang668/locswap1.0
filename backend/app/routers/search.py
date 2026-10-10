"""Item-finder chat entry point — the HTTP shell around the retrieval pipeline.

No LLM is called here to paraphrase results: copy comes from fixed templates,
cards go through the structured whitelist.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..config import CAMPUS_LOCATIONS, settings
from ..db import get_db
from ..deps import get_index, get_optional_user
from ..models import User
from ..repositories import ItemRepository, ReservationRepository
from ..repositories.want_repo import ItemTrafficRepository
from ..schemas import ChatIn, ChatOut, MapLocationOut, MapOut
from ..services.item_index import ItemIndexService
from ..services.wants import sediment_want
from ..services.retrieval import (
    CONTACT_REFUSAL_TEXT,
    RetrievalService,
    is_contact_request,
    render_reply,
)
from ..services.views import build_card

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/search", tags=["search"])


@router.post("/chat", response_model=ChatOut)
def chat(
    payload: ChatIn,
    # Open to signed-out visitors. Browsing is what makes someone want an
    # account; requiring one first is asking for the decision before giving them
    # anything to decide about. `user` is None for them, and every layer below
    # already accepts that — retrieval's hard filter, `_own_item_ids` and
    # `build_card` were all written against `User | None`.
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
    index: ItemIndexService = Depends(get_index),
) -> ChatOut:
    # §7: requests for other people's contact info / IDs are always refused,
    # without spending a single LLM token
    if is_contact_request(payload.message):
        return ChatOut(
            reply=CONTACT_REFUSAL_TEXT,
            cards=[],
            decision="privacy_refusal",
            confidence=0.0,
            clarify_round=payload.clarify_round,
            debug={"privacy_guard": True},
        )

    # Chat context carries only the last N turns (hard token-control requirement)
    history = [t.model_dump() for t in payload.history][-(settings.chat_history_turns * 2) :]

    outcome = RetrievalService(index).search(
        query=payload.message,
        user=user,
        history=history,
        clarify_round=payload.clarify_round,
    )

    cards = []
    if outcome.item_ids:
        items = {i.id: i for i in ItemRepository(db).get_many(outcome.item_ids)}
        res_repo = ReservationRepository(db)
        counts = res_repo.count_active_bulk(outcome.item_ids)
        mine = res_repo.active_by_buyer_bulk(user.id, outcome.item_ids) if user else {}
        for item_id in outcome.item_ids:  # preserve the post-fusion ordering
            item = items.get(item_id)
            if not item:
                continue
            my_reservation = mine.get(item_id)
            cards.append(
                build_card(
                    item,
                    user,
                    counts.get(item_id, 0),
                    res_repo.rank_of(my_reservation) if my_reservation else None,
                )
            )

    next_round = payload.clarify_round + 1 if outcome.decision == "clarify" else 0
    reply = render_reply(outcome, len(cards))

    # ---- Traffic instrumentation (the data source for E6) ----
    # Increment in place, no per-row ORM writes: this is the retrieval hot path.
    _record_traffic(db, outcome)

    # ---- E2: search unfulfilled → capture the want + reverse-scan inventory ----
    # Zero LLM: filters / semantic_text come from outcome.parsed, which we
    # already have whether or not call site B fired, so the incremental token
    # cost is 0.
    # A want belongs to a buyer, so a signed-out search leaves none behind. That
    # is a real cost, not a technicality: demand analytics only see the traffic
    # that is signed in, and the share that is will fall now that browsing is
    # open. Worth revisiting with a nullable buyer on `demand_event` if the gap
    # gets big enough to distort `analyze_demand`.
    want, hint = (None, None) if user is None else sediment_want(
        db,
        index,
        user,
        decision=outcome.decision,
        raw_query=payload.message,
        filters=outcome.parsed.filters_dict(),
        semantic_text=outcome.parsed.semantic_text,
    )
    if hint:
        reply = f"{reply}\n{hint}"

    return ChatOut(
        reply=reply,
        cards=cards,
        decision=outcome.decision,
        confidence=outcome.confidence,
        clarify_round=next_round,
        # The retrieval trace (per-leg scores, thresholds, parsed filters) is a
        # tuning aid for the signed-in "检索详情" panel, not something to hand
        # every anonymous caller of a public endpoint.
        debug=outcome.debug if user else {},
        # The frontend uses this to show "notify me when it's in": the want is
        # already captured — one tap just upgrades its source to explicit,
        # no re-parsing needed
        want_id=want.id if want else None,
    )


@router.get("/map", response_model=MapOut)
def map_view(
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> MapOut:
    """Map data for the Search workbench: all preset locations + the reservable
    items at each.

    Zero LLM; cards still go through the `build_card` whitelist, and location
    names are public pickup spots to begin with.
    """
    items = ItemRepository(db).list_searchable()
    ids = [i.id for i in items]
    res_repo = ReservationRepository(db)
    counts = res_repo.count_active_bulk(ids) if ids else {}
    mine = res_repo.active_by_buyer_bulk(user.id, ids) if (ids and user) else {}

    by_location: dict[str, list] = {}
    for item in items:
        my_reservation = mine.get(item.id)
        card = build_card(
            item,
            user,
            counts.get(item.id, 0),
            res_repo.rank_of(my_reservation) if my_reservation else None,
        )
        by_location.setdefault(item.pickup_location_name, []).append(card)

    return MapOut(
        locations=[
            MapLocationOut(
                name=loc["name"],
                lat=loc["lat"],
                lng=loc["lng"],
                items=by_location.get(loc["name"], []),
            )
            for loc in CAMPUS_LOCATIONS
        ]
    )


def _record_traffic(db: Session, outcome) -> None:
    """Impression = made it into the result cards; candidate-but-not-result = "almost seen"."""
    try:
        shown = list(outcome.item_ids)
        near_miss = [i for i in outcome.candidate_ids if i not in set(shown)]
        traffic = ItemTrafficRepository(db)
        if shown:
            traffic.bump(shown, "impressions")
        if near_miss:
            traffic.bump(near_miss, "entered_candidates")
        db.commit()
    except Exception as exc:  # noqa: BLE001  instrumentation must never affect retrieval
        db.rollback()
        logger.warning("[search] traffic tracking failed (retrieval unaffected): %s", exc)
