from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import StatusLog, TokenUsageLog


class StatusLogRepository:
    def __init__(self, db: Session):
        self.db = db

    def append(self, item_id: int, from_status: str | None, to_status: str, reason: str = "") -> StatusLog:
        log = StatusLog(item_id=item_id, from_status=from_status, to_status=to_status, reason=reason)
        self.db.add(log)
        self.db.flush()
        return log

    def list_for_item(self, item_id: int) -> list[StatusLog]:
        return list(
            self.db.scalars(
                select(StatusLog).where(StatusLog.item_id == item_id).order_by(StatusLog.at)
            )
        )


class TokenUsageRepository:
    def __init__(self, db: Session):
        self.db = db

    def append(
        self,
        call_site: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> TokenUsageLog:
        row = TokenUsageLog(
            call_site=call_site,
            model=model,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cache_read_tokens=int(cache_read_tokens or 0),
            cache_write_tokens=int(cache_write_tokens or 0),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def usage_since(self, since: datetime, call_site_prefix: str = "") -> list[TokenUsageLog]:
        """Usage detail since a point in time, optionally narrowed by call_site prefix."""
        stmt = select(TokenUsageLog).where(TokenUsageLog.at >= since)
        if call_site_prefix:
            stmt = stmt.where(TokenUsageLog.call_site.like(call_site_prefix + "%"))
        return list(self.db.scalars(stmt))

    def summary_by_call_site(self) -> list[dict]:
        rows = self.db.execute(
            select(
                TokenUsageLog.call_site,
                TokenUsageLog.model,
                func.count(TokenUsageLog.id),
                func.sum(TokenUsageLog.input_tokens),
                func.sum(TokenUsageLog.output_tokens),
                func.sum(TokenUsageLog.cache_read_tokens),
                func.sum(TokenUsageLog.cache_write_tokens),
            )
            .group_by(TokenUsageLog.call_site, TokenUsageLog.model)
            .order_by(TokenUsageLog.call_site)
        ).all()
        return [
            {
                "call_site": r[0],
                "model": r[1],
                "calls": int(r[2] or 0),
                "input_tokens": int(r[3] or 0),
                "output_tokens": int(r[4] or 0),
                "cache_read_tokens": int(r[5] or 0),
                "cache_write_tokens": int(r[6] or 0),
            }
            for r in rows
        ]
