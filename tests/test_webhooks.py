import hashlib
import hmac
import json
from decimal import Decimal

import httpx
from sqlalchemy import select

from app.accounts.models import Partner
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.security import encrypt_secret
from app.webhooks.models import WebhookDelivery
from app.webhooks.service import (
    claim_due_events,
    deliver_claimed_event,
    ensure_terminal_webhook_event,
    request_manual_resend,
)


class RecordingWebhookClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def post(self, url: str, *, content: bytes, headers: dict[str, str]) -> httpx.Response:
        self.calls.append({"url": url, "content": content, "headers": headers})
        return httpx.Response(204)


async def test_terminal_webhook_is_signed_and_manual_resend_keeps_event_identity(
    db_session,
    monkeypatch,
):
    partner = Partner(
        telegram_id="webhook-partner",
        company_name="Webhook Partner",
        project_name="Webhook Bot",
    )
    db_session.add(partner)
    await db_session.flush()

    secret = "partner-webhook-secret"
    encrypted_secret = encrypt_secret(secret, get_settings().provider_credentials_master_key or "")
    generation = Generation(
        partner_id=partner.id,
        model_id="model-webhook",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="webhook-generation-idem",
        partner_price_rub=Decimal("100.00"),
        prompt="webhook test",
        request_payload={"duration_seconds": 5, "billing_unit": "second"},
        result_url="https://cdn.example.test/result.mp4",
        status="completed",
        webhook_url_snapshot="https://partner.example.test/hooks/neironych",
        webhook_secret_encrypted_snapshot=encrypted_secret,
    )
    db_session.add(generation)
    await db_session.flush()

    event = await ensure_terminal_webhook_event(db_session, generation)
    assert event is not None
    original_event_id = event.id

    async def allow_test_url(url: str) -> None:
        assert url == "https://partner.example.test/hooks/neironych"

    recorder = RecordingWebhookClient()
    monkeypatch.setattr("app.webhooks.service.validate_public_webhook_url", allow_test_url)
    monkeypatch.setattr("app.webhooks.service._client", recorder)

    claimed = await claim_due_events(db_session, limit=10)
    assert claimed == [original_event_id]

    delivered = await deliver_claimed_event(db_session, original_event_id)
    assert delivered is True
    assert event.status == "delivered"
    assert event.attempt_count == 1
    assert len(recorder.calls) == 1

    first = recorder.calls[0]
    first_headers = first["headers"]
    first_body = first["content"]
    assert isinstance(first_headers, dict)
    assert isinstance(first_body, bytes)
    assert first_headers["X-Neironych-Event-Id"] == original_event_id
    assert first_headers["X-Neironych-Attempt"] == "1"
    assert first_headers["X-Neironych-Delivery-Id"]
    assert first_headers["X-Neironych-Signature"].startswith("sha256=")

    timestamp = first_headers["X-Neironych-Timestamp"]
    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + first_body,
        hashlib.sha256,
    ).hexdigest()
    assert first_headers["X-Neironych-Signature"] == f"sha256={expected}"

    payload = json.loads(first_body)
    assert payload == {
        "generation_id": generation.id,
        "status": "completed",
        "model": "seedance-2.5",
        "params": {"duration_seconds": 5, "billing_unit": "second"},
        "result_url": "https://cdn.example.test/result.mp4",
        "charged_amount_rub": "100.00",
    }
    assert "balance" not in payload
    assert "provider" not in payload

    resent = await request_manual_resend(
        db_session,
        generation_id=generation.id,
        partner_id=partner.id,
    )
    assert resent.id == original_event_id

    claimed_again = await claim_due_events(db_session, limit=10)
    assert claimed_again == [original_event_id]
    delivered_again = await deliver_claimed_event(db_session, original_event_id)
    assert delivered_again is True
    assert event.attempt_count == 2
    assert len(recorder.calls) == 2

    second_headers = recorder.calls[1]["headers"]
    assert isinstance(second_headers, dict)
    assert second_headers["X-Neironych-Event-Id"] == original_event_id
    assert second_headers["X-Neironych-Attempt"] == "2"
    assert second_headers["X-Neironych-Delivery-Id"] != first_headers["X-Neironych-Delivery-Id"]

    delivery_result = await db_session.execute(
        select(WebhookDelivery).where(WebhookDelivery.event_id == original_event_id).order_by(WebhookDelivery.attempt)
    )
    deliveries = list(delivery_result.scalars().all())
    assert [delivery.attempt for delivery in deliveries] == [1, 2]
    assert all(delivery.status == "delivered" for delivery in deliveries)


async def test_failed_webhook_event_has_stable_public_error_payload(db_session):
    partner = Partner(
        telegram_id="webhook-failed-partner",
        company_name="Failed Partner",
        project_name="Failed Bot",
    )
    db_session.add(partner)
    await db_session.flush()

    generation = Generation(
        partner_id=partner.id,
        model_id="model-failed",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="webhook-failed-idem",
        partner_price_rub=Decimal("100.00"),
        prompt="failed webhook test",
        request_payload={"duration_seconds": 5},
        status="failed",
        public_error_code="provider_generation_failed",
        webhook_url_snapshot="https://partner.example.test/hooks/neironych",
    )
    db_session.add(generation)
    await db_session.flush()

    event = await ensure_terminal_webhook_event(db_session, generation)
    assert event is not None
    assert event.payload["error_code"] == "provider_generation_failed"
    assert event.payload["human_message"] == "Generation failed."
    assert "charged_amount_rub" not in event.payload
    assert "balance" not in event.payload


async def test_webhook_event_identity_is_per_terminal_business_event(db_session):
    from app.webhooks.models import WebhookEvent

    partner = Partner(
        telegram_id="webhook-transition-partner",
        company_name="Transition Partner",
        project_name="Transition Bot",
    )
    db_session.add(partner)
    await db_session.flush()

    generation = Generation(
        partner_id=partner.id,
        model_id="model-transition",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="webhook-transition-idem",
        partner_price_rub=Decimal("100.00"),
        prompt="transition webhook test",
        request_payload={"duration_seconds": 5},
        status="timeout",
        public_error_code="generation_timeout",
        webhook_url_snapshot="https://partner.example.test/hooks/neironych",
    )
    db_session.add(generation)
    await db_session.flush()

    timeout_event = await ensure_terminal_webhook_event(db_session, generation)
    assert timeout_event is not None
    assert timeout_event.event_type == "timeout"

    generation.status = "completed"
    generation.public_error_code = None
    generation.result_url = "https://cdn.example.test/late-result.mp4"
    completed_event = await ensure_terminal_webhook_event(db_session, generation)

    assert completed_event is not None
    assert completed_event.id != timeout_event.id
    assert completed_event.event_type == "completed"

    result = await db_session.execute(
        select(WebhookEvent)
        .where(WebhookEvent.generation_id == generation.id)
        .order_by(WebhookEvent.created_at)
    )
    events = list(result.scalars().all())
    assert [event.event_type for event in events] == ["timeout", "completed"]
