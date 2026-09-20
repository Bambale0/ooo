from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import is_due, is_older_than, next_retry_at
from app.media.service import create_provider_ready_asset
from app.providers.base import ProviderGenerationRequest
from app.providers.models import ProviderAttempt, ProviderCredential, ProviderModelCapability
from app.providers.registry import get_provider_adapter

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
    provider: str = PRIMARY_PROVIDER,
) -> bool:
    result = await db.execute(
        select(ProviderCredential).where(
            ProviderCredential.provider == provider,
            ProviderCredential.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none() is not None


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

    adapter = get_provider_adapter(provider)
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
        result = await adapter.submit_generation(request)
        if attempt is None:
            attempt = ProviderAttempt(generation_id=generation.id, provider=provider)
            db.add(attempt)
        attempt.provider_task_id = result.provider_task_id
        attempt.status = result.status
        attempt.public_error_code = None
        attempt.raw_error = None
        attempt.last_error = None
        attempt.next_attempt_at = None
        generation.status = "sent_to_provider"
    except Exception as exc:
        normalized = adapter.normalize_error(exc)
        if attempt is None:
            attempt = ProviderAttempt(generation_id=generation.id, provider=provider)
            db.add(attempt)
        _mark_attempt_error(attempt, generation, normalized.public_code, normalized.raw_error)
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
    if is_older_than(attempt.created_at, settings.worker_provider_processing_timeout_seconds):
        attempt.status = "timeout"
        attempt.public_error_code = "generation_timeout"
        attempt.last_error = "provider_processing_timeout"
        attempt.next_attempt_at = None
        generation.status = "timeout"
        generation.public_error_code = "generation_timeout"
        await db.flush()
        await db.refresh(generation)
        return generation
    if not is_due(attempt.next_attempt_at):
        return generation

    adapter = get_provider_adapter(provider)
    try:
        result = await adapter.poll_generation(attempt.provider_task_id)
    except Exception as exc:
        normalized = adapter.normalize_error(exc)
        _mark_attempt_error(attempt, generation, normalized.public_code, normalized.raw_error)
        await db.flush()
        await db.refresh(generation)
        return generation

    attempt.status = result.status
    attempt.public_error_code = None
    attempt.raw_error = None
    attempt.last_error = None
    attempt.next_attempt_at = None
    if result.status == "completed":
        generation.status = "completed"
        if result.result_url:
            await create_provider_ready_asset(
                db=db,
                generation=generation,
                provider=provider,
                provider_content_url=result.result_url,
            )
    elif result.status == "failed":
        generation.status = "failed"
        generation.public_error_code = "provider_generation_failed"
        attempt.public_error_code = "provider_generation_failed"
        attempt.raw_error = result.raw_error
    elif result.status == "processing":
        generation.status = "processing"
    await db.flush()
    await db.refresh(generation)
    return generation


def _mark_attempt_error(
    attempt: ProviderAttempt,
    generation: Generation,
    public_code: str,
    raw_error: str | None,
) -> None:
    settings = get_settings()
    attempt.public_error_code = public_code
    attempt.raw_error = raw_error
    attempt.last_error = raw_error or public_code
    retryable = public_code == "provider_temporarily_unavailable"
    if retryable and attempt.retry_count < settings.worker_max_retries:
        attempt.retry_count += 1
        attempt.status = "retry_pending"
        attempt.next_attempt_at = next_retry_at(
            attempt.retry_count,
            base_seconds=settings.worker_retry_base_seconds,
            max_seconds=settings.worker_retry_max_seconds,
        )
        if attempt.provider_task_id:
            generation.status = "processing"
        else:
            generation.status = "queued"
        return
    attempt.status = "failed"
    attempt.next_attempt_at = None
    generation.status = "failed"
    generation.public_error_code = public_code
