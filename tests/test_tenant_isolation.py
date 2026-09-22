from decimal import Decimal

from sqlalchemy import select

from app.accounts.models import ApiKey, Partner
from app.generations.models import Generation
from app.infrastructure.security import hash_secret
from app.media.models import MediaAsset
from app.webhooks.models import WebhookEvent


async def _create_partner_with_key(db_session, *, telegram_id: str, token: str) -> tuple[Partner, ApiKey]:
    partner = Partner(
        telegram_id=telegram_id,
        company_name=f"{telegram_id} company",
        project_name=f"{telegram_id} project",
        balance_rub=Decimal("1000.00"),
    )
    db_session.add(partner)
    await db_session.flush()

    api_key = ApiKey(
        partner_id=partner.id,
        name="isolation-test",
        key_hash=hash_secret(token),
        key_prefix=token[:8],
        is_active=True,
    )
    db_session.add(api_key)
    await db_session.flush()
    return partner, api_key


async def test_partner_cannot_read_another_partners_generation(client, db_session):
    partner_a, _ = await _create_partner_with_key(
        db_session,
        telegram_id="tenant-a-generation",
        token="nrn_tenant_a_generation_key",
    )
    partner_b, _ = await _create_partner_with_key(
        db_session,
        telegram_id="tenant-b-generation",
        token="nrn_tenant_b_generation_key",
    )

    generation = Generation(
        partner_id=partner_b.id,
        model_id="tenant-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="tenant-b-generation",
        partner_price_rub=Decimal("100.00"),
        prompt="private partner B generation",
        status="completed",
        result_url="https://cdn.example.test/private-b.mp4",
    )
    db_session.add(generation)
    await db_session.flush()

    unauthorized = await client.get(
        f"/api/v1/generations/{generation.id}",
        headers={"Authorization": "Bearer nrn_tenant_a_generation_key"},
    )
    assert unauthorized.status_code == 404
    assert unauthorized.json()["detail"] == "generation_not_found"

    own = await client.get(
        f"/api/v1/generations/{generation.id}",
        headers={"Authorization": "Bearer nrn_tenant_b_generation_key"},
    )
    assert own.status_code == 200
    assert own.json()["id"] == generation.id
    assert own.json()["partner_id"] == partner_b.id
    assert partner_a.id != partner_b.id


async def test_partner_cannot_resend_another_partners_webhook_or_mutate_event(client, db_session):
    _, _ = await _create_partner_with_key(
        db_session,
        telegram_id="tenant-a-webhook",
        token="nrn_tenant_a_webhook_key",
    )
    partner_b, _ = await _create_partner_with_key(
        db_session,
        telegram_id="tenant-b-webhook",
        token="nrn_tenant_b_webhook_key",
    )

    generation = Generation(
        partner_id=partner_b.id,
        model_id="tenant-webhook-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="tenant-b-webhook-generation",
        partner_price_rub=Decimal("100.00"),
        prompt="private webhook",
        status="completed",
        webhook_url_snapshot="https://partner-b.example.test/webhook",
    )
    db_session.add(generation)
    await db_session.flush()

    event = WebhookEvent(
        generation_id=generation.id,
        event_type="completed",
        partner_id=partner_b.id,
        webhook_url="https://partner-b.example.test/webhook",
        payload={"generation_id": generation.id, "status": "completed"},
        status="delivered",
        attempt_count=3,
        next_attempt_at=None,
    )
    db_session.add(event)
    await db_session.flush()

    response = await client.post(
        f"/api/v1/generations/{generation.id}/webhook/resend",
        headers={"Authorization": "Bearer nrn_tenant_a_webhook_key"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "generation_not_found"

    await db_session.refresh(event)
    assert event.status == "delivered"
    assert event.attempt_count == 3
    assert event.next_attempt_at is None


async def test_partner_cannot_stream_another_partners_media_before_provider_access(
    client,
    db_session,
    monkeypatch,
):
    await _create_partner_with_key(
        db_session,
        telegram_id="tenant-a-media",
        token="nrn_tenant_a_media_key",
    )
    partner_b, _ = await _create_partner_with_key(
        db_session,
        telegram_id="tenant-b-media",
        token="nrn_tenant_b_media_key",
    )

    generation = Generation(
        partner_id=partner_b.id,
        model_id="tenant-media-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key="tenant-b-media-generation",
        partner_price_rub=Decimal("100.00"),
        prompt="private media",
        status="completed",
    )
    db_session.add(generation)
    await db_session.flush()

    asset = MediaAsset(
        generation_id=generation.id,
        partner_id=partner_b.id,
        provider="argolink",
        provider_content_url="https://argolink.io/v1/videos/private-b/content",
        public_url=f"http://localhost:8000/api/v1/media/private-b/content",
        status="provider_ready",
        content_type="video/mp4",
    )
    db_session.add(asset)
    await db_session.flush()

    async def provider_access_must_not_happen(*args, **kwargs):
        raise AssertionError("provider access occurred before tenant ownership check")

    monkeypatch.setattr(
        "app.media.router.get_partner_provider_adapter",
        provider_access_must_not_happen,
    )

    response = await client.get(
        f"/api/v1/media/{asset.id}/content",
        headers={"Authorization": "Bearer nrn_tenant_a_media_key"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "media_asset_not_found"


async def test_partner_key_cannot_access_admin_billing_of_another_partner(client, db_session):
    _, _ = await _create_partner_with_key(
        db_session,
        telegram_id="tenant-a-admin-boundary",
        token="nrn_tenant_a_admin_boundary_key",
    )
    partner_b, _ = await _create_partner_with_key(
        db_session,
        telegram_id="tenant-b-admin-boundary",
        token="nrn_tenant_b_admin_boundary_key",
    )

    response = await client.get(
        f"/api/v1/billing/partners/{partner_b.id}/balance",
        headers={"Authorization": "Bearer nrn_tenant_a_admin_boundary_key"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "admin_auth_required"


async def test_same_idempotency_key_is_isolated_per_partner(db_session):
    partner_a, _ = await _create_partner_with_key(
        db_session,
        telegram_id="tenant-a-idempotency",
        token="nrn_tenant_a_idempotency_key",
    )
    partner_b, _ = await _create_partner_with_key(
        db_session,
        telegram_id="tenant-b-idempotency",
        token="nrn_tenant_b_idempotency_key",
    )

    shared_key = "same-client-visible-idempotency-key"
    generation_a = Generation(
        partner_id=partner_a.id,
        model_id="tenant-idem-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key=shared_key,
        partner_price_rub=Decimal("100.00"),
        prompt="partner A",
        status="queued",
    )
    generation_b = Generation(
        partner_id=partner_b.id,
        model_id="tenant-idem-model",
        model_slug="seedance-2.5",
        mode="text_to_video",
        resolution="720p",
        duration_seconds=5,
        idempotency_key=shared_key,
        partner_price_rub=Decimal("100.00"),
        prompt="partner B",
        status="queued",
    )
    db_session.add_all([generation_a, generation_b])
    await db_session.flush()

    result = await db_session.execute(
        select(Generation)
        .where(Generation.idempotency_key == shared_key)
        .order_by(Generation.partner_id)
    )
    generations = list(result.scalars().all())
    assert len(generations) == 2
    assert {generation.partner_id for generation in generations} == {partner_a.id, partner_b.id}
