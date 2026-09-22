import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.generations.models import Generation
from app.generations.service import (
    PRIMARY_PROVIDER,
    dispatch_generation_to_provider,
    poll_generation_provider,
)
from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.logging import configure_logging
from app.infrastructure.metrics import update_generation_queue_metrics
from app.infrastructure.retry import utc_now
from app.providers.http_client import close_provider_http_clients
from app.providers.models import ProviderAttempt

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
    """Single-session worker path kept for focused tests and admin/debug use."""
    dispatched = await _dispatch_queued_generations(db, limit=limit, provider=provider)
    polled = await _poll_active_generations(db, limit=limit, provider=provider)
    await _refresh_generation_queue_metrics_with_session(db)
    return WorkerCycleResult(dispatched=dispatched, polled=polled)


async def process_generation_work_concurrently_once(
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    limit: int = 20,
    provider: str = PRIMARY_PROVIDER,
    submit_concurrency: int = 10,
    poll_concurrency: int = 10,
) -> WorkerCycleResult:
    queued_ids, active_ids = await _load_candidate_ids(
        session_factory=session_factory,
        limit=limit,
        provider=provider,
    )

    dispatched = await _run_bounded(
        queued_ids,
        concurrency=submit_concurrency,
        handler=lambda generation_id: _dispatch_generation_candidate(
            session_factory=session_factory,
            generation_id=generation_id,
            provider=provider,
        ),
    )
    polled = await _run_bounded(
        active_ids,
        concurrency=poll_concurrency,
        handler=lambda generation_id: _poll_generation_candidate(
            session_factory=session_factory,
            generation_id=generation_id,
            provider=provider,
        ),
    )
    await _refresh_generation_queue_metrics(session_factory)
    return WorkerCycleResult(dispatched=dispatched, polled=polled)


async def run_generation_worker_forever() -> None:
    configure_logging()
    settings = get_settings()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    logger.info("generation_worker_started")
    try:
        while not stop_event.is_set():
            result = await process_generation_work_concurrently_once(
                limit=settings.worker_batch_size,
                submit_concurrency=settings.worker_submit_concurrency,
                poll_concurrency=settings.worker_poll_concurrency,
            )
            if result.did_work:
                logger.info(
                    "generation_worker_cycle_completed",
                    extra={
                        "dispatched": result.dispatched,
                        "polled": result.polled,
                    },
                )
                await asyncio.sleep(0)
            else:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_poll_interval_seconds)
    finally:
        await close_provider_http_clients()
    logger.info("generation_worker_stopped")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


async def _load_candidate_ids(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    limit: int,
    provider: str = PRIMARY_PROVIDER,
) -> tuple[list[str], list[str]]:
    async with session_factory() as db:
        queued_result = await db.execute(
            _fair_candidate_ids_query(("queued",), limit=limit)
        )
        active_result = await db.execute(
            _fair_due_active_candidate_ids_query(provider=provider, limit=limit)
        )
        return list(queued_result.scalars().all()), list(active_result.scalars().all())


def _fair_candidate_ids_query(statuses: tuple[str, ...], *, limit: int):
    ranked = (
        select(
            Generation.id.label("generation_id"),
            Generation.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=Generation.partner_id,
                order_by=(Generation.created_at, Generation.id),
            )
            .label("partner_position"),
        )
        .where(Generation.status.in_(statuses))
        .subquery()
    )
    return (
        select(ranked.c.generation_id)
        .order_by(
            ranked.c.partner_position,
            ranked.c.created_at,
            ranked.c.generation_id,
        )
        .limit(limit)
    )


def _fair_due_active_candidate_ids_query(*, provider: str, limit: int):
    due_at = func.coalesce(ProviderAttempt.next_attempt_at, ProviderAttempt.next_poll_at)
    now = utc_now()
    ranked = (
        select(
            Generation.id.label("generation_id"),
            Generation.created_at.label("created_at"),
            func.row_number()
            .over(
                partition_by=Generation.partner_id,
                order_by=(Generation.created_at, Generation.id),
            )
            .label("partner_position"),
        )
        .join(
            ProviderAttempt,
            (ProviderAttempt.generation_id == Generation.id)
            & (ProviderAttempt.provider == provider),
        )
        .where(
            Generation.status.in_(("sent_to_provider", "processing", "timeout")),
            ProviderAttempt.status.in_(("accepted", "processing", "retry_pending", "timeout")),
            (due_at.is_(None)) | (due_at <= now),
        )
        .subquery()
    )
    return (
        select(ranked.c.generation_id)
        .order_by(
            ranked.c.partner_position,
            ranked.c.created_at,
            ranked.c.generation_id,
        )
        .limit(limit)
    )


async def _dispatch_generation_candidate(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    generation_id: str,
    provider: str,
) -> bool:
    async with session_factory() as db:
        try:
            result = await db.execute(
                select(Generation)
                .where(Generation.id == generation_id)
                .with_for_update()
            )
            generation = result.scalar_one_or_none()
            if generation is None or generation.status != "queued":
                await db.rollback()
                return False

            await dispatch_generation_to_provider(db, generation, provider)
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            logger.exception(
                "generation_dispatch_failed",
                extra={"generation_id": generation_id, "provider": provider},
            )
            return False


async def _poll_generation_candidate(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    generation_id: str,
    provider: str,
) -> bool:
    async with session_factory() as db:
        try:
            result = await db.execute(
                select(Generation)
                .where(Generation.id == generation_id)
                .with_for_update()
            )
            generation = result.scalar_one_or_none()
            if generation is None or generation.status not in {"sent_to_provider", "processing", "timeout"}:
                await db.rollback()
                return False

            await poll_generation_provider(db, generation, provider)
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            logger.exception(
                "generation_poll_failed",
                extra={"generation_id": generation_id, "provider": provider},
            )
            return False


async def _run_bounded[T](
    items: Sequence[T],
    *,
    concurrency: int,
    handler: Callable[[T], Awaitable[bool]],
) -> int:
    if not items:
        return 0

    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_one(item: T) -> bool:
        async with semaphore:
            return await handler(item)

    results = await asyncio.gather(*(run_one(item) for item in items))
    return sum(results)


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
        .join(
            ProviderAttempt,
            (ProviderAttempt.generation_id == Generation.id)
            & (ProviderAttempt.provider == provider),
        )
        .where(
            Generation.status.in_(("sent_to_provider", "processing", "timeout")),
            ProviderAttempt.status.in_(("accepted", "processing", "retry_pending", "timeout")),
        )
        .order_by(Generation.created_at)
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    count = 0
    for generation in result.scalars().all():
        await poll_generation_provider(db, generation, provider)
        count += 1
    return count


async def _refresh_generation_queue_metrics(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        await _refresh_generation_queue_metrics_with_session(db)


async def _refresh_generation_queue_metrics_with_session(db: AsyncSession) -> None:
    statuses = ("queued", "sent_to_provider", "processing", "timeout")
    result = await db.execute(
        select(
            Generation.status,
            func.count(Generation.id),
            func.min(Generation.created_at),
        )
        .where(Generation.status.in_(statuses))
        .group_by(Generation.status)
    )
    rows = {status: (int(depth), oldest) for status, depth, oldest in result.all()}
    update_generation_queue_metrics(rows, now=utc_now())


if __name__ == "__main__":
    asyncio.run(run_generation_worker_forever())
