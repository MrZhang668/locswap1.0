"""Named conversations with the sales agent: the registry and its rules.

Two stores, one key. The transcript lives in the LangGraph checkpoint; the
`advisor_threads` table holds what a person actually manipulates — which
conversations exist, what they are called, who owns them. They share the thread
id, so there is no mapping to drift.

The thread id is `"<seller_id>-<random>"`:
* the random half is what makes a thread unguessable;
* the seller_id prefix is a cheap cross-check, but it is **not** the
  authorisation — every lookup here filters by `seller_id` in SQL, so a
  well-formed id belonging to someone else still resolves to nothing.

An id that fails either check is not an error to the caller: it gets a fresh
conversation. A stale browser tab is normal, and an error screen where a new
chat is the right answer helps nobody.

Nothing expires. Conversations are named things a person made on purpose, and
silently reaping them after an idle timeout — which is what the earlier
in-process version did — would delete work the seller can see in a list.
Deletion is explicit, and it removes the checkpoint along with the row.
"""

from __future__ import annotations

import logging
import secrets

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...models import AdvisorThread, utcnow

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "New chat"
#: Auto-titles are the first message, trimmed. Long enough to tell two
#: conversations apart in a narrow rail, short enough not to wrap.
TITLE_MAX = 48


def issue_thread_id(seller_id: int) -> str:
    return f"{int(seller_id)}-{secrets.token_urlsafe(16)}"


def _belongs_to(thread_id: str, seller_id: int) -> bool:
    prefix, sep, rest = thread_id.partition("-")
    return bool(sep) and bool(rest) and prefix == str(int(seller_id))


def get_thread(db: Session, seller_id: int, thread_id: str) -> AdvisorThread | None:
    """Ownership check and lookup in one place — always filtered by seller."""
    if not thread_id or not _belongs_to(thread_id, seller_id):
        return None
    return db.scalars(
        select(AdvisorThread).where(
            AdvisorThread.id == thread_id, AdvisorThread.seller_id == int(seller_id)
        )
    ).first()


def list_threads(db: Session, seller_id: int) -> list[AdvisorThread]:
    return list(
        db.scalars(
            select(AdvisorThread)
            .where(AdvisorThread.seller_id == int(seller_id))
            .order_by(AdvisorThread.updated_at.desc())
        )
    )


def create_thread(db: Session, seller_id: int, title: str = "") -> AdvisorThread:
    thread = AdvisorThread(
        id=issue_thread_id(seller_id),
        seller_id=int(seller_id),
        title=(title.strip()[:TITLE_MAX] or DEFAULT_TITLE),
        title_is_custom=bool(title.strip()),
    )
    db.add(thread)
    db.commit()
    return thread


def resolve_thread(db: Session, seller_id: int, thread_id: str | None) -> AdvisorThread:
    """The thread this turn belongs to. Anything unusable is replaced, not refused."""
    if thread_id:
        found = get_thread(db, seller_id, thread_id)
        if found is not None:
            return found
        logger.warning("[sessions] thread %r not usable for seller %s", thread_id, seller_id)
    return create_thread(db, seller_id)


def note_turn(db: Session, thread: AdvisorThread, first_message: str) -> None:
    """Mark the conversation as just used, and name it if it still has no name.

    Auto-titling only ever fills a placeholder: once the seller renames a
    conversation, later messages must not quietly rewrite their label.
    """
    thread.updated_at = utcnow()
    if not thread.title_is_custom and thread.title == DEFAULT_TITLE:
        candidate = " ".join((first_message or "").split())[:TITLE_MAX].strip()
        if candidate:
            thread.title = candidate
    db.add(thread)
    db.commit()


def rename_thread(db: Session, seller_id: int, thread_id: str, title: str) -> AdvisorThread | None:
    thread = get_thread(db, seller_id, thread_id)
    if thread is None:
        return None
    cleaned = " ".join((title or "").split())[:TITLE_MAX].strip()
    # An empty rename means "go back to automatic", not "make it blank".
    thread.title = cleaned or DEFAULT_TITLE
    thread.title_is_custom = bool(cleaned)
    db.add(thread)
    db.commit()
    return thread


def delete_thread(db: Session, seller_id: int, thread_id: str) -> bool:
    """Remove the conversation and its transcript. Returns False if it was not theirs."""
    thread = get_thread(db, seller_id, thread_id)
    if thread is None:
        return False
    db.execute(delete(AdvisorThread).where(AdvisorThread.id == thread.id))
    db.commit()
    try:
        from .graph import get_checkpointer

        get_checkpointer().delete_thread(thread.id)
    except Exception as exc:  # noqa: BLE001  the row is already gone; a stray
        # checkpoint is invisible to the user and must not fail their delete
        logger.warning("[sessions] could not drop checkpoint %s: %s", thread.id, exc)
    return True


def transcript(thread_id: str) -> list[dict]:
    """The stored turns of one conversation, for re-opening it in the UI.

    Text only: buyer cards, price stats and listing drafts are rendered from a
    live tool result, and replaying a stale copy of them would show a seller
    yesterday's buyers as if they were current.
    """
    try:
        from .graph import get_graph

        state = get_graph().get_state({"configurable": {"thread_id": thread_id}})
        turns = (state.values or {}).get("turns") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("[sessions] could not read transcript %s: %s", thread_id, exc)
        return []
    return [
        {"role": t.get("role", "assistant"), "content": t.get("content", "")}
        for t in turns
        if t.get("content")
    ]
