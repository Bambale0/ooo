from datetime import timedelta
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.accounts.models import ApiKey, Partner
from app.generations.models import Generation
from app.generations.service import poll_generation_provider
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.infrastructure.security import decrypt_secret, encrypt_secret, hash_secret
from app.providers.models import ProviderAttempt
from app.webhooks.models import WebhookDelivery, WebhookEvent
from app.webhooks.security import validate_webhook_url
from app.webhooks.service import (
    deliver_webhook_once,
    ensure_terminal_webhook_event,
    sign_webhook_payload,
)


def test_webhook_signature_is_hmac_sha256_over_timestamp_dot_raw_body():
    signature = sign_webhook_payload("secret-value", "1700000000", b'{"status":"completed"}')

    assert signature == "sha256=79b4eca6678e184ea382c2fa74aef65a6d8e407ce90dabade9adda786f28790e"


def test_webhook_url_rejects_private_destinations():
    for url in (
        "http://example.com/hook",
        "https://localhost/hook",
        "https://127.0.0.1/hook",
        "https://169.254.169.254/latest/meta-data",
        "https://10.10.10.10/hook",
    ):
        try:
            validate_webhook_url(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe webhook URL accepted: {url}")


async def test_terminal_timeout_creates_durable_event_and_delivery(db_session):
    settings = get_settings()
    old_timeout = settings.worker_provider_processing_timeout_seconds
    settings.worker_provider_processing_timeout_seconds = 1
    try:
        partner = Partner(
            telegram_id="webhook-timeout",
            company_name="Webhook Partner",
            project_name="Webhook Project",
        )
        db_session.add(partner)
        await db_session.flush()

        generation = Generation(
            partner_id=partner.id,
            model_id="model-webhook-timeout",
            model_slug="seedance-2.5",
            mode="text_to_video",
            resolution="720p",
            duration_seconds=5,
            idempotency_key="webhook-timeout-idem",
            partner_price_rub=Decimal("100.00"),
            prompt="timeout webhook",
            status="sent_to_provider",
            webhook_url_snapshot="https://hooks.example.com/generation",
        )
        db_session.add(generation)
        await db_session.flush()

        attempt = ProviderAttempt(
            generation_id=generation.id,
            provider="argolink",
            provider_task_id="timeout-provider-task",
            status="accepted",
            created_at=utc_now() - timedelta(minutes=5),
        )
        db_session.add(attempt)
        await db_session.flush()

        await poll_generation_provider(db_session, generation, "argolink")

        event_result = await db_session.execute(
            select(WebhookEvent).where(WebhookEvent.generation_id == generation.id)
        )
        event = event_result.scalar_one()
        delivery_result = await db_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.event_id == event.id)
        )
        delivery = delivery_result.scalar_one()

        assert generation.status == "timeout"
        assert event.event_type == "timeout"
        assert event.payload["charged_amount_rub"] == "0.00"
        assert event.payload["error_code"] == "generation_timeout"
        assert delivery.attempt == 1
        assert delivery.status == "pending"
    finally:
        settings.worker_provider_processing_timeout_seconds = old_timeout


async def test_signed_delivery_uses_snapshot_secret_and_delivery_identity(db_session):
    settings = get_settings()
    secret = "partner-webhook-secret"
    encrypted = encrypt_secret(secret, settings.webhook_secrets_master_key)

    partner = Partner(
        telegram_id="webhook-delivery",
        company_name="Webhook Delivery",
        project_name="Webhook Delivery Project",
    )
    db_session.add(partner)
    await db_session.flush()

    api_key = ApiKey(
        partner_id=partner.id,
        name="webhook-key",
        key_hash=hash_secret("nrn_test_webhook_key"),
        key_prefix="nrn_test",
        webhook_url="https://hooks.example.com/generation",
        webhook_secret_encrypted=encrypted,
    )
    db_session.add(api_key)
    await db_session.flush()

    generation = Generation(
        partner_id=partner.id,
        api_key_id=api_key.id,
        webhook_url_snapshot=api_key.webhook_url,
        webhook_secret_encrypted_snapshot=api_key.webhook_secret_encrypted,
        model_id="model-webhook-delivery",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="webhook-delivery-idem",
        partner_price_rub=Decimal("100.00"),
        prompt="delivery webhook",
        status="completed",
        result_url="https://api.example.com/result/1",
    )
    db_session.add(generation)
    await db_session.flush()

    event = await ensure_terminal_webhook_event(db_session, generation)
    assert event is not None
    delivery_result = await db_session.execute(
        select(WebhookDelivery).where(WebhookDelivery.event_id == event.id)
    )
    delivery = delivery_result.scalar_one()

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(204, request=request)

    async def resolver(host: str, port: int) -> list[str]:
        assert host == "hooks.example.com"
        assert port == 443
        return ["93.184.216.34"]

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await deliver_webhook_once(db_session, delivery, client=client, resolver=resolver)

    assert delivery.status == "succeeded"
    assert delivery.delivered_at is not None
    headers = captured["headers"]
    body = captured["body"]
    assert headers["x-neironych-event-id"] == event.id
    assert headers["x-neironych-delivery-id"] == delivery.id
    assert headers["x-neironych-attempt"] == "1"
    timestamp = headers["x-neironych-timestamp"]
    assert headers["x-neironych-signature"] == sign_webhook_payload(secret, timestamp, body)
    assert decrypt_secret(event.secret_encrypted, settings.webhook_secrets_master_key) == secret


async def test_failed_delivery_schedules_new_delivery_id(db_session):
    partner = Partner(
        telegram_id="webhook-retry",
        company_name="Webhook Retry",
        project_name="Webhook Retry Project",
    )
    db_session.add(partner)
    await db_session.flush()

    generation = Generation(
        partner_id=partner.id,
        model_id="model-webhook-retry",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="webhook-retry-idem",
        partner_price_rub=Decimal("100.00"),
        prompt="retry webhook",
        status="failed",
        public_error_code="provider_generation_failed",
        webhook_url_snapshot="https://hooks.example.com/generation",
    )
    db_session.add(generation)
    await db_session.flush()

    event = await ensure_terminal_webhook_event(db_session, generation)
    assert event is not None
    delivery_result = await db_session.execute(
        select(WebhookDelivery).where(WebhookDelivery.event_id == event.id)
    )
    first = delivery_result.scalar_one()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    async def resolver(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        await deliver_webhook_once(db_session, first, client=client, resolver=resolver)

    deliveries_result = await db_session.execute(
        select(WebhookDelivery)
        .where(WebhookDelivery.event_id == event.id)
        .order_by(WebhookDelivery.attempt)
    )
    deliveries = list(deliveries_result.scalars().all())

    assert first.status == "failed"
    assert len(deliveries) == 2
    assert deliveries[1].attempt == 2
    assert deliveries[1].id != first.id
    assert deliveries[1].status == "pending"
    assert deliveries[1].next_attempt_at is not None


async def test_api_key_webhook_secret_is_encrypted_and_not_returned(
    client,
    db_session,
    admin_headers,
):
    partner = Partner(
        telegram_id="webhook-api-key",
        company_name="Webhook API Key",
        project_name="Webhook API Key Project",
    )
    db_session.add(partner)
    await db_session.flush()

    response = await client.post(
        f"/api/v1/accounts/partners/{partner.id}/api-keys",
        headers=admin_headers,
        json={
            "name": "with-webhook",
            "webhook_url": "https://hooks.example.com/generation",
            "webhook_secret": "super-secret-webhook-value",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert "webhook_secret" not in body
    assert body["webhook_url"] == "https://hooks.example.com/generation"

    stored = await db_session.get(ApiKey, body["id"])
    assert stored is not None
    assert stored.webhook_secret_encrypted != "super-secret-webhook-value"
    assert (
        decrypt_secret(
            stored.webhook_secret_encrypted,
            get_settings().webhook_secrets_master_key,
        )
        == "super-secret-webhook-value"
    )
