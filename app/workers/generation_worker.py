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
    FALLBACK_PROVIDERS,
    PRIMARY_PROVIDER,
    dispatch_generation_to_provider,
    poll_generation_provider,
)
from app.generations.video_recovery import active_video_poll_clause
from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.logging import configure_logging
from app.infrastructure.metrics import update_generation_queue_metrics
from app.infrastructure.production_check import require_production_config
from app.infrastructure.retry import utc_now
from app.payments.crypto_pay import close_crypto_pay_client
from app.payments.reconciliation import payment_reconciliation_loop
from app.providers.http_client import close_provider_http_clients
from app.providers.models import ProviderAttempt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerCycleResult:
    dispatched: int = 0
    polled: int = 0
    released: int = 0

    @property
    def did_work(self) -> bool:
        return any((self.dispatched, self.polled, self.released))


async def process_generation_work_once(
    db: AsyncSession,
    *,
    limit: int = 10,
    provider: str | None = None,
) -> WorkerCycleResult:
    """Single-session worker path kept for focused tests and admin/debug use."""
    released = await _release_due_client_reserves(db, limit=limit)
    dispatched = await _dispatch_queued_generations(db, limit=limit, provider=provider)
    polled = await _poll_active_generations(db, limit=limit, provider=provider)
    await _refresh_generation_queue_metrics_with_session(db)
    return WorkerCycleResult(dispatched=dispatched, polled=polled, released=released)


async def process_generation_work_concurrently_once(
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    limit: int = 20,
    provider: str | None = None,
    submit_concurrency: int = 10,
    poll_concurrency: int = 10,
) -> WorkerCycleResult:
    # A DB-backed clock survives restarts and has no dependency on polling an
    # unknown upstream identity. Each financial decision is rechecked under lock.
    async with session_factory() as db:
        release_ids = list(await db.scalars(_due_client_release_ids_query(limit=limit)))
    released = await _run_bounded(
        release_ids, concurrency=4,
        handler=lambda generation_id: _release_client_reserve_candidate(
            session_factory=session_factory, generation_id=generation_id,
        ),
    )
    queued_ids, active_ids = await _load_candidate_ids(
        session_factory=session_factory,
        limit=limit,
        provider=provider,
    )
    from app.providers.circuit import recovery_tick

    async with session_factory() as db:
        for recovery_provider in [provider] if provider else [PRIMARY_PROVIDER, *FALLBACK_PROVIDERS]:
            await recovery_tick(db, recovery_provider)
        await db.commit()

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
    return WorkerCycleResult(dispatched=dispatched, polled=polled, released=released)


async def run_generation_worker_forever() -> None:
    require_production_config()
    configure_logging()
    settings = get_settings()
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    logger.info("generation_worker_started")
    payment_task = asyncio.create_task(payment_reconciliation_loop(stop_event))
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
                        "released": result.released,
                    },
                )
                await asyncio.sleep(0)
            else:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=settings.worker_poll_interval_seconds)
    finally:
        payment_task.cancel()
        with suppress(asyncio.CancelledError):
            await payment_task
        await close_crypto_pay_client()
        await close_provider_http_clients()
    logger.info("generation_worker_stopped")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, stop_event.set)


def _due_client_release_ids_query(*, limit: int):
    from app.billing.client_release import CLIENT_RELEASE_POLICY
    from app.billing.models import LedgerEntry

    return (
        select(Generation.id)
        .where(
            Generation.client_release_policy == CLIENT_RELEASE_POLICY,
            Generation.client_release_due_at <= utc_now(),
            Generation.client_reserve_released_at.is_(None),
            Generation.actual_charge_rub.is_(None),
            Generation.partner_price_rub > 0,
            Generation.status.in_(("sent_to_provider", "reconciliation_required")),
            select(ProviderAttempt.id).where(
                ProviderAttempt.generation_id == Generation.id,
                ProviderAttempt.status.in_(("submitting", "reconciliation_required")),
                (ProviderAttempt.provider_task_id.is_(None)) | (ProviderAttempt.provider_task_id == ""),
            ).exists(),
            select(func.count(ProviderAttempt.id)).where(
                ProviderAttempt.generation_id == Generation.id,
                ProviderAttempt.status.not_in(("failed", "cancelled")),
            ).scalar_subquery() == 1,
            select(LedgerEntry.id).where(
                LedgerEntry.generation_id == Generation.id,
                LedgerEntry.operation_type == "generation_reserve",
                LedgerEntry.amount_rub < 0,
            ).exists(),
        )
        .order_by(Generation.client_release_due_at, Generation.id)
        .limit(limit)
    )


async def _release_client_reserve_candidate(*, session_factory, generation_id: str) -> bool:
    from app.billing.client_release import release_expired_client_reserve

    # Never accumulate Partner locks across different generations/partners.
    async with session_factory() as db:
        try:
            generation = await db.get(Generation, generation_id)
            if generation is None:
                return False
            changed = await release_expired_client_reserve(db, generation)
            await db.commit()
            return changed
        except Exception:
            await db.rollback()
            logger.exception("generation_client_release_failed", extra={"generation_id": generation_id})
            return False


async def _release_due_client_reserves(db: AsyncSession, *, limit: int) -> int:
    """Single-session debug lane; each financial decision still commits alone."""
    from app.billing.client_release import release_expired_client_reserve

    ids = list(await db.scalars(_due_client_release_ids_query(limit=limit)))
    released = 0
    for generation_id in ids:
        try:
            generation = await db.get(Generation, generation_id)
            changed = False
            if generation is not None:
                changed = await release_expired_client_reserve(db, generation)
            await db.commit()
            released += int(changed)
        except Exception:
            await db.rollback()
            logger.exception("generation_client_release_failed", extra={"generation_id": generation_id})
    return released


async def _load_candidate_ids(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    limit: int,
    provider: str | None = None,
) -> tuple[list[str], list[str]]:
    async with session_factory() as db:
        queued_result = await db.execute(_fair_candidate_ids_query(("queued",), limit=limit))
        active_result = await db.execute(_fair_due_active_candidate_ids_query(provider=provider, limit=limit))
        return list(queued_result.scalars().all()), list(active_result.scalars().all())


def _fair_candidate_ids_query(statuses: tuple[str, ...], *, limit: int):
    deferred = select(ProviderAttempt.generation_id).where(ProviderAttempt.next_attempt_at > utc_now())
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
        .where(Generation.status.in_(statuses), Generation.id.not_in(deferred))
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


def _fair_due_active_candidate_ids_query(*, provider: str | None, limit: int):
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
        .join(ProviderAttempt, ProviderAttempt.generation_id == Generation.id)
        .where(
            active_video_poll_clause(),
            (due_at.is_(None)) | (due_at <= now),
            *([ProviderAttempt.provider == provider] if provider else []),
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
    provider: str | None,
) -> bool:
    async with session_factory() as db:
        try:
            result = await db.execute(select(Generation).where(Generation.id == generation_id).with_for_update())
            generation = result.scalar_one_or_none()
            if generation is None or generation.status != "queued":
                await db.rollback()
                return False

            selected_provider = (
                provider or (generation.request_payload or {}).get("fallback_provider") or PRIMARY_PROVIDER
            )
            attempt = await dispatch_generation_to_provider(db, generation, selected_provider)
            await db.commit()
            if attempt is not None:
                logger.info(
                    "generation_dispatched",
                    extra={
                        "trace_id": generation.id,
                        "generation_id": generation.id,
                        "partner_id": generation.partner_id,
                        "attempt_id": getattr(attempt, "id", None),
                    },
                )
            return attempt is not None
        except Exception:
            await db.rollback()
            logger.exception(
                "generation_dispatch_failed",
                extra={"generation_id": generation_id, "provider": provider or "routed"},
            )
            return False


async def _poll_generation_candidate(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    generation_id: str,
    provider: str | None,
) -> bool:
    async with session_factory() as db:
        try:
            result = await db.execute(select(Generation).where(Generation.id == generation_id).with_for_update())
            generation = result.scalar_one_or_none()
            if generation is None or generation.status not in {
                "sent_to_provider", "processing", "timeout", "reconciliation_required"
            }:
                await db.rollback()
                return False

            if generation.status == "reconciliation_required":
                # Recheck the narrow automatic lane after acquiring the row lock,
                # even when a provider filter was explicitly passed to the worker.
                selected_provider = await _active_provider_for_generation(db, generation.id)
                if provider is not None and selected_provider != provider:
                    selected_provider = None
            else:
                selected_provider = provider or await _active_provider_for_generation(db, generation.id)
            if selected_provider is None:
                await db.rollback()
                return False
            await poll_generation_provider(db, generation, selected_provider)
            attempt = (
                await db.execute(
                    select(ProviderAttempt).where(
                        ProviderAttempt.generation_id == generation.id,
                        ProviderAttempt.provider == selected_provider,
                    )
                )
            ).scalar_one_or_none()
            await db.commit()
            logger.info(
                "generation_polled",
                extra={
                    "trace_id": generation.id,
                    "generation_id": generation.id,
                    "partner_id": generation.partner_id,
                    "attempt_id": attempt.id if attempt else None,
                    "generation_status": generation.status,
                    "attempt_status": attempt.status if attempt else None,
                },
            )
            return True
        except Exception:
            await db.rollback()
            logger.exception(
                "generation_poll_failed",
                extra={"generation_id": generation_id, "provider": provider or "routed"},
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
    provider: str | None,
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
        selected_provider = provider or (generation.request_payload or {}).get("fallback_provider") or PRIMARY_PROVIDER
        attempt = await dispatch_generation_to_provider(db, generation, selected_provider)
        count += int(attempt is not None)
    return count


async def _poll_active_generations(
    db: AsyncSession,
    *,
    limit: int,
    provider: str | None,
) -> int:
    result = await db.execute(
        select(Generation)
        .join(ProviderAttempt, ProviderAttempt.generation_id == Generation.id)
        .where(
            active_video_poll_clause(),
            *([ProviderAttempt.provider == provider] if provider else []),
        )
        .order_by(Generation.created_at)
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    count = 0
    for generation in result.scalars().unique().all():
        selected_provider = provider or await _active_provider_for_generation(db, generation.id)
        if selected_provider is None:
            continue
        await poll_generation_provider(db, generation, selected_provider)
        count += 1
    return count


async def _active_provider_for_generation(db: AsyncSession, generation_id: str) -> str | None:
    result = await db.execute(
        select(ProviderAttempt.provider)
        .join(Generation, Generation.id == ProviderAttempt.generation_id)
        .where(
            ProviderAttempt.generation_id == generation_id,
            active_video_poll_clause(),
        )
        .order_by(ProviderAttempt.created_at.desc(), ProviderAttempt.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


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
