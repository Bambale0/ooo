from decimal import Decimal

from sqlalchemy import select

from app.accounts.models import ApiKey, Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.catalog.models import Model, PartnerPrice
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.security import encrypt_secret, hash_secret
from app.providers.models import ProviderCredential, ProviderModelCapability


async def _seed_generation_preflight(db_session):
    partner = Partner(
        telegram_id="coverage-preflight",
        company_name="Coverage Partner",
        project_name="Coverage Bot",
        balance_rub=Decimal("1000.00"),
        cost_coverage_rub=Decimal("0.00"),
    )
    db_session.add(partner)
    await db_session.flush()

    token = "nrn_coverage_preflight_test_key"
    db_session.add(
        ApiKey(
            partner_id=partner.id,
            name="coverage-test",
            key_hash=hash_secret(token),
            key_prefix=token[:8],
            is_active=True,
        )
    )

    model = Model(
        slug="seedance-2.5",
        name="Coverage Test Video",
        modality="video",
        status="production",
        has_provider_integration=True,
        has_public_docs=True,
        has_successful_smoke=True,
    )
    db_session.add(model)
    await db_session.flush()

    db_session.add(
        PartnerPrice(
            model_id=model.id,
            mode="text_to_video",
            resolution="720p",
            price_rub=Decimal("20.00"),
            provider_cost_usdt=Decimal("0.170000"),
            billing_unit="second",
        )
    )
    db_session.add(
        ProviderModelCapability(
            provider="argolink",
            model_id=model.id,
            mode="text_to_video",
            resolution="720p",
            is_active=True,
        )
    )
    settings = get_settings()
    assert settings.provider_credentials_master_key is not None
    provider_key = "coverage-provider-test-key"
    db_session.add(
        ProviderCredential(
            provider="argolink",
            label="coverage-test",
            key_hash=hash_secret(provider_key),
            key_prefix=provider_key[:8],
            encrypted_api_key=encrypt_secret(provider_key, settings.provider_credentials_master_key),
            partner_id=partner.id,
            is_active=True,
        )
    )
    await db_session.commit()
    return partner.id, token


async def test_generation_rejects_before_creation_when_cost_coverage_is_insufficient(
    client,
    db_session,
    admin_headers,
):
    partner_id, token = await _seed_generation_preflight(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "model_slug": "seedance-2.5",
        "mode": "text_to_video",
        "resolution": "720p",
        "duration_seconds": 5,
        "prompt": "coverage preflight",
        "idempotency_key": "coverage-retryable-idem",
    }

    rejected = await client.post("/api/v1/generations", headers=headers, json=payload)
    assert rejected.status_code == 503
    assert rejected.json()["detail"] == "provider_temporarily_unavailable"

    generations = await db_session.execute(
        select(Generation).where(
            Generation.partner_id == partner_id,
            Generation.idempotency_key == payload["idempotency_key"],
        )
    )
    assert generations.scalar_one_or_none() is None

    retail_entries = await db_session.execute(select(LedgerEntry).where(LedgerEntry.partner_id == partner_id))
    assert retail_entries.scalars().first() is None
    coverage_entries = await db_session.execute(
        select(CoverageLedgerEntry).where(CoverageLedgerEntry.partner_id == partner_id)
    )
    assert coverage_entries.scalars().first() is None

    funded = await client.post(
        "/api/v1/billing/coverage-adjustments",
        headers=admin_headers,
        json={
            "partner_id": partner_id,
            "amount_rub": "100.00",
            "idempotency_key": "coverage-fund-1",
            "reason": "Real client funds received outside payment integration",
        },
    )
    assert funded.status_code == 200

    accepted = await client.post("/api/v1/generations", headers=headers, json=payload)
    assert accepted.status_code == 202
    assert Decimal(accepted.json()["partner_price_rub"]) == Decimal("100.00")

    partner = await db_session.get(Partner, partner_id)
    assert partner is not None
    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("900.00")
    assert Decimal(partner.cost_coverage_rub) == Decimal("15.00")

    generation = await db_session.get(Generation, accepted.json()["id"])
    assert generation is not None
    assert Decimal(generation.provider_cost_usdt_snapshot) == Decimal("0.850000")
    assert Decimal(generation.rub_per_usdt_snapshot) == Decimal("100.000000")
    assert Decimal(generation.provider_cost_reserve_rub) == Decimal("85.00")


async def test_manual_retail_adjustment_does_not_increase_cost_coverage(client, db_session, admin_headers):
    partner = Partner(
        telegram_id="coverage-manual-separation",
        company_name="Coverage Separation",
        project_name="Coverage Separation Bot",
        balance_rub=Decimal("0.00"),
        cost_coverage_rub=Decimal("0.00"),
    )
    db_session.add(partner)
    await db_session.commit()
    partner_id = partner.id

    retail = await client.post(
        "/api/v1/billing/manual-adjustments",
        headers=admin_headers,
        json={
            "partner_id": partner_id,
            "amount_rub": "500.00",
            "idempotency_key": "retail-only-bonus",
            "description": "Compensation bonus without real funds",
        },
    )
    assert retail.status_code == 200

    partner = await db_session.get(Partner, partner_id)
    assert partner is not None
    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("500.00")
    assert Decimal(partner.cost_coverage_rub) == Decimal("0.00")

    coverage = await client.post(
        "/api/v1/billing/coverage-adjustments",
        headers=admin_headers,
        json={
            "partner_id": partner_id,
            "amount_rub": "300.00",
            "idempotency_key": "real-coverage-funds",
            "reason": "Confirmed off-platform money",
        },
    )
    assert coverage.status_code == 200

    await db_session.refresh(partner)
    assert Decimal(partner.balance_rub) == Decimal("500.00")
    assert Decimal(partner.cost_coverage_rub) == Decimal("300.00")


async def test_invalid_duration_does_not_reserve_funds_or_consume_key(client, db_session):
    partner_id, token = await _seed_generation_preflight(db_session)
    headers = {"Authorization": f"Bearer {token}"}
    invalid = await client.post(
        "/api/v1/generations",
        headers=headers,
        json={
            "model_slug": "seedance-2.5",
            "mode": "text_to_video",
            "resolution": "720p",
            "duration_seconds": 3,
            "prompt": "test",
            "idempotency_key": "invalid-duration",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "unsupported_duration"
    assert (await db_session.execute(select(Generation))).scalars().all() == []
    partner = await db_session.get(Partner, partner_id)
    assert partner.balance_rub == Decimal("1000.00")


async def test_fx_cost_increase_rejects_before_creation_and_key_can_be_reused(client, db_session, monkeypatch):
    partner_id, token = await _seed_generation_preflight(db_session)
    partner = await db_session.get(Partner, partner_id)
    partner.cost_coverage_rub = Decimal("1000")
    await db_session.commit()
    settings = get_settings()
    monkeypatch.setattr(settings, "rub_per_usdt", Decimal("200"))
    payload = {
        "model_slug": "seedance-2.5",
        "mode": "text_to_video",
        "resolution": "720p",
        "duration_seconds": 5,
        "prompt": "test",
        "idempotency_key": "fx-retry-same-key",
    }
    headers = {"Authorization": f"Bearer {token}"}
    rejected = await client.post("/api/v1/generations", headers=headers, json=payload)
    assert rejected.status_code == 503
    assert (await db_session.execute(select(Generation))).scalars().all() == []
    monkeypatch.setattr(settings, "rub_per_usdt", Decimal("100"))
    accepted = await client.post("/api/v1/generations", headers=headers, json=payload)
    assert accepted.status_code == 202
