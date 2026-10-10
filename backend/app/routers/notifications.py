from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..repositories import NotificationRepository
from ..repositories.want_repo import ItemTrafficRepository
from ..schemas import NotificationOut
from ..services.views import build_notification

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NotificationOut]:
    return [build_notification(n) for n in NotificationRepository(db).list_for_user(user.id)]


@router.get("/unread-count")
def unread_count(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    return {"count": NotificationRepository(db).unread_count(user.id)}


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    ok = NotificationRepository(db).mark_read(user.id, notification_id)
    db.commit()
    return {"ok": ok}


@router.post("/{notification_id}/click")
def mark_clicked(
    notification_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """E6: a push notification was opened — mark it read + count one click for the item.

    Kept separate from /read on purpose: "read" means the buyer glanced at the
    message list, "click" means they actually got interested in the item. The
    §6.3 "stuck" heuristic needs to distinguish "pushed but ignored" from
    "pushed and viewed"; merge the two into one counter and the weak negative
    samples (E6) are gone.
    """
    repo = NotificationRepository(db)
    n = repo.get_owned(user.id, notification_id)
    if n is None:
        return {"ok": False}
    if not n.read:
        repo.mark_read(user.id, notification_id)

    item_id = (n.payload or {}).get("item_id")
    if item_id:
        try:
            ItemTrafficRepository(db).bump([int(item_id)], "clicks")
        except Exception as exc:  # noqa: BLE001  instrumentation must not block mark-as-read
            logger.warning("[notifications] click tracking failed: %s", exc)
    db.commit()
    return {"ok": True}


@router.post("/read-all")
def mark_all_read(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    count = NotificationRepository(db).mark_all_read(user.id)
    db.commit()
    return {"ok": True, "count": count}
