import asyncio
import logging
import signal
from contextlib import suppress
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.generations.service import (
    PRIMARY_PROVIDER,
    dispatch_generation_to_provider,
    poll_generation_provider,
)
from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.logging import configure_logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerCycleResult:
    dispatched: int = 0
    polled: int = 0

    @property
    def did_work(self) -> bool:
        return any((self.dispatched, self.polled))


async def process_generation_work_once(
    db: AsyncSession,
    *,
    limit: int = 10,
    provider: str = PRIMARY_PROVIDER,
) -> WorkerCycleResult:
    dispatched = await _dispatch_queued_generations(db, limit=limit, provider=provider)
    polled = await _poll_active_generations(db, limit=limit, provider=provider)
    return WorkerCycleResult(dispatched=dispatched, polled=polled)


async def run_generation_worker_forever() -> None:
    configure_logging()
    settings = get_settings()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    logger.info("generation_worker_started")
    while not stop_event.is_set():
        async with SessionLocal() as db:
            try:
                result = await process_generation_work_once(db, limit=settings.worker_batch_size)
                await db.commit()
            except Exception:
                await db.rollback()
                logger.exception("generation_worker_cycle_failed")
                await asyncio.sleep(settings.worker_poll_interval_seconds)
                continue
        if result.did_work:
            logger.info(
                "generation_worker_cycle_completed",
                extra={
                    "dispatched": result.dispatched,
                    "polled": result.polled,
                },
            )
        if result.did_work:
            await asyncio.sleep(0)
        else:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_poll_interval_seconds)
    logger.info("generation_worker_stopped")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


async def _dispatch_queued_generations(
    db: AsyncSession,
    *,
    limit: int,
    provider: str,
) -> int:
    result = await db.execute(
        select(Generation)
        .where(Generation.status == "queued")
        .order_by(Generation.created_at)
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    count = 0
    for generation in result.scalars().all():
        await dispatch_generation_to_provider(db, generation, provider)
        count += 1
    return count


async def _poll_active_generations(
    db: AsyncSession,
    *,
    limit: int,
    provider: str,
) -> int:
    result = await db.execute(
        select(Generation)
        .where(Generation.status.in_(("sent_to_provider", "processing")))
        .order_by(Generation.created_at)
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    count = 0
    for generation in result.scalars().all():
        await poll_generation_provider(db, generation, provider)
        count += 1
    return count


if __name__ == "__main__":
    asyncio.run(run_generation_worker_forever())
