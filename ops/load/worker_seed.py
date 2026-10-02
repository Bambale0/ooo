"""Seed/inspect/clean a synthetic worker backlog for the local provider stub."""

from __future__ import annotations

import argparse
import asyncio
import os
from decimal import Decimal
from urllib.parse import urlsplit

from sqlalchemy import delete, func, select

from app.accounts.models import Partner
from app.billing.models import CoverageLedgerEntry, LedgerEntry
from app.generations.models import Generation
from app.infrastructure.config import get_settings
from app.infrastructure.database import SessionLocal
from app.infrastructure.security import encrypt_secret, hash_secret
from app.media.models import MediaAsset
from app.providers.models import ProviderAttempt, ProviderCircuit, ProviderCredential, ProviderOutcome
from app.webhooks.models import WebhookDelivery, WebhookEvent

PREFIX = "worker-load-"
MODEL_SLUG = "seedance-2.5"
ACK = "I_UNDERSTAND_LOCAL_STUB_ONLY"


def _assert_safe_worker_environment() -> None:
    settings = get_settings()
    if settings.app_env.lower() in {"prod", "production"}:
        raise RuntimeError("Refusing worker load fixture in production")

    parsed = urlsplit(settings.argolink_base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("ARGOLINK_BASE_URL must point to a loopback local provider stub")

    if os.getenv("LOAD_WORKER_TEST_ACK") != ACK:
        raise RuntimeError(f"Set LOAD_WORKER_TEST_ACK={ACK}")

    if not settings.provider_credentials_master_key:
        raise RuntimeError("PROVIDER_CREDENTIALS_MASTER_KEY is required")


def _positive_int(name: str, default: int, *, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value < 1 or value > maximum:
        raise RuntimeError(f"{name} must be between 1 and {maximum}")
    return value


async def seed() -> None:
    _assert_safe_worker_environment()
    settings = get_settings()
    partner_count = _positive_int("LOAD_WORKER_PARTNERS", 5, maximum=100)
    per_partner = _positive_int("LOAD_WORKER_GENERATIONS_PER_PARTNER", 100, maximum=100_000)
    master_key = settings.provider_credentials_master_key
    assert master_key is not None

    async with SessionLocal() as db:
        for partner_index in range(1, partner_count + 1):
            telegram_id = f"{PREFIX}{partner_index}"
            partner = (
                await db.execute(select(Partner).where(Partner.telegram_id == telegram_id))
            ).scalar_one_or_none()
            if partner is None:
                partner = Partner(
                    telegram_id=telegram_id,
                    company_name=f"Worker Load Partner {partner_index}",
                    project_name=f"Worker Load Project {partner_index}",
                    balance_rub=Decimal("1000000000.00"),
                    cost_coverage_rub=Decimal("1000000000.00"),
                )
                db.add(partner)
                await db.flush()
            else:
                partner.status = "active"

            await db.execute(
                delete(ProviderCredential).where(
                    ProviderCredential.partner_id == partner.id,
                    ProviderCredential.provider == "argolink",
                    ProviderCredential.label == "worker-load-stub",
                )
            )
            upstream_key = f"local-load-partner-{partner_index}"
            db.add(
                ProviderCredential(
                    provider="argolink",
                    label="worker-load-stub",
                    key_hash=hash_secret(upstream_key),
                    key_prefix=upstream_key[:8],
                    encrypted_api_key=encrypt_secret(upstream_key, master_key),
                    partner_id=partner.id,
                    is_active=True,
                )
            )

            existing_count = int(
                (
                    await db.execute(
                        select(func.count(Generation.id)).where(
                            Generation.partner_id == partner.id,
                            Generation.idempotency_key.like("worker-load-%"),
                        )
                    )
                ).scalar_one()
            )
            for generation_index in range(existing_count + 1, per_partner + 1):
                db.add(
                    Generation(
                        partner_id=partner.id,
                        model_id="worker-load-model",
                        model_slug=MODEL_SLUG,
                        mode="text_to_video",
                        resolution="720p",
                        duration_seconds=5,
                        aspect_ratio="16:9",
                        idempotency_key=f"worker-load-{partner_index}-{generation_index}",
                        partner_price_rub=Decimal("0.01"),
                        provider_cost_usdt_snapshot=Decimal("0.000001"),
                        rub_per_usdt_snapshot=Decimal("100.000000"),
                        provider_cost_reserve_rub=Decimal("0.01"),
                        prompt="synthetic worker resilience load",
                        request_payload={
                            "duration_seconds": 5,
                            "aspect_ratio": "16:9",
                            "reference_images": [],
                            "billing_unit": "second",
                            "billable_units": 5,
                        },
                        status="queued",
                    )
                )

        await db.commit()

    print(f"Seeded {partner_count * per_partner} synthetic queued generations across {partner_count} partners.")


async def status_report() -> None:
    _assert_safe_worker_environment()
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Generation.status, func.count(Generation.id))
                .join(Partner, Partner.id == Generation.partner_id)
                .where(Partner.telegram_id.like(f"{PREFIX}%"))
                .group_by(Generation.status)
                .order_by(Generation.status)
            )
        ).all()
        partner_rows = (
            await db.execute(
                select(Partner.telegram_id, Generation.status, func.count(Generation.id))
                .join(Generation, Generation.partner_id == Partner.id)
                .where(Partner.telegram_id.like(f"{PREFIX}%"))
                .group_by(Partner.telegram_id, Generation.status)
                .order_by(Partner.telegram_id, Generation.status)
            )
        ).all()

    print("Totals:")
    for generation_status, count in rows:
        print(f"  {generation_status}: {count}")
    print("Per partner:")
    for telegram_id, generation_status, count in partner_rows:
        print(f"  {telegram_id} {generation_status}: {count}")


async def clean() -> None:
    _assert_safe_worker_environment()
    async with SessionLocal() as db:
        partners = list(
            (
                await db.execute(select(Partner).where(Partner.telegram_id.like(f"{PREFIX}%")))
            ).scalars().all()
        )
        partner_ids = [partner.id for partner in partners]

        if partner_ids:
            generation_ids = list(
                (
                    await db.execute(select(Generation.id).where(Generation.partner_id.in_(partner_ids)))
                ).scalars().all()
            )
            if generation_ids:
                event_ids = list(
                    (
                        await db.execute(
                            select(WebhookEvent.id).where(WebhookEvent.generation_id.in_(generation_ids))
                        )
                    ).scalars().all()
                )
                if event_ids:
                    await db.execute(delete(WebhookDelivery).where(WebhookDelivery.event_id.in_(event_ids)))
                await db.execute(delete(WebhookEvent).where(WebhookEvent.generation_id.in_(generation_ids)))
                await db.execute(delete(ProviderOutcome).where(ProviderOutcome.generation_id.in_(generation_ids)))
                await db.execute(delete(MediaAsset).where(MediaAsset.generation_id.in_(generation_ids)))
                await db.execute(delete(ProviderAttempt).where(ProviderAttempt.generation_id.in_(generation_ids)))
                await db.execute(
                    delete(CoverageLedgerEntry).where(CoverageLedgerEntry.generation_id.in_(generation_ids))
                )
                await db.execute(delete(LedgerEntry).where(LedgerEntry.generation_id.in_(generation_ids)))
                await db.execute(delete(Generation).where(Generation.id.in_(generation_ids)))

            await db.execute(delete(ProviderCredential).where(ProviderCredential.partner_id.in_(partner_ids)))
            await db.execute(delete(Partner).where(Partner.id.in_(partner_ids)))

        # The circuit is provider-global. The harness requires an isolated disposable DB,
        # so cleanup resets synthetic breaker state between scenarios.
        await db.execute(delete(ProviderCircuit).where(ProviderCircuit.provider == "argolink"))
        await db.commit()

    print(f"Removed {len(partner_ids)} synthetic worker-load partner(s).")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("seed", "status", "clean"))
    args = parser.parse_args()
    if args.command == "seed":
        await seed()
    elif args.command == "status":
        await status_report()
    else:
        await clean()


if __name__ == "__main__":
    asyncio.run(main())
