"""Background polling: item confirmation timeouts, standing-want expiry, and
the deferred-sale watcher.

Spec §1 explicitly forbids adding queue/scheduler dependencies, so these are the
plainest possible asyncio loops — no celery, no APScheduler, no cron.

The sale-plan watcher runs on its own loop rather than piggybacking on the
expiry pass, because the two have nothing to do with each other and very
different periods: expiry polls every minute, the watcher fires once a week per
plan. Sharing a loop would mean either polling the plans sixty times an hour or
letting a listing expire an hour late.

Due-ness is decided **inside** `run_weekly_pass`, not by how often this loop
ticks. That is what makes a missed tick harmless: the loop only asks "is
anything due", and a process that was down all Monday picks the week up on
Tuesday instead of losing it.
"""

from __future__ import annotations

import asyncio
import logging

from ..config import settings
from ..db import SessionLocal
from . import email_outbox, sale_plans
from .item_index import ItemIndexService
from .lifecycle import LifecycleService
from .wants import expire_due_wants

logger = logging.getLogger(__name__)


def run_expiry_pass(index: ItemIndexService) -> int:
    db = SessionLocal()
    try:
        n = LifecycleService(db, index).expire_due_items()
        # Standing wants are expired in the same pass — no second loop
        expire_due_wants(db)
        return n
    except Exception as exc:  # noqa: BLE001  one bad pass must not kill the poller
        db.rollback()
        logger.exception("[scheduler] expiry pass failed: %s", exc)
        return 0
    finally:
        db.close()


def run_sale_plan_pass() -> dict:
    """One sweep of the deferred-sale plans, plus a flush of whatever it queued.

    Sending happens in the same pass as writing so a queued message does not sit
    for an hour waiting for the next tick; when no SMTP is configured the flush
    is a no-op and the rows stay pending, which is the normal state in dev.
    """
    db = SessionLocal()
    try:
        result = sale_plans.run_weekly_pass(db)
        result["email"] = email_outbox.flush(db)
        return result
    except Exception as exc:  # noqa: BLE001  one bad pass must not kill the poller
        db.rollback()
        logger.exception("[scheduler] sale-plan pass failed: %s", exc)
        return {"checked": 0, "notified": 0, "closed": 0}
    finally:
        db.close()


async def sale_plan_loop() -> None:
    interval = max(60, settings.sale_plan_poll_interval_seconds)
    logger.info(
        "[scheduler] sale-plan watcher started, checking every %ss "
        "(weekly per plan, %s weeks before the target date)",
        interval, settings.sale_plan_watch_weeks,
    )
    while True:
        try:
            outcome = await asyncio.to_thread(run_sale_plan_pass)
            if outcome.get("notified"):
                logger.info(
                    "[scheduler] sale-plan watcher sent %d notifications", outcome["notified"]
                )
        except asyncio.CancelledError:
            logger.info("[scheduler] sale-plan watcher stopped")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[scheduler] sale-plan loop error: %s", exc)
        await asyncio.sleep(interval)


async def expiry_loop(index: ItemIndexService) -> None:
    interval = max(5, settings.expiry_poll_interval_seconds)
    logger.info("[scheduler] expiry polling started, interval %ss, confirmation window %sh",
                interval, settings.confirm_window_hours)
    while True:
        try:
            expired = await asyncio.to_thread(run_expiry_pass, index)
            if expired:
                logger.info("[scheduler] handled %d timed-out items this round", expired)
        except asyncio.CancelledError:
            logger.info("[scheduler] expiry polling stopped")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[scheduler] polling error: %s", exc)
        await asyncio.sleep(interval)
