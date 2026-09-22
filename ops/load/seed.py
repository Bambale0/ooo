"""Seed/clean an isolated load-test fixture.

Safety rules:
- refuses APP_ENV=production;
- requires caller-supplied test API keys;
- stores deliberately invalid provider ciphertext so an accidentally started worker fails
  closed instead of sending paid upstream traffic.
"""

import argparse
import asyncio
import os
from decimal import Decimal

from sqlalchemy import delete, select

from app.accounts.models import ApiKey, Partner
from app.billing.models import LedgerEntry
from app.catalog.models import Model, PartnerPrice
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.security import hash_secret
from app.providers.models import ProviderAttempt, ProviderCredential, ProviderModelCapability
from app.webhooks.models import WebhookDelivery, WebhookEvent

LOAD_MODEL_SLUG = "load-test-video"
LOAD_TELEGRAM_PREFIX = "load-test-"
LOAD_BALANCE_RUB = Decimal("1000000000.00")


def _load_api_keys() -> list[str]:
    raw = os.getenv("LOAD_TEST_API_KEYS", "")
    keys = [value.strip() for value in raw.split(",") if value.strip()]
    if not keys:
        raise RuntimeError("LOAD_TEST_API_KEYS must contain at least one test API key")
    if any(len(key) < 16 for key in keys):
        raise RuntimeError("Every LOAD_TEST_API_KEYS value must be at least 16 characters")
    return list(dict.fromkeys(keys))


def _assert_safe_environment() -> None:
    settings = get_settings()
    if settings.app_env.lower() in {"prod", "production"}:
        raise RuntimeError("Refusing to seed or clean load-test data in production")


async def seed() -> None:
    _assert_safe_environment()
    api_keys = _load_api_keys()

    async with SessionLocal() as db:
        model_result = await db.execute(select(Model).where(Model.slug == LOAD_MODEL_SLUG))
        model = model_result.scalar_one_or_none()
        if model is None:
            model = Model(
                slug=LOAD_MODEL_SLUG,
                name="Load Test Video",
                modality="video",
                status="production",
                has_provider_integration=True,
                has_public_docs=True,
                has_successful_smoke=True,
            )
            db.add(model)
            await db.flush()
        else:
            model.status = "production"
            model.has_provider_integration = True
            model.has_public_docs = True
            model.has_successful_smoke = True

        price_result = await db.execute(
            select(PartnerPrice).where(
                PartnerPrice.model_id == model.id,
                PartnerPrice.mode == "default",
                PartnerPrice.resolution == "default",
            )
        )
        price = price_result.scalar_one_or_none()
        if price is None:
            db.add(
                PartnerPrice(
                    model_id=model.id,
                    mode="default",
                    resolution="default",
                    price_rub=Decimal("0.01"),
                    provider_cost_usdt=Decimal("0.000001"),
                    billing_unit="generation",
                )
            )
        else:
            price.price_rub = Decimal("0.01")
            price.provider_cost_usdt = Decimal("0.000001")
            price.billing_unit = "generation"

        capability_result = await db.execute(
            select(ProviderModelCapability).where(
                ProviderModelCapability.provider == "argolink",
                ProviderModelCapability.model_id == model.id,
                ProviderModelCapability.mode == "default",
                ProviderModelCapability.resolution == "default",
            )
        )
        capability = capability_result.scalar_one_or_none()
        if capability is None:
            db.add(
                ProviderModelCapability(
                    provider="argolink",
                    model_id=model.id,
                    mode="default",
                    resolution="default",
                    is_active=True,
                )
            )
        else:
            capability.is_active = True

        for index, api_key in enumerate(api_keys, start=1):
            telegram_id = f"{LOAD_TELEGRAM_PREFIX}{index}"
            partner_result = await db.execute(select(Partner).where(Partner.telegram_id == telegram_id))
            partner = partner_result.scalar_one_or_none()
            if partner is None:
                partner = Partner(
                    telegram_id=telegram_id,
                    company_name=f"Load Test Partner {index}",
                    project_name=f"Load Test Project {index}",
                    balance_rub=LOAD_BALANCE_RUB,
                )
                db.add(partner)
                await db.flush()
            else:
                partner.status = "active"
                partner.balance_rub = LOAD_BALANCE_RUB

            api_key_hash = hash_secret(api_key)
            existing_key_result = await db.execute(select(ApiKey).where(ApiKey.key_hash == api_key_hash))
            existing_key = existing_key_result.scalar_one_or_none()
            if existing_key is None:
                db.add(
                    ApiKey(
                        partner_id=partner.id,
                        name="load-test",
                        key_hash=api_key_hash,
                        key_prefix=api_key[:8],
                        is_active=True,
                    )
                )
            else:
                if existing_key.partner_id != partner.id:
                    raise RuntimeError("A supplied load-test API key is already owned by another partner")
                existing_key.is_active = True

            credential_result = await db.execute(
                select(ProviderCredential).where(
                    ProviderCredential.partner_id == partner.id,
                    ProviderCredential.provider == "argolink",
                    ProviderCredential.label == "load-test-never-send",
                )
            )
            credential = credential_result.scalar_one_or_none()
            if credential is None:
                dummy_secret = f"load-test-never-send-{partner.id}"
                db.add(
                    ProviderCredential(
                        provider="argolink",
                        label="load-test-never-send",
                        key_hash=hash_secret(dummy_secret),
                        key_prefix="loadtest",
                        encrypted_api_key="invalid-load-test-ciphertext",
                        partner_id=partner.id,
                        is_active=True,
                    )
                )
            else:
                credential.is_active = True

        await db.commit()

    print(f"Seeded {len(api_keys)} isolated load-test partner(s).")
    print("Run the API service WITHOUT generation/webhook workers during acceptance load tests.")


async def clean() -> None:
    _assert_safe_environment()

    async with SessionLocal() as db:
        partners_result = await db.execute(
            select(Partner).where(Partner.telegram_id.like(f"{LOAD_TELEGRAM_PREFIX}%"))
        )
        partners = list(partners_result.scalars().all())
        partner_ids = [partner.id for partner in partners]

        if partner_ids:
            generation_ids_result = await db.execute(
                select(Generation.id).where(Generation.partner_id.in_(partner_ids))
            )
            generation_ids = list(generation_ids_result.scalars().all())

            if generation_ids:
                event_ids_result = await db.execute(
                    select(WebhookEvent.id).where(WebhookEvent.generation_id.in_(generation_ids))
                )
                event_ids = list(event_ids_result.scalars().all())
                if event_ids:
                    await db.execute(delete(WebhookDelivery).where(WebhookDelivery.event_id.in_(event_ids)))
                await db.execute(delete(WebhookEvent).where(WebhookEvent.generation_id.in_(generation_ids)))
                await db.execute(delete(ProviderAttempt).where(ProviderAttempt.generation_id.in_(generation_ids)))
                await db.execute(delete(LedgerEntry).where(LedgerEntry.generation_id.in_(generation_ids)))
                await db.execute(delete(Generation).where(Generation.id.in_(generation_ids)))

            await db.execute(delete(LedgerEntry).where(LedgerEntry.partner_id.in_(partner_ids)))
            await db.execute(delete(ApiKey).where(ApiKey.partner_id.in_(partner_ids)))
            await db.execute(delete(ProviderCredential).where(ProviderCredential.partner_id.in_(partner_ids)))
            await db.execute(delete(Partner).where(Partner.id.in_(partner_ids)))

        model_result = await db.execute(select(Model).where(Model.slug == LOAD_MODEL_SLUG))
        model = model_result.scalar_one_or_none()
        if model is not None:
            await db.execute(delete(ProviderModelCapability).where(ProviderModelCapability.model_id == model.id))
            await db.execute(delete(PartnerPrice).where(PartnerPrice.model_id == model.id))
            await db.delete(model)

        await db.commit()

    print(f"Removed {len(partner_ids)} load-test partner(s) and their generated test data.")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seed", "clean"))
    args = parser.parse_args()
    if args.command == "seed":
        await seed()
    else:
        await clean()


if __name__ == "__main__":
    asyncio.run(main())
