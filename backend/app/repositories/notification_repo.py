from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Notification


class NotificationRepository:
    def __init__(self, db: Session):
        self.db = db

    def create(self, user_id: int, type_: str, payload: dict[str, Any]) -> Notification:
        n = Notification(user_id=user_id, type=type_, payload=payload, read=False)
        self.db.add(n)
        self.db.flush()
        return n

    def list_for_user(self, user_id: int, limit: int = 100) -> list[Notification]:
        return list(
            self.db.scalars(
                select(Notification)
                .where(Notification.user_id == user_id)
                .order_by(Notification.created_at.desc(), Notification.id.desc())
                .limit(limit)
            )
        )

    def unread_count(self, user_id: int) -> int:
        return int(
            self.db.scalar(
                select(func.count(Notification.id)).where(
                    Notification.user_id == user_id, Notification.read.is_(False)
                )
            )
            or 0
        )

    def get_owned(self, user_id: int, notification_id: int) -> Notification | None:
        n = self.db.get(Notification, notification_id)
        return n if n and n.user_id == user_id else None

    def mark_read(self, user_id: int, notification_id: int) -> bool:
        n = self.db.get(Notification, notification_id)
        if not n or n.user_id != user_id:
            return False
        n.read = True
        self.db.add(n)
        self.db.flush()
        return True

    def mark_all_read(self, user_id: int) -> int:
        rows = list(
            self.db.scalars(
                select(Notification).where(
                    Notification.user_id == user_id, Notification.read.is_(False)
                )
            )
        )
        for n in rows:
            n.read = True
            self.db.add(n)
        self.db.flush()
        return len(rows)
