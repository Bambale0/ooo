import hashlib
import hmac
import json
from datetime import timedelta
from time import time

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.infrastructure.security import decrypt_secret
from app.webhooks.models import WebhookDelivery, WebhookEvent
from app.webhooks.security import Resolver, ensure_public_webhook_destination, resolve_public_addresses

TERMINAL_STATUSES = {"completed", "failed", "timeout", "cancelled"}


async def ensure_terminal_webhook_event(
    db: AsyncSession,
    generation: Generation,
) -> WebhookEvent | None:
    if generation.status not in TERMINAL_STATUSES or not generation.webhook_url_snapshot:
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
        partner_id=generation.partner_id,
        api_key_id=generation.api_key_id,
        event_type=generation.status,
        payload=_build_payload(generation),
        destination_url=generation.webhook_url_snapshot,
        secret_encrypted=generation.webhook_secret_encrypted_snapshot,
    )
    db.add(event)
    await db.flush()

    delivery = WebhookDelivery(
        event_id=event.id,
        attempt=1,
        status="pending",
        next_attempt_at=utc_now(),
    )
    db.add(delivery)
    await db.flush()
    return event


async def schedule_manual_resend(
    db: AsyncSession,
    event: WebhookEvent,
) -> WebhookDelivery:
    max_attempt_result = await db.execute(
        select(func.max(WebhookDelivery.attempt)).where(WebhookDelivery.event_id == event.id)
    )
    next_attempt = int(max_attempt_result.scalar_one() or 0) + 1
    delivery = WebhookDelivery(
        event_id=event.id,
        attempt=next_attempt,
        status="pending",
        next_attempt_at=utc_now(),
    )
    db.add(delivery)
    await db.flush()
    await db.refresh(delivery)
    return delivery


async def deliver_webhook_once(
    db: AsyncSession,
    delivery: WebhookDelivery,
    *,
    client: httpx.AsyncClient | None = None,
    resolver: Resolver = resolve_public_addresses,
) -> WebhookDelivery:
    event = await db.get(WebhookEvent, delivery.event_id)
    if event is None:
        delivery.status = "failed_terminal"
        delivery.last_error = "webhook_event_missing"
        delivery.next_attempt_at = None
        await db.flush()
        return delivery

    try:
        await ensure_public_webhook_destination(event.destination_url, resolver=resolver)
    except ValueError as exc:
        error_code = str(exc)
        delivery.last_error = error_code
        delivery.next_attempt_at = None
        if error_code in {
            "webhook_url_must_use_https",
            "webhook_url_credentials_forbidden",
            "webhook_url_host_required",
            "webhook_url_private_destination",
        }:
            delivery.status = "failed_terminal"
        else:
            delivery.status = "failed"
            await _schedule_retry_if_allowed(db, event, delivery)
        await db.flush()
        return delivery

    timestamp = str(int(time()))
    body = _serialize_payload(event.payload)
    headers = {
        "Content-Type": "application/json",
        "X-Neironych-Timestamp": timestamp,
        "X-Neironych-Event-Id": event.id,
        "X-Neironych-Delivery-Id": delivery.id,
        "X-Neironych-Attempt": str(delivery.attempt),
    }
    try:
        signature = _signature_for_event(event, timestamp, body)
    except (RuntimeError, ValueError) as exc:
        delivery.status = "failed"
        delivery.last_error = str(exc)
        delivery.next_attempt_at = None
        await _schedule_retry_if_allowed(db, event, delivery)
        await db.flush()
        return delivery
    if signature is not None:
        headers["X-Neironych-Signature"] = signature

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=get_settings().webhook_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )

    response_status: int | None = None
    error_name: str | None = None
    try:
        response = await client.post(event.destination_url, content=body, headers=headers)
        response_status = response.status_code
        succeeded = 200 <= response.status_code < 300
        if not succeeded:
            error_name = f"http_{response.status_code}"
    except httpx.HTTPError as exc:
        succeeded = False
        error_name = type(exc).__name__
    finally:
        if owns_client:
            await client.aclose()

    delivery.response_status = response_status
    delivery.last_error = error_name
    delivery.next_attempt_at = None

    if succeeded:
        delivery.status = "succeeded"
        delivery.delivered_at = utc_now()
        await db.flush()
        return delivery

    delivery.status = "failed"
    await _schedule_retry_if_allowed(db, event, delivery)
    await db.flush()
    return delivery


def sign_webhook_payload(secret: str, timestamp: str, raw_body: bytes) -> str:
    signed = timestamp.encode("utf-8") + b"." + raw_body
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _signature_for_event(event: WebhookEvent, timestamp: str, raw_body: bytes) -> str | None:
    if not event.secret_encrypted:
        return None
    master_key = get_settings().webhook_secrets_master_key
    if not master_key:
        raise RuntimeError("webhook_secret_encryption_not_configured")
    secret = decrypt_secret(event.secret_encrypted, master_key)
    return sign_webhook_payload(secret, timestamp, raw_body)


def _serialize_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _build_payload(generation: Generation) -> dict[str, object]:
    completed = generation.status == "completed"
    payload: dict[str, object] = {
        "generation_id": generation.id,
        "status": generation.status,
        "model": generation.model_slug,
        "params": {
            "mode": generation.mode,
            "resolution": generation.resolution,
            "duration_seconds": generation.duration_seconds,
            "aspect_ratio": generation.aspect_ratio,
        },
        "charged_amount_rub": str(generation.partner_price_rub if completed else "0.00"),
    }
    if completed:
        payload["result"] = {"url": generation.result_url}
    else:
        payload["error_code"] = generation.public_error_code or f"generation_{generation.status}"
        payload["human_message"] = {
            "failed": "Generation failed",
            "timeout": "Generation timed out",
            "cancelled": "Generation cancelled",
        }.get(generation.status, "Generation failed")
    return payload


async def _schedule_retry_if_allowed(
    db: AsyncSession,
    event: WebhookEvent,
    delivery: WebhookDelivery,
) -> None:
    settings = get_settings()
    created_at = event.created_at
    if hasattr(created_at, "tzinfo") and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=utc_now().tzinfo)
    deadline = created_at + timedelta(hours=settings.webhook_retry_window_hours)
    now = utc_now()
    if now >= deadline:
        delivery.status = "failed_terminal"
        return

    retry_at = min(
        now + timedelta(seconds=settings.webhook_retry_interval_seconds),
        deadline,
    )
    retry = WebhookDelivery(
        event_id=event.id,
        attempt=delivery.attempt + 1,
        status="pending",
        next_attempt_at=retry_at,
    )
    db.add(retry)
