from decimal import Decimal

from app.accounts.models import Partner
from app.billing.service import apply_cost_coverage_change
from app.generations.models import Generation
from app.generations.service import (
    dispatch_generation_with_routing,
    poll_generation_provider,
    select_fallback_provider,
)
from app.infrastructure.config import get_settings
from app.infrastructure.retry import utc_now
from app.providers.base import ProviderPollResult, ProviderSubmitResult
from app.providers.models import ProviderAttempt, ProviderModelCapability


def _generation(partner_id: str, *, model_id="seedance-model", resolution="720p", mode="text_to_video", payload=None):
    return Generation(
        partner_id=partner_id,
        model_id=model_id,
        model_slug="seedance-2.5",
        mode=mode,
        resolution=resolution,
        duration_seconds=5,
        aspect_ratio="16:9",
        idempotency_key=f"fallback-{resolution}-{mode}",
        partner_price_rub=Decimal("100.00"),
        provider_cost_usdt_snapshot=Decimal("0.85"),
        rub_per_usdt_snapshot=Decimal("100"),
        provider_cost_reserve_rub=Decimal("85.00"),
        prompt="fallback",
        request_payload=payload or {},
        status="processing",
    )


async def _seed(db_session, *, resolution="720p", mode="text_to_video", ceiling="0.10", payload=None):
    partner = Partner(
        telegram_id=f"fallback-{resolution}-{mode}-{ceiling}",
        company_name="Fallback",
        project_name="Fallback",
        status="active",
    )
    db_session.add(partner)
    await db_session.flush()
    model_id = f"seedance-model-{resolution}-{mode}-{ceiling}"
    generation = _generation(partner.id, model_id=model_id, resolution=resolution, mode=mode, payload=payload)
    capability = ProviderModelCapability(
        provider="asale",
        model_id=generation.model_id,
        mode=mode,
        resolution=resolution,
        is_active=True,
        provider_cost_ceiling_usdt=Decimal(ceiling),
        billing_unit="second",
    )
    db_session.add_all([generation, capability])
    await db_session.flush()
    return generation


async def test_fallback_selection_requires_equivalent_request_and_reserved_cost(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "asale_api_key", "sk-asale-platform")

    generation = await _seed(db_session)
    assert await select_fallback_provider(db_session, generation, after_provider="argolink") == (
        "asale",
        Decimal("0.50"),
    )

    generation_1080 = await _seed(db_session, resolution="1080p")
    assert await select_fallback_provider(db_session, generation_1080, after_provider="argolink") is None

    generation_reference = await _seed(
        db_session,
        mode="reference",
        payload={"reference_images": [{"url": "https://example.test/ref.jpg"}]},
    )
    assert await select_fallback_provider(db_session, generation_reference, after_provider="argolink") is None

    expensive = await _seed(db_session, ceiling="0.20")
    assert await select_fallback_provider(db_session, expensive, after_provider="argolink") is None


async def test_safe_primary_failure_queues_and_dispatches_asale_fallback(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "asale_api_key", "sk-asale-platform")
    monkeypatch.setattr(settings, "worker_generation_max_retries", 0)

    generation = await _seed(db_session)
    primary = ProviderAttempt(
        generation_id=generation.id,
        provider="argolink",
        provider_task_id="primary-task",
        status="processing",
        next_poll_at=utc_now(),
    )
    db_session.add(primary)
    await db_session.flush()

    class PrimaryAdapter:
        async def poll_generation(self, provider_task_id):
            assert provider_task_id == "primary-task"
            return ProviderPollResult(
                status="failed",
                raw_error="upstream internal error",
                error_code="internal_error",
                retryable_failure=True,
            )

        def normalize_error(self, error):
            raise error

    class FallbackAdapter:
        async def submit_generation(self, request):
            assert request.model_slug == "seedance-2.5"
            assert request.resolution == "720p"
            return ProviderSubmitResult(provider_task_id="asale-task")

        def normalize_error(self, error):
            raise error

    async def adapter(db, partner_id, provider, **kwargs):
        return PrimaryAdapter() if provider == "argolink" else FallbackAdapter()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)

    await poll_generation_provider(db_session, generation, "argolink")
    await db_session.refresh(generation)
    await db_session.refresh(primary)

    assert generation.status == "queued"
    assert primary.status == "failed"
    assert generation.request_payload["fallback_provider"] == "asale"
    assert Decimal(generation.request_payload["fallback_provider_cost_ceiling_usdt"]) == Decimal("0.50")

    fallback_attempt = await dispatch_generation_with_routing(db_session, generation)
    assert fallback_attempt is not None
    assert fallback_attempt.provider == "asale"
    assert fallback_attempt.provider_task_id == "asale-task"
    assert generation.status == "sent_to_provider"


async def test_native_video_fallback_uses_default_capability_and_native_prompt(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "asale_api_key", "sk-asale-platform")

    partner = Partner(
        telegram_id="native-fallback",
        company_name="Native fallback",
        project_name="Native fallback",
        status="active",
    )
    db_session.add(partner)
    await db_session.flush()
    generation = Generation(
        partner_id=partner.id,
        model_id="seedance-native-model",
        model_slug="seedance-2.5",
        mode="videos/generations",
        resolution="720p",
        duration_seconds=7,
        aspect_ratio=None,
        idempotency_key="native-fallback",
        partner_price_rub=Decimal("100.00"),
        provider_cost_usdt_snapshot=Decimal("0.85"),
        rub_per_usdt_snapshot=Decimal("100"),
        provider_cost_reserve_rub=Decimal("85.00"),
        prompt="",
        request_payload={
            "native_body": {
                "model": "seedance-2.5",
                "prompt": "native prompt",
                "duration": 7,
                "resolution": "720p",
                "aspect_ratio": "16:9",
            }
        },
        status="queued",
    )
    capability = ProviderModelCapability(
        provider="asale",
        model_id=generation.model_id,
        mode="default",
        resolution="720p",
        is_active=True,
        provider_cost_ceiling_usdt=Decimal("0.0615"),
        billing_unit="second",
    )
    db_session.add_all([generation, capability])
    await db_session.flush()

    selected = await select_fallback_provider(db_session, generation, after_provider="argolink")
    assert selected == ("asale", Decimal("0.4305"))

    seen = {}

    class FallbackAdapter:
        async def submit_generation(self, request):
            seen["request"] = request
            return ProviderSubmitResult(provider_task_id="asale-native-task")

        def normalize_error(self, error):
            raise error

    async def adapter(db, partner_id, provider, **kwargs):
        assert provider == "asale"
        return FallbackAdapter()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)
    generation.request_payload = {
        **generation.request_payload,
        "fallback_provider": "asale",
        "fallback_provider_cost_ceiling_usdt": "0.4305",
    }
    attempt = await dispatch_generation_with_routing(db_session, generation)

    assert attempt is not None
    request = seen["request"]
    assert request.mode == "videos/generations"
    assert request.prompt == "native prompt"
    assert request.duration_seconds == 7
    assert request.resolution == "720p"
    assert request.aspect_ratio == "16:9"


async def test_fallback_can_exceed_primary_snapshot_when_capital_reserve_covers_it(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "asale_api_key", "sk-asale-platform")

    generation = await _seed(db_session, ceiling="0.20")
    generation.provider_cost_usdt_snapshot = Decimal("0.55")
    generation.provider_cost_reserve_usdt = Decimal("1.20")
    generation.rub_per_usdt_snapshot = Decimal("80")
    generation.partner_price_rub = Decimal("100")
    await db_session.flush()

    selected = await select_fallback_provider(db_session, generation, after_provider="argolink")
    assert selected == ("asale", Decimal("1.00"))


async def test_primary_success_refunds_fallback_only_cost_reserve(db_session, monkeypatch):
    partner = Partner(
        telegram_id="primary-reserve-refund",
        company_name="Primary reserve refund",
        project_name="Primary reserve refund",
        status="active",
        cost_coverage_rub=Decimal("1000.00"),
    )
    db_session.add(partner)
    await db_session.flush()
    generation = Generation(
        partner_id=partner.id,
        model_id="seedance-primary-refund",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        aspect_ratio="16:9",
        idempotency_key="primary-reserve-refund",
        partner_price_rub=Decimal("100.00"),
        provider_cost_usdt_snapshot=Decimal("0.85"),
        provider_cost_reserve_usdt=Decimal("0.95"),
        rub_per_usdt_snapshot=Decimal("100"),
        provider_cost_reserve_rub=Decimal("95.00"),
        prompt="refund fallback reserve",
        request_payload={},
        status="processing",
    )
    db_session.add(generation)
    await db_session.flush()
    await apply_cost_coverage_change(
        db_session,
        partner,
        Decimal("-95.00"),
        "provider_cost_reserve",
        f"provider-cost-reserve:{generation.id}",
        generation.id,
        "reserve",
        allow_negative=True,
    )
    attempt = ProviderAttempt(
        generation_id=generation.id,
        provider="argolink",
        provider_task_id="primary-success-task",
        status="processing",
        next_poll_at=utc_now(),
    )
    db_session.add(attempt)
    await db_session.flush()

    class PrimaryAdapter:
        async def poll_generation(self, provider_task_id):
            assert provider_task_id == "primary-success-task"
            return ProviderPollResult(status="completed")

        def normalize_error(self, error):
            raise error

    async def adapter(db, partner_id, provider, **kwargs):
        assert provider == "argolink"
        return PrimaryAdapter()

    monkeypatch.setattr("app.generations.service.get_partner_provider_adapter", adapter)

    await poll_generation_provider(db_session, generation, "argolink")
    await db_session.refresh(generation)
    await db_session.refresh(partner)

    assert generation.status == "completed"
    assert Decimal(generation.actual_provider_cost_usdt) == Decimal("0.85")
    assert Decimal(partner.cost_coverage_rub) == Decimal("915.00")
