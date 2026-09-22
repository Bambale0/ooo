import asyncio
import logging
import signal
from contextlib import suppress

from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.logging import configure_logging
from app.webhooks.service import (
    claim_due_events,
    close_webhook_http_client,
    finalize_prepared_delivery,
    prepare_claimed_delivery,
    send_prepared_delivery,
)

logger = logging.getLogger(__name__)


async def process_webhook_work_once() -> int:
    settings = get_settings()
    async with SessionLocal() as db:
        event_ids = await claim_due_events(db, limit=settings.webhook_worker_batch_size)
        await db.commit()

    if not event_ids:
        return 0

    semaphore = asyncio.Semaphore(settings.webhook_worker_concurrency)

    async def deliver(event_id: str) -> bool:
        async with semaphore:
            async with SessionLocal() as db:
                try:
                    prepared = await prepare_claimed_delivery(db, event_id)
                    if prepared is None:
                        await db.rollback()
                        return False
                    # Persist delivery_id + monotonically increasing attempt before
                    # the HTTP side effect. A crash after send can then retry as
                    # a new delivery rather than reusing the same attempt identity.
                    await db.commit()
                except Exception:
                    await db.rollback()
                    logger.exception("webhook_delivery_prepare_failed", extra={"event_id": event_id})
                    return False

            outcome = await send_prepared_delivery(prepared)

            async with SessionLocal() as db:
                try:
                    delivered = await finalize_prepared_delivery(db, prepared, outcome)
                    await db.commit()
                    return delivered
                except Exception:
                    await db.rollback()
                    logger.exception("webhook_delivery_finalize_failed", extra={"event_id": event_id})
                    return False

    await asyncio.gather(*(deliver(event_id) for event_id in event_ids))
    return len(event_ids)


async def run_webhook_worker_forever() -> None:
    configure_logging()
    settings = get_settings()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    logger.info("webhook_worker_started")
    try:
        while not stop_event.is_set():
            processed = await process_webhook_work_once()
            if processed:
                await asyncio.sleep(0)
            else:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_poll_interval_seconds)
    finally:
        await close_webhook_http_client()
    logger.info("webhook_worker_stopped")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


if __name__ == "__main__":
    asyncio.run(run_webhook_worker_forever())
