import hashlib
import hmac
import json
from datetime import timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.infrastructure.security import decrypt_secret
from app.webhooks.models import WebhookDelivery, WebhookEvent
from app.webhooks.security import validate_public_webhook_url

_TERMINAL_STATUSES = {"completed", "failed", "timeout", "cancelled"}
_client: httpx.AsyncClient | None = None


async def ensure_terminal_webhook_event(db: AsyncSession, generation: Generation) -> WebhookEvent | None:
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


async def deliver_claimed_event(db: AsyncSession, event_id: str) -> bool:
    event = await db.get(WebhookEvent, event_id)
    if event is None or event.status != "pending":
        return False

    settings = get_settings()
    now = utc_now()
    attempt = event.attempt_count + 1
    delivery = WebhookDelivery(
        event_id=event.id,
        attempt=attempt,
        status="pending",
    )
    db.add(delivery)
    await db.flush()

    raw_body = json.dumps(event.payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    timestamp = str(int(now.timestamp()))
    signature = _signature(event.webhook_secret_encrypted, timestamp, raw_body)
    headers = {
        "Content-Type": "application/json",
        "X-Neironych-Timestamp": timestamp,
        "X-Neironych-Event-Id": event.id,
        "X-Neironych-Delivery-Id": delivery.id,
        "X-Neironych-Attempt": str(attempt),
    }
    if signature is not None:
        headers["X-Neironych-Signature"] = f"sha256={signature}"

    try:
        await validate_public_webhook_url(event.webhook_url)
        response = await _http_client().post(
            event.webhook_url,
            content=raw_body,
            headers=headers,
        )
        delivery.response_status = response.status_code
        if 200 <= response.status_code < 300:
            delivery.status = "delivered"
            event.status = "delivered"
            event.delivered_at = utc_now()
            event.next_attempt_at = None
            event.claimed_until = None
            event.attempt_count = attempt
            await db.flush()
            return True
        delivery.status = "failed"
        delivery.error = f"http_{response.status_code}"
    except Exception as exc:
        delivery.status = "failed"
        delivery.error = type(exc).__name__

    event.attempt_count = attempt
    event.claimed_until = None
    if now - event.created_at >= timedelta(seconds=settings.webhook_retry_window_seconds):
        event.status = "failed"
        event.failed_at = utc_now()
        event.next_attempt_at = None
    else:
        event.next_attempt_at = utc_now() + timedelta(seconds=settings.webhook_retry_interval_seconds)

    await db.flush()
    return False


async def close_webhook_http_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _http_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = httpx.AsyncClient(
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
        "params": generation.request_payload or {},
    }
    if generation.status == "completed":
        payload["result_url"] = generation.result_url
        payload["charged_amount_rub"] = str(generation.partner_price_rub)
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
