"""Final client-only release for explicitly enrolled, unconfirmed video submits.

Execution and procurement reconciliation continue independently. Existing jobs
are never enrolled by this module's read/settlement paths.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.infrastructure.retry import is_due, utc_now
from app.providers.models import ProviderAttempt

CLIENT_RELEASE_POLICY = "unconfirmed_video_30m_v1"
CLIENT_RELEASE_DELAY = timedelta(minutes=30)


def enroll_client_release_policy(generation: Generation) -> None:
    """Only admission of a new video request may call this function."""
    if Decimal(generation.partner_price_rub) > 0:
        generation.client_release_policy = CLIENT_RELEASE_POLICY


def arm_client_release_policy(generation: Generation, *, now: datetime | None = None) -> None:
    """Snapshot the deadline before committing a new submit intent.

    Retries/restarts of an unresolved intent must never extend this deadline.
    A confirmed acceptance clears it; a genuinely new submit may then rearm it.
    """
    if (
        generation.client_release_policy == CLIENT_RELEASE_POLICY
        and generation.client_release_due_at is None
        and not is_client_reserve_final(generation)
    ):
        generation.client_release_due_at = (now or utc_now()) + CLIENT_RELEASE_DELAY


def clear_client_release_deadline(generation: Generation) -> None:
    """Call only after a definitive provider acceptance or safe rejection."""
    generation.client_release_due_at = None


def is_client_reserve_final(generation: Generation) -> bool:
    return generation.client_reserve_released_at is not None


async def release_expired_client_reserve(
    db: AsyncSession,
    generation: Generation,
    *,
    now: datetime | None = None,
) -> bool:
    """Credit one canonical retail reserve without releasing provider exposure.

    Serialize with submit/poll/reconciliation on Generation, then Partner. The
    deadline is only a candidate filter; eligibility is rechecked under the lock.
    Caller commits the marker and ledger atomically with the same transaction.
    """
    from app.billing.service import lock_generation_for_update, release_generation_reserve

    generation = await lock_generation_for_update(db, generation)
    current = now or utc_now()
    if (
        generation.client_release_policy != CLIENT_RELEASE_POLICY
        or is_client_reserve_final(generation)
        or generation.client_release_due_at is None
        or not is_due(generation.client_release_due_at, now=current)
        or generation.actual_charge_rub is not None
        or generation.status not in {"sent_to_provider", "reconciliation_required"}
    ):
        return False
    attempts = list(await db.scalars(
        select(ProviderAttempt)
        .where(ProviderAttempt.generation_id == generation.id)
        .execution_options(populate_existing=True)
    ))
    # Old definitively failed/cancelled attempts may exist before fallback. Any
    # remaining known task, newer retry, or other active intent blocks release.
    unresolved = [attempt for attempt in attempts if attempt.status not in {"failed", "cancelled"}]
    if (
        len(unresolved) != 1
        or unresolved[0].provider_task_id
        or unresolved[0].status not in {"submitting", "reconciliation_required"}
    ):
        return False
    released = await release_generation_reserve(
        db, generation, reason="Final client reserve release after 30 minutes of unconfirmed submission",
    )
    if released is None:
        return False
    generation.client_reserve_released_at = current
    generation.client_release_due_at = None
    # actual_charge_rub remains NULL: it is the existing usage settlement guard.
    # Setting it to zero now would discard later verified provider cost/usage.
    await db.flush()
    return True
