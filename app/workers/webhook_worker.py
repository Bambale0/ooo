import asyncio
import logging
import signal
from contextlib import suppress

from sqlalchemy import select

from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.logging import configure_logging
from app.infrastructure.retry import utc_now
from app.webhooks.models import WebhookDelivery
from app.webhooks.service import deliver_webhook_once

logger = logging.getLogger(__name__)


async def process_due_webhooks_once(*, limit: int = 50) -> int:
    async with SessionLocal() as db:
        result = await db.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status == "pending",
                WebhookDelivery.next_attempt_at <= utc_now(),
            )
            .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.created_at)
            .limit(limit)
        )
        deliveries = list(result.scalars().all())

        processed = 0
        for delivery in deliveries:
            await deliver_webhook_once(db, delivery)
            await db.commit()
            processed += 1
        return processed


async def run_webhook_worker_forever() -> None:
    configure_logging()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    logger.info("webhook_worker_started")
    while not stop_event.is_set():
        try:
            processed = await process_due_webhooks_once()
        except Exception:
            logger.exception("webhook_worker_cycle_failed")
            processed = 0
        if processed:
            await asyncio.sleep(0)
        else:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=min(5.0, get_settings().webhook_retry_interval_seconds),
                )
    logger.info("webhook_worker_stopped")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


if __name__ == "__main__":
    asyncio.run(run_webhook_worker_forever())
