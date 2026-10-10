from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.accounts.models import Partner
from app.billing.models import LedgerEntry
from app.billing.service import apply_cost_coverage_change, apply_partner_balance_change
from app.generations.models import Generation
from app.generations.service import (
    _provider_request_for_generation,
    dispatch_generation_with_routing,
    fallback_cost_ceiling_for_request,
    poll_generation_provider,
)
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.infrastructure.security import encrypt_secret, hash_secret
from app.providers.base import ProviderAdapterError, ProviderGenerationRequest, ProviderPollResult, ProviderSubmitResult
from app.providers.models import ProviderAttempt, ProviderCredential, ProviderModelCapability


async def seed(db):
    partner = Partner(
        telegram_id="infai-partner",
        company_name="Infai",
        project_name="Test",
        status="active",
        balance_rub=Decimal("1000"),
        cost_coverage_rub=Decimal("1000"),
    )
    db.add(partner)
    await db.flush()
    credential = ProviderCredential(
        provider="infai",
        label="infai-seedance-1",
        partner_id=None,
        key_hash=hash_secret("infai-test-key"),
        key_prefix="infai-test",
        encrypted_api_key=encrypt_secret("infai-test-key", get_settings().provider_credentials_master_key),
    )
    native = {
        "model": "seedance-2.5",
        "prompt": "Six references",
        "duration": 15,
        "resolution": "720p",
        "aspect_ratio": "9:16",
        "generate_audio": True,
        "omni_reference_task_type": "reference",
        "reference_images": [{"url": f"https://example.com/{i}.png"} for i in range(6)],
    }
    generation = Generation(
        partner_id=partner.id,
        model_id="model",
        model_slug="seedance-2.5",
        mode="videos/generations",
        resolution="720p",
        duration_seconds=15,
        prompt="",
        idempotency_key="infai-fallback",
        partner_price_rub=Decimal("327"),
        provider_cost_usdt_snapshot=Decimal("2.5"),
        provider_cost_reserve_usdt=Decimal("5.754940"),
        provider_cost_reserve_rub=Decimal("489.17"),
        rub_per_usdt_snapshot=Decimal("85"),
        status="processing",
        request_payload={
            "native_body": native,
            "rates": {"seconds": {"retail": "21.8", "cost": "0.166666"}},
            "protocol": "videos/generations",
            "reserved_units": {"seconds": 15},
        },
    )
    db.add_all(
        [
            credential,
            generation,
            ProviderModelCapability(
                provider="infai",
                model_id="model",
                mode="default",
                resolution="720p",
                provider_cost_ceiling_usdt=Decimal("9.0415"),
                billing_unit="million_video_tokens",
                is_active=True,
            ),
        ]
    )
    await db.flush()
    primary = ProviderAttempt(
        generation_id=generation.id,
        provider="argolink",
        provider_task_id="failed-argo",
        status="processing",
        next_poll_at=utc_now(),
    )
    db.add(primary)
    await apply_partner_balance_change(
        db, partner, Decimal("-327"), "generation_reserve", f"generation-reserve:{generation.id}", generation.id, "Test"
    )
    await apply_cost_coverage_change(
        db,
        partner,
        Decimal("-489.17"),
        "provider_cost_reserve",
        f"provider-cost-reserve:{generation.id}",
        generation.id,
        "Test",
    )
    await db.commit()
    return partner, generation, primary, credential


class PrimaryFailure:
    async def poll_generation(self, task_id):
        return ProviderPollResult(status="failed", error_code="internal_error", retryable_failure=True)

    def normalize_error(self, error):
        raise error


async def test_nonretryable_argolink_failure_still_queues_equivalent_infai(db_session, monkeypatch):
    _, generation, primary, _ = await seed(db_session)

    class NonRetryableFailure(PrimaryFailure):
        async def poll_generation(self, task_id):
            return ProviderPollResult(
                status="failed",
                error_code="internal_error",
                retryable_failure=False,
            )

    async def adapter(*args, **kwargs):
        return NonRetryableFailure()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)

    await poll_generation_provider(db_session, generation, "argolink")

    assert generation.status == "queued"
    assert generation.request_payload["fallback_provider"] == "infai"
    assert primary.status == "failed"
    assert primary.provider_task_id == "failed-argo"
    assert primary.cost_status == "confirmed_free"


async def test_seedance20_fallback_quote_uses_video_input_group_rate(db_session):
    partner, generation, _, _ = await seed(db_session)
    db_session.add(
        ProviderModelCapability(
            provider="infai",
            model_id=generation.model_id,
            mode="default",
            resolution="1080p",
            provider_cost_ceiling_usdt=Decimal("6.506500"),
            billing_unit="million_video_tokens",
            is_active=True,
        )
    )
    await db_session.flush()
    request = ProviderGenerationRequest(
        generation_id="seedance20-quote",
        model_slug="seedance-2.0",
        mode="videos/generations",
        resolution="1080p",
        prompt="Reference the camera movement",
        duration_seconds=12,
        aspect_ratio="16:9",
        reference_videos=("https://example.com/reference.mp4",),
        native_body={
            "model": "seedance-2.0",
            "prompt": "Reference the camera movement",
            "duration": 12,
            "resolution": "1080p",
            "aspect_ratio": "16:9",
            "reference_videos": [{"url": "https://example.com/reference.mp4"}],
        },
    )

    selected = await fallback_cost_ceiling_for_request(
        db_session,
        partner_id=partner.id,
        model_id=generation.model_id,
        request=request,
    )

    assert selected == ("infai", Decimal("5.3615250000"))


def test_seedance20_durable_generation_restores_every_reference_for_fallback():
    generation = Generation(
        partner_id="partner",
        model_id="model",
        model_slug="seedance-2.0",
        mode="videos/generations",
        resolution="720p",
        duration_seconds=8,
        aspect_ratio="16:9",
        idempotency_key="seedance20-all-references",
        partner_price_rub=Decimal("100"),
        provider_cost_usdt_snapshot=Decimal("1"),
        rub_per_usdt_snapshot=Decimal("90"),
        status="queued",
        request_payload={
            "native_body": {
                "model": "seedance-2.0",
                "prompt": "Use the references",
                "duration": 8,
                "resolution": "720p",
                "aspect_ratio": "16:9",
                "reference_images": [{"url": "https://example.com/image.png"}],
                "reference_videos": [{"url": "https://example.com/video.mp4"}],
                "reference_audios": [{"url": "https://example.com/audio.mp3"}],
            }
        },
    )

    request = _provider_request_for_generation(generation)

    assert request.reference_images == ("https://example.com/image.png",)
    assert request.reference_videos == ("https://example.com/video.mp4",)
    assert request.reference_audios == ("https://example.com/audio.mp3",)


async def test_infai_fallback_precedes_primary_retry_and_charges_once(db_session, monkeypatch):
    partner, generation, primary, credential = await seed(db_session)
    monkeypatch.setattr(get_settings(), "worker_generation_max_retries", 2)
    submits = []

    class Infai:
        async def submit_generation(self, request):
            submits.append(request)
            return ProviderSubmitResult(provider_task_id="infai-job")

        async def poll_generation(self, task_id):
            return ProviderPollResult(
                status="completed",
                usage={"billed_seconds": 15, "completion_tokens": 324000},
                result_url="https://infai.cc/api/v3/contents/generations/tasks/infai-job/content",
            )

        def normalize_error(self, error):
            raise error

    async def adapter(db, partner_id, provider, **kwargs):
        return PrimaryFailure() if provider == "argolink" else Infai()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation)
    assert generation.status == "queued"
    assert primary.provider_task_id == "failed-argo"
    assert primary.status == "failed"
    assert generation.request_payload["fallback_provider"] == "infai"
    assert generation.partner_price_rub == Decimal("327")
    attempt = await dispatch_generation_with_routing(db_session, generation)
    await db_session.commit()
    # Re-entry/restart sees the durable attempt and never repeats the paid POST.
    again = await dispatch_generation_with_routing(db_session, generation)
    assert again.id == attempt.id and len(submits) == 1
    assert attempt.credential_id == credential.id
    assert len(submits[0].reference_images) == 6
    attempt.next_poll_at = utc_now()
    await poll_generation_provider(db_session, generation, "infai")
    await db_session.commit()
    await poll_generation_provider(db_session, generation, "infai")
    assert generation.status == "completed"
    assert generation.actual_charge_rub == Decimal("327")
    assert generation.actual_provider_cost_usdt == Decimal("2.929446")
    assert partner.balance_rub == Decimal("673")
    assert partner.cost_coverage_rub == Decimal("751.00")
    entries = list(await db_session.scalars(select(LedgerEntry).where(LedgerEntry.generation_id == generation.id)))
    assert sum((e.amount_rub for e in entries), Decimal(0)) == Decimal("-327")


@pytest.mark.parametrize(
    "usage",
    [
        {},
        {"billed_seconds": 16, "completion_tokens": 324000},
        {"billed_seconds": 15, "completion_tokens": True},
        {"billed_seconds": 15, "completion_tokens": 900000},
    ],
)
async def test_infai_unknown_or_excess_usage_does_not_reprice_partner(db_session, monkeypatch, usage):
    partner, generation, primary, credential = await seed(db_session)

    class Infai(PrimaryFailure):
        async def poll_generation(self, task_id):
            return ProviderPollResult(status="completed", usage=usage)

    async def adapter(db, partner_id, provider, **kwargs):
        return PrimaryFailure() if provider == "argolink" else Infai()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation)
    db_session.add(
        ProviderAttempt(
            generation_id=generation.id,
            provider="infai",
            credential_id=credential.id,
            provider_task_id="infai-job",
            status="processing",
            next_poll_at=utc_now(),
        )
    )
    generation.status = "processing"
    await db_session.commit()
    await poll_generation_provider(db_session, generation, "infai")
    assert generation.status == "reconciliation_required"
    assert generation.partner_price_rub == Decimal("327")
    assert generation.actual_charge_rub is None
    assert partner.balance_rub == Decimal("673")


async def test_infai_unknown_submit_is_not_sent_twice(db_session, monkeypatch):
    _, generation, _, _ = await seed(db_session)
    calls = []

    class Infai:
        async def submit_generation(self, request):
            calls.append(request)
            raise ProviderAdapterError("provider_temporarily_unavailable", "infai_transport_error", retryable=False)

        def normalize_error(self, error):
            return error

    async def adapter(db, partner_id, provider, **kwargs):
        return PrimaryFailure() if provider == "argolink" else Infai()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation)
    await dispatch_generation_with_routing(db_session, generation)
    await db_session.commit()
    await dispatch_generation_with_routing(db_session, generation)
    assert generation.status == "reconciliation_required"
    assert len(calls) == 1


async def test_infai_revoked_group_credential_cancels_without_key_switch(db_session, monkeypatch):
    partner, generation, _, credential = await seed(db_session)

    async def adapter(db, partner_id, provider, **kwargs):
        assert provider == "argolink"  # No new provider adapter may be opened after revocation.
        return PrimaryFailure()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation)
    credential.is_active = False
    await db_session.commit()
    await dispatch_generation_with_routing(db_session, generation)
    assert generation.status == "cancelled"
    assert partner.balance_rub == Decimal("1000")


async def test_infai_failed_task_releases_retail_without_another_provider_submit(db_session, monkeypatch):
    partner, generation, _, credential = await seed(db_session)

    class Infai(PrimaryFailure):
        async def poll_generation(self, task_id):
            return ProviderPollResult(status="failed", error_code="InternalError")

    async def adapter(db, partner_id, provider, **kwargs):
        return PrimaryFailure() if provider == "argolink" else Infai()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation)
    db_session.add(
        ProviderAttempt(
            generation_id=generation.id,
            provider="infai",
            credential_id=credential.id,
            provider_task_id="infai-job",
            status="processing",
            next_poll_at=utc_now(),
        )
    )
    generation.status = "processing"
    await db_session.commit()
    await poll_generation_provider(db_session, generation, "infai")
    await db_session.commit()
    await poll_generation_provider(db_session, generation, "infai")
    assert generation.status == "failed"
    assert partner.balance_rub == Decimal("1000")
    assert partner.cost_coverage_rub == Decimal("510.83")
    assert generation.actual_provider_cost_usdt is None


@pytest.mark.parametrize("terminal", ["failed", "completed"])
async def test_infai_timeout_then_definitive_outcome_settles_once(db_session, monkeypatch, terminal):
    partner, generation, _, credential = await seed(db_session)

    class Infai(PrimaryFailure):
        async def poll_generation(self, task_id):
            return ProviderPollResult(status=terminal, usage={"billed_seconds": 15, "completion_tokens": 324000})

    async def adapter(db, partner_id, provider, **kwargs):
        return PrimaryFailure() if provider == "argolink" else Infai()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    await poll_generation_provider(db_session, generation)
    attempt = ProviderAttempt(
        generation_id=generation.id,
        provider="infai",
        credential_id=credential.id,
        provider_task_id="infai-job",
        status="processing",
        next_poll_at=utc_now(),
        created_at=utc_now() - timedelta(days=3),
    )
    db_session.add(attempt)
    generation.status = "processing"
    await db_session.commit()
    await poll_generation_provider(db_session, generation, "infai")
    assert generation.status == "timeout"
    assert partner.balance_rub == Decimal("1000")
    assert partner.cost_coverage_rub == Decimal("510.83")
    attempt.next_poll_at = utc_now()
    await db_session.commit()
    await poll_generation_provider(db_session, generation, "infai")
    await db_session.commit()
    await poll_generation_provider(db_session, generation, "infai")
    assert partner.balance_rub == (Decimal("1000") if terminal == "failed" else Decimal("673"))
    assert partner.cost_coverage_rub == (Decimal("1000.00") if terminal == "failed" else Decimal("751.00"))
    assert attempt.status == terminal
    assert attempt.next_poll_at is None
