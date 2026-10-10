"""Seller sales-advisor chat endpoint (call sites C and D).

The router does auth and assembly only. Intent routing, the conversation
thread, and the tool loop all live under `services.agents` /
`services.seller_advisor`; SQL lives in the repository / tool layer.

Two boundaries worth keeping in view:

* **Ownership** is not checked here. It moved down to
  `tools.resolve_owned_item`, which runs for every item_id the model names —
  a wider net than one check at the door.
* **The conversation** is not sent by the client either. The request carries a
  thread id; the history behind it is server-side state. An unusable or
  foreign thread id silently starts a new conversation rather than erroring,
  because a stale browser tab is normal and a fresh thread is the right answer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user, get_index
from ..models import User
from ..services.agents import run_turn, sessions
from ..services.agents.lc_llm import read_turn_meter
from ..services.item_index import ItemIndexService
from ..services.publishing.approve import approve_listing
from ..services.publishing.schemas import PublishApproveIn, PublishApproveOut
from ..services.seller_advisor import (
    AdvisorChatIn,
    AdvisorChatOut,
    AdvisorThreadRow,
    ThreadRenameIn,
    ThreadTranscript,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/seller/advisor", tags=["seller-advisor"])


@router.post("/chat", response_model=AdvisorChatOut)
def advisor_chat(
    payload: AdvisorChatIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AdvisorChatOut:
    thread = sessions.resolve_thread(db, user.id, payload.thread_id)
    thread_id = thread.id
    try:
        state = run_turn(db, user.id, payload.message, thread_id, payload.photo_path or "")
    except Exception as exc:  # noqa: BLE001  graph/provider failure must not 500 the chat
        logger.exception("[advisor] turn failed")
        # A turn that failed halfway still spent whatever it spent. Reporting
        # zero there would make the cheap-looking turns the broken ones.
        spent = read_turn_meter()
        return AdvisorChatOut(
            ok=False,
            thread_id=thread_id,
            input_tokens=spent["input_tokens"],
            output_tokens=spent["output_tokens"],
            llm_calls=spent["calls"],
            token_detail=spent["by_call_site"],
            message=f"顾问暂时不可用（{type(exc).__name__}）。请稍后再试。",
        )

    # Recorded after the turn, so a failed turn does not retitle the
    # conversation after something the model never answered.
    sessions.note_turn(db, thread, payload.message)
    out = AdvisorChatOut.model_validate(state.get("result") or {"ok": True})

    # A suspended graph comes back with __interrupt__ rather than a finished
    # result. Surfaced as `pending_action` so the UI can render a yes/no; the
    # graph stays parked until the next message resumes it, so this is a real
    # state of the conversation, not a hint.
    interrupts = state.get("__interrupt__") or []
    if interrupts:
        payload_ = getattr(interrupts[0], "value", None)
        if isinstance(payload_, dict):
            out.pending_action = payload_
            # Also written into the reply. `pending_action` is the structured
            # form for a UI that renders a yes/no control; the question has to
            # reach the seller either way, and a confirm prompt that only
            # exists in a field nothing displays is a conversation that stops
            # for no visible reason.
            question = str(payload_.get("question") or "").strip()
            if question and question not in out.reply:
                out.reply = (out.reply + "\n\n" + question).strip()
    if state.get("action_result"):
        out.action_result = state["action_result"]
    out.thread_id = thread_id
    out.route = state.get("route", "")
    # Token cost comes from the graph's per-turn meter, which is the only view
    # that sees every model call the turn made — the routing call and the
    # proposal check are spent by nodes that never report them up through the
    # result, so adding up what the result carries would understate the bill.
    usage = state.get("usage") or {}
    out.input_tokens = int(usage.get("input_tokens") or 0)
    out.output_tokens = int(usage.get("output_tokens") or 0)
    out.llm_calls = int(usage.get("calls") or 0)
    out.token_detail = usage.get("by_call_site") or {}
    return out


def _row(thread) -> AdvisorThreadRow:
    return AdvisorThreadRow(
        thread_id=thread.id, title=thread.title, updated_at=f"{thread.updated_at:%Y-%m-%d %H:%M}"
    )


@router.get("/threads", response_model=list[AdvisorThreadRow])
def list_threads(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[AdvisorThreadRow]:
    return [_row(t) for t in sessions.list_threads(db, user.id)]


@router.post("/threads", response_model=AdvisorThreadRow)
def new_thread(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> AdvisorThreadRow:
    return _row(sessions.create_thread(db, user.id))


@router.get("/threads/{thread_id}", response_model=ThreadTranscript)
def read_thread(
    thread_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ThreadTranscript:
    thread = sessions.get_thread(db, user.id, thread_id)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return ThreadTranscript(
        thread_id=thread.id, title=thread.title, turns=sessions.transcript(thread.id)
    )


@router.patch("/threads/{thread_id}", response_model=AdvisorThreadRow)
def rename_thread(
    thread_id: str,
    payload: ThreadRenameIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AdvisorThreadRow:
    thread = sessions.rename_thread(db, user.id, thread_id, payload.title)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return _row(thread)


@router.delete("/threads/{thread_id}")
def drop_thread(
    thread_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not sessions.delete_thread(db, user.id, thread_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return {"ok": True}


@router.post("/publish/approve", response_model=PublishApproveOut)
def publish_approve(
    payload: PublishApproveIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    index: ItemIndexService = Depends(get_index),
) -> PublishApproveOut:
    """Publish the draft the seller reviewed.

    A separate endpoint rather than a chat turn on purpose: this is the one
    irreversible step in the flow, it carries fields the conversation never
    handled (price, pickup spot), and the text may have been edited since the
    model last saw it — so the content check runs again in `approve_listing`,
    on what was actually submitted.
    """
    return approve_listing(db, index, user, payload)
