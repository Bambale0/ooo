"""Bounded recovery after a lost request process. Never resubmit native work."""

from datetime import UTC, timedelta

from fastapi.responses import JSONResponse
from sqlalchemy import Float, and_, cast, func, or_, select

from app.generations.models import Generation, NativeResponse
from app.inference.outcomes import finalize_native_failure
from app.inference.replay import save_response
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.providers.models import ProviderAttempt

SYNC_PROTOCOLS = ("responses", "messages", "chat/completions", "images/generations", "images/edits")


def aware(value):
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def recover_native_outcomes_once(db, *, limit=100):
    now, settings = utc_now(), get_settings()
    # Filter due work BEFORE LIMIT. An old completed result awaiting usage must
    # not hide a newer abandoned request behind an unexpired review deadline.
    request_expiry = func.extract("epoch", Generation.created_at) + func.coalesce(
        cast(Generation.request_payload["native_deadline_seconds"].as_string(), Float),
        settings.native_request_timeout_seconds,
    ) + settings.native_abandonment_grace_seconds
    usage_expiry = func.extract("epoch", Generation.client_finalized_at) + func.coalesce(
        cast(Generation.request_payload["native_usage_reconciliation_seconds"].as_string(), Float),
        settings.native_usage_reconciliation_seconds,
    )
    rows = list(
        await db.scalars(
            select(Generation)
            .where(
                Generation.mode.in_(SYNC_PROTOCOLS),
                Generation.billing_status == "pending",
                or_(
                    and_(Generation.status == "submitting", request_expiry <= now.timestamp()),
                    and_(
                        Generation.status == "completed",
                        Generation.client_finalized_at.is_not(None),
                        Generation.request_payload["usage_reconciliation_pending"].as_boolean().is_(True),
                        usage_expiry <= now.timestamp(),
                    ),
                ),
                Generation.request_payload["native_finality_policy"].as_string() == "v1",
            )
            .order_by(Generation.created_at, Generation.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    count = 0
    for generation in rows:
        payload = generation.request_payload or {}
        if generation.status == "completed":
            if not payload.get("usage_reconciliation_pending") or generation.client_finalized_at is None:
                continue
            expiry = aware(generation.client_finalized_at) + timedelta(
                seconds=payload.get("native_usage_reconciliation_seconds", settings.native_usage_reconciliation_seconds)
            )
            if expiry > now:
                continue
            generation.billing_status = "waived"
            generation.request_payload = {
                **payload,
                "usage_reconciliation_pending": False,
                "usage_waived_at": now.isoformat(),
            }
            count += 1
            continue
        expiry = aware(generation.created_at) + timedelta(
            seconds=payload.get("native_deadline_seconds", settings.native_request_timeout_seconds)
            + settings.native_abandonment_grace_seconds
        )
        if expiry > now:
            continue
        attempt = await db.scalar(
            select(ProviderAttempt)
            .where(ProviderAttempt.generation_id == generation.id)
            .order_by(ProviderAttempt.created_at.desc(), ProviderAttempt.id.desc())
            .limit(1)
        )
        if attempt is None:
            continue
        code = "request_deadline_exceeded"
        await finalize_native_failure(db, generation, attempt, code, definitive=False)
        await save_response(
            db,
            generation,
            JSONResponse({"error": {"type": code, "message": code}, "request_id": generation.id}, status_code=503),
        )
        count += 1
    # Delete retained content only. The permanent generation key prevents a new
    # provider submission even after the encrypted response has been removed.
    expired = list(
        await db.scalars(
            select(NativeResponse)
            .where(NativeResponse.expires_at <= now)
            .order_by(NativeResponse.expires_at, NativeResponse.generation_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for response in expired:
        await db.delete(response)
    await db.flush()
    return count
