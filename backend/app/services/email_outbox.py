"""出站邮件：先写下来，再送出去。

这是这个系统里**唯一一种会离开本机、且无法撤回**的输出。所以它被拆成两步，而
不是在业务代码里直接调一次 SMTP：

1. `queue()` 把整封邮件（收件人、标题、正文）写成 `email_outbox` 的一行；
2. `flush()` 才尝试真的发。没有配置 SMTP 时这一步什么都不做，行就停在 pending。

这么拆有三个好处，每一个都在别处吃过亏：

* **验收能断言内容。** 「有没有发邮件」这种断言只能证明函数被调用过；「发出去的
  那封信里有没有写清楚是哪件商品、哪一周的数字」才是要验的东西，而那需要正文
  是一行可读的数据。
* **开发期不会误伤真人。** 数据是模拟的，邮箱字段里是 `tester1@campus.edu` 这种
  值，但也可能不是。默认不发，是让「配错一个地址」这件事的代价停在数据库里。
* **上线只改配置。** 填一组 SMTP 设置，同一批 pending 行就会被同一个 `flush()`
  发出去，业务代码一行不动。

失败不重试到天荒地老：`MAX_ATTEMPTS` 次之后置为 failed 并留下原因。一个一直在
重试的队列会把真正的故障藏在噪声里。
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import EmailOutbox, EmailStatus, utcnow

logger = logging.getLogger(__name__)

#: 一封信最多尝试几次。超过就认输——邮箱写错了不会因为多试三次而变对。
MAX_ATTEMPTS = 3


def smtp_configured() -> bool:
    """有没有一个真的能连的邮件服务器。

    `email_enabled` 单独存在而不是「有 host 就发」：一台配好的 SMTP 服务器和
    「现在可以开始给人发信了」是两个决定，后者应该有人明确点过头。
    """
    return bool(settings.email_enabled and settings.smtp_host)


def queue(
    db: Session,
    *,
    to_email: str,
    subject: str,
    body: str,
    kind: str = "",
    user_id: int | None = None,
) -> EmailOutbox | None:
    """写下一封待发邮件。没有收件地址就不写——一行发不出去的信只是垃圾。"""
    address = (to_email or "").strip()
    if not address or "@" not in address:
        logger.warning("[email] dropping %s: no usable address", kind or "message")
        return None
    row = EmailOutbox(
        user_id=user_id,
        to_email=address[:255],
        subject=(subject or "")[:200],
        body=body or "",
        kind=(kind or "")[:40],
        status=EmailStatus.PENDING,
    )
    db.add(row)
    db.flush()
    logger.info("[email] queued %s -> %s (#%s)", kind or "message", address, row.id)
    return row


def pending(db: Session, limit: int = 50) -> list[EmailOutbox]:
    return list(
        db.scalars(
            select(EmailOutbox)
            .where(EmailOutbox.status == EmailStatus.PENDING)
            .order_by(EmailOutbox.id)
            .limit(limit)
        )
    )


def _send_one(row: EmailOutbox) -> None:
    message = EmailMessage()
    message["From"] = settings.smtp_from or settings.smtp_user
    message["To"] = row.to_email
    message["Subject"] = row.subject
    message.set_content(row.body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
        if settings.smtp_use_tls:
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(message)


def flush(db: Session, limit: int | None = None) -> dict[str, int]:
    """把队列里的信发出去。返回 {"sent", "failed", "skipped"}。

    没配 SMTP 时全部计入 skipped 并且**不动状态**——这些行不是失败的，它们只是
    还在等一个邮件服务器。把它们标成 failed 会让上线那天需要先做一次数据修复。
    """
    rows = pending(db, limit or settings.email_outbox_batch)
    if not rows:
        return {"sent": 0, "failed": 0, "skipped": 0}
    if not smtp_configured():
        logger.debug("[email] %d queued, no SMTP configured — leaving them", len(rows))
        return {"sent": 0, "failed": 0, "skipped": len(rows)}

    sent = failed = 0
    for row in rows:
        row.attempts += 1
        try:
            _send_one(row)
        except Exception as exc:  # noqa: BLE001  一封信发不出去不能拖垮整批
            row.error = f"{type(exc).__name__}: {exc}"[:300]
            if row.attempts >= MAX_ATTEMPTS:
                row.status = EmailStatus.FAILED
                logger.warning("[email] giving up on #%s: %s", row.id, row.error)
            failed += 1
            continue
        row.status = EmailStatus.SENT
        row.sent_at = utcnow()
        row.error = ""
        sent += 1
    db.commit()
    return {"sent": sent, "failed": failed, "skipped": 0}
