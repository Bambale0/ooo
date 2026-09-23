import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.metrics import monotonic_seconds, observe_webhook_delivery
from app.infrastructure.retry import utc_now
from app.infrastructure.security import decrypt_secret
from app.webhooks.models import WebhookDelivery, WebhookEvent
from app.webhooks.security import validate_public_webhook_url

_TERMINAL_STATUSES = {"completed", "failed", "timeout", "cancelled"}
_client: httpx.AsyncClient | None = None


@dataclass(frozen=True)
class PreparedWebhookDelivery:
    event_id: str
    delivery_id: str
    attempt: int
    webhook_url: str
    webhook_secret_encrypted: str | None
    payload: dict[str, object]


@dataclass(frozen=True)
class WebhookDeliveryOutcome:
    delivered: bool
    response_status: int | None = None
    error: str | None = None


async def ensure_terminal_webhook_event(db: AsyncSession, generation: Generation) -> WebhookEvent | None:
    from app.providers.circuit import observe

    await observe(db, generation)
    trial_user = (generation.request_payload or {}).get("trial_telegram_id")
    if trial_user and generation.status in _TERMINAL_STATUSES:
        from app.accounts.models import Partner
        from app.telegram.service import notify
        from app.telegram.trials import download_link

        owner = await db.get(Partner, generation.partner_id)
        if owner and owner.status == "active":
            detail = (
                download_link(generation) if generation.status == "completed" else _human_message(generation.status)
            )
            await notify(
                db,
                owner.telegram_id,
                f"Пробная генерация {generation.id}: {generation.status}.\n{detail}",
                f"trial:{generation.id}:{generation.status}",
            )
    if generation.status not in _TERMINAL_STATUSES or not generation.webhook_url_snapshot:
        return None

    existing_result = await db.execute(
        select(WebhookEvent).where(
            WebhookEvent.generation_id == generation.id,
            WebhookEvent.event_type == generation.status,
        )
    )
    existing = existing_result.scalar_one_or_none()
    if existing is not None:
        return existing

    event = WebhookEvent(
        generation_id=generation.id,
        event_type=generation.status,
        partner_id=generation.partner_id,
        webhook_url=generation.webhook_url_snapshot,
        webhook_secret_encrypted=generation.webhook_secret_encrypted_snapshot,
        payload=_build_terminal_payload(generation),
        status="pending",
        next_attempt_at=utc_now(),
    )
    db.add(event)
    await db.flush()
    await db.refresh(event)
    return event


async def request_manual_resend(
    db: AsyncSession,
    *,
    generation_id: str,
    partner_id: str,
) -> WebhookEvent:
    result = await db.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.generation_id == generation_id,
            WebhookEvent.partner_id == partner_id,
        )
        .order_by(WebhookEvent.created_at.desc())
    )
    event = result.scalars().first()
    if event is None:
        raise LookupError("webhook_event_not_found")

    event.status = "pending"
    event.next_attempt_at = utc_now()
    event.claimed_until = None
    event.failed_at = None
    await db.flush()
    await db.refresh(event)
    return event


async def claim_due_events(
    db: AsyncSession,
    *,
    limit: int,
) -> list[str]:
    now = utc_now()
    lease_until = now + timedelta(seconds=get_settings().webhook_claim_lease_seconds)
    result = await db.execute(
        select(WebhookEvent)
        .where(
            WebhookEvent.status == "pending",
            WebhookEvent.next_attempt_at <= now,
            (WebhookEvent.claimed_until.is_(None)) | (WebhookEvent.claimed_until <= now),
        )
        .order_by(WebhookEvent.next_attempt_at, WebhookEvent.created_at)
        .with_for_update(skip_locked=True)
        .limit(limit)
    )
    events = list(result.scalars().all())
    for event in events:
        event.claimed_until = lease_until
    await db.flush()
    return [event.id for event in events]


async def prepare_claimed_delivery(
    db: AsyncSession,
    event_id: str,
) -> PreparedWebhookDelivery | None:
    result = await db.execute(select(WebhookEvent).where(WebhookEvent.id == event_id).with_for_update())
    event = result.scalar_one_or_none()
    if event is None or event.status != "pending":
        return None

    stale_result = await db.execute(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.event_id == event.id,
            WebhookDelivery.status == "pending",
        )
        .with_for_update()
    )
    for stale_delivery in stale_result.scalars().all():
        stale_delivery.status = "outcome_unknown"
        stale_delivery.error = "previous_delivery_not_finalized"

    attempt = event.attempt_count + 1
    delivery = WebhookDelivery(
        event_id=event.id,
        attempt=attempt,
        status="pending",
    )
    db.add(delivery)
    event.attempt_count = attempt
    await db.flush()

    return PreparedWebhookDelivery(
        event_id=event.id,
        delivery_id=delivery.id,
        attempt=attempt,
        webhook_url=event.webhook_url,
        webhook_secret_encrypted=event.webhook_secret_encrypted,
        payload=dict(event.payload),
    )


async def send_prepared_delivery(prepared: PreparedWebhookDelivery) -> WebhookDeliveryOutcome:
    now = utc_now()
    raw_body = json.dumps(prepared.payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(now.timestamp()))
    signature = _signature(prepared.webhook_secret_encrypted, timestamp, raw_body)
    headers = {
        "Content-Type": "application/json",
        "X-Neironych-Timestamp": timestamp,
        "X-Neironych-Event-Id": prepared.event_id,
        "X-Neironych-Delivery-Id": prepared.delivery_id,
        "X-Neironych-Attempt": str(prepared.attempt),
    }
    if signature is not None:
        headers["X-Neironych-Signature"] = f"sha256={signature}"

    delivery_started_at = monotonic_seconds()
    try:
        await validate_public_webhook_url(prepared.webhook_url)
        response = await _http_client().post(
            prepared.webhook_url,
            content=raw_body,
            headers=headers,
        )
        if 200 <= response.status_code < 300:
            observe_webhook_delivery(
                outcome="delivered",
                duration_seconds=monotonic_seconds() - delivery_started_at,
            )
            return WebhookDeliveryOutcome(
                delivered=True,
                response_status=response.status_code,
            )
        observe_webhook_delivery(
            outcome="http_error",
            duration_seconds=monotonic_seconds() - delivery_started_at,
        )
        return WebhookDeliveryOutcome(
            delivered=False,
            response_status=response.status_code,
            error=f"http_{response.status_code}",
        )
    except Exception as exc:
        observe_webhook_delivery(
            outcome="exception",
            duration_seconds=monotonic_seconds() - delivery_started_at,
        )
        return WebhookDeliveryOutcome(
            delivered=False,
            error=type(exc).__name__,
        )


async def finalize_prepared_delivery(
    db: AsyncSession,
    prepared: PreparedWebhookDelivery,
    outcome: WebhookDeliveryOutcome,
) -> bool:
    event_result = await db.execute(select(WebhookEvent).where(WebhookEvent.id == prepared.event_id).with_for_update())
    event = event_result.scalar_one_or_none()
    delivery_result = await db.execute(
        select(WebhookDelivery).where(WebhookDelivery.id == prepared.delivery_id).with_for_update()
    )
    delivery = delivery_result.scalar_one_or_none()
    if event is None or delivery is None:
        return False
    if delivery.status != "pending":
        return delivery.status == "delivered"

    delivery.response_status = outcome.response_status
    if outcome.delivered:
        delivery.status = "delivered"
        delivery.error = None
        event.status = "delivered"
        event.delivered_at = utc_now()
        event.next_attempt_at = None
        event.claimed_until = None
        await db.flush()
        return True

    delivery.status = "failed"
    delivery.error = outcome.error
    event.claimed_until = None
    now = utc_now()
    if now - event.created_at >= timedelta(seconds=get_settings().webhook_retry_window_seconds):
        event.status = "failed"
        event.failed_at = now
        event.next_attempt_at = None
    else:
        event.next_attempt_at = now + timedelta(seconds=get_settings().webhook_retry_interval_seconds)

    await db.flush()
    return False


async def deliver_claimed_event(db: AsyncSession, event_id: str) -> bool:
    """Crash-durable convenience path.

    The delivery identity and attempt number are committed before the external
    HTTP side effect. If the process dies after the send but before finalization,
    the next lease creates a new monotonically increasing attempt and delivery id.
    """

    prepared = await prepare_claimed_delivery(db, event_id)
    if prepared is None:
        return False
    await db.commit()

    outcome = await send_prepared_delivery(prepared)
    return await finalize_prepared_delivery(db, prepared, outcome)


async def close_webhook_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        settings = get_settings()
        from app.infrastructure.public_http import PublicHTTPTransport

        _client = httpx.AsyncClient(
            transport=PublicHTTPTransport(),
            timeout=httpx.Timeout(settings.webhook_timeout_seconds),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=50),
            follow_redirects=False,
            trust_env=False,
        )
    return _client


def _signature(secret_encrypted: str | None, timestamp: str, raw_body: bytes) -> str | None:
    if secret_encrypted is None:
        return None
    master_key = get_settings().provider_credentials_master_key
    if not master_key:
        return None
    secret = decrypt_secret(secret_encrypted, master_key).encode("utf-8")
    signed = timestamp.encode("ascii") + b"." + raw_body
    return hmac.new(secret, signed, hashlib.sha256).hexdigest()


def _build_terminal_payload(generation: Generation) -> dict[str, object]:
    payload: dict[str, object] = {
        "generation_id": generation.id,
        "status": generation.status,
        "model": generation.model_slug,
        "params": (generation.request_payload or {}).get("native_body")
        or {
            key: value
            for key, value in (generation.request_payload or {}).items()
            if key
            in {"duration_seconds", "aspect_ratio", "reference_images", "start_image", "end_image", "billing_unit"}
        },
    }
    if generation.status == "completed":
        payload["result_url"] = generation.result_url
        payload["charged_amount_rub"] = str(
            generation.actual_charge_rub if generation.actual_charge_rub is not None else generation.partner_price_rub
        )
    else:
        payload["error_code"] = generation.public_error_code or f"generation_{generation.status}"
        payload["human_message"] = _human_message(generation.status)
    return payload


def _human_message(status: str) -> str:
    return {
        "failed": "Generation failed.",
        "timeout": "Generation timed out.",
        "cancelled": "Generation was cancelled.",
    }.get(status, "Generation ended.")
