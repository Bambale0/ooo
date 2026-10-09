from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing.service import (
    apply_cost_coverage_change,
    lock_generation_for_update,
    lock_partner_for_update,
    release_generation_cost_reserve,
    release_generation_reserve,
    release_generation_reserves,
    settle_generation_reserves,
)
from app.contracts.registry import normalized_video
from app.generations.models import Generation
from app.generations.video_recovery import active_video_poll_clause, is_usage_review, is_video_generation
from app.infrastructure.config import get_settings
from app.infrastructure.metrics import monotonic_seconds, observe_provider_request
from app.infrastructure.retry import is_due, is_older_than, next_poll_at, next_retry_at, utc_now
from app.media.service import create_provider_ready_asset
from app.providers.asale import asale_supports_request
from app.providers.base import ProviderAdapterError, ProviderGenerationRequest, ProviderPollResult
from app.providers.infai_video import (
    INFAI_CREDENTIAL_LABEL,
    infai_cost_ceiling,
    infai_price_per_million,
    infai_supports_request,
)
from app.providers.models import ProviderAttempt, ProviderCredential, ProviderModelCapability
from app.providers.rate_limit import get_provider_rate_limiter
from app.providers.service import (
    get_active_provider_credential,
    get_partner_provider_adapter,
    has_provider_runtime_credential,
)
from app.webhooks.service import ensure_terminal_webhook_event

PRIMARY_PROVIDER = "argolink"
FALLBACK_PROVIDERS = ("infai", "asale")


def _provider_request_for_generation(generation: Generation) -> ProviderGenerationRequest:
    request_payload = generation.request_payload or {}
    native_body = request_payload.get("native_body")
    normalized: dict = {}
    if isinstance(native_body, dict):
        try:
            normalized = normalized_video(native_body)
        except ValueError:
            native_body = None

    def media_url(value) -> str | None:
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("url"), str):
            return value["url"]
        return None

    references = normalized.get("reference_images", request_payload.get("reference_images", []))
    reference_videos = normalized.get("reference_videos", request_payload.get("reference_videos", []))
    reference_audios = normalized.get("reference_audios", request_payload.get("reference_audios", []))
    return ProviderGenerationRequest(
        generation_id=generation.id,
        native_body=native_body if isinstance(native_body, dict) else None,
        model_slug=generation.model_slug,
        mode=generation.mode,
        resolution=str(normalized.get("resolution", generation.resolution)),
        prompt=str(normalized.get("prompt", generation.prompt)),
        duration_seconds=int(normalized.get("duration", generation.duration_seconds)),
        aspect_ratio=normalized.get("aspect_ratio", generation.aspect_ratio),
        start_image=media_url(normalized.get("start_image")) or media_url(request_payload.get("start_image")),
        end_image=media_url(normalized.get("end_image")) or media_url(request_payload.get("end_image")),
        reference_images=tuple(url for item in references if (url := media_url(item)) is not None),
        reference_videos=tuple(url for item in reference_videos if (url := media_url(item)) is not None),
        reference_audios=tuple(url for item in reference_audios if (url := media_url(item)) is not None),
    )


async def fallback_cost_ceiling_for_request(
    db: AsyncSession,
    *,
    partner_id: str,
    model_id: str,
    request: ProviderGenerationRequest,
) -> tuple[str, Decimal] | None:
    capability_mode = "default" if request.mode == "videos/generations" else request.mode
    for provider in FALLBACK_PROVIDERS:
        if provider == "infai" and not infai_supports_request(request):
            continue
        if provider == "asale" and not asale_supports_request(request):
            continue
        capability = (
            await db.execute(
                select(ProviderModelCapability).where(
                    ProviderModelCapability.provider == provider,
                    ProviderModelCapability.model_id == model_id,
                    ProviderModelCapability.mode == capability_mode,
                    ProviderModelCapability.resolution == request.resolution,
                    ProviderModelCapability.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if capability is None or capability.provider_cost_ceiling_usdt is None or capability.billing_unit is None:
            continue
        if not await has_provider_runtime_credential(db, partner_id, provider):
            continue
        rate = Decimal(capability.provider_cost_ceiling_usdt)
        if not rate.is_finite() or rate < 0:
            continue
        if provider == "infai":
            if capability.billing_unit != "million_video_tokens":
                continue
            total_cost = infai_cost_ceiling(request, infai_price_per_million(request, rate))
        elif capability.billing_unit == "second":
            total_cost = Decimal(capability.provider_cost_ceiling_usdt) * Decimal(request.duration_seconds)
        elif capability.billing_unit == "generation":
            total_cost = Decimal(capability.provider_cost_ceiling_usdt)
        else:
            continue
        if total_cost < 0:
            continue
        return provider, total_cost
    return None


async def select_fallback_provider(
    db: AsyncSession,
    generation: Generation,
    *,
    after_provider: str,
) -> tuple[str, Decimal] | None:
    if after_provider != PRIMARY_PROVIDER:
        return None
    selected = await fallback_cost_ceiling_for_request(
        db,
        partner_id=generation.partner_id,
        model_id=generation.model_id,
        request=_provider_request_for_generation(generation),
    )
    if selected is None:
        return None
    provider, total_cost = selected
    reserve = (
        Decimal(generation.provider_cost_reserve_usdt)
        if generation.provider_cost_reserve_usdt is not None
        else Decimal(generation.provider_cost_usdt_snapshot)
    )
    if total_cost > reserve:
        return None
    if total_cost * Decimal(generation.rub_per_usdt_snapshot) > Decimal(generation.partner_price_rub):
        return None
    return provider, total_cost


async def dispatch_generation_with_routing(
    db: AsyncSession,
    generation: Generation,
) -> ProviderAttempt | None:
    provider = (generation.request_payload or {}).get("fallback_provider")
    if provider not in FALLBACK_PROVIDERS:
        provider = PRIMARY_PROVIDER
    return await dispatch_generation_to_provider(db, generation, provider)


async def active_provider_for_generation(
    db: AsyncSession,
    generation_id: str,
) -> str | None:
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
) -> ProviderAttempt | None:
    from app.billing.client_release import (
        arm_client_release_policy,
        clear_client_release_deadline,
        is_client_reserve_final,
    )

    generation = await lock_generation_for_update(db, generation)
    existing_result = await db.execute(
        select(ProviderAttempt).where(
            ProviderAttempt.generation_id == generation.id,
            ProviderAttempt.provider == provider,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if is_client_reserve_final(generation):
        # Final customer release permits a recovered result, never another paid launch.
        return existing
    if existing is not None:
        if existing.provider_task_id or existing.status != "retry_pending":
            return existing
        if not is_due(existing.next_attempt_at):
            return existing
        attempt = existing
    else:
        attempt = None

    failed_tasks = (generation.request_payload or {}).get("provider_failed_tasks", [])
    retrying_failed_task = any(item.get("provider") == provider for item in failed_tasks)
    if retrying_failed_task and attempt and await _expire_generation_retry(db, generation, attempt):
        return attempt

    from app.billing.service import lock_partner_for_update

    owner = await lock_partner_for_update(db, generation.partner_id)
    if owner.status != "active":
        return await cancel_before_submit(db, generation, provider=provider)
    from app.providers.circuit import admit

    if not await admit(db, generation.id, provider=provider):
        return None
    if provider == "infai":
        # Pin the group-specific credential chosen with the procurement quote.
        # Rotation/revocation must not move a queued request to another group.
        pinned_id = (generation.request_payload or {}).get("fallback_credential_id")
        credential = (
            await db.execute(
                select(ProviderCredential)
                .where(
                    ProviderCredential.id == pinned_id,
                    ProviderCredential.provider == "infai",
                    ProviderCredential.partner_id.is_(None),
                    ProviderCredential.label == INFAI_CREDENTIAL_LABEL,
                    ProviderCredential.is_active.is_(True),
                    ProviderCredential.encrypted_api_key.is_not(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if credential is None:
            return await cancel_before_submit(db, generation, provider=provider)
        adapter = await get_partner_provider_adapter(db, generation.partner_id, provider, credential_id=credential.id)
    elif retrying_failed_task:
        # Retrying on another account could orphan billing/reconciliation. A
        # revoked original credential must not silently switch to a newer key.
        credential = (
            await db.execute(
                select(ProviderCredential)
                .where(
                    ProviderCredential.id == attempt.credential_id,
                    ProviderCredential.partner_id == generation.partner_id,
                    ProviderCredential.provider == provider,
                    ProviderCredential.is_active.is_(True),
                    ProviderCredential.encrypted_api_key.is_not(None),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if credential is None:
            return await cancel_before_submit(db, generation, provider=provider)
        adapter = await get_partner_provider_adapter(db, generation.partner_id, provider, credential_id=credential.id)
    else:
        adapter = await get_partner_provider_adapter(db, generation.partner_id, provider)
        credential = await get_active_provider_credential(db, generation.partner_id, provider)
    request = _provider_request_for_generation(generation)
    try:
        await get_provider_rate_limiter(provider, "submit").acquire()
        if retrying_failed_task and attempt and await _expire_generation_retry(db, generation, attempt):
            return attempt
        # Persist the submit intent BEFORE the external side effect. If the process
        # dies after acceptance, a restart must not blindly create a second paid job.
        # A submitting attempt without an ID requires operator reconciliation.
        if attempt is None:
            attempt = ProviderAttempt(generation_id=generation.id, provider=provider, status="submitting")
            db.add(attempt)
        if attempt.cost_reserve_usdt is None:
            attempt.cost_reserve_usdt = _attempt_cost_reserve(generation, provider)
        if credential is not None:
            attempt.credential_id = credential.id
        attempt.status = "submitting"
        generation.status = "sent_to_provider"
        arm_client_release_policy(generation)
        await db.commit()
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
        # The network call follows a committed intent; the deadline worker may
        # have made a final financial decision meanwhile. Re-read under its lock.
        generation = await lock_generation_for_update(db, generation)
        if attempt is not None:
            await db.refresh(attempt)
        if attempt is None:
            attempt = ProviderAttempt(generation_id=generation.id, provider=provider)
            db.add(attempt)
        attempt.provider_task_id = result.provider_task_id
        clear_client_release_deadline(generation)
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
        generation = await lock_generation_for_update(db, generation)
        if attempt is not None:
            await db.refresh(attempt)
        normalized = adapter.normalize_error(exc)
        if attempt is None:
            attempt = ProviderAttempt(generation_id=generation.id, provider=provider)
            db.add(attempt)
        _mark_attempt_error(attempt, generation, normalized)
        if is_client_reserve_final(generation) and attempt.status == "retry_pending":
            # A delayed submit response cannot reopen paid work after final
            # customer resolution. Retain the unresolved provider obligation.
            generation.status = attempt.status = "reconciliation_required"
            generation.public_error_code = "submission_outcome_unknown"
            attempt.next_attempt_at = attempt.next_poll_at = None
        if attempt.status in {"failed", "retry_pending"}:
            clear_client_release_deadline(generation)
        from app.providers.circuit import observe

        await observe(db, generation, provider=provider)
        if generation.status == "failed":
            await release_generation_reserves(
                db,
                generation,
                reason="Released partner reserve after provider submit failure",
            )
            await ensure_terminal_webhook_event(db, generation, provider=provider)
    await db.flush()
    await db.refresh(attempt)
    return attempt


async def poll_generation_provider(
    db: AsyncSession,
    generation: Generation,
    provider: str = PRIMARY_PROVIDER,
) -> Generation:
    generation = await lock_generation_for_update(db, generation)
    attempt_result = await db.execute(
        select(ProviderAttempt).where(
            ProviderAttempt.generation_id == generation.id,
            ProviderAttempt.provider == provider,
        )
    )
    attempt = attempt_result.scalar_one_or_none()
    if attempt is None or not attempt.provider_task_id:
        return generation

    if generation.status in {"completed", "failed", "cancelled"}:
        return generation

    settings = get_settings()
    from app.billing.client_release import is_client_reserve_final

    usage_review = is_usage_review(generation, attempt)
    reconciling_late_success = generation.status == "timeout" or attempt.status == "timeout"
    if not is_client_reserve_final(generation) and not usage_review and not reconciling_late_success and is_older_than(
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
        if provider == "infai":
            # Accepted work can still complete: release retail, retain unknown
            # procurement reserve for a later definitive poll/reconciliation.
            await release_generation_reserve(db, generation, reason="Released reserve after fallback timeout")
        elif provider == "asale":
            await release_generation_reserve(
                db,
                generation,
                reason="Released partner reserve after paid fallback processing timeout",
            )
            await _settle_fallback_provider_cost(db, generation, provider)
        else:
            await release_generation_reserves(
                db,
                generation,
                reason="Released partner reserve after provider processing timeout",
            )
        await ensure_terminal_webhook_event(db, generation, provider=provider)
        await db.flush()
        await db.refresh(generation)
        return generation

    if attempt.next_attempt_at is not None:
        if not is_due(attempt.next_attempt_at):
            return generation
    elif not is_due(attempt.next_poll_at):
        return generation

    adapter = await get_partner_provider_adapter(
        db,
        generation.partner_id,
        provider,
        **({"credential_id": attempt.credential_id} if attempt.credential_id else {}),
    )
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
        from app.providers.circuit import observe

        await observe(db, generation, outcome="error", provider=provider)
        if provider == PRIMARY_PROVIDER and is_video_generation(generation):
            # A status-read failure never proves that an accepted paid job failed.
            _defer_known_video_poll(attempt, generation, normalized, review=usage_review,
                                   timed_out=reconciling_late_success)
        elif reconciling_late_success:
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
                if provider in FALLBACK_PROVIDERS and attempt.provider_task_id:
                    attempt.status = generation.status = "reconciliation_required"
                    generation.public_error_code = "submission_outcome_unknown"
                    attempt.next_attempt_at = attempt.next_poll_at = None
                else:
                    await release_generation_reserves(
                        db,
                        generation,
                        reason="Released partner reserve after terminal provider polling failure",
                    )
                    await ensure_terminal_webhook_event(db, generation, provider=provider)
        await db.flush()
        await db.refresh(generation)
        return generation

    if usage_review and (
        not getattr(result, "task_identity_verified", False)
        or result.status not in {"completed", "failed"}
    ):
        _defer_known_video_poll(
            attempt, generation,
            ProviderAdapterError("provider_temporarily_unavailable", "video_review_pending"),
            review=True,
        )
        await db.flush()
        return generation

    prior_retry_count = attempt.retry_count
    attempt.status = result.status
    attempt.public_error_code = None
    attempt.raw_error = None
    attempt.last_error = None
    attempt.next_attempt_at = None
    attempt.retry_count = 0
    _record_attempt_result_accounting(generation, attempt, result)

    if result.status == "completed":
        generation.status = "completed"
        generation.public_error_code = None
        attempt.next_poll_at = None
        if provider == "infai":
            if not await _settle_infai_success(db, generation, attempt, result):
                attempt.status = "reconciliation_required"
                await db.flush()
                return generation
        elif (generation.request_payload or {}).get("rates"):
            from app.inference.accounting import settle_actual

            seconds = result.usage.get("billed_seconds") if isinstance(result.usage, dict) else None
            if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
                generation.status = attempt.status = "reconciliation_required"
                generation.public_error_code = "usage_reconciliation_required"
                if usage_review:
                    attempt.retry_count = prior_retry_count
                _defer_known_video_poll(
                    attempt, generation,
                    ProviderAdapterError("provider_temporarily_unavailable", "video_usage_missing"),
                    review=True,
                )
                from app.providers.circuit import observe

                await observe(db, generation, provider=provider)
                await db.flush()
                return generation
            await settle_actual(db, generation, {"seconds": seconds})
        else:
            await settle_generation_reserves(db, generation)
            if provider == "asale":
                await _settle_fallback_provider_cost(db, generation, provider)
            else:
                await _settle_provider_cost_reserve(
                    db,
                    generation,
                    provider=provider,
                    actual_cost=Decimal(generation.provider_cost_usdt_snapshot),
                )
            from app.billing.client_release import is_client_reserve_final

            if generation.status == "completed" and is_client_reserve_final(generation):
                generation.actual_charge_rub = Decimal("0.00")
        if result.result_url:
            await create_provider_ready_asset(
                db=db,
                generation=generation,
                provider=provider,
                provider_content_url=result.result_url,
            )
        await ensure_terminal_webhook_event(db, generation, provider=provider)
    elif result.status == "failed":
        attempt.public_error_code = "provider_generation_failed"
        attempt.raw_error = result.raw_error
        attempt.next_poll_at = None
        if reconciling_late_success:
            # A processing timeout is provisional. If the provider later gives a
            # definitive failed result, expose that terminal truth instead of
            # leaving clients on "expired" forever.
            generation.status = "failed"
            generation.public_error_code = "provider_generation_failed"
            if provider == "infai":
                unresolved_prior = await db.scalar(
                    select(ProviderAttempt.id)
                    .where(
                        ProviderAttempt.generation_id == generation.id,
                        ProviderAttempt.id != attempt.id,
                        ProviderAttempt.cost_status == "unknown",
                    )
                    .limit(1)
                )
                if unresolved_prior is None:
                    await release_generation_cost_reserve(
                        db,
                        generation,
                        reason="Released fallback procurement reserve after definitive late failure",
                    )
            await ensure_terminal_webhook_event(db, generation, provider=provider)
        elif provider == PRIMARY_PROVIDER and await _queue_fallback_after_safe_failure(
            db,
            generation,
            attempt,
            result,
            preferred_only="infai",
        ):
            pass
        elif result.retryable_failure and await _queue_fallback_after_safe_failure(
            db,
            generation,
            attempt,
            result,
        ):
            pass
        else:
            generation.status = "failed"
            generation.public_error_code = "provider_generation_failed"
            if attempt.provider_cost_usdt is not None:
                await release_generation_reserve(
                    db,
                    generation,
                    reason="Released partner reserve after charged provider generation failure",
                )
                await _settle_provider_cost_reserve(
                    db,
                    generation,
                    provider=provider,
                    actual_cost=Decimal(attempt.provider_cost_usdt),
                )
            elif attempt.cost_status == "unknown":
                # The accepted task may still have been billed. Return the retail
                # reserve, but retain procurement coverage until reconciliation.
                await release_generation_reserve(
                    db,
                    generation,
                    reason="Released partner reserve with unresolved provider cost",
                )
            elif provider == "asale":
                await release_generation_reserve(
                    db,
                    generation,
                    reason="Released partner reserve after paid fallback generation failure",
                )
                await _settle_fallback_provider_cost(db, generation, provider)
            else:
                await release_generation_reserves(
                    db,
                    generation,
                    reason="Released partner reserve after provider generation failure",
                )
            await ensure_terminal_webhook_event(db, generation, provider=provider)
    elif result.status == "processing":
        if reconciling_late_success:
            generation.status = "timeout"
            generation.public_error_code = "generation_timeout"
            attempt.status = "timeout"
        else:
            generation.status = "processing"
            if getattr(result, "task_identity_verified", False):
                generation.public_error_code = None
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


def _defer_known_video_poll(
    attempt: ProviderAttempt, generation: Generation, error: ProviderAdapterError,
    *, review: bool = False, timed_out: bool = False,
) -> None:
    settings = get_settings()
    attempt.public_error_code = error.public_code
    attempt.raw_error = attempt.last_error = error.raw_error or error.public_code
    attempt.retry_count += 1
    attempt.next_attempt_at = next_retry_at(
        min(attempt.retry_count, 32),
        base_seconds=settings.worker_retry_base_seconds,
        max_seconds=settings.worker_retry_max_seconds,
        retry_after_seconds=error.retry_after_seconds,
    )
    attempt.next_poll_at = None
    if review:
        generation.status = attempt.status = "reconciliation_required"
    elif timed_out:
        generation.status = attempt.status = "timeout"
    else:
        generation.status = "processing"
        attempt.status = "retry_pending"


def _attempt_cost_reserve(generation: Generation, provider: str) -> Decimal | None:
    if provider == PRIMARY_PROVIDER:
        return Decimal(generation.provider_cost_usdt_snapshot)
    raw = (generation.request_payload or {}).get("fallback_provider_cost_ceiling_usdt")
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except ArithmeticError:
        return None
    return value if value.is_finite() and value >= 0 else None


def _record_attempt_result_accounting(
    generation: Generation,
    attempt: ProviderAttempt,
    result: ProviderPollResult,
) -> None:
    prior_cost_status = attempt.cost_status
    if isinstance(result.usage, dict):
        attempt.usage_snapshot = dict(result.usage)
    if attempt.cost_reserve_usdt is None:
        attempt.cost_reserve_usdt = _attempt_cost_reserve(generation, attempt.provider)
    if result.status != "failed":
        return

    raw_cost = None
    if isinstance(result.usage, dict):
        for field in ("provider_cost_usdt", "provider_charge_usdt", "cost_usdt"):
            if field in result.usage:
                raw_cost = result.usage[field]
                break
    try:
        cost = Decimal(str(raw_cost)) if raw_cost is not None and not isinstance(raw_cost, bool) else None
    except (ArithmeticError, ValueError):
        cost = None
    if cost is not None and cost.is_finite() and cost >= 0:
        attempt.provider_cost_usdt = cost
        attempt.cost_status = "reported"
    else:
        # A terminal status proves completion of the task, not that the provider
        # waived its charge. Keep a queryable obligation until reconciliation.
        attempt.provider_cost_usdt = None
        attempt.cost_status = "unknown"
        if prior_cost_status != "unknown":
            hold_usdt = Decimal(attempt.cost_reserve_usdt or 0)
            generation.provider_cost_hold_usdt = Decimal(generation.provider_cost_hold_usdt or 0) + hold_usdt
            generation.provider_cost_hold_rub = Decimal(generation.provider_cost_hold_rub or 0) + (
                hold_usdt * Decimal(generation.rub_per_usdt_snapshot)
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _queue_fallback_after_safe_failure(
    db: AsyncSession,
    generation: Generation,
    attempt: ProviderAttempt,
    result: ProviderPollResult,
    *,
    preferred_only: str | None = None,
) -> bool:
    from app.billing.client_release import is_client_reserve_final

    if is_client_reserve_final(generation):
        return False
    if attempt.provider != PRIMARY_PROVIDER:
        return False
    selected = await select_fallback_provider(db, generation, after_provider=attempt.provider)
    if selected is None:
        return False
    provider, cost_ceiling = selected
    if preferred_only is not None and provider != preferred_only:
        return False
    infai_snapshot = {}
    if provider == "infai":
        infai_request = _provider_request_for_generation(generation)
        capability = (
            await db.execute(
                select(ProviderModelCapability).where(
                    ProviderModelCapability.provider == provider,
                    ProviderModelCapability.model_id == generation.model_id,
                    ProviderModelCapability.mode == "default",
                    ProviderModelCapability.resolution == generation.resolution,
                )
            )
        ).scalar_one()
        credential = await get_active_provider_credential(db, generation.partner_id, provider)
        if credential is None:
            return False
        infai_snapshot = {
            "infai_price_per_million_usd": str(
                infai_price_per_million(infai_request, Decimal(capability.provider_cost_ceiling_usdt))
            ),
            "infai_group": "Seedance-1",
            "fallback_credential_id": credential.id,
        }
    failed_at = utc_now()
    payload = generation.request_payload or {}
    history = payload.get("provider_failed_tasks", [])
    generation.request_payload = {
        **payload,
        "provider_failed_tasks": [
            *history,
            {
                "provider": attempt.provider,
                "provider_task_id": attempt.provider_task_id,
                "credential_id": attempt.credential_id,
                "error_code": result.error_code,
                "error": result.raw_error,
                "usage": result.usage,
                "failed_at": failed_at.isoformat(),
                "cost_status": "unknown",
            },
        ],
        "fallback_provider": provider,
        "fallback_provider_cost_ceiling_usdt": str(cost_ceiling),
        **infai_snapshot,
    }
    attempt.status = "failed"
    attempt.next_attempt_at = None
    attempt.next_poll_at = None
    attempt.last_error = result.raw_error
    generation.status = "queued"
    generation.public_error_code = None
    from app.providers.circuit import observe

    await observe(db, generation, outcome="error", provider=attempt.provider)
    return True


async def _settle_infai_success(
    db: AsyncSession,
    generation: Generation,
    attempt: ProviderAttempt,
    result: ProviderPollResult,
) -> bool:
    from app.inference.accounting import settle_actual

    payload = generation.request_payload or {}
    usage = result.usage or {}
    tokens, seconds = usage.get("completion_tokens"), usage.get("billed_seconds")
    try:
        if (
            isinstance(tokens, bool)
            or not isinstance(tokens, int)
            or tokens <= 0
            or isinstance(seconds, bool)
            or not isinstance(seconds, int)
            or seconds != generation.duration_seconds
        ):
            raise ValueError("invalid_usage")
        rate = Decimal(payload["infai_price_per_million_usd"])
        cost = rate * Decimal(tokens) / Decimal(1_000_000)
        ceiling = Decimal(payload["fallback_provider_cost_ceiling_usdt"])
        if not cost.is_finite() or cost < 0 or cost > ceiling:
            raise ValueError("invalid_cost")
    except (KeyError, ValueError, ArithmeticError):
        generation.status = "reconciliation_required"
        generation.public_error_code = "usage_reconciliation_required"
        return False
    prior_attempts = list(
        await db.scalars(
            select(ProviderAttempt).where(
                ProviderAttempt.generation_id == generation.id,
                ProviderAttempt.id != attempt.id,
            )
        )
    )
    known_prior_cost = sum(
        (Decimal(item.provider_cost_usdt) for item in prior_attempts if item.provider_cost_usdt is not None),
        Decimal(0),
    )
    # Retail still comes exclusively from the immutable accepted ArgoLink schedule.
    # Known provider costs are cumulative. An unresolved earlier task keeps its
    # own primary hold while unused fallback ceiling is returned.
    await settle_actual(
        db,
        generation,
        {"seconds": generation.duration_seconds},
        provider_cost=cost + known_prior_cost,
        provider_cost_hold_rub=Decimal(generation.provider_cost_hold_rub or 0),
    )
    generation.usage_snapshot = {"seconds": seconds, "completion_tokens": tokens}
    attempt.usage_snapshot = dict(usage)
    attempt.provider_cost_usdt = cost
    attempt.cost_status = "settled"
    return True


async def _settle_provider_cost_reserve(
    db: AsyncSession,
    generation: Generation,
    *,
    provider: str,
    actual_cost: Decimal,
) -> None:
    if generation.actual_provider_cost_usdt is not None:
        return
    reserve = (
        Decimal(generation.provider_cost_reserve_usdt)
        if generation.provider_cost_reserve_usdt is not None
        else Decimal(generation.provider_cost_usdt_snapshot)
    )
    cost = Decimal(actual_cost)
    if not cost.is_finite() or cost < 0 or cost > reserve:
        generation.status = "reconciliation_required"
        generation.public_error_code = "usage_reconciliation_required"
        return
    actual_rub = (cost * Decimal(generation.rub_per_usdt_snapshot)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    refund = Decimal(generation.provider_cost_reserve_rub) - actual_rub
    if refund < 0:
        generation.status = "reconciliation_required"
        generation.public_error_code = "usage_reconciliation_required"
        return
    if refund > 0:
        partner = await lock_partner_for_update(db, generation.partner_id)
        await apply_cost_coverage_change(
            db=db,
            partner=partner,
            amount_rub=refund,
            operation_type="provider_cost_reserve_adjustment",
            idempotency_key=f"provider-cost-settlement:{generation.id}",
            generation_id=generation.id,
            description=f"Settled provider cost reserve for {provider}",
            allow_negative=False,
        )
    generation.actual_provider_cost_usdt = cost


async def _settle_fallback_provider_cost(
    db: AsyncSession,
    generation: Generation,
    provider: str,
) -> None:
    payload = generation.request_payload or {}
    if payload.get("fallback_provider") != provider:
        return
    raw_cost = payload.get("fallback_provider_cost_ceiling_usdt")
    if not isinstance(raw_cost, str):
        generation.status = "reconciliation_required"
        generation.public_error_code = "usage_reconciliation_required"
        return
    try:
        cost = Decimal(raw_cost)
    except Exception:
        generation.status = "reconciliation_required"
        generation.public_error_code = "usage_reconciliation_required"
        return
    await _settle_provider_cost_reserve(
        db,
        generation,
        provider=provider,
        actual_cost=cost,
    )


async def _expire_generation_retry(db: AsyncSession, generation: Generation, attempt: ProviderAttempt) -> bool:
    if not is_older_than(attempt.created_at, get_settings().worker_provider_processing_timeout_seconds):
        return False
    generation.status = attempt.status = "timeout"
    generation.public_error_code = attempt.public_error_code = "generation_timeout"
    attempt.last_error = "provider_processing_timeout"
    attempt.next_attempt_at = attempt.next_poll_at = None
    await release_generation_reserves(db, generation, reason="Released partner reserve after retry deadline")
    await ensure_terminal_webhook_event(db, generation, provider=attempt.provider)
    await db.flush()
    return True


def _mark_attempt_error(
    attempt: ProviderAttempt,
    generation: Generation,
    error: ProviderAdapterError,
) -> None:
    settings = get_settings()
    attempt.public_error_code = error.public_code
    attempt.raw_error = error.raw_error
    attempt.last_error = error.raw_error or error.public_code
    if not error.retryable and error.public_code == "provider_temporarily_unavailable" and not attempt.provider_task_id:
        attempt.status = generation.status = "reconciliation_required"
        attempt.next_attempt_at = attempt.next_poll_at = None
        generation.public_error_code = "submission_outcome_unknown"
        return
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


async def cancel_before_submit(
    db: AsyncSession, generation: Generation, provider: str = PRIMARY_PROVIDER
) -> ProviderAttempt:
    from fastapi import HTTPException

    attempt = (
        await db.execute(
            select(ProviderAttempt).where(
                ProviderAttempt.generation_id == generation.id, ProviderAttempt.provider == provider
            )
        )
    ).scalar_one_or_none()
    if generation.status == "cancelled" and attempt:
        return attempt
    if generation.status not in {"queued", "sent_to_provider"} or (
        attempt and (attempt.provider_task_id or attempt.status != "retry_pending")
    ):
        raise HTTPException(409, "generation_not_cancellable")
    if attempt is None:
        attempt = ProviderAttempt(generation_id=generation.id, provider=provider, status="cancelled")
        db.add(attempt)
    generation.status = attempt.status = "cancelled"
    generation.actual_charge_rub = Decimal("0")
    generation.actual_provider_cost_usdt = (
        None if (generation.request_payload or {}).get("provider_failed_tasks") else Decimal("0")
    )
    attempt.next_attempt_at = attempt.next_poll_at = None
    await release_generation_reserves(db, generation, reason="Cancelled before provider submission")
    await ensure_terminal_webhook_event(db, generation, provider=provider)
    await db.flush()
    return attempt
