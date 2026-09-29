from datetime import timedelta
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.accounts.models import Partner
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.providers.base import ProviderPollResult, ProviderResultStream, ProviderSubmitResult
from app.providers.models import ProviderAttempt
from app.workers.generation_worker import process_generation_work_once


class FakeArgoLinkAdapter:
    provider_name = "argolink"

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    async def health_check(self) -> bool:
        return True

    async def prepaid_balance_usdt(self) -> Decimal:
        return Decimal("100")

    async def validate_key(self, api_key: str) -> bool:
        return not api_key.startswith("invalid")

    async def submit_generation(self, payload):
        assert self.api_key == "argolink-secret"
        assert payload.duration_seconds == 5
        assert payload.aspect_ratio == "9:16"
        assert payload.reference_images == ("https://cdn.example.test/reference.jpg",)
        return ProviderSubmitResult(provider_task_id=f"fake_{payload.generation_id}")

    async def poll_generation(self, provider_task_id: str):
        return ProviderPollResult(
            status="completed",
            result_url=f"https://argolink.io/v1/videos/{provider_task_id}/content",
        )

    async def open_result_stream(self, provider_content_url: str, *, range_header: str | None = None):
        assert provider_content_url.startswith("https://argolink.io/v1/videos/")
        assert range_header is None

        async def body():
            yield b"fake "
            yield b"mp4 bytes"

        return ProviderResultStream(
            body=body(),
            content_type="video/mp4",
            content_length=len(b"fake mp4 bytes"),
            accept_ranges="bytes",
        )

    def normalize_error(self, error: Exception):
        raise error


def fake_provider_adapter(provider: str, *, api_key: str | None = None) -> FakeArgoLinkAdapter:
    assert provider == "argolink"
    return FakeArgoLinkAdapter(api_key=api_key)


async def test_partner_can_create_idempotent_generation_after_manual_credit(
    client,
    db_session: AsyncSession,
    admin_headers,
    monkeypatch,
):
    monkeypatch.setattr("app.providers.router.get_provider_adapter", fake_provider_adapter)
    monkeypatch.setattr("app.providers.service.get_provider_adapter", fake_provider_adapter)

    application_response = await client.post(
        "/api/v1/accounts/applications",
        headers=admin_headers,
        json={
            "telegram_id": "100500",
            "company_name": "Demo Partner",
            "project_name": "Demo Bot",
            "accepted_terms": True,
            "accepted_privacy_policy": True,
        },
    )
    assert application_response.status_code == 201
    application_id = application_response.json()["id"]

    duplicate_application = await client.post(
        "/api/v1/accounts/applications",
        headers=admin_headers,
        json={
            "telegram_id": "100500",
            "company_name": "Demo Partner",
            "project_name": "Demo Bot",
            "accepted_terms": True,
            "accepted_privacy_policy": True,
        },
    )
    assert duplicate_application.status_code == 201
    assert duplicate_application.json()["id"] == application_id

    blocked_approval = await client.post(
        f"/api/v1/accounts/applications/{application_id}/approve",
        headers=admin_headers,
    )
    assert blocked_approval.status_code == 409
    assert blocked_approval.json()["detail"] == "required_provider_key_missing"

    invalid_provider_key_response = await client.post(
        "/api/v1/providers/credentials",
        headers=admin_headers,
        json={
            "provider": "argolink",
            "label": "bad",
            "api_key": "invalid-secret",
            "partner_application_id": application_id,
        },
    )
    assert invalid_provider_key_response.status_code == 409
    assert invalid_provider_key_response.json()["detail"] == "provider_key_invalid"

    provider_key_response = await client.post(
        "/api/v1/providers/credentials",
        headers=admin_headers,
        json={
            "provider": "argolink",
            "label": "test",
            "api_key": "argolink-secret",
            "partner_application_id": application_id,
        },
    )
    assert provider_key_response.status_code == 201
    assert "api_key" not in provider_key_response.json()

    approve_response = await client.post(
        f"/api/v1/accounts/applications/{application_id}/approve",
        headers=admin_headers,
    )
    assert approve_response.status_code == 200
    partner_id = approve_response.json()["id"]

    key_response = await client.post(
        f"/api/v1/accounts/partners/{partner_id}/api-keys",
        headers=admin_headers,
        json={"name": "integration"},
    )
    assert key_response.status_code == 201
    api_key = key_response.json()["api_key"]

    model_response = await client.post(
        "/api/v1/catalog/models",
        headers=admin_headers,
        json={
            "slug": "seedance-2.5",
            "name": "Seedance 2.5",
            "modality": "video",
            "status": "admin_only",
        },
    )
    assert model_response.status_code == 201

    too_low_price_response = await client.put(
        "/api/v1/catalog/pricing",
        headers=admin_headers,
        json={
            "model_slug": "seedance-2.5",
            "mode": "reference",
            "resolution": "720p",
            "price_rub": "10.00",
            "provider_cost_usdt": "0.170000",
            "billing_unit": "second",
        },
    )
    assert too_low_price_response.status_code == 409
    assert too_low_price_response.json()["detail"] == "partner_price_below_provider_cost"

    price_response = await client.put(
        "/api/v1/catalog/pricing",
        headers=admin_headers,
        json={
            "model_slug": "seedance-2.5",
            "mode": "reference",
            "resolution": "720p",
            "price_rub": "20.00",
            "provider_cost_usdt": "0.170000",
            "billing_unit": "second",
        },
    )
    assert price_response.status_code == 204

    blocked_enable = await client.post(
        "/api/v1/catalog/models/seedance-2.5/enable",
        headers=admin_headers,
    )
    assert blocked_enable.status_code == 409

    gates_response = await client.post(
        "/api/v1/catalog/models/seedance-2.5/enable-gates",
        headers=admin_headers,
        json={
            "has_provider_integration": True,
            "has_public_docs": True,
            "has_successful_smoke": True,
        },
    )
    assert gates_response.status_code == 200

    enable_response = await client.post(
        "/api/v1/catalog/models/seedance-2.5/enable",
        headers=admin_headers,
    )
    assert enable_response.status_code == 200
    assert enable_response.json()["status"] == "production"

    credit_response = await client.post(
        "/api/v1/billing/manual-adjustments",
        headers=admin_headers,
        json={
            "partner_id": partner_id,
            "amount_rub": "1000.00",
            "idempotency_key": "credit-demo-1",
            "description": "Initial test credit",
        },
    )
    assert credit_response.status_code == 200
    assert Decimal(credit_response.json()["balance_after_rub"]) == Decimal("1000.00")

    coverage_response = await client.post(
        "/api/v1/billing/coverage-adjustments",
        headers=admin_headers,
        json={
            "partner_id": partner_id,
            "amount_rub": "1000.00",
            "idempotency_key": "coverage-demo-1",
            "reason": "Initial real-money cost coverage for integration test",
        },
    )
    assert coverage_response.status_code == 200
    assert Decimal(coverage_response.json()["coverage_after_rub"]) == Decimal("1000.00")

    partner_headers = {"Authorization": f"Bearer {api_key}"}
    generation_payload = {
        "model_slug": "seedance-2.5",
        "mode": "reference",
        "resolution": "720p",
        "duration_seconds": 5,
        "aspect_ratio": "9:16",
        "reference_images": [{"url": "https://cdn.example.test/reference.jpg"}],
        "prompt": "Короткий ролик про запуск продукта",
        "idempotency_key": "idem-generation-1",
    }
    capability_mismatch = await client.post(
        "/api/v1/generations",
        headers=partner_headers,
        json=generation_payload,
    )
    assert capability_mismatch.status_code == 409
    assert capability_mismatch.json()["detail"] == "capability_mismatch"

    capability_response = await client.put(
        "/api/v1/providers/capabilities",
        headers=admin_headers,
        json={
            "provider": "argolink",
            "model_slug": "seedance-2.5",
            "mode": "reference",
            "resolution": "720p",
            "is_active": True,
        },
    )
    assert capability_response.status_code == 200

    first_generation = await client.post(
        "/api/v1/generations",
        headers=partner_headers,
        json=generation_payload,
    )
    assert first_generation.status_code == 202
    first_body = first_generation.json()
    assert first_body["status"] == "queued"
    assert first_body["duration_seconds"] == 5
    assert first_body["aspect_ratio"] == "9:16"
    assert Decimal(first_body["partner_price_rub"]) == Decimal("100.00")
    assert "provider_task_id" not in first_body

    dispatch_response = await client.post(
        f"/api/v1/generations/{first_body['id']}/dispatch",
        headers=admin_headers,
    )
    assert dispatch_response.status_code == 200
    assert dispatch_response.json()["status"] == "sent_to_provider"
    assert dispatch_response.json()["provider_attempt_status"] == "accepted"

    duplicate_dispatch_response = await client.post(
        f"/api/v1/generations/{first_body['id']}/dispatch",
        headers=admin_headers,
    )
    assert duplicate_dispatch_response.status_code == 200
    assert duplicate_dispatch_response.json()["status"] == "sent_to_provider"

    duplicate_generation = await client.post(
        "/api/v1/generations",
        headers=partner_headers,
        json=generation_payload,
    )
    assert duplicate_generation.status_code == 202
    assert duplicate_generation.json()["id"] == first_body["id"]
    assert duplicate_generation.json()["status"] == "sent_to_provider"

    attempt_result = await db_session.execute(
        select(ProviderAttempt).where(ProviderAttempt.generation_id == first_body["id"])
    )
    provider_attempt = attempt_result.scalar_one()
    assert provider_attempt.next_poll_at is not None
    provider_attempt.next_poll_at = utc_now() - timedelta(seconds=1)
    await db_session.flush()

    worker_result = await process_generation_work_once(db_session)
    assert worker_result.polled == 1

    completed_generation = await client.get(
        f"/api/v1/generations/{first_body['id']}",
        headers=partner_headers,
    )
    assert completed_generation.status_code == 200
    assert completed_generation.json()["status"] == "completed"
    assert completed_generation.json()["result_url"].startswith("http://localhost:8000/api/v1/media/")
    assert "argolink" not in completed_generation.json()["result_url"]

    media_path = urlsplit(completed_generation.json()["result_url"]).path
    media_response = await client.get(media_path, headers=partner_headers)
    assert media_response.status_code == 200
    assert media_response.content == b"fake mp4 bytes"

    balance_response = await client.get(
        f"/api/v1/billing/partners/{partner_id}/balance",
        headers=admin_headers,
    )
    assert balance_response.status_code == 200
    assert Decimal(balance_response.json()["balance_rub"]) == Decimal("900.00")

    coverage_balance_response = await client.get(
        f"/api/v1/billing/partners/{partner_id}/coverage",
        headers=admin_headers,
    )
    assert coverage_balance_response.status_code == 200
    assert Decimal(coverage_balance_response.json()["cost_coverage_rub"]) == Decimal("915.00")

    ledger_response = await client.get(
        f"/api/v1/billing/partners/{partner_id}/ledger",
        headers=admin_headers,
    )
    assert ledger_response.status_code == 200
    assert len(ledger_response.json()) == 2

    api_key_id = key_response.json()["id"]
    revoke_response = await client.post(
        f"/api/v1/accounts/partners/{partner_id}/api-keys/{api_key_id}/revoke",
        headers=admin_headers,
    )
    assert revoke_response.status_code == 200
    assert revoke_response.json()["is_active"] is False

    rejected_after_revoke = await client.get(f"/api/v1/generations/{first_body['id']}", headers=partner_headers)
    assert rejected_after_revoke.status_code == 401


async def test_worker_marks_stale_provider_attempt_timeout(
    db_session: AsyncSession,
):
    settings = get_settings()
    old_timeout = settings.worker_provider_processing_timeout_seconds
    settings.worker_provider_processing_timeout_seconds = 60
    try:
        partner = Partner(
            telegram_id="timeout-test",
            company_name="Timeout Partner",
            project_name="Timeout Bot",
        )
        db_session.add(partner)
        await db_session.flush()
        generation = Generation(
            partner_id=partner.id,
            model_id="model-timeout",
            model_slug="seedance-2.5",
            mode="text_to_video",
            resolution="720p",
            duration_seconds=5,
            idempotency_key="timeout-generation-idem",
            partner_price_rub=Decimal("100.00"),
            prompt="timeout media",
            status="sent_to_provider",
        )
        db_session.add(generation)
        await db_session.flush()
        attempt = ProviderAttempt(
            generation_id=generation.id,
            provider="argolink",
            provider_task_id="provider-timeout-task",
            status="accepted",
            created_at=utc_now() - timedelta(minutes=10),
        )
        db_session.add(attempt)
        await db_session.flush()

        result = await process_generation_work_once(db_session)
        await db_session.refresh(generation)
        await db_session.refresh(attempt)

        assert result.polled == 1
        assert generation.status == "timeout"
        assert generation.public_error_code == "generation_timeout"
        assert attempt.status == "timeout"
        assert attempt.last_error == "provider_processing_timeout"
    finally:
        settings.worker_provider_processing_timeout_seconds = old_timeout


async def test_worker_reconciles_late_provider_success_after_local_timeout(
    db_session: AsyncSession,
    monkeypatch,
):
    from app.billing.models import LedgerEntry
    from app.billing.service import apply_partner_balance_change
    from app.webhooks.models import WebhookEvent

    class LateSuccessAdapter(FakeArgoLinkAdapter):
        async def poll_generation(self, provider_task_id: str):
            assert provider_task_id == "provider-late-success-task"
            return ProviderPollResult(
                status="completed",
                result_url=f"https://argolink.io/v1/videos/{provider_task_id}/content",
            )

    adapter = LateSuccessAdapter(api_key="argolink-secret")

    async def get_adapter(db, partner_id: str, provider: str = "argolink"):
        assert provider == "argolink"
        return adapter

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", get_adapter)

    settings = get_settings()
    old_timeout = settings.worker_provider_processing_timeout_seconds
    old_poll_max = settings.worker_poll_backoff_max_seconds
    settings.worker_provider_processing_timeout_seconds = 60
    settings.worker_poll_backoff_max_seconds = 1

    try:
        partner = Partner(
            telegram_id="late-success-test",
            company_name="Late Success Partner",
            project_name="Late Success Bot",
            balance_rub=Decimal("1000.00"),
        )
        db_session.add(partner)
        await db_session.flush()

        generation = Generation(
            partner_id=partner.id,
            model_id="model-late-success",
            model_slug="seedance-2.5",
            mode="text_to_video",
            resolution="720p",
            duration_seconds=5,
            idempotency_key="late-success-idem",
            partner_price_rub=Decimal("100.00"),
            prompt="late success",
            status="sent_to_provider",
            webhook_url_snapshot="https://partner.example.test/hooks/neironych",
        )
        db_session.add(generation)
        await db_session.flush()

        await apply_partner_balance_change(
            db=db_session,
            partner=partner,
            amount_rub=Decimal("-100.00"),
            operation_type="generation_reserve",
            idempotency_key=f"generation-reserve:{generation.id}",
            generation_id=generation.id,
            allow_negative=False,
        )

        attempt = ProviderAttempt(
            generation_id=generation.id,
            provider="argolink",
            provider_task_id="provider-late-success-task",
            status="accepted",
            created_at=utc_now() - timedelta(minutes=10),
            next_poll_at=utc_now() - timedelta(seconds=1),
        )
        db_session.add(attempt)
        await db_session.flush()

        timed_out = await process_generation_work_once(db_session)
        assert timed_out.polled == 1
        await db_session.refresh(generation)
        await db_session.refresh(partner)
        await db_session.refresh(attempt)

        assert generation.status == "timeout"
        assert Decimal(partner.balance_rub) == Decimal("1000.00")
        assert attempt.status == "timeout"
        assert attempt.next_poll_at is not None

        attempt.next_poll_at = utc_now() - timedelta(seconds=1)
        await db_session.flush()

        reconciled = await process_generation_work_once(db_session)
        assert reconciled.polled == 1
        await db_session.refresh(generation)
        await db_session.refresh(partner)

        assert generation.status == "completed"
        assert Decimal(partner.balance_rub) == Decimal("900.00")
        assert generation.result_url is not None

        ledger_result = await db_session.execute(
            select(LedgerEntry).where(LedgerEntry.generation_id == generation.id).order_by(LedgerEntry.created_at)
        )
        ledger = list(ledger_result.scalars().all())
        assert [entry.operation_type for entry in ledger] == [
            "generation_reserve",
            "generation_reserve_release",
            "generation_late_charge",
        ]

        events_result = await db_session.execute(
            select(WebhookEvent).where(WebhookEvent.generation_id == generation.id).order_by(WebhookEvent.created_at)
        )
        events = list(events_result.scalars().all())
        assert [event.event_type for event in events] == ["timeout", "completed"]

        attempt.next_poll_at = utc_now() - timedelta(seconds=1)
        await db_session.flush()
        duplicate = await process_generation_work_once(db_session)
        assert duplicate.polled == 0

        await db_session.refresh(partner)
        assert Decimal(partner.balance_rub) == Decimal("900.00")
    finally:
        settings.worker_provider_processing_timeout_seconds = old_timeout
        settings.worker_poll_backoff_max_seconds = old_poll_max
