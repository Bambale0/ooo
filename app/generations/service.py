from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import release_generation_reserve, settle_generation_reserve
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.metrics import monotonic_seconds, observe_provider_request
from app.infrastructure.retry import is_due, is_older_than, next_poll_at, next_retry_at
from app.media.service import create_provider_ready_asset
from app.providers.base import ProviderAdapterError, ProviderGenerationRequest
from app.providers.models import ProviderAttempt, ProviderModelCapability
from app.providers.rate_limit import get_provider_rate_limiter
from app.providers.service import get_active_provider_credential, get_partner_provider_adapter
from app.webhooks.service import ensure_terminal_webhook_event

PRIMARY_PROVIDER = "argolink"


async def has_provider_capability(
    db: AsyncSession,
    model_id: str,
    mode: str,
    resolution: str,
    provider: str = PRIMARY_PROVIDER,
) -> bool:
    result = await db.execute(
        select(ProviderModelCapability).where(
            ProviderModelCapability.provider == provider,
            ProviderModelCapability.model_id == model_id,
            ProviderModelCapability.mode == mode,
            ProviderModelCapability.resolution == resolution,
            ProviderModelCapability.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none() is not None


async def has_active_provider_credential(
    db: AsyncSession,
    partner_id: str,
    provider: str = PRIMARY_PROVIDER,
) -> bool:
    return await get_active_provider_credential(db, partner_id, provider) is not None


async def dispatch_generation_to_provider(
    db: AsyncSession,
    generation: Generation,
    provider: str = PRIMARY_PROVIDER,
) -> ProviderAttempt:
    existing_result = await db.execute(
        select(ProviderAttempt).where(
            ProviderAttempt.generation_id == generation.id,
            ProviderAttempt.provider == provider,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        if existing.provider_task_id or existing.status not in {"retry_pending", "failed"}:
            return existing
        if not is_due(existing.next_attempt_at):
            return existing
        attempt = existing
    else:
        attempt = None

    adapter = await get_partner_provider_adapter(db, generation.partner_id, provider)
    request = ProviderGenerationRequest(
        generation_id=generation.id,
        model_slug=generation.model_slug,
        mode=generation.mode,
        resolution=generation.resolution,
        prompt=generation.prompt,
        duration_seconds=generation.duration_seconds,
        aspect_ratio=generation.aspect_ratio,
        reference_images=tuple(
            item["url"]
            for item in (generation.request_payload or {}).get("reference_images", [])
            if isinstance(item, dict) and isinstance(item.get("url"), str)
        ),
    )
    try:
        await get_provider_rate_limiter(provider, "submit").acquire()
        provider_started_at = monotonic_seconds()
        try:
            result = await adapter.submit_generation(request)
        except Exception:
            observe_provider_request(
                provider=provider,
                operation="submit",
                outcome="error",
                duration_seconds=monotonic_seconds() - provider_started_at,
            )
            raise
        observe_provider_request(
            provider=provider,
            operation="submit",
            outcome="success",
            duration_seconds=monotonic_seconds() - provider_started_at,
        )
        if attempt is None:
            attempt = ProviderAttempt(generation_id=generation.id, provider=provider)
            db.add(attempt)
        attempt.provider_task_id = result.provider_task_id
        attempt.status = result.status
        attempt.public_error_code = None
        attempt.raw_error = None
        attempt.last_error = None
        attempt.next_attempt_at = None
        attempt.poll_count = 0
        attempt.next_poll_at = next_poll_at(
            0,
            initial_seconds=get_settings().worker_initial_poll_delay_seconds,
            base_seconds=get_settings().worker_poll_backoff_base_seconds,
            max_seconds=get_settings().worker_poll_backoff_max_seconds,
        )
        generation.status = "sent_to_provider"
    except Exception as exc:
        normalized = adapter.normalize_error(exc)
        if attempt is None:
            attempt = ProviderAttempt(generation_id=generation.id, provider=provider)
            db.add(attempt)
        _mark_attempt_error(attempt, generation, normalized)
        if generation.status == "failed":
            await release_generation_reserve(
                db,
                generation,
                reason="Released partner reserve after provider submit failure",
            )
            await ensure_terminal_webhook_event(db, generation)
    await db.flush()
    await db.refresh(attempt)
    return attempt


async def poll_generation_provider(
    db: AsyncSession,
    generation: Generation,
    provider: str = PRIMARY_PROVIDER,
) -> Generation:
    attempt_result = await db.execute(
        select(ProviderAttempt).where(
            ProviderAttempt.generation_id == generation.id,
            ProviderAttempt.provider == provider,
        )
    )
    attempt = attempt_result.scalar_one_or_none()
    if attempt is None or not attempt.provider_task_id:
        return generation

    settings = get_settings()
    reconciling_late_success = generation.status == "timeout" or attempt.status == "timeout"
    if not reconciling_late_success and is_older_than(
        attempt.created_at,
        settings.worker_provider_processing_timeout_seconds,
    ):
        attempt.status = "timeout"
        attempt.public_error_code = "generation_timeout"
        attempt.last_error = "provider_processing_timeout"
        attempt.next_attempt_at = None
        attempt.poll_count += 1
        attempt.next_poll_at = next_poll_at(
            attempt.poll_count,
            initial_seconds=settings.worker_poll_backoff_max_seconds,
            base_seconds=settings.worker_poll_backoff_max_seconds,
            max_seconds=settings.worker_poll_backoff_max_seconds,
        )
        generation.status = "timeout"
        generation.public_error_code = "generation_timeout"
        await release_generation_reserve(
            db,
            generation,
            reason="Released partner reserve after provider processing timeout",
        )
        await ensure_terminal_webhook_event(db, generation)
        await db.flush()
        await db.refresh(generation)
        return generation

    if attempt.next_attempt_at is not None:
        if not is_due(attempt.next_attempt_at):
            return generation
    elif not is_due(attempt.next_poll_at):
        return generation

    adapter = await get_partner_provider_adapter(db, generation.partner_id, provider)
    try:
        await get_provider_rate_limiter(provider, "poll").acquire()
        provider_started_at = monotonic_seconds()
        try:
            result = await adapter.poll_generation(attempt.provider_task_id)
        except Exception:
            observe_provider_request(
                provider=provider,
                operation="poll",
                outcome="error",
                duration_seconds=monotonic_seconds() - provider_started_at,
            )
            raise
        observe_provider_request(
            provider=provider,
            operation="poll",
            outcome=result.status if result.status in {"completed", "failed", "processing"} else "success",
            duration_seconds=monotonic_seconds() - provider_started_at,
        )
    except Exception as exc:
        normalized = adapter.normalize_error(exc)
        if reconciling_late_success:
            attempt.public_error_code = normalized.public_code
            attempt.raw_error = normalized.raw_error
            attempt.last_error = normalized.raw_error or normalized.public_code
            attempt.status = "timeout"
            attempt.retry_count += 1
            attempt.next_attempt_at = next_retry_at(
                attempt.retry_count,
                base_seconds=settings.worker_retry_base_seconds,
                max_seconds=settings.worker_retry_max_seconds,
                retry_after_seconds=normalized.retry_after_seconds,
            )
            generation.status = "timeout"
            generation.public_error_code = "generation_timeout"
        else:
            _mark_attempt_error(attempt, generation, normalized)
            if generation.status == "failed":
                await release_generation_reserve(
                    db,
                    generation,
                    reason="Released partner reserve after terminal provider polling failure",
                )
                await ensure_terminal_webhook_event(db, generation)
        await db.flush()
        await db.refresh(generation)
        return generation

    attempt.status = result.status
    attempt.public_error_code = None
    attempt.raw_error = None
    attempt.last_error = None
    attempt.next_attempt_at = None
    attempt.retry_count = 0

    if result.status == "completed":
        generation.status = "completed"
        generation.public_error_code = None
        attempt.next_poll_at = None
        await settle_generation_reserve(db, generation)
        if result.result_url:
            await create_provider_ready_asset(
                db=db,
                generation=generation,
                provider=provider,
                provider_content_url=result.result_url,
            )
        await ensure_terminal_webhook_event(db, generation)
    elif result.status == "failed":
        attempt.public_error_code = "provider_generation_failed"
        attempt.raw_error = result.raw_error
        attempt.next_poll_at = None
        if reconciling_late_success:
            generation.status = "timeout"
            generation.public_error_code = "generation_timeout"
        else:
            generation.status = "failed"
            generation.public_error_code = "provider_generation_failed"
            await release_generation_reserve(
                db,
                generation,
                reason="Released partner reserve after provider generation failure",
            )
            await ensure_terminal_webhook_event(db, generation)
    elif result.status == "processing":
        if reconciling_late_success:
            generation.status = "timeout"
            generation.public_error_code = "generation_timeout"
            attempt.status = "timeout"
        else:
            generation.status = "processing"
        attempt.poll_count += 1
        attempt.next_poll_at = next_poll_at(
            attempt.poll_count,
            initial_seconds=settings.worker_initial_poll_delay_seconds,
            base_seconds=settings.worker_poll_backoff_base_seconds,
            max_seconds=settings.worker_poll_backoff_max_seconds,
        )

    await db.flush()
    await db.refresh(generation)
    return generation


def _mark_attempt_error(
    attempt: ProviderAttempt,
    generation: Generation,
    error: ProviderAdapterError,
) -> None:
    settings = get_settings()
    attempt.public_error_code = error.public_code
    attempt.raw_error = error.raw_error
    attempt.last_error = error.raw_error or error.public_code
    if error.retryable and attempt.retry_count < settings.worker_max_retries:
        attempt.retry_count += 1
        attempt.status = "retry_pending"
        attempt.next_attempt_at = next_retry_at(
            attempt.retry_count,
            base_seconds=settings.worker_retry_base_seconds,
            max_seconds=settings.worker_retry_max_seconds,
            retry_after_seconds=error.retry_after_seconds,
        )
        if attempt.provider_task_id:
            generation.status = "processing"
        else:
            generation.status = "queued"
        return

    attempt.status = "failed"
    attempt.next_attempt_at = None
    attempt.next_poll_at = None
    generation.status = "failed"
    generation.public_error_code = error.public_code
