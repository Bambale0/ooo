"""Close abandoned customer submissions, retaining unresolved supplier liabilities."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import or_, select

from app.billing.service import release_generation_reserve
from app.generations.models import Generation
from app.generations.video_recovery import LEGACY_VIDEO_MODES
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.providers.models import ProviderAttempt
from app.providers.video_contract import VIDEO_MODELS
from app.webhooks.service import ensure_terminal_webhook_event

logger = logging.getLogger(__name__)
TERMINAL_FAILURES = ("failed", "cancelled")


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def recover_unconfirmed_submissions_once(db, *, limit: int = 100) -> int:
    now = utc_now()
    cutoff = now - timedelta(seconds=get_settings().worker_provider_processing_timeout_seconds)
    attempts = select(ProviderAttempt.id).where(ProviderAttempt.generation_id == Generation.id)
    unknown = attempts.where(
        ProviderAttempt.status.in_(("submitting", "reconciliation_required")),
        or_(ProviderAttempt.provider_task_id.is_(None), ProviderAttempt.provider_task_id == ""),
    )
    known = attempts.where(
        ProviderAttempt.provider_task_id.is_not(None),
        ProviderAttempt.provider_task_id != "",
        ProviderAttempt.status.not_in(TERMINAL_FAILURES),
    )
    submit_started = Generation.request_payload["provider_submission_started_at"].as_string()
    recent = attempts.where(ProviderAttempt.created_at > cutoff)
    # Never mass-refund legacy reservations, expire a live attempt, or interfere
    # with known-ID video recovery/late success. Filter before LIMIT to avoid starvation.
    rows = list(
        await db.scalars(
            select(Generation)
            .where(
                Generation.status.in_(("submitting", "sent_to_provider", "reconciliation_required")),
                Generation.billing_status == "pending",
                Generation.actual_charge_rub.is_(None),
                Generation.result_url.is_(None),
                Generation.created_at <= cutoff,
                or_(submit_started.is_(None), submit_started <= cutoff.isoformat()),
                Generation.request_payload["partner_billing_policy"].as_string() == "actual_only",
                or_(
                    Generation.mode == "videos/generations",
                    Generation.mode.in_(LEGACY_VIDEO_MODES) & Generation.model_slug.in_(VIDEO_MODELS),
                ),
                unknown.exists(),
                ~known.exists(),
                ~recent.exists(),
            )
            .order_by(Generation.created_at, Generation.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    )
    count = 0
    for generation in rows:
        items = list(
            await db.scalars(
                select(ProviderAttempt)
                .where(ProviderAttempt.generation_id == generation.id)
                .order_by(ProviderAttempt.created_at, ProviderAttempt.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        if not items or any(
            (a.provider_task_id and a.status not in TERMINAL_FAILURES) or _aware(a.created_at) > cutoff for a in items
        ):
            continue
        # Recheck after locking, including a retry that began after selection.
        raw_started = (generation.request_payload or {}).get("provider_submission_started_at")
        if raw_started is not None:
            try:
                started = datetime.fromisoformat(raw_started)
                if started.tzinfo is None or started > cutoff:
                    continue
            except (TypeError, ValueError):
                continue
        if generation.billing_status != "pending" or generation.actual_charge_rub is not None:
            continue
        unresolved = [a for a in items if a.status in {"submitting", "reconciliation_required"}]
        if not unresolved:
            continue
        for attempt in unresolved:
            attempt.status = "reconciliation_required"
            attempt.next_poll_at = attempt.next_attempt_at = None
            if attempt.cost_status not in {"confirmed_free", "confirmed_charged"}:
                attempt.cost_status = "unknown"
                if attempt.cost_reserve_usdt is None:
                    attempt.cost_reserve_usdt = (
                        generation.provider_cost_reserve_usdt
                        if generation.provider_cost_reserve_usdt is not None
                        else generation.provider_cost_usdt_snapshot
                    )
        obligation = sum((Decimal(a.cost_reserve_usdt or 0) for a in items if a.cost_status == "unknown"), Decimal(0))
        generation.provider_cost_hold_usdt = max(Decimal(generation.provider_cost_hold_usdt or 0), obligation)
        generation.provider_cost_hold_rub = max(
            Decimal(generation.provider_cost_hold_rub or 0),
            (obligation * generation.rub_per_usdt_snapshot).quantize(Decimal(".01"), rounding=ROUND_HALF_UP),
        )
        await release_generation_reserve(db, generation, reason="Unconfirmed submission exceeded customer deadline")
        generation.status = "failed"
        generation.billing_status = "not_chargeable"
        generation.client_finalized_at = now
        generation.public_error_code = "generation_timeout"
        generation.request_payload = {
            **(generation.request_payload or {}),
            "submission_recovery": {"reason": "unconfirmed_submission_deadline", "finalized_at": now.isoformat()},
        }
        await ensure_terminal_webhook_event(db, generation, provider=unresolved[-1].provider)
        logger.warning(
            "unconfirmed_submission_finalized",
            extra={
                "generation_id": generation.id,
                "partner_id": generation.partner_id,
                "attempt_id": unresolved[-1].id,
                "provider": unresolved[-1].provider,
                "billing_status": "not_chargeable",
                "provider_cost_status": "reconciliation_required",
            },
        )
        count += 1
    await db.flush()
    return count
